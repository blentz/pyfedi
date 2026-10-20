"""The domains this site answers on (spec 2026-10-17 §3.1).

`SERVER_NAME` stays the canonical federation domain. With an empty `site_domain` table it is the one
implicit default, so installs that never ran the migration's seed still work.
"""
from __future__ import annotations

import hashlib
import ipaddress
import re
import unicodedata
from contextlib import contextmanager
from contextvars import ContextVar
from typing import NamedTuple
from urllib.parse import unquote, unquote_to_bytes, urlsplit, urlunsplit

import idna
from bs4 import BeautifulSoup
from bs4.dammit import EntitySubstitution
from bs4.formatter import HTMLFormatter
from flask import Response, current_app, g, has_app_context, has_request_context, request
from flask.globals import request_ctx
from flask_babel import _
from sqlalchemy import or_
from sqlalchemy.exc import SQLAlchemyError

from app import cache, db
from app.onion import onion_proxy_address
from app.onion_media import MEDIA_SUFFIXES, proxy_url
# The module, not the name: app.models imports this module for File's media URLs, so whichever of the two is
# imported first finds the other mid-import (import cycle: models)
import app.models as models


class DomainInfo(NamedTuple):
    domain: str
    scheme: str
    federates: bool
    registration: str
    is_default: bool
    is_onion: bool
    id: int | None   # the site_domain row; None only for the synthesised default


_REGISTRATION_FROM_MODE = {'Open': 'anyone', 'RequireApplication': 'anyone', 'Closed': 'admin'}


def registration_from_mode(mode: str) -> str:
    """Map `site.registration_mode` to a `site_domain.registration` value. Unknown modes are closed."""
    return _REGISTRATION_FROM_MODE.get(mode, 'admin')


def public_registration_open(storefront: DomainInfo) -> bool:
    """Whether anyone may sign up on `storefront`. The default domain is governed by `Site.registration_mode` alone
    (its own `registration` column is ignored); any other domain takes public sign-ups only when it is 'anyone', and
    then `Site.registration_mode` applies as well. A 'staff' or 'admin' domain's accounts are made by the admin
    add-user form alone."""
    return storefront.is_default or storefront.registration == 'anyone'


_NOT_IN_AUTHORITY = frozenset('@/\\?#')


def _bare_host(authority: str) -> str:
    """The host of an authority (`host`, `host:port`, `[v6]:port`), lower-cased, brackets and trailing dot stripped;
    '' when it names none. A bare IPv6 literal (`::1`, as `site_domain.domain` holds it) is its own host."""
    authority = authority.strip()
    # urlsplit would read `x@host` as userinfo and drop a tab or newline from it: a value with either, a path or
    # query delimiter, or any other whitespace or control character is not an authority this site can be named by
    if any(c in _NOT_IN_AUTHORITY or c.isspace() or not c.isprintable() for c in authority):
        return ''
    if authority.count(':') > 1 and not authority.startswith('['):
        authority = f'[{authority}]'
    try:
        return (urlsplit(f'//{authority}').hostname or '').rstrip('.')
    except ValueError:   # an unbalanced IPv6 bracket
        return ''


def normalise_host(host: str) -> str:
    """Lower-case, port stripped, trailing dot stripped, IPv6 brackets stripped. The form `site_domain.domain` holds."""
    return _bare_host(host)


def _authority(storefront) -> str:
    """`host[:port]` for building URLs on `storefront`. It comes from configuration only, never from the request:
    the Host header's port would let a client put its own port into absolute URLs. SERVER_NAME's host uses
    SERVER_NAME verbatim (a dev install on 127.0.0.1:5000 keeps its port); any other registered domain is its bare
    host."""
    server_name = current_app.config.get('SERVER_NAME') or ''
    if storefront.domain == _bare_host(server_name):
        return server_name
    return f'[{storefront.domain}]' if ':' in storefront.domain else storefront.domain


def _synthesised_default() -> DomainInfo:
    protocol = current_app.config['HTTP_PROTOCOL']
    scheme = 'https' if protocol in ('https', 'mixed') else protocol
    domain = normalise_host(current_app.config['SERVER_NAME'])
    return DomainInfo(domain, scheme, True, 'anyone', True, domain.endswith('.onion'), None)


@cache.memoize(timeout=300)
def domain_rows() -> tuple[DomainInfo, ...]:
    rows = db.session.query(models.SiteDomain).order_by(models.SiteDomain.id).all()
    if not rows:
        return (_synthesised_default(),)
    return tuple(DomainInfo(r.domain, r.scheme, r.federates, r.registration, r.is_default, r.is_onion, r.id)
                 for r in rows)


def clear_domain_cache() -> None:
    cache.delete_memoized(domain_rows)


def domain_for_host(host: str) -> DomainInfo | None:
    """The row serving `host`, or None. SERVER_NAME's host is always served: when no row names it (a seed made under
    another SERVER_NAME, or SERVER_NAME changed since) it is the synthesised default, so the canonical site and its
    inbox never answer 421. `check_default_domain` logs that mismatch at startup."""
    wanted = normalise_host(host)
    found = next((row for row in domain_rows() if normalise_host(row.domain) == wanted), None)
    if found is None and wanted == _synthesised_default().domain:
        return _synthesised_default()
    return found


def default_domain() -> DomainInfo:
    return next((row for row in domain_rows() if row.is_default), _synthesised_default())


def domain_by_id(site_domain_id: int | None) -> DomainInfo:
    """The row with this id; the default domain for None or an id no row has (a row since removed)."""
    return next((row for row in domain_rows() if row.id == site_domain_id), None) or default_domain()


def on_domain(domain: DomainInfo, model=None):
    """A filter for the local rows of `model` (users by default) that belong to `domain`: its own, and on the default
    domain the ones no domain was stamped on."""
    column = (model or models.User).site_domain_id
    if domain.is_default:
        return or_(column == domain.id, column.is_(None))
    return column == domain.id


ON_DOMAIN_PARAM = 'storefront_site_domain_id'


def on_domain_sql(domain: DomainInfo, alias: str = 'c') -> str:
    """`on_domain` for raw SQL over a community (or user) table aliased `alias`; the caller binds
    `ON_DOMAIN_PARAM` to `domain.id`. It does not say the row is local: a remote row has no domain either, so the
    default domain's NULL arm matches it. Put the local test beside it. The synthesised default's id is None: its
    `= NULL` arm matches nothing and the NULL arm takes the unstamped rows."""
    column = f'{alias}.site_domain_id'
    if not domain.is_default:
        return f'{column} = :{ON_DOMAIN_PARAM}'
    return f'({column} = :{ON_DOMAIN_PARAM} OR {column} IS NULL)'


def domain_base_url(info: DomainInfo) -> str:
    """The origin that identifies accounts and communities owned by `info`, for ActivityPub URLs stored at creation.
    The default domain is SERVER_URL itself, so its identity stays byte-for-byte what it was before storefronts."""
    if info.is_default:
        return current_app.config['SERVER_URL']
    return f'{info.scheme}://{_authority(info)}'


def domain_authority(info: DomainInfo) -> str:
    """`host[:port]` that names `info`'s owned rows as `ap_domain`; SERVER_NAME verbatim for the default domain."""
    return current_app.config['SERVER_NAME'] if info.is_default else _authority(info)


def is_registered_host(host: str) -> bool:
    return domain_for_host(host) is not None


def is_local_host(host: str | None) -> bool:
    """Whether `host` names this site: SERVER_NAME (the canonical host, ours even before a site_domain row names it)
    or any registered domain. Case, a port and a trailing dot are ignored.

    The one answer to "is this domain ours" (spec §3.4): a link pasted from the onion is as local as one from the
    clearnet. The canonical host is compared first, so the common case reads no rows."""
    bare = _bare_host(host or '')
    return bool(bare) and (bare == _bare_host(current_app.config.get('SERVER_NAME') or '') or is_registered_host(bare))


def is_served_name(name: str | None) -> bool:
    """Whether `name` is, exactly and ignoring case only, a name this site answers on: SERVER_NAME as configured
    (its port included) or a registered domain. Stricter than `is_local_host`, which also takes a port or a trailing
    dot: for an identifier a peer resolves, such as a WebFinger acct: domain, as before the storefronts."""
    name = (name or '').lower()
    return bool(name) and (name == (current_app.config.get('SERVER_NAME') or '').lower() or
                           (name == normalise_host(name) and is_registered_host(name)))


def _link_host(url) -> str:
    """The bare host of absolute (or scheme-relative) `url`, '' when it names none. The host is urlsplit's, after
    any userinfo. A browser reads `\\` as `/`, so a URL with a backslash has no host we can name."""
    if not isinstance(url, str) or '\\' in url:
        return ''
    try:
        return _bare_host(urlsplit(url).hostname or '')
    except ValueError:
        return ''


def is_local_link(url) -> bool:
    """Whether absolute (or scheme-relative) `url` points at this site: `is_local_host` of its host (`_link_host`).
    A URL with a backslash is not local."""
    return is_local_host(_link_host(url))


def check_default_domain() -> None:
    """Log an error when the default row is not SERVER_NAME, and a warning when an onion domain is registered while
    `ONION_PROXY_IP` is unset (or not one address): no request is then treated as onion, so onion visitors are
    banned, limited and recorded as the one shared tor address. Quiet when the table does not exist yet
    (CLI commands and `flask db upgrade` run this before the migration has made it)."""
    try:
        default = default_domain()
    except SQLAlchemyError:
        db.session.rollback()
        return
    if not any(row.is_default for row in domain_rows()):
        current_app.logger.error('site_domain has rows but none is the default; using SERVER_NAME')
    elif normalise_host(default.domain) != normalise_host(current_app.config['SERVER_NAME']):
        current_app.logger.error('site_domain default %s does not match SERVER_NAME %s',
                                 default.domain, current_app.config['SERVER_NAME'])
    if onion_proxy_address() is None and any(row.is_onion for row in domain_rows()):
        current_app.logger.warning('an onion domain is registered but ONION_PROXY_IP is unset or not one address; '
                                   'no request will be treated as onion')


def current_storefront() -> DomainInfo:
    """The storefront this request was addressed to; the default one outside a request."""
    return g.storefront if has_request_context() and 'storefront' in g else default_domain()


def storefront_url() -> str:
    """Origin of the current storefront. Use for links a visitor follows, never for federation identity."""
    storefront = current_storefront()
    return f'{storefront.scheme}://{_authority(storefront)}'


def storefront_host() -> str:
    """`host[:port]` of the current storefront, for naming this site in text a visitor reads."""
    return _authority(current_storefront())


def storefront_cache_key() -> str:
    """`key_prefix` for an `@cache.cached` presentation view (spec §3.4a). Flask-Caching's default key is the path
    alone, so a page cached on one storefront, and the storefront URLs in it, would be served on every other.
    ActivityPub documents name only the canonical origin and keep their path keys."""
    return f'view/{current_storefront().domain}{request.path}'


def storefront_query_cache_key(*_view_args, **_view_kwargs) -> str:
    """`make_cache_key` for an `@cache.cached` presentation view that reads its query string: `storefront_cache_key`
    plus a hash of the arguments, in any order. Flask-Caching's `query_string=True` ignores `key_prefix`, so a view
    keyed by its query string names its storefront through this instead. The view's own arguments are in the path."""
    pairs = str(tuple(sorted(request.args.items(multi=True)))).encode()
    return f'{storefront_cache_key()}?{hashlib.md5(pairs, usedforsecurity=False).hexdigest()}'


def storefront_memo_name(fname: str) -> str:
    """`make_name` for an `@cache.memoize` function whose result holds storefront URLs: one entry per storefront.
    `cache.delete_memoized(f)` with no arguments still clears every storefront's entry, because the version it
    bumps is keyed by `fname`; with arguments it would clear only the current storefront's."""
    return f'{fname}@{_memo_domain.get() or current_storefront().domain}'


_memo_domain: ContextVar[str | None] = ContextVar('storefront_memo_domain', default=None)


def delete_memoized_everywhere(fn, *args, **kwargs) -> None:
    """`cache.delete_memoized(fn, *args, **kwargs)` on every storefront: each registered domain and the default.
    For a `storefront_memo_name` function called with arguments, the plain call deletes only the entry the current
    storefront keyed, and every other storefront went on serving the stale one. Only these arguments' entries go."""
    for domain in sorted({row.domain for row in domain_rows()} | {default_domain().domain}):
        token = _memo_domain.set(domain)
        try:
            cache.delete_memoized(fn, *args, **kwargs)
        finally:
            _memo_domain.reset(token)


def _moved_to(url, base: str):
    """`url` on origin `base` when it points at this site (any registered host); any other URL, or a value that is
    not a string, unchanged."""
    if not is_local_link(url):
        return url
    parts = urlsplit(url)
    origin = urlsplit(base)
    return urlunsplit((origin.scheme, origin.netloc, parts.path, parts.query, parts.fragment))


def storefront_link(url):
    """`url` moved onto the current storefront when it points at this site. For stored links shown where no HTML
    rewrite reaches (RSS)."""
    return _moved_to(url, storefront_url())


def canonical_link(url):
    """`url` moved onto the canonical origin when it points at this site: what a URL a client read on any storefront
    becomes before it is stored or federated."""
    return _moved_to(url, canonical_url())


def canonical_url() -> str:
    """The federation origin. It never varies with the request host."""
    return current_app.config['SERVER_URL']


# Stored and rendered links (spec §3.5). Attributes only, never text.
# Characters a browser strips from a URL before parsing it (tab, LF, CR). We strip them too, so this module and the
# browser cannot disagree about the host (`is_safe_redirect_target` uses the same table).
URL_STRIPPED_BY_BROWSERS = str.maketrans('', '', '\t\n\r')
_STORED_LINK_ATTRS = frozenset({'href', 'src', 'srcset'})
_RENDER_LINK_ATTRS = _STORED_LINK_ATTRS | {'action'}
# the characters a browser strips from both ends of a URL
_C0_OR_SPACE = ''.join(chr(c) for c in range(0x21))
_REDIRECT_HEADERS = ('Location', 'HX-Redirect')


class _SourceOrderFormatter(HTMLFormatter):
    """bs4's 'minimal' formatter (only &, < and > escaped; script and style text verbatim) without its alphabetical
    sort of attributes: a rewritten page keeps its attributes in the order the template wrote them."""

    def attributes(self, tag):
        return list(tag.attrs.items())


_FORMATTER = _SourceOrderFormatter(entity_substitution=EntitySubstitution.substitute_xml)


def _local_hosts() -> frozenset[str]:
    """Every bare host of this site: SERVER_NAME's and each registered domain's, lower-case."""
    hosts = {_bare_host(current_app.config.get('SERVER_NAME') or '')} | {normalise_host(r.domain) for r in domain_rows()}
    return frozenset(hosts - {''})


def _origin(storefront) -> str:
    return f'{storefront.scheme}://{_authority(storefront)}'


def _rebase(url, base: str, hosts: frozenset[str]):
    """`url` on origin `base` when it is an http(s) or scheme-relative URL whose host is in `hosts`. Everything after
    the authority (path, query, fragment, a bare '?') is kept byte for byte. Any other value comes back unchanged."""
    if _link_host(url) not in hosts:
        return url
    parts = urlsplit(url)
    start = url.find(f'//{parts.netloc}')
    if parts.scheme not in ('', 'http', 'https') or start < 0:   # < 0: urlsplit dropped a tab or newline from it
        return url
    return base + url[start + 2 + len(parts.netloc):]


# srcset (and a URL list in general) splits on these five characters only, not on Unicode white space
_SPACE_RUN = re.compile(r'[\t\n\f\r ]*')
_NON_SPACE_RUN = re.compile(r'[^\t\n\f\r ]+')


def _srcset_urls(srcset: str) -> list[tuple[int, int]]:
    """The (start, end) of each candidate URL in a `srcset`: the first run free of ASCII white space at the start and
    after each comma, less the commas that end it (`a.jpg, b.jpg 2x`, `a,b.jpg,`). A scan, not a pattern: linear in `srcset`
    whatever runs of commas, spaces or letters it holds."""
    spans, i, size = [], 0, len(srcset)
    while True:
        i = _SPACE_RUN.match(srcset, i).end()
        if i < size and srcset[i] != ',':
            stop = _NON_SPACE_RUN.match(srcset, i).end()
            while srcset[stop - 1] == ',':
                stop -= 1   # the first character is no comma, so this stops
            spans.append((i, stop))
            i = stop
        comma = srcset.find(',', i)
        if comma < 0:
            return spans
        i = comma + 1


def _rebase_srcset(srcset: str, base: str, hosts: frozenset[str]) -> str:
    out, last = [], 0
    for start, end in _srcset_urls(srcset):
        out += [srcset[last:start], _rebase(srcset[start:end], base, hosts)]
        last = end
    return ''.join(out) + srcset[last:]


def _is_link_attr(tag, name: str, render: bool) -> bool:
    if name in _STORED_LINK_ATTRS:
        return True
    # On render, also what JavaScript and a page's metadata follow: a form's action, <meta content> (og:url,
    # og:image) and data-* attributes (data-sse-url). _rebase only moves a value that is a URL of ours.
    return render and (name in _RENDER_LINK_ATTRS or name.startswith('data-') or
                       (name == 'content' and tag.name == 'meta'))


def _rebase_links(html: str, base: str, hosts: frozenset[str], render: bool, embedded: frozenset[str] = frozenset(),
                  s3_host: str = '', proxy: bool = False) -> str:
    """`html` with its link attributes on `base` where their host is in `hosts`. The parse is skipped when no host
    appears in the text at all (case-insensitively), and `html` itself comes back when no attribute moved, so markup
    is re-serialised only when a link really changes. With `embedded`, a stored link also has any URL of those hosts
    inside it moved (`?u=http://abc.onion/x`), as `canonicalise_text` moves them in text. With `s3_host`, the one
    parse also hides what loads from that host (`_hide_s3`), and is never skipped: no raw-text check can see a host
    spelled with an entity or a percent-escape. With `proxy` (an onion storefront), the same parse then points remote
    media at the onion's media proxy (`_proxy_remote`), and is never skipped either: any host may be remote."""
    lowered = html.lower()
    if not s3_host and not proxy and not any(host in lowered for host in hosts):
        return html
    soup = BeautifulSoup(html, 'html.parser', on_duplicate_attribute='ignore')   # the first, as a browser does
    changed = False
    for tag in soup.find_all(True):
        if tag.decomposed:   # inside something already replaced or removed
            continue
        if s3_host and _hide_s3_or_the_tag(soup, tag, s3_host):
            changed = True
        if tag.parent is None or tag.decomposed:   # replaced, removed or unwrapped: nothing left to rebase
            continue
        if proxy and _proxy_remote_or_the_tag(soup, tag):   # after S3 hiding: what it hid is gone by now
            changed = True
        if tag.parent is None or tag.decomposed:
            continue
        for name, value in tag.attrs.items():
            if not _is_link_attr(tag, name, render):
                continue
            moved = _rebase_srcset(value, base, hosts) if name == 'srcset' else _rebase(value, base, hosts)
            if embedded:
                moved = _rebase_text(moved, base, embedded)
            if moved != value:
                tag.attrs[name] = moved
                changed = True
    return soup.decode(formatter=_FORMATTER) if changed else html


def canonicalise_links(html: str) -> str:
    """`html` with every `href`/`src`/`srcset` naming any host of this site moved onto the canonical origin, for
    content about to be stored or federated. The end of `allowlist_html`. A link of ours is also moved where it sits
    inside another URL (`https://ex.com/?u=http://abc.onion/x`, `https://web.archive.org/web/1/http://abc.onion/p`)
    when it appears literally, as `canonicalise_text` moves it in text: leaving it would store the onion host, and
    the privacy invariant outranks fidelity to what the author typed. A percent-encoded copy stays as written. With
    no app context (a fuzz campaign) there is no registry of hosts, so `html` comes back as it is."""
    if not has_app_context():
        return html
    hosts = _local_hosts()
    return _rebase_links(html, canonical_url(), hosts, render=False,
                         embedded=hosts - {_bare_host(current_app.config.get('SERVER_NAME') or '')})


# An http(s) or scheme-relative URL's start and authority in plain text (Markdown): not inside a word, another
# scheme or a path. Only `[userinfo@]host[:port]` is matched, so whatever follows a bare host (`]`, `~`, `_`, `,`, a
# path) stays text: `_rebase` keeps everything after the authority, so where the URL itself ends does not matter.
_TEXT_URL_AUTHORITY = re.compile(r'(?:(?<![^\W_])(?<![+.-])https?:|(?<![^\W_])(?<![+.:/-]))//'
                                 r'(?:[^\s/?#@<>()\[\]"\'`\\{}|^*]*@)?'   # userinfo
                                 r'(?:\[[0-9A-Fa-f:.]*\]|(?:[^\W_]|[.-])+)'   # an IPv6 literal, or a name
                                 r'(?::[0-9]*)?', re.IGNORECASE)
_TRAILING_PUNCTUATION = '.,;:!?'


def _rebase_text(text: str, base: str, hosts: frozenset[str]) -> str:
    """`text` with every URL of `hosts` in it moved onto `base`, `text` itself when none moved."""
    lowered = text.lower()
    if not any(host in lowered for host in hosts):
        return text

    def move(match):
        authority = match[0].rstrip(_TRAILING_PUNCTUATION)
        return _rebase(authority, base, hosts) + match[0][len(authority):]

    moved = _TEXT_URL_AUTHORITY.sub(move, text)
    return text if moved == text else moved


def canonicalise_text(text: str | None) -> str | None:
    """`text` (Markdown, a plain-text body) with every absolute http(s) or scheme-relative URL on a non-canonical host
    of this site moved onto the canonical origin, for text about to be stored or federated: `source.content` is
    rendered in preference to the HTML. Hosts match as `is_local_link` matches them (case, a port, a trailing dot
    ignored); everything after the authority is kept byte for byte. Foreign hosts, the canonical host as written and
    text that is not a URL are left alone, and `text` itself comes back when nothing moved. Sentence punctuation
    straight after a bare host stays in the text. A URL of ours is moved wherever it appears literally in the text, inside
    inline code, a fenced block or a foreign URL (`https://ex.com/?u=http://abc.onion/x`): leaving it would store the
    onion host, and the privacy invariant outranks fidelity to what the author typed. A percent-encoded copy stays
    as written. With no app context (a fuzz
    campaign) there is no registry of hosts, so nothing moves."""
    if not text or not has_app_context():
        return text
    return _rebase_text(text, canonical_url(), _local_hosts() - {_bare_host(current_app.config.get('SERVER_NAME') or '')})


def _on_default_storefront(storefront) -> bool:
    """`is_default` first: the default storefront's every response passes here, and it then reads no domain rows
    (an error handler's response may come from a request whose session can no longer query)."""
    return storefront.is_default or storefront.domain == default_domain().domain


# Media on the S3 public host is not loaded on an onion storefront (owner ruling 2026-10-18): the link rewrite's one
# parse replaces it with a placeholder, so the onion never makes a request to that clearnet host.
_MEDIA_TAGS = frozenset({'img', 'video', 'audio', 'picture', 'iframe', 'embed', 'object'})
_MEDIA_PARTS = frozenset({'source', 'track'})   # part of the <video>, <audio> or <picture> around them
_MEDIA_CONTAINERS = frozenset({'video', 'audio', 'picture'})
_REMOVED_TAGS = frozenset({'link', 'meta', 'script'})   # a preload, a preview image, a script: nothing to show
# The attributes a browser reads as a URL to load or follow. Never alt, title or a form field's value: they are
# text, and a post's edit form holds its URL in one.
_URL_ATTRS = frozenset({'src', 'srcset', 'href', 'xlink:href', 'poster', 'data', 'action', 'formaction', 'background',
                        'ping', 'manifest', 'cite', 'longdesc'})
_PRECONNECT_RELS = frozenset({'preconnect', 'dns-prefetch'})
_CSS_ESCAPE = re.compile(r'\\(?:([0-9a-fA-F]{1,6})[ \t\n\r\f]?|(.))', re.DOTALL)
_QUOTE = re.compile(r'["\']')
_SPECIAL_SCHEME = re.compile(r'([A-Za-z][A-Za-z0-9+.-]*):')
_SPECIAL_SCHEMES = frozenset({'http', 'https', 'ws', 'wss', 'ftp'})
# Code points a browser refuses in a host once it is decoded and mapped (the WHATWG forbidden domain code points)
_FORBIDDEN_IN_HOST = frozenset(''.join(chr(c) for c in range(0x21)) + '#%/:<>?@[\\]^|\x7f')
UNMAPPABLE = '\0unmappable'   # browser_host of a host UTS-46 cannot map: neither "this host" nor "another", see below


_IPV4_DIGITS = {10: frozenset('0123456789'), 16: frozenset('0123456789abcdefABCDEF'), 8: frozenset('01234567')}


def _ipv4_part(part: str):
    """One part of a WHATWG IPv4 address as an int, None when it is no number (int() alone takes `-1`, `1_0`)."""
    radix = 10
    if part[:2] in ('0x', '0X'):
        part, radix = part[2:], 16
    elif len(part) > 1 and part[0] == '0':
        part, radix = part[1:], 8
    if not set(part) <= _IPV4_DIGITS[radix]:
        return None
    return int(part, radix) if part else 0


def _ipv4_host(host: str) -> str:
    """The dotted-decimal address of a host that ends in a number (`3405803781`, `0xcb.0.113.5`), as the WHATWG
    IPv4 parser reads it; '' when it is no address, which a browser refuses."""
    parts = host.split('.')
    if parts[-1] == '' and len(parts) > 1:
        parts.pop()
    numbers = [_ipv4_part(part) for part in parts] if len(parts) <= 4 and '' not in parts else [None]
    if None in numbers or any(n > 255 for n in numbers[:-1]) or numbers[-1] >= 256 ** (5 - len(numbers)):
        return ''
    value = numbers[-1] + sum(n * 256 ** (3 - i) for i, n in enumerate(numbers[:-1]))
    return str(ipaddress.IPv4Address(value))


def _ends_in_a_number(host: str) -> bool:
    parts = host.split('.')
    if parts[-1] == '' and len(parts) > 1:
        parts.pop()   # one trailing dot only: `a.1..` ends in an empty part, so in no number
    last = parts[-1]
    if last[:2] in ('0x', '0X'):
        return set(last[2:]) <= _IPV4_DIGITS[16]
    return last.isascii() and last.isdecimal()


def browser_host(url) -> str:
    """The bare host a browser loads absolute or scheme-relative `url` from, '' when it names none or the browser
    refuses it. It follows the WHATWG URL host parser: C0 controls and spaces stripped from both ends, tab and line
    breaks removed (the table `is_safe_redirect_target` uses), `\\` as `/`, any number of slashes after a special
    scheme or in front of a scheme-relative authority (`https:host`, `https:///host`, `///host`), userinfo and
    port ignored, the host percent-decoded and mapped by UTS-46 (`idna.uts46_remap`: case, fullwidth and ideographic
    stops, the characters it deletes), a trailing dot dropped. A host ending in a number is the IPv4 address it
    names, an IPv6 literal its compressed address. A host UTS-46 cannot map comes back as UNMAPPABLE, which callers
    that hide things must treat as a match: whether a browser loads it is not worth betting on."""
    if not isinstance(url, str):
        return ''
    candidate = url.strip(_C0_OR_SPACE).translate(URL_STRIPPED_BY_BROWSERS).replace('\\', '/')
    scheme = _SPECIAL_SCHEME.match(candidate)
    if scheme and scheme[1].lower() in _SPECIAL_SCHEMES:
        authority = candidate[scheme.end():].lstrip('/')
    elif candidate.startswith('//'):
        authority = candidate.lstrip('/')
    else:
        return ''
    authority = re.split(r'[/?#]', authority, maxsplit=1)[0].rpartition('@')[2]
    if authority.startswith('['):
        literal, closed, after = authority[1:].partition(']')
        try:
            if not closed or after.partition(':')[0]:
                return ''
            address = ipaddress.IPv6Address(literal)
            return str(address.ipv4_mapped or address.compressed)   # the IPv4 host a dual-stack client reaches
        except ValueError:
            return ''
    try:
        host = unquote_to_bytes(authority.partition(':')[0]).decode('utf-8')
    except UnicodeDecodeError:
        return ''
    except UnicodeEncodeError:
        return UNMAPPABLE   # a lone surrogate: no browser text holds one, so no answer is worth betting on
    try:
        host = idna.uts46_remap(host, std3_rules=False, transitional=False)
    except idna.IDNAError:
        return UNMAPPABLE
    if not host.isascii():
        # one spelling for an IDN: its labels in punycode (idna.encode would refuse hosts a browser loads)
        host = '.'.join(label if label.isascii() else 'xn--' + label.encode('punycode').decode()
                        for label in host.split('.'))
    if _FORBIDDEN_IN_HOST & set(host):
        return ''
    if _ends_in_a_number(host):
        return _ipv4_host(host)
    return host.rstrip('.')


def _s3_host() -> str:
    """The host of `S3_PUBLIC_URL` (`host`, `host/prefix`, `https://host/`, `203.0.113.5:9000`), '' when S3 is not
    configured or the value names no host, read as `browser_host` reads a URL so the two compare."""
    public = (current_app.config.get('S3_PUBLIC_URL') or '').strip()
    host = browser_host(public if '//' in public else f'//{public}') if public else ''
    return '' if host == UNMAPPABLE else host


# CSS Syntax 3 section 4, on the raw value. A name is a whole run of ident code points and escapes (an ident, a
# function's name, a number's unit, a hash or an at-keyword: none of them opens or closes anything, so they need not be
# told apart), and it is url( only when, escapes undone, it is "url" in any case. A string ends at its quote or an
# unescaped newline; escapes are undone per token only.
_CSS_NAME_ESCAPE = r'\\(?:[0-9a-fA-F]{1,6}[ \t\n]?|[^\n0-9a-fA-F])'
_CSS_TOKEN = re.compile(r'([@#]?(?:[\w\x00\x80-\U0010ffff-]|' + _CSS_NAME_ESCAPE + r')+)(\()?|["\']|/\*',
                        re.ASCII | re.DOTALL)
_CSS_SPACE = re.compile(r'[ \t\n\r\f]*')
_CSS_STRING = {'"': re.compile(r'[^"\\\n]*(?:\\.[^"\\\n]*)*', re.DOTALL),
               "'": re.compile(r"[^'\\\n]*(?:\\.[^'\\\n]*)*", re.DOTALL)}
_CSS_BARE_URL = re.compile(r'[^)\\]*(?:\\.[^)\\]*)*', re.DOTALL)
_CSS_NEWLINE = re.compile(r'\r\n?|\f')


def _css_code_point(digits: str) -> str:
    """The character of a hex escape; U+FFFD for zero, a surrogate or past U+10FFFF (CSS Syntax 4.3.7)."""
    code = int(digits, 16)
    return chr(code) if 0 < code <= 0x10FFFF and not 0xD800 <= code <= 0xDFFF else '\ufffd'


def _css_unescape(text: str) -> str:
    return _CSS_ESCAPE.sub(lambda m: _css_code_point(m[1]) if m[1] else '' if m[2] == '\n' else m[2], text)


def _css_url_spans(style: str) -> tuple[str, list[tuple[int, int, str]]]:
    """`style` with its newlines normalised, and the `(start, end, url)` of each token `_css_urls` finds in it:
    the span of the raw text in that normalised value, and the URL with its escapes undone."""
    style = _CSS_NEWLINE.sub('\n', style)
    found, pos = [], 0
    while True:
        token = _CSS_TOKEN.search(style, pos)
        if token is None:
            return style, found
        pos = token.end()
        if token[1]:
            if not token[2] or _css_unescape(token[1]).lower() != 'url':
                continue   # a name, or another function: what follows is scanned like the rest
            pos = _CSS_SPACE.match(style, pos).end()
            if style[pos:pos + 1] in _CSS_STRING:
                continue   # url("x"): the string is the token
            end = _CSS_BARE_URL.match(style, pos).end()
            found.append((pos, end, _css_unescape(style[pos:end]).strip()))
            pos = end + 1
        elif token[0] == '/*':
            end = style.find('*/', pos)
            if end < 0:
                return style, found
            pos = end + 2
        else:
            end = _CSS_STRING[token[0]].match(style, pos).end()
            if end > pos:
                found.append((pos, end, _css_unescape(style[pos:end])))
            pos = end + 1


def _css_urls(style: str) -> list[str]:
    """The `url(...)` tokens and quoted strings (`image-set('x' 1x)`, `src("x")`) of a `style` value, in one
    left-to-right scan of the raw value as CSS Syntax tokenises it (newlines normalised first): a `url(` inside a
    string, a comment or a longer name (`xurl(`, `1url(`) is not a token, `u\\72l(` and `\\75 rl(` are, a quote
    inside an unquoted `url(...)` is not a string, and an escaped quote, `*/` or newline opens or closes nothing.
    Escapes are undone on each token found, not before. A string ends at its quote or an unescaped line break, an
    unclosed `url(` and comment at the end. Every character is passed once, so the scan is linear."""
    return [url for _start, _end, url in _css_url_spans(style)[1]]


_PERCENT_PASSES = 4   # %2563 is %63 is c: decoded until it no longer changes, at most this often
_IPV4_RUN = re.compile(r'[0-9a-fx.]+')   # all an IPv4 host is made of, once read as a browser reads a host
_IPV6_LITERAL = re.compile(r'\[[0-9a-f:.]*\]')


class _Uts46Table(dict):
    """A `str.translate` table: each code point mapped by UTS-46 as a browser maps a host (case, fullwidth forms,
    ideographic stops, the code points it ignores dropped), itself when UTS-46 refuses it. Filled as characters are
    met, up to a bound, so a page of distinct characters cannot grow it without end."""

    def __missing__(self, code: int) -> str:
        try:
            mapped = idna.uts46_remap(chr(code), std3_rules=False, transitional=False)
        except idna.IDNAError:
            mapped = chr(code)
        if len(self) < 0x10000:
            self[code] = mapped
        return mapped


_UTS46 = _Uts46Table()


def _read_as_a_host(text: str) -> str:
    """All of `text` read the way a browser reads a host: line breaks normalised and CSS escapes undone as CSS does,
    tab and line breaks removed and percent-decoded until stable, then UTS-46-mapped one character at a time (which
    lower-cases) and NFC-composed. Applied to the whole value, not only to what looks like a host, so no spelling of
    a host in it survives as a different text."""
    text = _css_unescape(_CSS_NEWLINE.sub('\n', text))
    for _pass in range(_PERCENT_PASSES):
        decoded = unquote(text, errors='replace').translate(URL_STRIPPED_BY_BROWSERS)
        if decoded == text:
            break
        text = decoded
    return unicodedata.normalize('NFC', text.translate(_UTS46))


def _is_address(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _names_host(text: str, host: str) -> bool:
    """Whether `text` names `host` in any spelling a browser reads as that host: `text` is read as a host
    (`_read_as_a_host`) and searched for the host in punycode and in Unicode. An address host is also found in any
    numeric spelling (`3405803781`, `0xcb.0.113.5`, `[::ffff:cb00:7105]`): every run that could be an address is
    read by `browser_host`. A backstop for what the scan of a `style` value does not read as a URL; it hides too much
    (a host in a comment or a font name) rather than too little."""
    value = _read_as_a_host(text)
    spellings = {host}
    try:
        spellings.add(idna.decode(host))
    except (idna.IDNAError, UnicodeError):
        pass   # an address, or a name with no Unicode spelling
    if any(spelling in value for spelling in spellings):
        return True
    candidates = set(_IPV4_RUN.findall(value)) | set(_IPV6_LITERAL.findall(value)) if _is_address(host) else ()
    return any(browser_host(f'//{candidate}') == host for candidate in candidates)


def _points_at(tag, name: str, value, host: str) -> bool:
    """Whether attribute `name` of `tag` makes the browser load `value` from `host`."""
    if not isinstance(value, str):
        return False
    hosts = (host, UNMAPPABLE)
    if name == 'srcset':
        return any(browser_host(value[start:end]) in hosts for start, end in _srcset_urls(value))
    if name == 'style':
        return any(browser_host(url.strip('\'" ')) in hosts for url in _css_urls(value)) or _names_host(value, host)
    if name == 'href' and tag.name == 'link' and _PRECONNECT_RELS & set(tag.get('rel') or ()):
        return browser_host(value) in hosts or browser_host(f'//{value}') in hosts   # base.html writes no scheme
    # a data-* value counts only when it is itself a URL
    return (name in _URL_ATTRS or name.startswith('data-')) and browser_host(value) in hosts


def _hide_s3(soup, tag, host: str) -> bool:
    """Replace what loads from the S3 public host `host` on `tag`; whether anything changed. An `img`, `video`,
    `audio`, `picture`, `iframe`, `embed` or `object` with a URL attribute (`src`, `srcset`, `poster`, `data-*`,
    `style` url(...) and image-set(...)) on that host becomes a placeholder, a `source` or `track` taking its `video`,
    `audio` or `picture` with it. A `link`, `meta` or `script` is removed, a link (`a`) loses its anchor and keeps its
    text, and any other tag loses the attribute. Hosts are compared (`browser_host`); a `style` value is also hidden
    when it names the host in any spelling (`_names_host`). The text of a `<style>` block or a script is not read."""
    hits = [name for name, value in tag.attrs.items() if _points_at(tag, name, value, host)]
    if not hits:
        return False
    if tag.name in _REMOVED_TAGS:
        tag.decompose()
    elif tag.name == 'a':
        tag.unwrap()
    elif tag.name in _MEDIA_TAGS | _MEDIA_PARTS:
        media = tag.parent if tag.name in _MEDIA_PARTS and tag.parent.name in _MEDIA_CONTAINERS else tag
        media.replace_with(_placeholder(soup)).decompose()
    else:
        for name in hits:
            del tag.attrs[name]
    return True


def _placeholder(soup):
    placeholder = soup.new_tag('span', attrs={'class': 'onion-media-hidden'})
    placeholder.string = _('Media hidden on the onion site')
    return placeholder


def _hide_s3_or_the_tag(soup, tag, host: str) -> bool:
    """`_hide_s3`, failing closed: should it raise, the error is logged and `tag` is replaced by the placeholder
    whole, so the page is still served and still makes no request to the S3 host."""
    try:
        return _hide_s3(soup, tag, host)
    except Exception:
        current_app.logger.exception('storefront: the S3 media check failed; the element is hidden')
        tag.replace_with(_placeholder(soup)).decompose()
        return True


# Remote media on an onion page loads through the onion (onion media proxy spec 2026-10-19, section 3.1): the link
# rewrite's one parse points it at the signed proxy, so the visitor's browser never asks a remote host for it.
_PROXIED_TAGS = frozenset({'img', 'video', 'audio', 'source', 'track', 'picture'})
_EMBED_TAGS = frozenset({'iframe', 'embed', 'object'})
_MEDIA_EXTENSIONS = tuple(MEDIA_SUFFIXES)


def _is_remote(url) -> bool:
    """Whether a browser loads `url` from a host that is not one of ours. One UTS-46 cannot map counts as remote."""
    host = browser_host(url)
    return bool(host) and (host == UNMAPPABLE or not is_local_host(host))


def _fetchable(url: str) -> str | None:
    """Remote `url` as the absolute http(s) URL the proxy fetches, https for a scheme-relative one; None when it is
    written in a spelling only a browser reads (`https:host`, a backslash, a tab, a space at an end)."""
    if url != url.strip(_C0_OR_SPACE) or url != url.translate(URL_STRIPPED_BY_BROWSERS) or '\\' in url:
        return None
    if url.startswith('//'):
        url = 'https:' + url
    try:
        parts = urlsplit(url)
    except ValueError:   # an unbalanced bracket in the authority: no client fetches it
        return None
    if parts.scheme.lower() not in ('http', 'https') or not url[len(parts.scheme) + 1:].startswith('//') \
            or not parts.netloc:
        return None
    return url


def _is_media_path(url: str) -> bool:
    path = urlsplit(url).path.lower()
    return path.endswith(_MEDIA_EXTENSIONS) or '/pictrs/image/' in path


def _proxied(url, media_only: bool = False):
    """`url` as its proxy URL when it is remote (and, with `media_only`, a media file); `url` itself when it is not;
    None when it is remote and cannot be fetched as written, so the caller hides it."""
    if not isinstance(url, str) or not _is_remote(url):
        return url
    remote = _fetchable(url)
    if remote is not None and media_only and not _is_media_path(remote):
        return url   # a link to a remote page: the visitor's choice to leave
    return None if remote is None else proxy_url(remote)


def _proxied_srcset(srcset: str) -> str | None:
    out, last = [], 0
    for start, end in _srcset_urls(srcset):
        moved = _proxied(srcset[start:end])
        if moved is None:
            return None
        out += [srcset[last:start], moved]
        last = end
    return ''.join(out) + srcset[last:]


def _proxied_style(style: str) -> str | None:
    """`style` with each remote `url(...)` or string (`_css_url_spans`) replaced, at its own position, by its proxy
    URL; None when one cannot be fetched as written, or a remote one is still there afterwards. `style` itself when
    nothing is remote, otherwise with its newlines normalised."""
    text, spans = _css_url_spans(style)
    out, last = [], 0
    for start, end, url in spans:
        url = url.strip('\'" ')
        moved = _proxied(url) if url else url
        if moved is None:
            return None
        if moved != url:
            out += [text[last:start], moved]
            last = end
    if not out:
        return None if any(_is_remote(url.strip('\'" ')) for _s, _e, url in spans) else style
    result = ''.join(out) + text[last:]
    return None if any(_is_remote(url.strip('\'" ')) for url in _css_urls(result)) else result


def _proxied_value(name: str, value):
    """Attribute `name` of a media element with its remote URLs on the proxy, None when one must be hidden."""
    if not isinstance(value, str):
        return value
    if name == 'srcset':
        return _proxied_srcset(value)
    if name == 'style':
        return _proxied_style(value)
    return _proxied(value) if name in ('src', 'poster') or name.startswith('data-') else value


def _proxy_remote(soup, tag) -> bool:
    """Point what `tag` loads from a remote host at the media proxy; whether anything changed. On an `img`, `video`,
    `audio`, `source`, `track` or `picture`: `src`, `poster`, `data-*`, each `srcset` candidate and `style` url(...).
    On any other element: `style` url(...) (a community, feed or profile banner), and on an `a` also `href`, only
    when it names a media file (an extension of MEDIA_SUFFIXES, or a `/pictrs/image/` path). A remote `iframe`, `embed`
    or `object` becomes the placeholder and a plain link to it, which loads nothing until the visitor follows it.
    What cannot be proxied as written is hidden: a media element becomes the placeholder, an `a` keeps its text, and
    any other element loses the `style`."""
    if tag.name in _EMBED_TAGS:
        if not any(_proxied_value(name, value) != value or (name in _URL_ATTRS and _is_remote(value))
                   for name, value in tag.attrs.items()):   # `data` on an `object`: the embed itself
            return False
        target = next((tag[name] for name in ('src', 'data') if _is_remote(tag.get(name))), None)
        shown = [_placeholder(soup)]
        if target:
            link = soup.new_tag('a', attrs={'href': target, 'rel': 'nofollow noreferrer'})
            link.string = target
            shown.append(link)
        tag.replace_with(*shown).decompose()
        return True
    if tag.name in _PROXIED_TAGS:
        moved = {name: _proxied_value(name, value) for name, value in tag.attrs.items()}
    else:   # any element's style, a banner's background-image among them (spec section 2)
        moved = {'style': _proxied_value('style', tag.get('style'))}
        if tag.name == 'a':
            moved['href'] = _proxied(tag.get('href'), media_only=True)
    moved = {name: value for name, value in moved.items() if value != tag.get(name)}
    if not moved:
        return False
    if tag.name in _PROXIED_TAGS and None in moved.values():
        tag.replace_with(_placeholder(soup)).decompose()
    elif tag.name == 'a' and 'href' in moved and moved['href'] is None:
        tag.unwrap()
    else:   # a style that cannot be proxied as written goes; the element and its content stay
        tag.attrs.update({name: value for name, value in moved.items() if value is not None})
        for name in [name for name, value in moved.items() if value is None]:
            del tag.attrs[name]
    return True


def _proxy_remote_or_the_tag(soup, tag) -> bool:
    """`_proxy_remote`, failing closed as `_hide_s3_or_the_tag` does: should it raise, the error is logged and `tag` is
    replaced by the placeholder whole, so the page is still served and asks no remote host for the element."""
    try:
        return _proxy_remote(soup, tag)
    except Exception:
        current_app.logger.exception('storefront: the remote media proxy rewrite failed; the element is hidden')
        tag.replace_with(_placeholder(soup)).decompose()
        return True


def _s3_host_to_hide(storefront) -> str:
    """The S3 host to hide on `storefront`: '' off the onion, with S3 unset, or when that host is one of ours (the
    link rewrite moves it onto the onion). A clearnet or S3-less install reads nothing more than that."""
    if not storefront.is_onion:
        return ''
    host = _s3_host()
    return host if host and host not in _local_hosts() else ''


def rewrite_links(body: str, storefront: DomainInfo) -> str:
    """`body` with the links of this site's other hosts (`href`, `src`, `srcset`, `action`, `<meta content>`,
    `data-*`) moved onto `storefront`, and on an onion storefront whatever loads from the S3 public host hidden
    (`_hide_s3`) and remote media pointed at the media proxy (`_proxy_remote`), in one parse. Off the onion, `body`
    itself, unparsed, when no such host appears in it and there is no S3 host to hide; an onion page is always
    parsed. Links already on `storefront` are left alone, so a clearnet page that names only its own storefront is
    never parsed."""
    return _rebase_links(body, _origin(storefront), _local_hosts() - {normalise_host(storefront.domain)}, render=True,
                         s3_host=_s3_host_to_hide(storefront), proxy=storefront.is_onion)


def storefront_html(html: str | None) -> str | None:
    """Stored HTML as the current storefront shows it: `rewrite_links` off the default storefront. For HTML that
    leaves in something other than a text/html response (an RSS item body)."""
    storefront = current_storefront()
    if html is None or _on_default_storefront(storefront):
        return html
    return rewrite_links(html, storefront)


def rewrite_response_for_storefront(response: Response) -> Response:
    """`after_request`: a response on a storefront other than the default has its registered-host links moved onto
    that storefront (spec §3.5), once per response instead of at each `|safe` in the templates, and on an onion
    storefront its remote media pointed at the media proxy (`rewrite_links`). A redirect header
    naming another host of ours moves too; a relative one is already on the storefront. Streamed, passthrough
    (SSE, files) and encoded (Content-Encoding) bodies are never read. set_data recomputes Content-Length; status
    and other headers are kept."""
    storefront = current_storefront()
    if _on_default_storefront(storefront):
        return response
    base, hosts = _origin(storefront), _local_hosts() - {normalise_host(storefront.domain)}
    for header in _REDIRECT_HEADERS:
        if header in response.headers:
            response.headers[header] = _rebase(response.headers[header], base, hosts)
    if (response.mimetype == 'text/html' and not response.direct_passthrough and not response.is_streamed
            and 'Content-Encoding' not in response.headers):   # an encoded body is bytes, never decoded here
        body = response.get_data(as_text=True)
        rewritten = rewrite_links(body, storefront)
        if rewritten is not body:
            response.set_data(rewritten)
    return response


HEALTH_ENDPOINTS = frozenset({'main.health', 'main.health2'})   # probes name localhost or an internal hostname


def _bind(storefront) -> None:
    """Point this request's URL building (`url_for(_external=True)`) at `storefront`."""
    request_ctx.url_adapter = current_app.url_map.bind(_authority(storefront), script_name=request.script_root,
                                                       url_scheme=storefront.scheme)


@contextmanager
def default_storefront():
    """Render under the default storefront for the duration of the block, whatever the request's Host was.

    For mail to someone other than the requester (a newsletter, an approval, an invite, a message to the admins):
    its recipient may not use Tor, and links on the sender's storefront would tell them which one the sender uses.
    Mail to the requester themself (their password reset, their own verification) follows their storefront.
    Outside a request the default storefront is already in effect."""
    if not has_request_context():
        yield
        return
    had_storefront, before = 'storefront' in g, g.get('storefront')
    adapter = request_ctx.url_adapter
    g.storefront = default_domain()
    _bind(g.storefront)
    try:
        yield
    finally:
        request_ctx.url_adapter = adapter
        if had_storefront:
            g.storefront = before
        else:
            del g.storefront


def resolve_storefront() -> Response | None:
    """First `before_request`: set `g.storefront` from the Host header, or refuse an unregistered host with 421.

    Flask 3.1 builds the request's URL adapter with SERVER_NAME as the server name, so a request on another
    registered host still routes (3.1 no longer restricts requests to SERVER_NAME) but `url_for(_external=True)`
    would name SERVER_NAME. The adapter is rebound to the storefront's own domain and scheme, so external URLs
    follow the host. SERVER_NAME itself is untouched."""
    storefront = domain_for_host(request.host)
    if storefront is None and request.endpoint in HEALTH_ENDPOINTS:
        storefront = default_domain()   # health checks are exempt from the 421
    if storefront is None:
        return Response('Misdirected Request: this host is not served here.\n', status=421, mimetype='text/plain')
    g.storefront = storefront
    _bind(storefront)
    return None


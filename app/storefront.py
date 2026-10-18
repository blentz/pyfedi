"""The domains this site answers on (spec 2026-10-17 §3.1).

`SERVER_NAME` stays the canonical federation domain. With an empty `site_domain` table it is the one
implicit default, so installs that never ran the migration's seed still work.
"""
from __future__ import annotations

import hashlib
import ipaddress
import re
from contextlib import contextmanager
from contextvars import ContextVar
from typing import NamedTuple
from urllib.parse import unquote_to_bytes, urlsplit, urlunsplit

import idna
from bs4 import BeautifulSoup
from bs4.dammit import EntitySubstitution
from bs4.formatter import HTMLFormatter
from flask import Response, current_app, g, has_app_context, has_request_context, request
from flask.globals import request_ctx
from flask_babel import _
from sqlalchemy.exc import SQLAlchemyError

from app import cache, db
from app.onion import onion_proxy_address
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


_REGISTRATION_FROM_MODE = {'Open': 'anyone', 'RequireApplication': 'anyone', 'Closed': 'admin'}


def registration_from_mode(mode: str) -> str:
    """Map `site.registration_mode` to a `site_domain.registration` value. Unknown modes are closed."""
    return _REGISTRATION_FROM_MODE.get(mode, 'admin')


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
    return DomainInfo(domain, scheme, True, 'anyone', True, domain.endswith('.onion'))


@cache.memoize(timeout=300)
def domain_rows() -> tuple[DomainInfo, ...]:
    rows = db.session.query(models.SiteDomain).order_by(models.SiteDomain.id).all()
    if not rows:
        return (_synthesised_default(),)
    return tuple(DomainInfo(r.domain, r.scheme, r.federates, r.registration, r.is_default, r.is_onion)
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
# A srcset candidate's URL: after the start or a comma, up to whitespace; trailing commas end the candidate.
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


_SPACE_RUN = re.compile(r'\s*')
_NON_SPACE_RUN = re.compile(r'\S+')


def _srcset_urls(srcset: str) -> list[tuple[int, int]]:
    """The (start, end) of each candidate URL in a `srcset`: the first non-space run at the start and after each
    comma, less the commas that end it (`a.jpg, b.jpg 2x`, `a,b.jpg,`). A scan, not a pattern: linear in `srcset`
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
                  s3_host: str = '') -> str:
    """`html` with its link attributes on `base` where their host is in `hosts`. The parse is skipped when no host
    appears in the text at all (case-insensitively), and `html` itself comes back when no attribute moved, so markup
    is re-serialised only when a link really changes. With `embedded`, a stored link also has any URL of those hosts
    inside it moved (`?u=http://abc.onion/x`), as `canonicalise_text` moves them in text. With `s3_host`, the one
    parse also hides what loads from that host (`_hide_s3`), and is never skipped: no raw-text check can see a host
    spelled with an entity or a percent-escape."""
    lowered = html.lower()
    if not s3_host and not any(host in lowered for host in hosts):
        return html
    soup = BeautifulSoup(html, 'html.parser', on_duplicate_attribute='ignore')   # the first, as a browser does
    changed = False
    for tag in soup.find_all(True):
        if tag.decomposed:   # inside something already replaced or removed
            continue
        if s3_host and _hide_s3(soup, tag, s3_host):
            changed = True
        if tag.parent is None or tag.decomposed:   # replaced, removed or unwrapped: nothing left to rebase
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
            return ipaddress.IPv6Address(literal).compressed if closed and not after.partition(':')[0] else ''
        except ValueError:
            return ''
    try:
        host = unquote_to_bytes(authority.partition(':')[0]).decode('utf-8')
    except UnicodeDecodeError:
        return ''
    try:
        host = idna.uts46_remap(host, std3_rules=False, transitional=False)
    except idna.IDNAError:
        return UNMAPPABLE
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


def _quoted_strings(text: str) -> list[str]:
    strings, pos = [], 0
    while True:
        opener = _QUOTE.search(text, pos)
        if opener is None:
            return strings
        pos = opener.end()
        end = text.find(opener[0], pos)
        if end < 0:
            continue   # no later quote of this kind exists, so this is the one scan it will cost
        strings.append(text[pos:end])
        pos = end + 1


def _css_urls(style: str) -> list[str]:
    """The `url(...)` tokens and quoted strings (`image-set('x' 1x)`) of a `style` value, CSS escapes undone. Found
    with `str.find`, not a pattern: a `url(` that never closes costs one scan, not one per `url(`."""
    style = _CSS_ESCAPE.sub(lambda m: chr(int(m[1], 16) % 0x110000 or 0xfffd) if m[1] else m[2], style)
    urls, lowered, pos = [], style.lower(), 0
    while True:
        start = lowered.find('url(', pos)
        end = lowered.find(')', start + 4) if start >= 0 else -1
        if end < 0:
            return urls + _quoted_strings(style)
        urls.append(style[start + 4:end].strip())
        pos = end + 1


def _points_at(tag, name: str, value, host: str) -> bool:
    """Whether attribute `name` of `tag` makes the browser load `value` from `host`."""
    if not isinstance(value, str):
        return False
    hosts = (host, UNMAPPABLE)
    if name == 'srcset':
        return any(browser_host(value[start:end]) in hosts for start, end in _srcset_urls(value))
    if name == 'style':
        return any(browser_host(url.strip('\'" ')) in hosts for url in _css_urls(value))
    if name == 'href' and tag.name == 'link' and _PRECONNECT_RELS & set(tag.get('rel') or ()):
        return browser_host(value) in hosts or browser_host(f'//{value}') in hosts   # base.html writes no scheme
    # a data-* value counts only when it is itself a URL
    return (name in _URL_ATTRS or name.startswith('data-')) and browser_host(value) in hosts


def _hide_s3(soup, tag, host: str) -> bool:
    """Replace what loads from the S3 public host `host` on `tag`; whether anything changed. An `img`, `video`,
    `audio`, `picture`, `iframe`, `embed` or `object` with a URL attribute (`src`, `srcset`, `poster`, `data-*`,
    `style` url(...) and image-set(...)) on that host becomes a placeholder, a `source` or `track` taking its `video`,
    `audio` or `picture` with it. A `link`, `meta` or `script` is removed, a link (`a`) loses its anchor and keeps its
    text, and any other tag loses the attribute. Hosts are compared (`browser_host`), never searched for as text.
    The text of a `<style>` block or a script is not read."""
    hits = [name for name, value in tag.attrs.items() if _points_at(tag, name, value, host)]
    if not hits:
        return False
    if tag.name in _REMOVED_TAGS:
        tag.decompose()
    elif tag.name == 'a':
        tag.unwrap()
    elif tag.name in _MEDIA_TAGS | _MEDIA_PARTS:
        media = tag.parent if tag.name in _MEDIA_PARTS and tag.parent.name in _MEDIA_CONTAINERS else tag
        placeholder = soup.new_tag('span', attrs={'class': 'onion-media-hidden'})
        placeholder.string = _('Media hidden on the onion site')
        media.replace_with(placeholder).decompose()
    else:
        for name in hits:
            del tag.attrs[name]
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
    (`_hide_s3`), in one parse. `body` itself, unparsed, when no such host appears in it and there is no S3 host to
    hide. Links already on `storefront` are left alone, so a page that names only its own storefront is never parsed."""
    return _rebase_links(body, _origin(storefront), _local_hosts() - {normalise_host(storefront.domain)}, render=True,
                         s3_host=_s3_host_to_hide(storefront))


def storefront_html(html: str | None) -> str | None:
    """Stored HTML as the current storefront shows it: `rewrite_links` off the default storefront. For HTML that
    leaves in something other than a text/html response (an RSS item body)."""
    storefront = current_storefront()
    if html is None or _on_default_storefront(storefront):
        return html
    return rewrite_links(html, storefront)


def rewrite_response_for_storefront(response: Response) -> Response:
    """`after_request`: a response on a storefront other than the default has its registered-host links moved onto
    that storefront (spec §3.5), once per response instead of at each `|safe` in the templates. A redirect header
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


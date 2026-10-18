"""The domains this site answers on (spec 2026-10-17 §3.1).

`SERVER_NAME` stays the canonical federation domain. With an empty `site_domain` table it is the one
implicit default, so installs that never ran the migration's seed still work.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import NamedTuple
from urllib.parse import urlsplit, urlunsplit

from flask import Response, current_app, g, has_request_context, request
from flask.globals import request_ctx
from sqlalchemy.exc import SQLAlchemyError

from app import cache, db
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


def _bare_host(authority: str) -> str:
    """The host of an authority (`host`, `host:port`, `[v6]:port`), lower-cased, brackets and trailing dot stripped;
    '' when it names none. A bare IPv6 literal (`::1`, as `site_domain.domain` holds it) is its own host."""
    authority = authority.strip()
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
    wanted = normalise_host(host)
    return next((row for row in domain_rows() if normalise_host(row.domain) == wanted), None)


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


def is_local_link(url) -> bool:
    """Whether absolute (or scheme-relative) `url` points at this site: `is_local_host` of its host. The host is
    urlsplit's, after any userinfo. A browser reads `\\` as `/`, so a URL with a backslash has no host we can name
    and is not local."""
    if not isinstance(url, str) or '\\' in url:
        return False
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return False
    return is_local_host(host)


def check_default_domain() -> None:
    """Log an error when the default row is not SERVER_NAME. Quiet when the table does not exist yet
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


def storefront_link(url):
    """`url` moved onto the current storefront when it points at this site (any registered host); any other URL,
    or a value that is not a string, unchanged. For stored links shown where no HTML rewrite reaches (RSS)."""
    if not is_local_link(url):
        return url
    parts = urlsplit(url)
    origin = urlsplit(storefront_url())
    return urlunsplit((origin.scheme, origin.netloc, parts.path, parts.query, parts.fragment))


def canonical_url() -> str:
    """The federation origin. It never varies with the request host."""
    return current_app.config['SERVER_URL']


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

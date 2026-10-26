"""Requests that arrived over the onion (onion storefront spec 2026-10-17, section 3.7).

Tor traffic reaches the app as tor container -> Caddy's internal listener -> app, so every onion visitor shares the
tor container's address. Caddy's internal listener sets `X-PieFed-Via: onion` and the public listeners strip any
copy a client sent. The header alone is client-writable on any other path, so a request is onion only when the TCP
peer is Caddy as well: the address ProxyFix saw before it rewrote `REMOTE_ADDR` from `X-Forwarded-For`, compared
with `ONION_PROXY_IP`. X-Forwarded-For and TRUSTED_CLIENT_IP_HEADER are never consulted here.

This module imports nothing from `app`, so `app/__init__.py` can import it without a cycle.
"""
from __future__ import annotations

import ipaddress
import secrets
from datetime import datetime, timezone
from functools import wraps
from typing import TypeVar
from urllib.parse import urlsplit

from flask import current_app, flash, has_request_context, redirect, request, session, url_for
from flask_babel import _

ONION_MARKER_HEADER = 'X-PieFed-Via'
ONION_MARKER_VALUE = 'onion'
PROXY_FIX_ORIG = 'werkzeug.proxy_fix.orig'

# Rate-limit keys for onion requests (section 3.7). Every cookieless or unauthenticated onion client shares
# ONION_ANON_KEY, so each endpoint's own limit applies to all of them together.
ONION_ANON_KEY = 'onion-anon'
ONION_API_PREFIX = '/api/alpha/'
# Kept on the request's WSGI environ rather than in g: an app context, and so g, can outlive one request (the test
# suite pushes one for the whole session), while the environ is the request's own.
ARRIVED_SID = 'piefed.onion_sid_arrived'
API_RATE_KEY = 'piefed.onion_api_rate_key'

T = TypeVar('T')


def single_address(value) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """`value` as one IP address, None when it is not exactly one. An IPv4-mapped IPv6 address is its IPv4 address,
    so a dual-stack socket's `::ffff:10.89.0.2` is `10.89.0.2`. A network (`10.89.0.0/24`) is not an address."""
    if not isinstance(value, str):
        return None
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    return getattr(address, 'ipv4_mapped', None) or address


def onion_proxy_address() -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The configured `ONION_PROXY_IP`, None when it is unset or not a single address. None means no request is onion."""
    return single_address(current_app.config.get('ONION_PROXY_IP'))


def is_onion_host(host) -> bool:
    """Whether a Host header names a .onion: lower-cased, port and IPv6 brackets stripped, one trailing dot stripped.
    A host that does not parse is not an onion."""
    try:
        name = urlsplit(f'//{host}').hostname or ''
    except ValueError:   # an unbalanced IPv6 bracket
        return False
    if name.endswith('.'):
        name = name[:-1]
    return name.endswith('.onion')


def is_onion_request() -> bool:
    """Whether this request came over the onion: Caddy's marker header, a .onion Host, and Caddy as the TCP peer.
    False outside a request, when `ONION_PROXY_IP` is unset, and when ProxyFix did not run (no original environ to
    read). The Host check is defence in depth: should a public listener fail to strip a client's marker, a clearnet
    request still names a clearnet host."""
    if not has_request_context():
        return False
    if request.headers.get(ONION_MARKER_HEADER) != ONION_MARKER_VALUE:
        return False
    if not is_onion_host(request.host):
        return False
    proxy = onion_proxy_address()
    if proxy is None:
        return False
    original = request.environ.get(PROXY_FIX_ORIG) or {}
    return single_address(original.get('REMOTE_ADDR')) == proxy


def clearnet_only(fallback_endpoint: str):
    """View decorator: on the onion, flash that the feature is on the clearnet site and redirect to
    `fallback_endpoint`. For flows that hand a third party (an OAuth provider, Stripe) a URL back to this site,
    which would name the onion host to it."""
    def decorate(view):
        @wraps(view)
        def guarded(*args, **kwargs):
            if is_onion_request():
                flash(_('This is only available on the clearnet site.'), 'error')
                return redirect(url_for(fallback_endpoint))
            return view(*args, **kwargs)
        return guarded
    return decorate


# What a stored-IP audit returns for a request it must not write: the caller leaves the column as it is (and, for a
# row being created, at its default, NULL).
KEEP = object()
# Every onion-registered account stores this instead of an address (link-local, so never a real client's).
ONION_ACCOUNT_IP = '169.254.88.88'


def audit_field(value: T) -> T | object:
    """An IP-derived value to store (an instance's address): `KEEP` for an onion request, whose only address is the
    tor container's and so everybody's and which must not overwrite what is stored; `value` unchanged otherwise.
    Store it with `apply_audit`."""
    return KEEP if is_onion_request() else value


def has_real_ip(user) -> bool:
    """Whether `user` has a stored address that is a client's: not empty and not the onion accounts' shared sentinel.
    The IP-ban feature (same-IP lists, banning an address) treats anything else as no IP."""
    return bool(user.ip_address) and user.ip_address != ONION_ACCOUNT_IP


def apply_audit(obj, field: str, result) -> None:
    """Set `obj.field` to an audit result, unless the result is `KEEP`."""
    if result is not KEEP:
        setattr(obj, field, result)


def utcnow() -> datetime:
    """Now, as the naive UTC datetime the columns hold (the same value as `app.models.utcnow`, which this module may not
    import)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def stamp_activity(user) -> None:
    """Record that `user` was active just now: `last_seen` is now and the unread-notifications email may be sent again.
    An onion request records nothing, so no stored time links an account to onion use (spec 2026-10-26, goal 3). Work
    outside a request (a scheduled task, federation) is not an onion request and stamps as before."""
    if is_onion_request():
        return
    user.last_seen = utcnow()
    user.email_unread_sent = False


def is_api_path() -> bool:
    """Whether the request is for the alpha API, whose clients authenticate with a bearer token and keep no cookies."""
    return request.path.startswith(ONION_API_PREFIX)


def arrived_onion_sid() -> str | None:
    """The onion sid the request's session cookie brought back, None when it brought none. Read once, on first use, and
    every mint goes through `mint_onion_sid`, which reads it first: a sid minted during this request is never taken
    for one that came back."""
    if ARRIVED_SID not in request.environ:
        request.environ[ARRIVED_SID] = session.get('onion_sid')
    return request.environ[ARRIVED_SID]


def mint_onion_sid() -> None:
    """Give the session an onion sid if it has none, so the response's session cookie carries one."""
    arrived_onion_sid()
    if not session.get('onion_sid'):
        session['onion_sid'] = secrets.token_urlsafe(16)


def onion_rate_key() -> str:
    """The rate-limit key of an onion request.

    - Alpha API: `onion-user:<id>` for a valid bearer token, as `resolve_onion_api_rate_key` (app/onion_guard.py)
      found before the view; otherwise the shared key. No sid is minted: API clients do not keep cookies.
    - Web: `onion-session:<sid>` only for a sid that came back in the session cookie. A sid minted now is free to
      anyone who drops cookies, so until it comes back the request is in the shared bucket. One is minted here so
      the response carries it."""
    if is_api_path():
        return request.environ.get(API_RATE_KEY) or ONION_ANON_KEY
    sid = arrived_onion_sid()
    mint_onion_sid()
    return f'onion-session:{sid}' if sid else ONION_ANON_KEY

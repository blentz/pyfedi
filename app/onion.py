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
from typing import TypeVar
from urllib.parse import urlsplit

from flask import current_app, has_request_context, request

ONION_MARKER_HEADER = 'X-PieFed-Via'
ONION_MARKER_VALUE = 'onion'
PROXY_FIX_ORIG = 'werkzeug.proxy_fix.orig'

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


def audit_field(value: T) -> T | None:
    """An IP-derived value to store (`user.ip_address`, its country, an instance's address): None for an onion
    request, whose only address is the tor container's and so everybody's; `value` unchanged otherwise."""
    return None if is_onion_request() else value

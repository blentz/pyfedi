"""Outbound connections pinned to the address the SSRF guard checked (R162).

`is_invalid_get_request_uri` (app/utils.py) resolves a name and checks its addresses,
and httpx used to resolve the name again to connect, so a name that answered
differently the second time (DNS rebinding) reached whatever it answered. The network
backend here resolves once, refuses the name if any address is one the guard refuses,
and connects to an address it checked. The URL is untouched, so the Host header and
the TLS server name stay the hostname; a redirect to another origin is a new
connection, and so is checked again.

Every stream the backend hands out also enforces a fetch's wall-clock deadline
(fetch_deadline), when one is set: each connect, TLS handshake, read and write gets at
most the time left, and is refused once none is. httpx's own timeouts are per read, so
without this a peer that kept a connection busy without sending body bytes (h2 PINGs or
empty DATA frames, an HTTP/1.1 header block trickled a byte at a time) held a capped
fetch for as long as it liked.

This module imports nothing from app, so app/__init__.py can build its client with it.
"""
import ipaddress
import socket
import threading
import time
from contextlib import contextmanager

import httpcore
import httpx
from flask import current_app, has_app_context


def is_refused_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """An address this instance does not fetch from. D1358: `not ip.is_global` alone
    lets IPv4-compatible and NAT64 spellings of loopback and private addresses through
    (`is_reserved` covers them) and multicast (`is_multicast`). IPv6 site-local (fec0::/10) is deprecated but still
    routed to a site's own hosts where configured, and ipaddress calls it global (`is_site_local`)."""
    return not ip.is_global or ip.is_reserved or ip.is_multicast or getattr(ip, 'is_site_local', False)


_fetch = threading.local()


@contextmanager
def fetch_deadline(seconds: float):
    """Run the block under a wall-clock deadline `seconds` from now, enforced by every PinnedNetworkBackend stream
    this thread uses meanwhile (including a pooled connection opened earlier). Always cleared on the way out."""
    previous = getattr(_fetch, 'deadline', None)
    _fetch.deadline = time.monotonic() + seconds
    try:
        yield
    finally:
        _fetch.deadline = previous


def remaining_time() -> float | None:
    """Seconds left before this thread's fetch deadline, or None when no deadline is set."""
    deadline = getattr(_fetch, 'deadline', None)
    return None if deadline is None else deadline - time.monotonic()


def _clamped(timeout: float | None, refusal: type[Exception]) -> float | None:
    """`timeout`, cut to the time left before the fetch deadline; with no deadline set, `timeout` unchanged. Raises
    `refusal` once no time is left."""
    remaining = remaining_time()
    if remaining is None:
        return timeout
    if remaining <= 0:
        raise refusal('the deadline for this fetch has passed')
    return remaining if timeout is None else min(timeout, remaining)


class DeadlineStream(httpcore.NetworkStream):
    """A network stream whose every call is clamped to the fetch deadline (_clamped). start_tls wraps the TLS
    stream it returns too, or the reads after the handshake would escape the clamp."""

    def __init__(self, stream: httpcore.NetworkStream):
        self._stream = stream

    def read(self, max_bytes, timeout=None):
        return self._stream.read(max_bytes, timeout=_clamped(timeout, httpcore.ReadTimeout))

    def write(self, buffer, timeout=None):
        self._stream.write(buffer, timeout=_clamped(timeout, httpcore.WriteTimeout))

    def close(self):
        self._stream.close()

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        try:
            timeout = _clamped(timeout, httpcore.ConnectTimeout)
        except httpcore.ConnectTimeout:
            # httpcore closes the plain stream only when the handshake itself fails
            self._stream.close()
            raise
        return DeadlineStream(self._stream.start_tls(ssl_context, server_hostname=server_hostname, timeout=timeout))

    def get_extra_info(self, info):
        return self._stream.get_extra_info(info)


class PinnedNetworkBackend(httpcore.NetworkBackend):
    """Wraps httpcore's own backend, which still opens the socket and does TLS."""

    def __init__(self, backend: httpcore.NetworkBackend):
        self._backend = backend

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except (OSError, UnicodeError) as e:
            raise httpcore.ConnectError(f'{host}: {e}')
        addresses = list(dict.fromkeys(info[4][0] for info in infos))
        # as the guard: in debug a developer's peers may be on their own network
        if not (has_app_context() and current_app.debug):
            for address in addresses:
                try:
                    refused = is_refused_address(ipaddress.ip_address(address))
                except ValueError:
                    refused = True
                if refused:
                    raise httpcore.ConnectError(f'{host} resolves to {address}, which this instance does not fetch from')
        error = httpcore.ConnectError(f'{host}: no address')
        for address in addresses:
            try:
                return DeadlineStream(self._backend.connect_tcp(
                    address, port, timeout=_clamped(timeout, httpcore.ConnectTimeout), local_address=local_address,
                    socket_options=socket_options))
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as e:
                error = e
        raise error

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        return self._backend.connect_unix_socket(path, timeout=timeout, socket_options=socket_options)

    def sleep(self, seconds):
        self._backend.sleep(seconds)


def pinned_transport(**kwargs) -> httpx.HTTPTransport:
    """An httpx transport whose connections go through PinnedNetworkBackend. Through a
    proxy the connection is to the proxy, which resolves the name itself, so there is
    nothing here to pin and the transport is left as httpx built it."""
    transport = httpx.HTTPTransport(**kwargs)
    if type(transport._pool) is httpcore.ConnectionPool:
        transport._pool._network_backend = PinnedNetworkBackend(transport._pool._network_backend)
    return transport

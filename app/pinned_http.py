"""Outbound connections pinned to the address the SSRF guard checked (R162).

`is_invalid_get_request_uri` (app/utils.py) resolves a name and checks its addresses,
and httpx used to resolve the name again to connect, so a name that answered
differently the second time (DNS rebinding) reached whatever it answered. The network
backend here resolves once, refuses the name if any address is one the guard refuses,
and connects to an address it checked. The URL is untouched, so the Host header and
the TLS server name stay the hostname; a redirect to another origin is a new
connection, and so is checked again.

This module imports nothing from app, so app/__init__.py can build its client with it.
"""
import ipaddress
import socket

import httpcore
import httpx
from flask import current_app, has_app_context


def is_refused_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """An address this instance does not fetch from. D1358: `not ip.is_global` alone
    lets IPv4-compatible and NAT64 spellings of loopback and private addresses through
    (`is_reserved` covers them) and multicast (`is_multicast`)."""
    return not ip.is_global or ip.is_reserved or ip.is_multicast


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
                return self._backend.connect_tcp(address, port, timeout=timeout, local_address=local_address,
                                                 socket_options=socket_options)
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

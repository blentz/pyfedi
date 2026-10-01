"""R162, owner ruling: outbound connections are pinned to the address the SSRF guard checked.

`is_invalid_get_request_uri` (app/utils.py) resolved a name and checked the addresses,
and then httpx resolved it AGAIN to connect, so a name answering a public address the
first time and a private one the second (DNS rebinding) got through. The connection is
now made by `PinnedNetworkBackend` (app/pinned_http.py), which resolves once, refuses
the name if any address is one the guard refuses, and connects to the address it
checked. The URL is untouched, so the Host header and TLS SNI stay the hostname, and
every redirect hop to a new origin is a new connection and so is checked again.

respx intercepts at httpcore's ConnectionPool, above the network backend, so these
rows drive the backend through httpcore's own HTTPConnection, and the session's
block_outbound_http harness never sees them. The last row shows the harness still
serving an ordinary fetch through the pinned client.
"""
import socket

import httpcore
import pytest

from app import httpx_client
from app.pinned_http import PinnedNetworkBackend
from app.utils import get_request, is_invalid_get_request_uri

PUBLIC = '93.184.216.34'
PUBLIC_V6 = '2606:4700:4700::1111'
PRIVATE = '127.0.0.1'


def answers(*addresses):
    """A getaddrinfo result naming these addresses, as the resolver returns them."""
    return [(socket.AF_INET6 if ':' in a else socket.AF_INET, socket.SOCK_STREAM, 6, '', (a, 443))
            for a in addresses]


class RecordingStream(httpcore.MockStream):
    def __init__(self, buffer, record):
        super().__init__(buffer)
        self.record = record

    def write(self, buffer, timeout=None):
        self.record['written'] += buffer

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self.record['sni'] = server_hostname
        return self


class RecordingBackend(httpcore.MockBackend):
    """httpcore's MockBackend, remembering where it was asked to connect."""

    def __init__(self, buffer=(), refuse=()):
        super().__init__(list(buffer))
        self.refuse = refuse
        self.record = {'connected': [], 'written': b'', 'sni': None}

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.record['connected'].append(host)
        if host in self.refuse:
            raise httpcore.ConnectError(f'{host} unreachable')
        return RecordingStream(list(self._buffer), self.record)


@pytest.fixture
def resolver(monkeypatch):
    """Answers each lookup with the next list of addresses given."""
    queue = []
    monkeypatch.setattr(socket, 'getaddrinfo', lambda host, *args, **kwargs: answers(*queue.pop(0)))
    return queue


def test_a_name_that_rebinds_to_a_private_address_after_the_check_is_refused(app, resolver):
    """The rebinding itself: the guard is asked first and sees a public address; the
    connection resolves again and is answered with loopback."""
    resolver.extend([[PUBLIC], [PRIVATE]])
    backend = RecordingBackend()

    assert is_invalid_get_request_uri('https://rebind.example/r162') is False
    with pytest.raises(httpcore.ConnectError, match='rebind.example'):
        PinnedNetworkBackend(backend).connect_tcp('rebind.example', 443)
    assert backend.record['connected'] == []


def test_a_name_with_any_private_address_is_refused(app, resolver):
    resolver.append([PUBLIC, PRIVATE])
    backend = RecordingBackend()

    with pytest.raises(httpcore.ConnectError):
        PinnedNetworkBackend(backend).connect_tcp('mixed.example', 443)
    assert backend.record['connected'] == []


def test_the_connection_goes_to_the_checked_address_with_the_hostname_kept(app, resolver):
    """A whole request through httpcore: the socket is opened to the address that was
    checked, and the Host header and the TLS server name are still the hostname.

    HTTPConnection's own `handle_request` is what respx replaces, so this runs its two
    halves itself: `_connect`, which calls the backend and starts TLS, then the HTTP/1.1
    exchange over the stream that returns."""
    resolver.append([PUBLIC])
    backend = RecordingBackend([b'HTTP/1.1 200 OK\r\n', b'Content-Length: 2\r\n\r\n', b'ok'])
    origin = httpcore.Origin(b'https', b'peer.example', 443)
    connection = httpcore.HTTPConnection(origin=origin, network_backend=PinnedNetworkBackend(backend))
    request = httpcore.Request('GET', 'https://peer.example/actor', headers=[(b'Host', b'peer.example')])

    stream = connection._connect(request)
    response = httpcore.HTTP11Connection(origin=origin, stream=stream).handle_request(request)

    assert (response.status, response.read()) == (200, b'ok')
    assert backend.record['connected'] == [PUBLIC]
    assert backend.record['sni'] == 'peer.example'
    assert b'Host: peer.example' in backend.record['written']


def test_each_new_origin_is_resolved_and_checked_again(app, resolver):
    """A redirect to another host is a new connection, so it is checked on its own:
    the first hop's public answer does not carry over to the second's private one."""
    resolver.extend([[PUBLIC], [PRIVATE]])
    backend = RecordingBackend()
    pinned = PinnedNetworkBackend(backend)

    pinned.connect_tcp('first.example', 443)
    with pytest.raises(httpcore.ConnectError):
        pinned.connect_tcp('second.example', 443)
    assert backend.record['connected'] == [PUBLIC]


def test_the_next_checked_address_is_tried_when_one_will_not_connect(app, resolver):
    """As socket.create_connection does: a host with an IPv6 address this server
    cannot route to is still reached over IPv4."""
    resolver.append([PUBLIC_V6, PUBLIC])
    backend = RecordingBackend(refuse=(PUBLIC_V6,))

    PinnedNetworkBackend(backend).connect_tcp('dualstack.example', 443)

    assert backend.record['connected'] == [PUBLIC_V6, PUBLIC]


def test_a_name_that_does_not_resolve_is_a_connect_error(app, monkeypatch):
    def fail(*args, **kwargs):
        raise socket.gaierror(-2, 'Name or service not known')
    monkeypatch.setattr(socket, 'getaddrinfo', fail)

    with pytest.raises(httpcore.ConnectError):
        PinnedNetworkBackend(RecordingBackend()).connect_tcp('nowhere.example', 443)


def test_in_debug_a_private_address_is_allowed_as_the_guard_allows_it(app, resolver, monkeypatch):
    monkeypatch.setattr(app, 'debug', True)
    resolver.append([PRIVATE])
    backend = RecordingBackend()

    PinnedNetworkBackend(backend).connect_tcp('localpeer.example', 443)

    assert backend.record['connected'] == [PRIVATE]


def test_the_shared_client_connects_through_the_pinned_backend():
    pool = httpx_client._transport._pool

    assert isinstance(pool._network_backend, PinnedNetworkBackend)
    assert pool._http2 is True


def test_an_ordinary_fetch_still_works_under_the_harness(app, http_mock):
    http_mock.get('https://peer.example/actor').respond(200, json={'id': 'https://peer.example/actor'})

    response = get_request('https://peer.example/actor')

    assert response.status_code == 200
    assert response.json() == {'id': 'https://peer.example/actor'}

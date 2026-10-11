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
import time
from types import SimpleNamespace

import httpcore
import httpx
import pytest

from app import httpx_client
from app.pinned_http import PinnedNetworkBackend, fetch_deadline, remaining_time
from app.utils import _capped_get, get_request, get_request_capped, is_invalid_get_request_uri

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


# --------------------------------------------------------------------------
# The fetch deadline, enforced at the transport
# --------------------------------------------------------------------------
#
# _capped_get's body loop checks its deadline only when body bytes arrive, so a peer that kept the connection busy
# without sending any (h2 PINGs or empty DATA frames, an HTTP/1.1 header block trickled a byte at a time, each read
# inside the per-read timeout) held the worker for as long as it liked. fetch_deadline sets a thread-local deadline
# for one fetch; every stream PinnedNetworkBackend hands out clamps each read and write to the time left, and refuses
# one once none is.


class TimedStream(httpcore.MockStream):
    """A MockStream that records the timeout of each call and advances the fake clock by `step` per read."""

    def __init__(self, buffer, clock, step=0.0):
        super().__init__(list(buffer))
        self.clock = clock
        self.step = step
        self.timeouts = []

    def read(self, max_bytes, timeout=None):
        self.timeouts.append(('read', timeout))
        self.clock[0] += self.step
        return super().read(max_bytes, timeout)

    def write(self, buffer, timeout=None):
        self.timeouts.append(('write', timeout))

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self.timeouts.append(('start_tls', timeout))
        return self


class TimedBackend(httpcore.MockBackend):
    def __init__(self, stream):
        super().__init__([])
        self.stream = stream
        self.connect_timeouts = []

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.connect_timeouts.append(timeout)
        return self.stream


@pytest.fixture
def transport_clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr('app.pinned_http.time', SimpleNamespace(**{**vars(time), 'monotonic': lambda: now[0]}))
    return now


def test_with_no_deadline_every_timeout_passes_through_unchanged(app, resolver, transport_clock):
    resolver.append([PUBLIC])
    stream = TimedStream([b'x'], transport_clock)
    backend = TimedBackend(stream)

    wrapped = PinnedNetworkBackend(backend).connect_tcp('peer.example', 443, timeout=7)
    tls = wrapped.start_tls(None, server_hostname='peer.example', timeout=7)
    tls.write(b'GET', timeout=7)
    assert tls.read(10, timeout=7) == b'x'
    assert tls.read(10, timeout=None) == b''

    assert backend.connect_timeouts == [7]
    assert stream.timeouts == [('start_tls', 7), ('write', 7), ('read', 7), ('read', None)]
    # httpcore reads the negotiated protocol (h2 or not) through it
    assert type(tls.get_extra_info('ssl_object')) is type(stream.get_extra_info('ssl_object'))
    tls.close()


def test_under_a_deadline_each_call_gets_at_most_the_time_left(app, resolver, transport_clock):
    resolver.append([PUBLIC])
    stream = TimedStream([b'x', b'y'], transport_clock)
    backend = TimedBackend(stream)

    with fetch_deadline(15):
        wrapped = PinnedNetworkBackend(backend).connect_tcp('peer.example', 443, timeout=20)
        transport_clock[0] += 10
        tls = wrapped.start_tls(None, timeout=20)
        tls.write(b'GET', timeout=3)
        tls.read(10, timeout=None)
        transport_clock[0] += 4
        tls.read(10, timeout=10)

    assert backend.connect_timeouts == [15]
    assert stream.timeouts == [('start_tls', 5), ('write', 3), ('read', 5), ('read', 1)]


@pytest.mark.parametrize('call, error', [
    (lambda stream: stream.read(10, timeout=10), httpcore.ReadTimeout),
    (lambda stream: stream.write(b'x', timeout=10), httpcore.WriteTimeout),
    (lambda stream: stream.start_tls(None, timeout=10), httpcore.ConnectTimeout),
])
def test_once_the_deadline_has_passed_a_call_is_refused_without_reaching_the_socket(app, resolver, transport_clock,
                                                                                    call, error):
    resolver.append([PUBLIC])
    stream = TimedStream([b'x'], transport_clock)

    with fetch_deadline(15):
        wrapped = PinnedNetworkBackend(TimedBackend(stream)).connect_tcp('peer.example', 443)
        transport_clock[0] += 15
        with pytest.raises(error):
            call(wrapped)

    assert stream.timeouts == []


def test_a_handshake_refused_at_the_deadline_closes_the_plain_socket(app, resolver, transport_clock):
    """httpcore closes the TCP stream only when the handshake itself fails; a refusal before it must close it here."""
    resolver.append([PUBLIC])
    stream = TimedStream([b'x'], transport_clock)
    closed = []
    stream.close = lambda: closed.append(True)

    with fetch_deadline(15):
        wrapped = PinnedNetworkBackend(TimedBackend(stream)).connect_tcp('peer.example', 443)
        transport_clock[0] += 15
        with pytest.raises(httpcore.ConnectTimeout):
            wrapped.start_tls(None, timeout=10)

    assert closed == [True]


def test_a_connect_after_the_deadline_is_refused(app, resolver, transport_clock):
    resolver.append([PUBLIC])
    backend = TimedBackend(TimedStream([], transport_clock))

    with fetch_deadline(15):
        transport_clock[0] += 16
        with pytest.raises(httpcore.ConnectTimeout):
            PinnedNetworkBackend(backend).connect_tcp('peer.example', 443, timeout=5)
    assert backend.connect_timeouts == []


def test_a_header_block_trickled_a_byte_at_a_time_is_cut_at_the_deadline(app, resolver, transport_clock):
    """Each read answers inside any per-read timeout, five fake seconds apart; h11 would wait for 100 KiB of it."""
    resolver.append([PUBLIC])
    stream = TimedStream([bytes([byte]) for byte in b'HTTP/1.1 200 OK\r\nX-Slow: ' + b'a' * 1000], transport_clock,
                         step=5)
    origin = httpcore.Origin(b'https', b'peer.example', 443)
    request = httpcore.Request('GET', 'https://peer.example/actor', headers=[(b'Host', b'peer.example')],
                               extensions={'timeout': {'read': 10, 'write': 10, 'connect': 10, 'pool': 10}})

    with fetch_deadline(15):
        connected = PinnedNetworkBackend(TimedBackend(stream)).connect_tcp('peer.example', 443)
        with pytest.raises(httpcore.ReadTimeout):
            httpcore.HTTP11Connection(origin=origin, stream=connected).handle_request(request)

    assert len([call for call in stream.timeouts if call[0] == 'read']) <= 3


def test_the_deadline_is_cleared_after_the_fetch_even_when_it_raises(app, transport_clock):
    with pytest.raises(RuntimeError):
        with fetch_deadline(15):
            assert remaining_time() == 15
            raise RuntimeError('boom')

    assert remaining_time() is None


def test_capped_fetches_run_under_the_deadline_and_clear_it(app, http_mock):
    seen = []

    def answer(request):
        seen.append(remaining_time())
        return httpx.Response(200, text='ok')

    http_mock.get('https://peer.example/a').mock(side_effect=answer)
    http_mock.get('https://peer.example/b').mock(side_effect=answer)

    _capped_get('https://peer.example/a', max_seconds=20)
    get_request_capped('https://peer.example/b', 100, max_seconds=12)

    assert 19 < seen[0] <= 20 and 11 < seen[1] <= 12
    assert remaining_time() is None

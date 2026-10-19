"""The onion media cache fill: a streamed, SSRF-guarded fetch (onion media proxy spec 2026-10-19, section 3.3).

The remote servers are an httpx.MockTransport put in place of `app.httpx_client`, the pinned client `fill` sends
through, so no test reaches the network. Host names under `.example` resolve to a public address through a stubbed
`socket.getaddrinfo`, so `is_invalid_get_request_uri` runs its real checks (DEBUG off) without a DNS lookup. A refused
hop is proven refused before connecting: the mock transport never sees its request. Redis is the `redis_double`
fakeredis.
"""
import hashlib
import socket

import httpx
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

import app as app_pkg
from app import db
from app.models import Domain
from app.onion_media import cache_name, cached_file, kind
from app.onion_media_fill import fill, is_dead
from app.pinned_http import PinnedNetworkBackend
from app.utils import set_setting
from tests.factories import make_banned_instance

PNG = b'\x89PNG\r\n\x1a\n' + b'x' * 100
REMOTE = 'https://media.example/pictrs/image/a.png'
_real_getaddrinfo = socket.getaddrinfo


class Remote:
    """The remote web: url -> httpx.Response, and every request the transport saw."""

    def __init__(self):
        self.routes = {}
        self.seen = []

    def handler(self, request):
        self.seen.append(request)
        template = self.routes.get(str(request.url), httpx.Response(404))
        if not template.is_stream_consumed:          # a streamed body, served once as it is
            return template
        # httpx reads a bytes body at once, and `fill` streams the raw body, so each answer gets a fresh stream
        return httpx.Response(template.status_code, headers=template.headers, content=iter([template.content]))

    def urls(self):
        return [str(request.url) for request in self.seen]


@pytest.fixture
def remote(app, db_session, redis_double, tmp_path, monkeypatch):
    def getaddrinfo(host, *args, **kwargs):
        if isinstance(host, str) and host.endswith('.example'):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 0))]
        return _real_getaddrinfo(host, *args, **kwargs)

    web = Remote()
    monkeypatch.setattr(socket, 'getaddrinfo', getaddrinfo)
    monkeypatch.setattr(app, 'debug', False)
    monkeypatch.setitem(app.config, 'MEDIA_PROXY_DIR', str(tmp_path))
    monkeypatch.setattr('app.httpx_client', httpx.Client(transport=httpx.MockTransport(web.handler)))
    return web


def image(body=PNG, content_type='image/png', **headers):
    return httpx.Response(200, headers={'Content-Type': content_type, **headers}, content=body)


def moved(location, status=301):
    return httpx.Response(status, headers={'Location': location})


def leftovers(tmp_path):
    return sorted(path.name for path in tmp_path.iterdir())


def test_kind():
    assert kind('image/png') == 'image'
    assert kind('IMAGE/JPEG; charset=binary') == 'image'
    assert kind('video/mp4') == 'av'
    assert kind('audio/flac') == 'av'
    assert kind('image/svg+xml') is None
    assert kind('text/html') is None
    assert kind('video/x-unknown') is None
    assert kind('') is None


def test_image_fills_and_names_by_type(remote, tmp_path):
    remote.routes[REMOTE] = image(content_type='image/png; charset=binary')
    path = fill(REMOTE, max_seconds=15)
    assert path == tmp_path / cache_name(REMOTE, 'image/png')
    assert path.read_bytes() == PNG
    assert cached_file(REMOTE) == path
    assert leftovers(tmp_path) == [path.name]
    assert not is_dead(REMOTE)
    sent = remote.seen[0]
    assert sent.headers['User-Agent'].startswith('PieFed/')
    assert sent.headers['Accept-Encoding'] == 'identity'


def test_fill_sends_through_the_pinned_client(app):
    """Production sends through app.httpx_client, built with pinned_transport; the tests replace exactly it."""
    pool = app_pkg.httpx_client._transport._pool
    assert isinstance(pool._network_backend, PinnedNetworkBackend)


def test_redirect_hops_are_followed_and_checked(remote, tmp_path):
    hops = [f'https://hop{n}.example/x' for n in range(6)]
    for here, there in zip([REMOTE] + hops, hops):
        remote.routes[here] = moved(there, status=302)
    remote.routes[hops[3]] = moved('/final.png', status=308)   # the 5th redirect, relative to the current URL
    remote.routes['https://hop3.example/final.png'] = image()
    assert fill(REMOTE, max_seconds=15) == tmp_path / cache_name(REMOTE, 'image/png')
    assert len(remote.seen) == 6                     # the first request and 5 redirects

    other = 'https://media.example/b.png'
    remote.routes[other] = moved(hops[0], status=301)
    remote.routes[hops[3]] = moved(hops[4])          # the 5th redirect
    remote.routes[hops[4]] = moved(hops[5])          # the 6th: refused
    remote.routes[hops[5]] = image()
    remote.seen.clear()
    assert fill(other, max_seconds=15) is None
    assert remote.urls() == [other] + hops[:5]       # hop 5 was never sent
    assert is_dead(other)
    assert leftovers(tmp_path) == [cache_name(REMOTE, 'image/png')]


@pytest.mark.parametrize('target', ['http://127.0.0.1/a.png', 'http://10.89.0.250:8080/a.png',
                                    'https://[::1]/a.png', 'file:///etc/passwd', 'https://printer.local/a.png'])
def test_redirect_to_private_address_refused(remote, tmp_path, target):
    remote.routes[REMOTE] = moved(target)
    remote.routes[target] = image()
    assert fill(REMOTE, max_seconds=15) is None
    assert remote.urls() == [REMOTE]                 # the refused hop was never sent
    assert leftovers(tmp_path) == []
    assert cached_file(REMOTE) is None
    assert is_dead(REMOTE)


def test_private_address_refused_without_a_request(remote, tmp_path):
    assert fill('http://127.0.0.1/a.png', max_seconds=15) is None
    assert remote.seen == []
    assert is_dead('http://127.0.0.1/a.png')


@pytest.mark.parametrize('content_type', ['text/html', 'image/svg+xml', 'application/octet-stream', None])
def test_html_and_svg_refused(remote, tmp_path, content_type):
    headers = {} if content_type is None else {'Content-Type': content_type}
    remote.routes[REMOTE] = httpx.Response(200, headers=headers, content=b'<svg onload="alert(1)"/>')
    assert fill(REMOTE, max_seconds=15) is None
    assert leftovers(tmp_path) == []
    assert is_dead(REMOTE)


def test_over_cap_refused_declared_and_streamed(remote, tmp_path):
    set_setting('onion_media_image_max_bytes', 50)
    remote.routes[REMOTE] = image(body=b'x' * 40, **{'Content-Length': '51'})   # declared over, body under
    assert fill(REMOTE, max_seconds=15) is None
    assert is_dead(REMOTE)

    def endless():
        while True:
            yield b'x' * 16

    streamed = 'https://media.example/streamed.png'
    remote.routes[streamed] = httpx.Response(200, headers={'Content-Type': 'image/png'}, content=endless())
    assert fill(streamed, max_seconds=15) is None
    assert is_dead(streamed)
    assert leftovers(tmp_path) == []

    exact = 'https://media.example/exact.png'
    remote.routes[exact] = image(body=b'x' * 50)
    assert fill(exact, max_seconds=15).read_bytes() == b'x' * 50


def test_av_cap_and_cache_cap(remote, tmp_path):
    set_setting('onion_media_av_max_bytes', 1000)
    video = 'https://media.example/v.mp4'
    remote.routes[video] = image(body=b'v' * 900, content_type='video/mp4')
    assert fill(video, max_seconds=15).name.endswith('.mp4')     # under the av cap

    set_setting('onion_media_cache_bytes', 800)                  # one file larger than the whole cache
    big = 'https://media.example/big.mp4'
    remote.routes[big] = image(body=b'v' * 900, content_type='video/mp4')
    assert fill(big, max_seconds=15) is None
    assert leftovers(tmp_path) == [cache_name(video, 'video/mp4')]


@pytest.mark.parametrize('encoding', ['gzip', 'br', 'identity, gzip'])
def test_compressed_body_refused(remote, tmp_path, encoding):
    remote.routes[REMOTE] = httpx.Response(200, headers={'Content-Type': 'image/png', 'Content-Encoding': encoding},
                                           content=iter([PNG]))     # a stream: httpx would decode bytes at once
    assert fill(REMOTE, max_seconds=15) is None
    assert leftovers(tmp_path) == []


def test_identity_encoding_accepted(remote, tmp_path):
    remote.routes[REMOTE] = image(**{'Content-Encoding': 'identity'})
    assert fill(REMOTE, max_seconds=15).read_bytes() == PNG


@pytest.mark.parametrize('response', [httpx.Response(500), httpx.Response(206, headers={'Content-Type': 'image/png'}),
                                      httpx.Response(302), image(**{'Content-Length': 'many'})])
def test_bad_status_location_or_length_refused(remote, tmp_path, response):
    remote.routes[REMOTE] = response
    assert fill(REMOTE, max_seconds=15) is None
    assert is_dead(REMOTE)
    assert leftovers(tmp_path) == []


def test_transport_error_and_deadline_fail(remote, tmp_path, monkeypatch):
    def refuse(request):
        raise httpx.ConnectError('down', request=request)

    monkeypatch.setattr('app.httpx_client', httpx.Client(transport=httpx.MockTransport(refuse)))
    assert fill(REMOTE, max_seconds=15) is None
    assert is_dead(REMOTE)

    clock = [0.0]
    monkeypatch.setattr('app.onion_media_fill.monotonic', lambda: clock[0])

    def slow_body():
        for _ in range(4):
            clock[0] += 6                            # each chunk takes 6 s: the third passes the 15 s deadline
            yield b'x'

    slow = 'https://media.example/slow.png'
    monkeypatch.setattr('app.httpx_client', httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, headers={'Content-Type': 'image/png'}, content=slow_body()))))
    assert fill(slow, max_seconds=15) is None
    assert is_dead(slow)
    assert leftovers(tmp_path) == []


def test_a_body_under_the_throughput_floor_is_refused_and_a_fast_one_passes(remote, tmp_path, monkeypatch):
    """After 20 s of body transfer a body averaging under 64 KiB/s is refused. The clock is the fill's own
    `monotonic`, moved 10 s by each chunk, so nothing sleeps."""
    clock = [0.0]
    monkeypatch.setattr('app.onion_media_fill.monotonic', lambda: clock[0])

    def body(chunk, count):
        for _ in range(count):
            clock[0] += 10
            yield chunk

    def serve(chunk, count):
        return lambda request: httpx.Response(200, headers={'Content-Type': 'video/mp4'}, content=body(chunk, count))

    slow = 'https://media.example/slow.mp4'
    monkeypatch.setattr('app.httpx_client', httpx.Client(transport=httpx.MockTransport(serve(b'x' * 65536 * 6, 4))))
    assert fill(slow, max_seconds=600) is None          # 6 x 64 KiB every 10 s: refused at 30 s, the first check past 20
    assert is_dead(slow)
    assert leftovers(tmp_path) == []

    fast = 'https://media.example/fast.mp4'
    monkeypatch.setattr('app.httpx_client', httpx.Client(transport=httpx.MockTransport(serve(b'x' * 65536 * 11, 4))))
    path = fill(fast, max_seconds=600)                  # 11 x 64 KiB every 10 s stays above the floor
    assert path is not None and path.stat().st_size == 65536 * 11 * 4
    assert not is_dead(fast)


def test_disk_error_leaves_no_file_and_is_not_remembered(remote, tmp_path, monkeypatch):
    remote.routes[REMOTE] = image()

    def broken(*args):
        raise OSError('disk full')

    monkeypatch.setattr('app.onion_media_fill.os.replace', broken)
    assert fill(REMOTE, max_seconds=15) is None
    assert leftovers(tmp_path) == []
    assert not is_dead(REMOTE)                       # our fault, not the URL's


def test_banned_instance_and_domain_refused(remote, tmp_path, redis_double):
    make_banned_instance('banned.example')
    db.session.add(Domain(name='blocked.example', banned=True))
    db.session.add(Domain(name='fine.example', banned=False))
    db.session.commit()
    for target in ['https://banned.example/a.png', 'https://www.blocked.example/a.png']:
        remote.routes[target] = image()
        assert fill(target, max_seconds=15) is None
        assert is_dead(target)
        remote.routes[REMOTE] = moved(target)        # and as a redirect hop
        remote.seen.clear()
        redis_key = 'onion_media:dead:' + hashlib.sha256(REMOTE.encode()).hexdigest()
        assert fill(REMOTE, max_seconds=15) is None
        assert remote.urls() == [REMOTE]
        assert is_dead(REMOTE)
        assert 3500 < redis_double.ttl(redis_key) <= 3600
        redis_double.delete(redis_key)
    assert leftovers(tmp_path) == []
    remote.routes['https://fine.example/a.png'] = image()
    assert fill('https://fine.example/a.png', max_seconds=15) is not None


def test_no_cookie_or_referer_sent(remote):
    app_pkg.httpx_client.cookies.set('session', 'secret', domain='media.example')
    remote.routes[REMOTE] = moved('https://media.example/b.png')
    remote.routes['https://media.example/b.png'] = httpx.Response(
        200, headers={'Content-Type': 'image/png', 'Set-Cookie': 'tracker=1; Path=/'}, content=PNG)
    assert fill(REMOTE, max_seconds=15) is not None
    assert len(remote.seen) == 2
    for request in remote.seen:
        assert 'Cookie' not in request.headers
        assert 'Referer' not in request.headers


def test_redis_failure_does_not_raise(remote, monkeypatch):
    class Down:
        def set(self, *args, **kwargs):
            raise RedisConnectionError('redis down')

        def exists(self, *args):
            raise RedisConnectionError('redis down')

    monkeypatch.setattr('app.redis_client', Down())
    assert fill('http://127.0.0.1/a.png', max_seconds=15) is None
    assert not is_dead(REMOTE)

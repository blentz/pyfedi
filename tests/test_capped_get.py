"""get_request reads at most a capped body in bounded time.

get_request used to buffer the whole body with only a per-read timeout, so a hostile or broken peer could answer
any GET with an endless or huge body (chunked, lying Content-Length, gzip bomb), or trickle it, and pin a worker
and its memory. Every attempt now streams through _capped_get: a byte cap counted on the DECODED body, an early
refusal of a declared oversize body, and a wall-clock deadline over the whole download. Over the cap or past the
deadline raises ResponseTooLarge, which is never retried.
"""
import gzip
import json
import time
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from app import db
from app.utils import FETCH_MAX_BYTES, FETCH_MAX_SECONDS, ResponseTooLarge, _capped_get, get_request, \
    get_request_instance
from tests.factories import make_instance

URI = 'https://peer.example/thing'
CHUNK = 65536


def _endless(pulled):
    while True:
        pulled.append(1)
        yield b' ' * CHUNK


@pytest.fixture
def sleeps(monkeypatch):
    """get_request's retry arms sleep 3-10 s first; a test that reaches one must not really sleep."""
    sleeps = Mock()
    monkeypatch.setattr('app.utils.sleep', sleeps)
    return sleeps


@pytest.fixture
def clock(monkeypatch):
    """A fake time.monotonic for app.utils: advance it with clock[0] += seconds."""
    now = [1000.0]
    monkeypatch.setattr('app.utils.time', SimpleNamespace(**{**vars(time), 'monotonic': lambda: now[0]}))
    return now


class TestTheDefaultCap:

    def test_the_default_cap_is_one_mebibyte_and_fifteen_seconds(self):
        assert (FETCH_MAX_BYTES, FETCH_MAX_SECONDS) == (1024 * 1024, 15)

    def test_too_large_is_an_http_error(self):
        """Every caller already catches httpx.HTTPError, so a refused body fails the way a refused connection
        does."""
        assert issubclass(ResponseTooLarge, httpx.HTTPError)

    def test_a_declared_oversize_body_is_refused_without_reading(self, app, http_mock, sleeps):
        route = http_mock.get(URI).respond(200, content=b'{}',
                                           headers={'Content-Length': str(FETCH_MAX_BYTES + 1)})

        with pytest.raises(ResponseTooLarge):
            get_request(URI)
        assert route.call_count == 1

    def test_an_undeclared_endless_body_is_cut_off_unbuffered(self, app, http_mock, sleeps):
        pulled = []
        http_mock.get(URI).respond(200, content=_endless(pulled))

        with pytest.raises(ResponseTooLarge):
            get_request(URI)
        assert len(pulled) * CHUNK <= FETCH_MAX_BYTES + CHUNK

    def test_a_body_exactly_at_the_cap_is_read(self, app, http_mock):
        http_mock.get(URI).respond(200, content=b'x' * FETCH_MAX_BYTES)

        assert len(get_request(URI).content) == FETCH_MAX_BYTES

    def test_a_gzip_body_that_expands_past_the_cap_is_refused(self, app, http_mock, sleeps):
        """The cap counts decoded bytes: a few kilobytes of gzip that expand to over a mebibyte are refused, though
        the declared (compressed) length is far under the cap."""
        compressed = gzip.compress(b' ' * (FETCH_MAX_BYTES + 1))
        assert len(compressed) < FETCH_MAX_BYTES // 100
        http_mock.get(URI).respond(200, content=compressed, headers={'Content-Encoding': 'gzip'})

        with pytest.raises(ResponseTooLarge):
            get_request(URI)

    def test_an_explicit_cap_replaces_the_default(self, app, http_mock):
        http_mock.get(URI).respond(200, content=b'x' * 11)

        with pytest.raises(ResponseTooLarge):
            get_request(URI, max_bytes=10)


class TestTheDeadline:

    def test_a_slow_body_is_cut_at_the_wall_clock(self, app, http_mock, clock, sleeps):
        pulled = []

        def trickle():
            for _ in range(1000):
                pulled.append(1)
                clock[0] += 5   # five fake seconds per byte
                yield b' '

        http_mock.get(URI).respond(200, content=trickle())

        with pytest.raises(ResponseTooLarge):
            get_request(URI)
        assert len(pulled) <= 4

    def test_a_response_that_ends_only_after_the_deadline_is_refused(self, app, http_mock, clock, sleeps):
        """An empty body reads no chunk, so this is the check after the loop: the response itself came late."""
        def late(request):
            clock[0] += FETCH_MAX_SECONDS + 1
            return httpx.Response(204)

        http_mock.get(URI).mock(side_effect=late)

        with pytest.raises(ResponseTooLarge):
            get_request(URI)

    def test_an_explicit_deadline_replaces_the_default(self, app, http_mock, clock):
        def late(request):
            clock[0] += 3
            return httpx.Response(200, json={})

        http_mock.get(URI).mock(side_effect=late)

        with pytest.raises(ResponseTooLarge):
            get_request(URI, max_seconds=2)


class TestNoRetry:

    def test_too_large_is_not_retried(self, app, http_mock, sleeps):
        """Retrying a hostile body doubles what it costs; the second answer would have been accepted."""
        route = http_mock.get(URI).mock(side_effect=[httpx.Response(200, content=_endless([])),
                                                     httpx.Response(200, json={})])

        with pytest.raises(ResponseTooLarge):
            get_request(URI)
        assert route.call_count == 1
        sleeps.assert_not_called()

    @pytest.mark.parametrize('error', [httpx.ReadError('boom'), httpx.ReadTimeout('slow')])
    def test_a_transport_error_is_still_retried_once(self, app, http_mock, sleeps, error):
        route = http_mock.get(URI).mock(side_effect=[error, httpx.Response(200, json={'ok': 1})])

        assert get_request(URI).json() == {'ok': 1}
        assert route.call_count == 2
        assert sleeps.call_count == 1

    @pytest.mark.parametrize('error', [httpx.InvalidURL('bad url'), ValueError('bad value'),
                                       httpx.StreamError('stream gone')])
    def test_a_request_httpx_refuses_is_normalised_and_not_retried(self, app, monkeypatch, sleeps, error):
        """None of these is an httpx.HTTPError; normalised, they must still not take the HTTPError retry arm,
        which they never reached before."""
        calls = []

        def boom(*args, **kwargs):
            calls.append(1)
            raise error
        monkeypatch.setattr('app.utils.httpx_client.stream', boom)

        with pytest.raises(httpx.HTTPError, match=str(error)):
            get_request(URI)
        assert calls == [1]
        sleeps.assert_not_called()


class TestTheReturnedResponse:

    def test_status_headers_and_json_are_the_peers(self, app, http_mock):
        http_mock.get(URI).respond(201, json={'id': URI}, headers={'X-Thing': 'y'})

        response = get_request(URI)

        assert response.status_code == 201
        assert response.json() == {'id': URI}
        assert response.headers['x-thing'] == 'y'
        assert response.headers['content-type'] == 'application/json'

    def test_a_gzip_encoded_body_under_the_cap_is_decoded_once(self, app, http_mock):
        """The bytes read are already decoded, so the Response is built without Content-Encoding (httpx would
        decode them again) and with the decoded length."""
        document = json.dumps({'id': URI, 'pad': 'x' * 1000}).encode()
        http_mock.get(URI).respond(200, content=gzip.compress(document),
                                   headers={'Content-Encoding': 'gzip', 'Content-Type': 'application/json'})

        response = get_request(URI)

        assert response.json() == {'id': URI, 'pad': 'x' * 1000}
        assert response.content == document
        assert 'content-encoding' not in response.headers
        assert response.headers['content-length'] == str(len(document))

    def test_a_non_200_keeps_its_status_and_body(self, app, http_mock):
        http_mock.get(URI).respond(404, text='gone')

        response = get_request(URI)

        assert (response.status_code, response.text) == (404, 'gone')

    def test_a_redirect_is_returned_not_followed(self, app, http_mock):
        """No route serves the Location, so following it would error the test."""
        http_mock.get(URI).respond(302, headers={'Location': 'https://elsewhere.example/x'})

        response = get_request(URI)

        assert (response.status_code, response.headers['location']) == (302, 'https://elsewhere.example/x')

    def test_the_request_is_attached(self, app, http_mock):
        """raise_for_status needs it."""
        http_mock.get(URI).respond(500)

        with pytest.raises(httpx.HTTPStatusError):
            get_request(URI).raise_for_status()


class TestTheHelper:

    def test_it_sends_the_given_headers_params_and_timeout(self, app, http_mock):
        route = http_mock.get(URI, params={'q': 'a b'}).respond(200, text='ok')

        response = _capped_get(URI, params={'q': 'a b'}, headers={'Accept': 'text/plain'}, timeout=3)

        assert response.text == 'ok'
        assert route.calls[0].request.headers['accept'] == 'text/plain'
        assert route.calls[0].request.extensions['timeout']['read'] == 3

    def test_it_follows_a_redirect_only_when_asked(self, app, http_mock):
        http_mock.get(URI).respond(302, headers={'Location': 'https://peer.example/there'})
        http_mock.get('https://peer.example/there').respond(200, text='here')

        assert _capped_get(URI).status_code == 302
        assert _capped_get(URI, follow_redirects=True).text == 'here'


class TestGetRequestInstance:

    def test_an_oversize_body_counts_as_a_failure_of_the_instance(self, app, db_session, http_mock, sleeps):
        """get_request_instance keeps its signature and inherits the default cap."""
        instance = make_instance('peer.example')
        http_mock.get(URI).respond(200, content=_endless([]))

        assert get_request_instance(URI, instance).status_code == 500
        db.session.refresh(instance)
        assert instance.failures == 1

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
import zlib
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from app import db, httpx_client
from app.utils import FETCH_MAX_BYTES, FETCH_MAX_SECONDS, ResponseTooLarge, UnreadableEncoding, _DECODE_STEP, \
    _capped_get, _capped_request, get_request, get_request_instance
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

    @pytest.mark.parametrize('declared', [b'\xb2', '\u00b2'.encode('utf-8')])
    def test_a_content_length_of_non_ascii_digits_is_not_a_declared_length(self, app, http_mock, declared):
        """httpx decodes a header value it cannot read as ASCII as UTF-8 or latin-1, so a peer's '\u00b2' arrives
        as a str for which isdigit() is true and int() fails: ValueError escaped _capped_get. It declares no
        length, and the body is read under the cap as any undeclared one is. Found by the capped_body fuzz
        target."""
        http_mock.get(URI).respond(200, content=iter([b'{}']), headers=[(b'Content-Length', declared)])

        assert _capped_get(URI).content == b'{}'

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


    def test_a_transport_timeout_at_the_deadline_is_too_slow_and_not_retried(self, app, http_mock, clock, sleeps):
        """The transport refuses a read once the fetch's deadline is reached (app/pinned_http.py): that timeout is the
        deadline refusing the response, so it is ResponseTooLarge, which get_request does not retry."""
        def stalls(request):
            clock[0] += FETCH_MAX_SECONDS
            raise httpx.ReadTimeout('the deadline for this fetch has passed')

        route = http_mock.get(URI).mock(side_effect=stalls)

        with pytest.raises(ResponseTooLarge, match='took longer than'):
            get_request(URI)
        assert route.call_count == 1
        sleeps.assert_not_called()


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
    def test_a_transport_error_is_retried_once_when_a_housekeeping_task_asks(self, app, http_mock, sleeps, error):
        route = http_mock.get(URI).mock(side_effect=[error, httpx.Response(200, json={'ok': 1})])

        assert get_request(URI, retry=True).json() == {'ok': 1}
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


class _RecordingZlib:
    """app.utils.zlib, with every decompressor recording the length of each piece it outputs."""

    def __init__(self):
        self.pieces = []

    def __getattr__(self, name):
        return getattr(zlib, name)

    def decompressobj(self, *args, **kwargs):
        real = zlib.decompressobj(*args, **kwargs)
        pieces = self.pieces

        class Recording:
            def decompress(self, data, max_length=0):
                piece = real.decompress(data, max_length)
                pieces.append(len(piece))
                return piece

            def flush(self, *args):
                piece = real.flush(*args)
                pieces.append(len(piece))
                return piece

            def __getattr__(self, name):
                return getattr(real, name)

        return Recording()


@pytest.fixture
def recording_zlib(monkeypatch):
    recorder = _RecordingZlib()
    monkeypatch.setattr('app.utils.zlib', recorder)
    return recorder


def _raw_deflate(data):
    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    return compressor.compress(data) + compressor.flush()


class TestCompressedBodies:
    """_capped_get decompresses gzip and deflate itself, in steps of at most _DECODE_STEP bytes and never past the
    remaining budget, so a bomb is refused having inflated at most the cap plus one step."""

    def test_only_gzip_and_deflate_are_offered_whatever_the_caller_asked(self, app, http_mock):
        route = http_mock.get(URI).respond(200, text='ok')

        _capped_get(URI, headers={'Accept-Encoding': 'br, zstd'})

        assert route.calls[0].request.headers.get_list('accept-encoding') == ['gzip, deflate']

    @pytest.mark.parametrize('encoding, compress', [('gzip', gzip.compress), ('x-gzip', gzip.compress),
                                                    ('deflate', zlib.compress), ('deflate', _raw_deflate)])
    def test_a_real_bomb_is_refused_within_the_cap_plus_one_step(self, app, http_mock, recording_zlib, sleeps,
                                                                 encoding, compress):
        """Twenty mebibytes of zeros compress to a few kilobytes."""
        bomb = compress(b'\0' * (20 * FETCH_MAX_BYTES))
        assert len(bomb) < 64 * 1024
        route = http_mock.get(URI).respond(200, content=bomb, headers={'Content-Encoding': encoding})

        with pytest.raises(ResponseTooLarge):
            get_request(URI)
        assert route.call_count == 1
        assert sum(recording_zlib.pieces) <= FETCH_MAX_BYTES + _DECODE_STEP
        assert max(recording_zlib.pieces) <= _DECODE_STEP

    @pytest.mark.parametrize('encoding, compress', [('gzip', gzip.compress), ('deflate', zlib.compress),
                                                    ('deflate', _raw_deflate), ('identity', lambda data: data),
                                                    (' GZIP ', gzip.compress)])
    def test_a_body_under_the_cap_is_decoded_in_steps(self, app, http_mock, recording_zlib, encoding, compress):
        """Over one step, so unconsumed_tail is fed back in; the Response carries the decoded body."""
        document = json.dumps({'pad': 'x' * (3 * _DECODE_STEP)}).encode()
        http_mock.get(URI).respond(200, content=compress(document), headers={'Content-Encoding': encoding})

        response = _capped_get(URI)

        assert response.content == document
        assert 'content-encoding' not in response.headers
        assert all(piece <= _DECODE_STEP for piece in recording_zlib.pieces)

    @pytest.mark.parametrize('encoding, compress', [('gzip', gzip.compress), ('deflate', _raw_deflate)])
    def test_a_body_arriving_in_several_chunks_keeps_one_decompressor(self, app, http_mock, encoding, compress):
        """The decompressor (and, for deflate, the zlib-or-raw choice) comes from the first chunk only."""
        document = json.dumps({'pad': 'y' * 5000, 'more': list(range(2000))}).encode()
        packed = compress(document)
        chunks = [packed[i:i + 100] for i in range(0, len(packed), 100)]
        assert len(chunks) > 2
        http_mock.get(URI).respond(200, content=iter(chunks), headers={'Content-Encoding': encoding})

        assert _capped_get(URI).content == document

    def test_endless_data_after_the_end_of_the_gzip_stream_is_refused_at_once(self, app, http_mock, sleeps):
        """zlib keeps whatever follows the end of the stream in unused_data and outputs nothing, so a decoded-byte
        count alone never fired: a valid gzip of `{}` then junk at line rate grew memory until the deadline."""
        pulled = []

        def junk_after_the_end():
            pulled.append(1)
            yield gzip.compress(b'{}') + b'junk' * 100
            while True:
                pulled.append(1)
                yield b'j' * CHUNK

        http_mock.get(URI).respond(200, content=junk_after_the_end(), headers={'Content-Encoding': 'gzip'})

        with pytest.raises(ResponseTooLarge, match='after the end'):
            get_request(URI)
        assert len(pulled) == 1

    def test_a_chunk_after_the_end_of_the_stream_is_refused(self, app, http_mock):
        http_mock.get(URI).respond(200, content=iter([gzip.compress(b'{}'), b'more']),
                                   headers={'Content-Encoding': 'gzip'})

        with pytest.raises(ResponseTooLarge, match='after the end'):
            _capped_get(URI)

    @pytest.mark.parametrize('header', [b'\x78\x9c', b''])   # zlib-wrapped, raw
    def test_an_endless_run_of_empty_deflate_blocks_is_refused_by_its_raw_size(self, app, http_mock, sleeps,
                                                                                header):
        """Empty non-final stored blocks inflate to nothing and never end the stream: only the raw bytes read count
        them, and they are refused once those pass the cap -- not at the deadline."""
        empty_blocks = b'\x00\x00\x00\xff\xff' * (CHUNK // 5)
        pulled = []

        def endless():
            yield header
            while True:
                pulled.append(1)
                yield empty_blocks

        http_mock.get(URI).respond(200, content=endless(), headers={'Content-Encoding': 'deflate'})

        with pytest.raises(ResponseTooLarge, match='over the cap'):
            get_request(URI)
        assert len(pulled) * len(empty_blocks) <= FETCH_MAX_BYTES + len(empty_blocks)

    def test_a_zlib_deflate_body_whose_first_chunk_is_one_byte_is_read(self, app, http_mock):
        """A chunked server may flush after one byte; one byte cannot say which deflate framing follows."""
        document = json.dumps({'id': URI}).encode()
        packed = zlib.compress(document)
        http_mock.get(URI).respond(200, content=iter([packed[:1], packed[1:]]), headers={'Content-Encoding': 'deflate'})

        assert _capped_get(URI).content == document

    def test_a_raw_deflate_body_that_begins_like_a_zlib_header_is_read(self, app, http_mock):
        """08 1d is a valid zlib header and also a raw non-final stored block of 29 bytes (its padding bits set).
        zlib framing fails at the stored block's lengths, before any output, so raw deflate is tried on the bytes
        read so far."""
        data = b'abcdefghijklmnopqrstuvwxyz012'
        body = bytes([0x08, 0x1d, 0x00, 0xe2, 0xff]) + data + bytes([0x01, 0x00, 0x00, 0xff, 0xff])
        assert int.from_bytes(body[:2], 'big') % 31 == 0
        http_mock.get(URI).respond(200, content=iter([body[:3], body[3:]]), headers={'Content-Encoding': 'deflate'})

        assert _capped_get(URI).content == data

    def test_a_deflate_body_that_is_neither_framing_is_a_decoding_error(self, app, http_mock):
        http_mock.get(URI).respond(200, content=iter([b'\xff\xff\xff\xff']), headers={'Content-Encoding': 'deflate'})

        with pytest.raises(httpx.DecodingError):
            _capped_get(URI)

    def test_a_body_decoded_to_exactly_the_cap_is_read(self, app, http_mock):
        http_mock.get(URI).respond(200, content=gzip.compress(b'x' * 1000), headers={'Content-Encoding': 'gzip'})

        assert len(_capped_get(URI, max_bytes=1000).content) == 1000

    def test_bytes_left_for_the_final_flush_still_count(self, app, http_mock, monkeypatch):
        """Output zlib still holds once all input is consumed comes out of flush(), and is counted against the cap
        too. Real zlib holds back only a few bytes, so a decompressor whose flush returns more stands in."""
        class HoldsBack:
            unconsumed_tail = b''
            unused_data = b''
            eof = False

            def decompress(self, data, max_length=0):
                return b''

            def flush(self, *args):
                return b'x' * 11

        monkeypatch.setattr('app.utils._Inflater', lambda encoding: HoldsBack())
        http_mock.get(URI).respond(200, content=iter([b'packed']), headers={'Content-Encoding': 'gzip'})

        with pytest.raises(ResponseTooLarge):
            _capped_get(URI, max_bytes=10)

    def test_an_empty_compressed_body_is_empty(self, app, http_mock):
        http_mock.get(URI).respond(200, content=b'', headers={'Content-Encoding': 'gzip'})

        assert _capped_get(URI).content == b''

    @pytest.mark.parametrize('encoding', ['br', 'zstd', 'compress', 'gzip, gzip', 'gzip,br'])
    def test_any_other_or_stacked_encoding_is_refused_and_not_retried(self, app, http_mock, sleeps, encoding):
        # A streamed body: respx would otherwise decode the bytes itself, as httpx would.
        route = http_mock.get(URI).respond(200, content=iter([b'whatever']), headers={'Content-Encoding': encoding})

        with pytest.raises(UnreadableEncoding):
            get_request(URI)
        assert issubclass(UnreadableEncoding, ResponseTooLarge)
        assert route.call_count == 1
        sleeps.assert_not_called()

    def test_another_encoding_is_unreadable_even_when_its_declared_length_is_over_the_cap(self, app, http_mock):
        """The encoding is judged before the declared length, so a body in any other encoding is refused as
        UnreadableEncoding whatever its Content-Length says. It was the plain ResponseTooLarge for a length over the
        cap. Found by the capped_body fuzz target."""
        http_mock.get(URI).respond(200, content=iter([b'whatever']),
                                   headers={'Content-Encoding': 'br', 'Content-Length': str(FETCH_MAX_BYTES + 1)})

        with pytest.raises(UnreadableEncoding):
            _capped_get(URI)

    def test_a_corrupt_body_is_a_decoding_error(self, app, http_mock):
        """What httpx's own decoder raised for it."""
        http_mock.get(URI).respond(200, content=iter([b'\x1f\x8b' + b'not gzip at all']),
                                   headers={'Content-Encoding': 'gzip'})

        with pytest.raises(httpx.DecodingError):
            _capped_get(URI)


class TestTheHelper:

    def test_it_sends_the_given_headers_params_and_timeout(self, app, http_mock):
        route = http_mock.get(URI, params={'q': 'a b'}).respond(200, text='ok')

        response = _capped_get(URI, params={'q': 'a b'}, headers={'Accept': 'text/plain'}, timeout=3)

        assert response.text == 'ok'
        assert route.calls[0].request.headers['accept'] == 'text/plain'
        assert route.calls[0].request.extensions['timeout']['read'] == 3

    def test_it_never_follows_a_redirect(self, app, http_mock):
        """_capped_get does no SSRF check, so a followed redirect could reach an address is_invalid_get_request_uri
        would refuse; there is no way to ask for one. No route serves the Location."""
        http_mock.get(URI).respond(302, headers={'Location': 'https://peer.example/there'})

        assert _capped_get(URI).status_code == 302
        with pytest.raises(TypeError):
            _capped_get(URI, follow_redirects=True)


class TestGetRequestInstance:

    def test_an_oversize_body_counts_as_a_failure_of_the_instance(self, app, db_session, http_mock, sleeps):
        """get_request_instance keeps its signature and inherits the default cap."""
        instance = make_instance('peer.example')
        http_mock.get(URI).respond(200, content=_endless([]))

        assert get_request_instance(URI, instance).status_code == 500
        db.session.refresh(instance)
        assert instance.failures == 1


@pytest.fixture
def opened_streams(monkeypatch):
    """Every streamed response _capped_request opens, to check it was closed once the request returned."""
    opened = []
    real = httpx_client.stream

    @contextmanager
    def recording(*args, **kwargs):
        with real(*args, **kwargs) as response:
            opened.append(response)
            yield response

    monkeypatch.setattr('app.utils.httpx_client.stream', recording)
    return opened


class TestCappedPost:
    """_capped_request with keep_status=True, for a POST: the request was already delivered, so a response body over
    the cap, past the deadline, unreadable or corrupt is dropped (empty content) and the peer's status kept, never
    raised -- raising would make a delivered activity look like a transport failure and re-send it."""

    INBOX = 'https://peer.example/inbox'

    def post(self, **kwargs):
        return _capped_request('POST', self.INBOX, content=b'{"type": "Create"}', keep_status=True, **kwargs)

    def test_the_request_goes_out_with_its_body_and_the_answer_comes_back(self, app, http_mock):
        route = http_mock.post(self.INBOX).respond(202, json={'ok': True}, headers={'X-Thing': 'y'})

        response = self.post(headers={'Content-Type': 'application/activity+json'}, timeout=5)

        assert (response.status_code, response.json(), response.headers['x-thing']) == (202, {'ok': True}, 'y')
        sent = route.calls[0].request
        assert sent.content == b'{"type": "Create"}'
        assert sent.headers['content-type'] == 'application/activity+json'
        assert sent.headers['accept-encoding'] == 'gzip, deflate'
        assert sent.extensions['timeout']['read'] == 5

    def test_form_data_and_json_are_sent_as_httpx_sends_them(self, app, http_mock):
        form = http_mock.post('https://peer.example/form').respond(200)
        document = http_mock.post('https://peer.example/json').respond(200)

        _capped_request('POST', 'https://peer.example/form', data={'emails': 'a,b'}, keep_status=True)
        _capped_request('POST', 'https://peer.example/json', json={'query': 'q'}, keep_status=True)

        assert form.calls[0].request.content == b'emails=a%2Cb'
        assert json.loads(document.calls[0].request.content) == {'query': 'q'}

    def test_an_endless_body_keeps_the_status_and_drops_the_body(self, app, http_mock, opened_streams):
        pulled = []
        http_mock.post(self.INBOX).respond(202, content=_endless(pulled), headers={'X-Thing': 'y'})

        response = self.post()

        assert (response.status_code, response.content, response.headers['x-thing']) == (202, b'', 'y')
        assert len(pulled) * CHUNK <= FETCH_MAX_BYTES + CHUNK
        assert [stream.is_closed for stream in opened_streams] == [True]

    def test_a_declared_oversize_body_is_dropped_without_reading(self, app, http_mock, opened_streams):
        http_mock.post(self.INBOX).respond(410, content=b'gone',
                                           headers={'Content-Length': str(FETCH_MAX_BYTES + 1)})

        response = self.post()

        assert (response.status_code, response.content) == (410, b'')
        assert [stream.is_closed for stream in opened_streams] == [True]
        assert 'content-length' not in response.headers or response.headers['content-length'] == '0'

    def test_a_gzip_bomb_keeps_the_status_and_drops_the_body(self, app, http_mock, recording_zlib):
        bomb = gzip.compress(b'\0' * (20 * FETCH_MAX_BYTES))
        http_mock.post(self.INBOX).respond(202, content=bomb, headers={'Content-Encoding': 'gzip'})

        response = self.post()

        assert (response.status_code, response.content) == (202, b'')
        assert sum(recording_zlib.pieces) <= FETCH_MAX_BYTES + _DECODE_STEP

    @pytest.mark.parametrize('encoding, body', [('br', b'whatever'), ('gzip', b'\x1f\x8b' + b'not gzip at all')])
    def test_an_unreadable_or_corrupt_body_keeps_the_status(self, app, http_mock, encoding, body):
        http_mock.post(self.INBOX).respond(202, content=iter([body]), headers={'Content-Encoding': encoding})

        response = self.post()

        assert (response.status_code, response.content) == (202, b'')

    def test_a_slow_body_is_cut_at_the_deadline_keeping_the_status(self, app, http_mock, clock):
        def trickle():
            for _ in range(1000):
                clock[0] += 5
                yield b' '

        http_mock.post(self.INBOX).respond(202, content=trickle())

        response = self.post()

        assert (response.status_code, response.content) == (202, b'')

    def test_a_transport_timeout_at_the_deadline_after_the_status_keeps_it(self, app, http_mock, clock):
        def stalls_mid_body():
            yield b'{"partial'
            clock[0] += FETCH_MAX_SECONDS
            raise httpx.ReadTimeout('the deadline for this fetch has passed')

        http_mock.post(self.INBOX).respond(202, content=stalls_mid_body())

        response = self.post()

        assert (response.status_code, response.content) == (202, b'')

    def test_a_read_timeout_mid_body_before_the_deadline_is_still_a_transport_error(self, app, http_mock):
        def times_out():
            yield b'{"partial'
            raise httpx.ReadTimeout('slow')

        http_mock.post(self.INBOX).respond(202, content=times_out())

        with pytest.raises(httpx.ReadTimeout):
            self.post()

    def test_no_status_line_by_the_deadline_is_a_transport_error(self, app, http_mock, clock):
        """Nothing was answered, so nothing is known to be delivered: the error stands, unconverted."""
        def stalls(request):
            clock[0] += FETCH_MAX_SECONDS
            raise httpx.ReadTimeout('the deadline for this fetch has passed')

        http_mock.post(self.INBOX).mock(side_effect=stalls)

        with pytest.raises(httpx.ReadTimeout):
            self.post()

    def test_a_get_through_the_general_helper_still_raises(self, app, http_mock):
        http_mock.get(URI).respond(200, content=_endless([]))

        with pytest.raises(ResponseTooLarge):
            _capped_request('GET', URI)

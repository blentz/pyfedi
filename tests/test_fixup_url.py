"""fixup_url (app/utils.py:3110-3167) returns (thumbnail_url, embed_url) for a
submitted link. Two independent halves: a peertube branch that fetches the
canonical video id over HTTP, and a YouTube URL matrix.

YouTube's URL formats have no specification. The expectations below are derived
from the formats the production code already handles -- observed behaviour, not
an authoritative source. WHATWG URL Standard and RFC 3986 govern the parsing
underneath; YouTube's path conventions do not.
"""
import httpx
import pytest
import respx

from app.utils import fixup_url, get_request
from tests.factories import make_instance


class TestNonYoutubePassesThrough:
    """Mutation that fails this: removing the youtube_domains membership test
    so every host takes the YouTube path."""

    def test_an_unrelated_host_returns_the_url_unchanged_in_both_slots(self, app):
        with app.test_request_context('/'):
            thumbnail, embed = fixup_url('https://example.com/article')
        assert thumbnail == 'https://example.com/article'
        assert embed == 'https://example.com/article'


class TestYoutubeVideoForms:
    """Each parametrised case is one path shape the production code handles.
    Mutation that fails each: deleting that shape's branch at 3145-3150."""

    @pytest.mark.parametrize('url,video_id', [
        ('https://www.youtube.com/watch?v=abc123', 'abc123'),
        ('https://youtube.com/shorts/abc123', 'abc123'),
        ('https://youtu.be/abc123', 'abc123'),
        ('https://m.youtube.com/watch?v=abc123', 'abc123'),
        ('https://music.youtube.com/watch?v=abc123', 'abc123'),
    ])
    def test_a_video_url_yields_canonical_thumbnail_and_embed(self, app, url, video_id):
        with app.test_request_context('/'):
            thumbnail, embed = fixup_url(url)
        assert thumbnail == f'https://youtu.be/{video_id}'
        assert embed == f'https://www.youtube.com/watch?v={video_id}'


class TestTimestamps:
    """`start` wins over `t` -- they are checked in that order at 3153-3156.
    Mutation that fails the third: swapping the elif order."""

    def test_a_start_parameter_is_appended(self, app):
        with app.test_request_context('/'):
            _, embed = fixup_url('https://www.youtube.com/watch?v=abc123&start=42')
        assert embed == 'https://www.youtube.com/watch?v=abc123&start=42'

    def test_a_t_parameter_is_appended_as_start(self, app):
        with app.test_request_context('/'):
            _, embed = fixup_url('https://www.youtube.com/watch?v=abc123&t=42')
        assert embed == 'https://www.youtube.com/watch?v=abc123&start=42'

    def test_start_takes_precedence_over_t(self, app):
        with app.test_request_context('/'):
            _, embed = fixup_url('https://www.youtube.com/watch?v=abc123&start=1&t=2')
        assert embed == 'https://www.youtube.com/watch?v=abc123&start=1'


class TestPassThroughYoutubeForms:
    """Playlists and posts are let through unmolested with an EMPTY thumbnail --
    a distinct return shape. Mutation that fails these: deleting the early
    return at 3139-3142."""

    def test_a_playlist_returns_an_empty_thumbnail(self, app):
        url = 'https://www.youtube.com/playlist?list=PL123'
        with app.test_request_context('/'):
            thumbnail, embed = fixup_url(url)
        assert thumbnail == ''
        assert embed == url

    def test_a_post_returns_an_empty_thumbnail(self, app):
        url = 'https://www.youtube.com/post/abc123'
        with app.test_request_context('/'):
            thumbnail, embed = fixup_url(url)
        assert thumbnail == ''
        assert embed == url


class TestNoVideoId:
    """A youtube-domain URL with no path segment never sets video_id, so the
    early return at 3153-3154 fires with both slots unchanged. This is a real
    coverage gap left by the brief's YouTube matrix cases -- none of them ever
    leaves `path` falsy or empty after the leading slash is stripped.

    Mutation that fails this: deleting the `if not video_id: return
    thumbnail_url, embed_url` guard -- video_id stays None and the
    concatenation `'https://youtu.be/' + video_id` below raises TypeError
    instead of returning.
    """

    def test_a_bare_youtube_host_returns_the_url_unchanged(self, app):
        url = 'https://youtube.com'
        with app.test_request_context('/'):
            thumbnail, embed = fixup_url(url)
        assert (thumbnail, embed) == (url, url)


class TestPeertube:
    """The peertube branch fires only when the path is /w/-shaped AND the host
    is a known peertube instance. Both halves must be seeded: a DB row via
    make_instance(software='peertube'), and an http_mock route.

    Mutation that fails the first test: deleting the `if parsed_url.netloc in
    peertube_domains` check, or the embed_url assignment at 3122.

    There is deliberately no "unknown host makes no request" test built on the
    shared `http_mock` fixture. `assert_all_called=True` there requires every
    registered route to be called, so such a test cannot register a response
    that would prove the branch was skipped without also failing on correct
    code.

    A route-free version used to prove nothing either, and that is worth
    recording because it is what motivated narrowing the excepts: while the
    outer handler was a bare `except:`, it swallowed block_outbound_http's
    respx AllMockedAssertionError just as readily as a real transport failure,
    so "no request was made" was unobservable from outcome alone. The outer
    handler now catches httpx.HTTPError only, and AllMockedAssertionError is an
    AssertionError, so a request that escapes the netloc gate with no matching
    route now propagates instead of vanishing.

    `test_an_unknown_host_never_reaches_a_route_that_would_change_the_result`
    below remains the primary proof of the gate, because it discriminates
    positively (a response that WOULD change the result is left unfetched)
    rather than merely on an exception escaping.
    """

    def test_a_known_peertube_host_uses_the_canonical_id(self, app, db_session, http_mock):
        make_instance('peertube.example', software='peertube')
        url = 'https://peertube.example/w/aaaaaaaaaaaaaaaaaaaaaa'
        http_mock.get(url).respond(json={'id': 'https://peertube.example/videos/watch/real-id'})
        with app.test_request_context('/'):
            thumbnail, embed = fixup_url(url)
        assert embed == 'https://peertube.example/videos/watch/real-id'
        assert thumbnail == url

    def test_an_unknown_host_never_reaches_a_route_that_would_change_the_result(self, app, db_session):
        """Uses its own respx router (assert_all_called=False, so an unused
        route is not itself a failure) carrying a response that WOULD change
        the result if fetched, and asserts it was not -- proving the netloc
        gate rather than merely the absence of an exception.

        Mutation that fails this: replacing the netloc membership check with
        `if True:`.
        """
        url = 'https://unknown.example/w/aaaaaaaaaaaaaaaaaaaaaa'
        with respx.mock(assert_all_called=False) as local_router:
            local_router.get(url).respond(
                json={'id': 'https://unknown.example/videos/watch/should-not-be-used'})
            with app.test_request_context('/'):
                thumbnail, embed = fixup_url(url)
        assert (thumbnail, embed) == (url, url)

    def test_a_non_200_response_leaves_the_embed_url_alone(self, app, db_session, http_mock):
        """Mutation that fails this: removing the `status_code == 200` check."""
        make_instance('peertube.example', software='peertube')
        url = 'https://peertube.example/w/aaaaaaaaaaaaaaaaaaaaaa'
        http_mock.get(url).respond(status_code=404)
        with app.test_request_context('/'):
            thumbnail, embed = fixup_url(url)
        assert embed == url

    def test_a_response_without_an_id_leaves_the_embed_url_alone(self, app, db_session, http_mock):
        make_instance('peertube.example', software='peertube')
        url = 'https://peertube.example/w/aaaaaaaaaaaaaaaaaaaaaa'
        http_mock.get(url).respond(json={'not_id': 'x'})
        with app.test_request_context('/'):
            thumbnail, embed = fixup_url(url)
        assert embed == url


class TestPeertubeErrorSwallowing:
    """The peertube branch's two handlers used to be BARE `except:`. They are
    now narrowed: the inner one to `(ValueError, TypeError)`, the outer one to
    `httpx.HTTPError`.

    Every test below drives one of the exception types actually reachable
    there, so the set is what proves the narrowing did not drop anything:

    - `ValueError` -- httpx's `.json()` is `json.loads(self.content)` over
      BYTES, and CPython's json decodes bytes itself. Some malformed bodies
      surface as `json.JSONDecodeError` (b'not json'), others as
      `UnicodeDecodeError` (b'\\x80\\x81\\x82'); neither descends from the
      other, and `ValueError` is their nearest common base. Catching
      `json.JSONDecodeError` alone would have let the second class escape.
    - `TypeError` -- a body that is valid JSON but not a mapping. `'id' in
      None` and `'id' in 5` raise on the membership test; a bare string body
      containing the substring "id" passes the membership test and raises on
      the subscript instead.
    - `httpx.HTTPError` -- every transport failure get_request can raise. It
      normalises the lot: the is_invalid_get_request_uri refusal,
      httpx.InvalidURL, ValueError, httpx.StreamError, and both retry paths.

    What is deliberately NOT caught any more: KeyboardInterrupt, SystemExit,
    and any AssertionError from the test harness -- see TestPeertube's
    docstring for why the last of those mattered.
    """

    def test_a_malformed_json_body_is_swallowed(self, app, db_session, http_mock):
        """json.JSONDecodeError. Fails if the inner handler stops catching
        ValueError."""
        make_instance('peertube.example', software='peertube')
        url = 'https://peertube.example/w/aaaaaaaaaaaaaaaaaaaaaa'
        http_mock.get(url).respond(status_code=200, content=b'not json')
        with app.test_request_context('/'):
            thumbnail, embed = fixup_url(url)
        assert (thumbnail, embed) == (url, url)

    def test_a_body_that_is_not_valid_utf8_is_swallowed(self, app, db_session, http_mock):
        """UnicodeDecodeError, NOT json.JSONDecodeError -- the case that
        rules out narrowing the inner handler to JSONDecodeError alone.
        Fails if the inner handler is narrowed that far."""
        make_instance('peertube.example', software='peertube')
        url = 'https://peertube.example/w/aaaaaaaaaaaaaaaaaaaaaa'
        http_mock.get(url).respond(status_code=200, content=b'\x80\x81\x82')
        with app.test_request_context('/'):
            thumbnail, embed = fixup_url(url)
        assert (thumbnail, embed) == (url, url)

    @pytest.mark.parametrize('body,why', [
        (b'null', "'id' in None raises on the membership test"),
        (b'5', "'id' in 5 raises on the membership test"),
        (b'"a valid id string"', "the membership test passes, the subscript raises"),
    ])
    def test_a_json_body_that_is_not_a_mapping_is_swallowed(self, app, db_session, http_mock,
                                                            body, why):
        """TypeError. Fails if the inner handler stops catching TypeError."""
        make_instance('peertube.example', software='peertube')
        url = 'https://peertube.example/w/aaaaaaaaaaaaaaaaaaaaaa'
        http_mock.get(url).respond(status_code=200, content=body)
        with app.test_request_context('/'):
            thumbnail, embed = fixup_url(url)
        assert (thumbnail, embed) == (url, url), why

    def test_a_transport_failure_is_swallowed(self, app, db_session, http_mock):
        """httpx.ConnectError reaches get_request's `except httpx.HTTPError`
        retry arm; the retry fails too, and it re-raises httpx.HTTPError."""
        make_instance('peertube.example', software='peertube')
        url = 'https://peertube.example/w/aaaaaaaaaaaaaaaaaaaaaa'
        http_mock.get(url).mock(side_effect=httpx.ConnectError('boom'))
        with app.test_request_context('/'):
            thumbnail, embed = fixup_url(url)
        assert (thumbnail, embed) == (url, url)

    def test_a_read_failure_on_both_attempts_is_swallowed(self, app, db_session, http_mock):
        """The one get_request path that did NOT normalise to httpx.HTTPError.

        httpx.ReadError takes get_request's dedicated `except httpx.ReadError`
        arm, whose failed retry re-raised `httpx_client.ReadError(...)` --
        httpx_client is an httpx.Client INSTANCE with no such attribute, so
        that spelling raised AttributeError instead. The old bare `except:`
        here hid it; a handler narrowed to httpx.HTTPError does not, so this
        test fails with AttributeError unless get_request raises httpx.ReadError
        as intended. It is the load-bearing proof that narrowing the outer
        handler introduced no crash.
        """
        make_instance('peertube.example', software='peertube')
        url = 'https://peertube.example/w/aaaaaaaaaaaaaaaaaaaaaa'
        http_mock.get(url).mock(side_effect=httpx.ReadError('boom'))
        with app.test_request_context('/'):
            thumbnail, embed = fixup_url(url)
        assert (thumbnail, embed) == (url, url)


class TestGetRequestNormalisesTransportFailure:
    """fixup_url's outer handler catches httpx.HTTPError only, which is safe
    exactly as far as get_request's normalisation invariant holds. Asserted
    directly here as well as through fixup_url, because roughly every other
    caller of get_request in app/ relies on the same invariant.
    """

    def test_a_read_failure_on_both_attempts_raises_an_http_error(self, app, http_mock):
        """Fails with AttributeError, not httpx.HTTPError, if the retry arm
        goes back to raising `httpx_client.ReadError`."""
        url = 'https://peertube.example/w/aaaaaaaaaaaaaaaaaaaaaa'
        http_mock.get(url).mock(side_effect=httpx.ReadError('boom'))
        with app.test_request_context('/'):
            with pytest.raises(httpx.HTTPError):
                get_request(url)

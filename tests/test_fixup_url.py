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

from app.utils import fixup_url
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
    """

    def test_a_known_peertube_host_uses_the_canonical_id(self, app, db_session, http_mock):
        make_instance('peertube.example', software='peertube')
        url = 'https://peertube.example/w/aaaaaaaaaaaaaaaaaaaaaa'
        http_mock.get(url).respond(json={'id': 'https://peertube.example/videos/watch/real-id'})
        with app.test_request_context('/'):
            thumbnail, embed = fixup_url(url)
        assert embed == 'https://peertube.example/videos/watch/real-id'
        assert thumbnail == url

    def test_an_unknown_host_makes_no_request(self, app, db_session, http_mock):
        """No instance row seeded, so the netloc test fails and no HTTP call is
        made. If the production code called out anyway, block_outbound_http
        would raise rather than reach the network."""
        url = 'https://unknown.example/w/aaaaaaaaaaaaaaaaaaaaaa'
        with app.test_request_context('/'):
            thumbnail, embed = fixup_url(url)
        assert (thumbnail, embed) == (url, url)

    def test_an_unknown_host_never_reaches_a_route_that_would_change_the_result(self, app, db_session):
        """The test above alone does not discriminate `if parsed_url.netloc in
        peertube_domains:` replaced by `if True:` -- block_outbound_http's
        AllMockedAssertionError, raised when the widened mutant calls out with
        no route registered, is itself swallowed by fixup_url's bare `except:`
        at 3128, so the mutant still returns (url, url) and that test still
        passes. This test uses its own respx router (assert_all_called=False,
        so an unused route is not itself a failure) carrying a response that
        WOULD change the result if fetched, and asserts it was not.

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
    """app/utils.py:3126 catches a malformed JSON body; :3128 catches a
    transport failure. Both are BARE `except:`, which also catches
    KeyboardInterrupt and SystemExit -- reported as a defect, not fixed here.
    """

    def test_a_malformed_json_body_is_swallowed(self, app, db_session, http_mock):
        make_instance('peertube.example', software='peertube')
        url = 'https://peertube.example/w/aaaaaaaaaaaaaaaaaaaaaa'
        http_mock.get(url).respond(status_code=200, content=b'not json')
        with app.test_request_context('/'):
            thumbnail, embed = fixup_url(url)
        assert (thumbnail, embed) == (url, url)

    def test_a_transport_failure_is_swallowed(self, app, db_session, http_mock):
        make_instance('peertube.example', software='peertube')
        url = 'https://peertube.example/w/aaaaaaaaaaaaaaaaaaaaaa'
        http_mock.get(url).mock(side_effect=httpx.ConnectError('boom'))
        with app.test_request_context('/'):
            thumbnail, embed = fixup_url(url)
        assert (thumbnail, embed) == (url, url)

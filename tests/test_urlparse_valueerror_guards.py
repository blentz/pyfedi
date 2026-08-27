"""`urlparse` raises `ValueError` on some netlocs, and the link helpers that
parse user-submitted post URLs did not catch it.

Three shapes reach `urlparse` from a submitted link and make it raise:

    urlparse('https://[::1/x')        -> ValueError: Invalid IPv6 URL
    urlparse('http://[abc')           -> ValueError: Invalid IPv6 URL
    urlparse('http://[1::2::3]')      -> ValueError: '1::2::3' does not appear
                                         to be an IPv4 or IPv6 address
    urlparse('http://exa℀mple.com/') -> ValueError: netloc '...' contains
                                         invalid characters under NFKC
                                         normalization

All four raise out of `urlparse()` itself, so touching `.hostname` is not
required to provoke them -- the constructor is enough. The out-of-range-port
shape is different and is covered by `TestAnOutOfRangePortIsNotAffected`
below.

Each guarded function returns the value it already returns for a URL it cannot
attribute a host to; it does not invent a new one. The point of the
ordinary-URL classes in this file is that the guard did not become a
catch-everything that turns every URL into that safe value -- a guard written
as `try: ... except ValueError: return safe` around too much code would pass
the malformed-input tests and still be a regression.

The idiom copied here is `is_safe_redirect_target` (app/utils.py:2043-2048),
which already caught `ValueError` around its parse before this commit.
"""
import httpx
import pytest

from app.post.util import url_needs_archive
from app.utils import (domain_from_url, fixup_url, inbox_domain, is_image_url,
                       is_video_url, mimetype_from_url,
                       remove_tracking_from_link)

# Every input that makes urlparse itself raise. Each guarded function is run
# over the whole list so no shape is guarded by accident in one place and
# missed in another.
MALFORMED = [
    'https://[::1/x',            # unterminated IPv6 bracket
    'http://[abc',               # bracket, not even an address
    'http://[1::2::3]',          # two '::' runs
    'http://exa℀mple.com/',  # fails urllib's NFKC confusability check
]

# urlparse accepts this; only reading `.port` raises `Port out of range
# 0-65535`. None of the functions in this file reads `.port`, so none of them
# raises on it -- see TestAnOutOfRangePortIsNotAffected.
OUT_OF_RANGE_PORT = 'http://example.com:99999/'


class TestDomainFromUrlSurvivesAMalformedNetloc:
    """`domain_from_url` already returns None for a URL with no hostname
    (app/utils.py, the `else:` arm). Unparseable gets the same answer.

    Before the fix each of these raised ValueError out of
    app/shared/post.py's post-creation path.
    """

    @pytest.mark.parametrize('url', MALFORMED)
    def test_a_malformed_netloc_yields_no_domain(self, url, app, db_session):
        assert domain_from_url(url, create=True) is None

    def test_no_domain_row_is_created_for_a_malformed_netloc(self, app, db_session):
        """The safe value is genuinely "no domain", not a row named after the
        broken host."""
        from app.models import Domain
        before = db_session.query(Domain).count()
        for url in MALFORMED:
            assert domain_from_url(url, create=True) is None
        assert db_session.query(Domain).count() == before


class TestDomainFromUrlStillAttributesOrdinaryUrls:
    """The over-correction guard. A guard placed around the whole function
    body -- or one that returned None for anything it found surprising --
    passes the class above and silently stops attributing every post to its
    domain, which is what Domain.banned and DomainBlock act on."""

    def test_an_ordinary_url_still_gets_its_domain(self, app, db_session):
        assert domain_from_url('https://example.com/post/1').name == 'example.com'

    def test_a_leading_www_is_still_stripped(self, app, db_session):
        assert domain_from_url('https://www.example.com/a').name == 'example.com'

    def test_youtu_be_is_still_aliased(self, app, db_session):
        assert domain_from_url('https://youtu.be/abc123').name == 'youtube.com'

    def test_a_hostless_url_still_returns_none(self, app, db_session):
        assert domain_from_url('not a url at all') is None

    def test_an_ipv6_literal_that_is_well_formed_still_parses(self, app, db_session):
        """The guard must not reject every bracketed host -- only the ones
        urlparse refuses."""
        assert domain_from_url('https://[::1]/x').name == '::1'


class TestDomainFromUrlAndANulByteInTheHost:
    """A NUL in the host is a DIFFERENT defect from the one this commit fixes
    and is deliberately NOT fixed here.

    urlparse accepts 'http://ex\\x00ample.com/p' -- it is only the Postgres
    driver, further down, that refuses the resulting host as a query
    parameter. Catching that in the parse guard would mean wrapping the DB
    query too, which would swallow real database errors. The guard is
    therefore scoped to the `urlparse()` call alone, and this test records
    that the NUL still reaches the driver so the finding stays visible rather
    than being quietly absorbed.

    Note the exception the driver raises is itself a ValueError: an
    over-broad guard around the whole function body would turn this into a
    silent None and hide it.
    """

    def test_a_nul_byte_in_the_host_still_reaches_the_database_driver(self, app, db_session):
        with pytest.raises(ValueError, match='NUL'):
            domain_from_url('http://ex\x00ample.com/p', create=True)


class TestRemoveTrackingFromLinkSurvivesAMalformedNetloc:
    """Its existing answer for a link it does not rewrite is the url
    unchanged (the `else:` arm), so that is the safe value."""

    @pytest.mark.parametrize('url', MALFORMED)
    def test_a_malformed_netloc_comes_back_unchanged(self, url, app):
        assert remove_tracking_from_link(url) == url


class TestRemoveTrackingFromLinkStillRewritesYoutuBe:
    """The over-correction guard: a guard that swallowed everything would
    return every url unchanged, which is exactly what this function does in
    its non-rewriting case -- so these are the only tests that can tell the
    two apart."""

    def test_a_youtu_be_link_is_still_canonicalised(self, app):
        assert remove_tracking_from_link('https://youtu.be/abc123') == \
            'https://youtube.com/watch?v=abc123'

    def test_a_youtu_be_timestamp_is_still_preserved(self, app):
        assert remove_tracking_from_link('https://youtu.be/abc123?t=42') == \
            'https://youtube.com/watch?v=abc123&start=42'

    def test_an_unrelated_link_is_still_returned_unchanged(self, app):
        url = 'https://example.com/article?utm_source=x'
        assert remove_tracking_from_link(url) == url


class TestFixupUrlSurvivesAMalformedNetloc:
    """Its existing passthrough is (url, url) -- the value it returns for any
    host outside youtube_domains."""

    @pytest.mark.parametrize('url', MALFORMED)
    def test_a_malformed_netloc_passes_through_in_both_slots(self, url, app):
        with app.test_request_context('/'):
            assert fixup_url(url) == (url, url)


class TestFixupUrlStillRewritesYoutubeLinks:
    """The over-correction guard. `(url, url)` is also the ordinary answer for
    a non-YouTube link, so only the YouTube matrix distinguishes a working
    guard from one that swallows the whole function."""

    @pytest.mark.parametrize('url,video_id', [
        ('https://www.youtube.com/watch?v=abc123', 'abc123'),
        ('https://youtube.com/shorts/abc123', 'abc123'),
        ('https://youtu.be/abc123', 'abc123'),
    ])
    def test_a_youtube_url_still_yields_thumbnail_and_embed(self, url, video_id, app):
        with app.test_request_context('/'):
            thumbnail, embed = fixup_url(url)
        assert thumbnail == f'https://youtu.be/{video_id}'
        assert embed == f'https://www.youtube.com/watch?v={video_id}'

    def test_a_non_youtube_url_still_passes_through(self, app):
        with app.test_request_context('/'):
            assert fixup_url('https://example.com/article') == \
                ('https://example.com/article', 'https://example.com/article')


class TestUrlNeedsArchiveSurvivesAMalformedNetloc:
    """These pass BEFORE this commit as well, because app/post/util.py's
    `except:` is bare and catches everything.

    That bare `except:` is a separately reported defect scheduled for its own
    fix. This commit adds an explicit `except ValueError:` ahead of it so the
    function keeps this behaviour when the bare clause is narrowed, and these
    tests are what will hold it then. They are a regression guard, not a
    reproduction -- see the report for the experiment that confirms the
    explicit clause is the thing carrying them once the bare clause no longer
    catches ValueError.
    """

    @pytest.mark.parametrize('url', MALFORMED)
    def test_a_malformed_netloc_does_not_need_an_archive_link(self, url):
        assert url_needs_archive(url) is False


class TestUrlNeedsArchiveStillDetectsPaywalledSites:
    """The over-correction guard."""

    def test_a_paywalled_host_still_needs_an_archive_link(self):
        assert url_needs_archive('https://www.nytimes.com/2024/01/01/x.html') is True

    def test_an_unrelated_host_still_does_not(self):
        assert url_needs_archive('https://example.com/x') is False

    def test_the_nytimes_unlocked_article_exemption_still_applies(self):
        assert url_needs_archive(
            'https://www.nytimes.com/x?unlocked_article_code=1') is False


class TestIsVideoUrlSurvivesAMalformedNetloc:
    """Reached from app/shared/post.py with `post.url` straight off the
    submission form. Its safe value is False -- the answer it already gives
    for any URL whose path has no video extension."""

    @pytest.mark.parametrize('url', MALFORMED)
    def test_a_malformed_netloc_is_not_a_video(self, url):
        assert is_video_url(url) is False


class TestIsVideoUrlStillRecognisesVideos:
    """The over-correction guard: False is also the ordinary answer for a
    non-video link, so only these can tell a working guard from a broken one.
    A guard that swallowed everything would silently stop every .mp4 post from
    being typed POST_TYPE_VIDEO."""

    @pytest.mark.parametrize('url', [
        'https://example.com/clip.mp4',
        'https://example.com/clip.webm',
        'https://example.com/CLIP.MP4',
    ])
    def test_a_video_extension_is_still_recognised(self, url):
        assert is_video_url(url) is True

    def test_a_non_video_is_still_false(self):
        assert is_video_url('https://example.com/article') is False


class TestMimetypeFromUrlSurvivesAMalformedNetloc:
    """Reached from seven route modules with `post.url` while rendering a post
    list. Its safe value is None -- what mimetypes.guess_type already returns
    for a path it does not recognise."""

    @pytest.mark.parametrize('url', MALFORMED)
    def test_a_malformed_netloc_has_no_mimetype(self, url):
        assert mimetype_from_url(url) is None


class TestMimetypeFromUrlStillGuessesTypes:
    """The over-correction guard."""

    def test_a_png_is_still_guessed(self):
        assert mimetype_from_url('https://example.com/a.png') == 'image/png'

    def test_a_query_string_is_still_stripped_first(self):
        assert mimetype_from_url('https://example.com/a.png?v=2') == 'image/png'

    def test_an_unknown_extension_is_still_none(self):
        assert mimetype_from_url('https://example.com/a.unknownext') is None


class TestInboxDomainSurvivesAMalformedNetloc:
    """Reached with a remote instance's own `inbox` URL, which
    app/activitypub/util.py:1874 copies verbatim out of the JSON that instance
    served. app/activitypub/routes.py:1963 then passes it to
    `instance_banned(instance.inbox)`, so a peer can choose this string.

    The safe value is '' -- the value inbox_domain already returns for input
    carrying no domain, and the only one that leaves all five callers on a
    defined path (`instance_allowed` and `instance_banned` both call `.strip()`
    or a regex on the result, which None would break).
    """

    @pytest.mark.parametrize('url', MALFORMED)
    def test_a_malformed_inbox_url_yields_no_domain(self, url):
        assert inbox_domain(url) == ''


class TestInboxDomainStillReducesOrdinaryValues:
    """The over-correction guard."""

    def test_an_inbox_url_still_reduces_to_its_host(self):
        assert inbox_domain('https://example.com/u/alice/inbox') == 'example.com'

    def test_a_bare_domain_still_passes_through_lower_cased(self):
        assert inbox_domain('Example.COM') == 'example.com'

    def test_a_port_is_still_dropped(self):
        assert inbox_domain('https://example.com:8443/inbox') == 'example.com'

    def test_empty_input_is_still_empty(self):
        assert inbox_domain('') == ''


class TestIsImageUrlSurvivesAMalformedNetloc:
    """`is_image_url` tries a HEAD request first and only falls through to
    `urlparse` when that yields no Content-Type, so the ValueError is reached
    only for a URL httpx accepts and urlparse refuses. 'http://[abc' is such a
    URL, verified by probe: httpx.URL('http://[abc') constructs fine.

    The HEAD is routed to a ConnectError here rather than left to reach the
    network -- tests/conftest.py's `block_outbound_http` docstring prescribes
    exactly this for observing a transport failure. The assertion is still on
    what is_image_url returns.

    Its safe value is False, the answer it already gives for a path with no
    image extension.
    """

    def test_a_malformed_netloc_is_not_an_image(self, app, http_mock):
        http_mock.route().mock(side_effect=httpx.ConnectError('no route in tests'))
        assert is_image_url('http://[abc') is False


class TestIsImageUrlStillRecognisesImages:
    """The over-correction guard."""

    @pytest.mark.parametrize('url', [
        'https://example.com/a.png',
        'https://example.com/a.jpg',
        'https://example.com/a.webp',
    ])
    def test_an_image_extension_is_still_recognised(self, url, app, http_mock):
        http_mock.route().mock(side_effect=httpx.ConnectError('no route in tests'))
        assert is_image_url(url) is True

    def test_a_non_image_is_still_false(self, app, http_mock):
        http_mock.route().mock(side_effect=httpx.ConnectError('no route in tests'))
        assert is_image_url('https://example.com/article') is False


class TestAnOutOfRangePortIsNotAffected:
    """`urlparse('http://example.com:99999/')` succeeds, and so does reading
    `.hostname` -- 'example.com'. Only reading `.port` raises `Port out of
    range 0-65535`.

    None of the functions in this file reads `.port`, so none of them raises
    on this shape, before or after this commit. `is_safe_redirect_target`
    touches `parsed.port` deliberately because it is deciding whether to send
    a browser somewhere; these functions are not, and adding a `.port` read to
    them would CHANGE behaviour -- turning a URL they handle today into the
    safe value. These tests pin that they still handle it.
    """

    def test_domain_from_url_still_attributes_the_host(self, app, db_session):
        assert domain_from_url(OUT_OF_RANGE_PORT).name == 'example.com'

    def test_remove_tracking_from_link_returns_it_unchanged(self, app):
        assert remove_tracking_from_link(OUT_OF_RANGE_PORT) == OUT_OF_RANGE_PORT

    def test_fixup_url_passes_it_through(self, app):
        with app.test_request_context('/'):
            assert fixup_url(OUT_OF_RANGE_PORT) == (OUT_OF_RANGE_PORT, OUT_OF_RANGE_PORT)

    def test_url_needs_archive_reads_the_host(self):
        assert url_needs_archive('http://www.nytimes.com:99999/x') is True

    def test_is_video_url_reads_the_path(self):
        assert is_video_url('http://example.com:99999/clip.mp4') is True

    def test_mimetype_from_url_reads_the_path(self):
        assert mimetype_from_url('http://example.com:99999/a.png') == 'image/png'

    def test_inbox_domain_reads_the_host(self):
        assert inbox_domain('http://example.com:99999/inbox') == 'example.com'

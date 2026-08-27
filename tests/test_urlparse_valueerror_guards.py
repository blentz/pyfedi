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

SECOND WAVE. Guarding `urlparse` was not enough on its own. Two more things
refuse these same netlocs, one of them EARLIER in the same call path:

* `httpx.InvalidURL`, raised while building a request, descends straight from
  `Exception` -- it is neither an `httpx.HTTPError` nor a `ValueError`. It
  escaped `mime_type_using_head`, which `is_image_url` calls BEFORE it reaches
  the guarded `urlparse`, so post creation still 500'd. It escaped
  `get_request` for the same reason, past four handlers whose whole job is to
  normalise failures into `httpx.HTTPError`.
* `extract_domain_and_actor` (app/activitypub/util.py) has the same unguarded
  `urlparse`, on an actor id chosen by a remote peer.

And `inbox_domain` had an adjacent hole with no exception involved at all: a
URL that parses but carries no host yielded `None`, which two of its callers
cannot take.
"""
import httpx
import pytest

from app.activitypub.util import extract_domain_and_actor
from app.models import Domain
from app.post.util import url_needs_archive
from app.utils import (domain_from_url, fixup_url, get_request, inbox_domain,
                       instance_allowed, is_image_url,
                       is_video_url, mime_type_using_head, mimetype_from_url,
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
    """THE USER-PATH TEST. `app/shared/post.py:410` and `:601` call this with a
    submitted post URL, so whatever escapes here 500s post creation.

    `is_image_url` tries a HEAD request first and only falls through to
    `urlparse` when that yields no Content-Type. The two steps refuse DIFFERENT
    inputs, which is why this test iterates all four shapes rather than picking
    one:

      'http://[abc'            httpx accepts -> HEAD fails as a transport error
                               -> falls through -> urlparse raises ValueError
      the other three          httpx.URL() itself raises InvalidURL, one call
                               BEFORE the urlparse guard

    The first commit of this fix guarded only the `urlparse`, so only
    'http://[abc' was actually closed and the other three still crashed with
    `httpx.InvalidURL` out of `mime_type_using_head`. This test is the one the
    earlier tests could not make: it asserts the whole user path is closed, not
    one layer of it.

    The HEAD is routed to a ConnectError rather than left to reach the network
    -- tests/conftest.py's `block_outbound_http` docstring prescribes exactly
    this for observing a transport failure. `http_mock` asserts its routes are
    called, and 'http://[abc' is the shape that reaches the transport, so the
    route is satisfied. The assertion is still on what is_image_url returns.

    Its safe value is False, the answer it already gives for a path with no
    image extension.
    """

    def test_no_malformed_netloc_is_an_image(self, app, http_mock):
        http_mock.route().mock(side_effect=httpx.ConnectError('no route in tests'))
        for url in MALFORMED:
            assert is_image_url(url) is False, url


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


class TestMimeTypeUsingHeadSurvivesAnInvalidUrl:
    """`httpx.InvalidURL` descends straight from `Exception` -- it is NOT an
    `httpx.HTTPError` and NOT a `ValueError`:

        issubclass(httpx.InvalidURL, httpx.HTTPError) -> False
        issubclass(httpx.InvalidURL, ValueError)      -> False

    So `except httpx.HTTPError: return ''` did not catch it, and httpx raises
    it while BUILDING the request -- before any transport, so respx never sees
    it and no route can absorb it. It escaped `mime_type_using_head` and, one
    frame up, `is_image_url`.

    '' is the safe value the existing `except httpx.HTTPError` handler already
    returns: "no Content-Type could be determined", which sends the caller down
    the extension-sniffing path.
    """

    @pytest.mark.parametrize('url', [
        'https://[::1/x',            # httpx: Invalid port: ':1'
        'http://[1::2::3]',          # httpx: Invalid IPv6 address
        'http://exa℀mple.com/',  # httpx: Invalid IDNA hostname
        'http://[v1.x]/y',           # httpx refuses this one though urlparse accepts it
    ])
    def test_a_url_httpx_refuses_yields_no_content_type(self, url, app):
        """No `http_mock`: these never reach a transport, so registering a
        route would leave it uncalled and fail the fixture's own assertion."""
        assert mime_type_using_head(url) == ''


class TestMimeTypeUsingHeadStillReadsContentType:
    """The over-correction guard. '' is also the answer for a transport
    failure, so only a successful HEAD distinguishes a working guard from one
    that swallowed the whole function.

    Distinct hosts per test: `mime_type_using_head` is `@cache.memoize`d
    against a FileSystemCache, so a URL reused across tests would serve a
    cached answer rather than exercising the route.
    """

    def test_a_successful_head_still_returns_the_content_type(self, app, http_mock):
        http_mock.head('https://mime-ok.example/x').mock(
            return_value=httpx.Response(200, headers={'Content-Type': 'image/png'}))
        assert mime_type_using_head('https://mime-ok.example/x') == 'image/png'

    def test_octet_stream_is_still_flattened_to_empty(self, app, http_mock):
        http_mock.head('https://mime-octet.example/x').mock(
            return_value=httpx.Response(200, headers={'Content-Type': 'application/octet-stream'}))
        assert mime_type_using_head('https://mime-octet.example/x') == ''

    def test_a_transport_error_is_still_empty(self, app, http_mock):
        http_mock.head('https://mime-down.example/x').mock(
            side_effect=httpx.ConnectError('down'))
        assert mime_type_using_head('https://mime-down.example/x') == ''


class TestGetRequestConvertsAnInvalidUrlToHttpError:
    """`get_request` already normalises `ValueError` and `httpx.StreamError`
    into `httpx.HTTPError`, because that is what its callers catch. It did not
    normalise `httpx.InvalidURL`, which is neither -- so the identical hole was
    open here.

    DEBUG is set for these because `is_invalid_get_request_uri` short-circuits
    to False under it (app/utils.py), which is the real configuration a dev
    install runs and is the shortest path to the httpx call. With DEBUG off,
    'http://[v1.x]/y' reaches httpx anyway -- furl accepts that host and the
    DNS check fails open -- but it costs a real `getaddrinfo`, so the flag is
    used instead of relying on a name lookup.
    """

    @pytest.mark.parametrize('url', [
        'https://[::1/x',
        'http://[1::2::3]',
        'http://exa℀mple.com/',
        'http://[v1.x]/y',
    ])
    def test_a_url_httpx_refuses_raises_the_error_callers_catch(self, url, app, monkeypatch):
        monkeypatch.setitem(app.config, 'DEBUG', True)
        with app.test_request_context('/'):
            with pytest.raises(httpx.HTTPError):
                get_request(url)

    def test_the_raised_error_is_not_an_invalid_url(self, app, monkeypatch):
        """The sharp one. `httpx.InvalidURL` is not an `httpx.HTTPError`, so
        `pytest.raises(httpx.HTTPError)` above already fails if it escapes --
        this states it directly so the reason is not lost."""
        monkeypatch.setitem(app.config, 'DEBUG', True)
        with app.test_request_context('/'):
            with pytest.raises(Exception) as caught:
                get_request('https://[::1/x')
        assert not isinstance(caught.value, httpx.InvalidURL)
        assert isinstance(caught.value, httpx.HTTPError)


class TestGetRequestStillFetchesOrdinaryUrls:
    """The over-correction guard: a guard that swallowed everything would turn
    a perfectly good response into an HTTPError."""

    def test_an_ordinary_url_still_returns_its_response(self, app, http_mock, monkeypatch):
        monkeypatch.setitem(app.config, 'DEBUG', True)
        http_mock.get('https://get-ok.example/x').mock(
            return_value=httpx.Response(200, text='hello'))
        with app.test_request_context('/'):
            response = get_request('https://get-ok.example/x')
        assert response.status_code == 200
        assert response.text == 'hello'


class TestExtractDomainAndActorSurvivesAMalformedNetloc:
    """Remote input, not local: `app/activitypub/actor.py:43`
    `validate_remote_actor(actor_url)` passes an actor id the PEER chose, on
    the inbound federation path. A broken or hostile peer picks this string.

    ('', '') is the safe value because it is what urlparse already yields for a
    string with no authority and no path -- `netloc` is '' and
    `path.split('/')[-1]` is ''. Nothing is invented. Every caller degrades to
    "not found": `validate_remote_actor` gets `instance_allowed('')`, which
    returns True on its own empty-host guard, and the actor fetch that follows
    fails; the callers that build `'!' + actor + '@' + server` produce '!@',
    which `search_for_community` splits into two empty strings and finds
    nothing.
    """

    @pytest.mark.parametrize('url', MALFORMED)
    def test_a_malformed_actor_url_yields_no_domain_and_no_actor(self, url):
        assert extract_domain_and_actor(url) == ('', '')


class TestExtractDomainAndActorStillSplitsOrdinaryUrls:
    """The over-correction guard."""

    def test_an_ordinary_actor_url_still_splits(self):
        assert extract_domain_and_actor('https://example.com/c/news') == \
            ('example.com', 'news')

    def test_a_trailing_slash_is_still_stripped_first(self):
        """The WordPress case the function opens with."""
        assert extract_domain_and_actor('https://example.com/c/news/') == \
            ('example.com', 'news')

    def test_a_port_is_still_part_of_the_netloc(self):
        assert extract_domain_and_actor('https://example.com:8443/u/alice') == \
            ('example.com:8443', 'alice')


class TestInboxDomainAnEmptyHost:
    """Adjacent to the ValueError guard and pre-existing: a URL that PARSES but
    carries no host ('https:///x') made `urlparse(...).hostname` return None,
    and inbox_domain returned that None straight out.

    Its callers cannot take None: `instance_allowed` calls `.strip()` on the
    result (AttributeError) and `instance_banned` matches it against a compiled
    regex (TypeError). '' is returned instead, matching the ValueError path in
    the same function and the empty-input case at the top of it, and leaving
    every caller on a defined path.
    """

    @pytest.mark.parametrize('url', ['https:///x', 'http:///', 'https://'])
    def test_a_url_with_no_host_yields_the_empty_string(self, url):
        assert inbox_domain(url) == ''

    def test_instance_allowed_no_longer_raises_on_a_hostless_url(self, app, db_session):
        """The consequence, asserted where it bites. Before this commit
        `instance_allowed('https:///x')` raised AttributeError on None.strip()."""
        assert instance_allowed('https:///x') is False

    @pytest.mark.parametrize('value', MALFORMED + [
        'https:///x', 'http:///', 'https://',
        'https://example.com/inbox', 'Example.COM', '',
    ])
    def test_the_return_is_always_a_string(self, value):
        """The invariant the callers actually need, stated once over every
        shape. `instance_banned` matches the result against a compiled regex
        and `instance_allowed` calls `.strip()` on it -- both need a str, and
        neither the ValueError path nor the empty-host path may hand them
        anything else.

        Asserted as a type invariant rather than by driving `instance_banned`
        with a wildcard ban row: that function is `@cache.memoize`d for 150
        seconds against a FileSystemCache, so a data-driven version would serve
        a stale answer on a re-run and could not be trusted as a revert check.
        """
        assert isinstance(inbox_domain(value), str)

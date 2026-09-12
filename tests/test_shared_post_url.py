"""`edit_post`'s two URL-type dispatches and the tail that closes the module.

SCOPE. Sub-project 39, the last 41 statements and 35 branch arcs in
`app/shared/post.py`. Every one of them is in `edit_post`; every other
function in the module is at zero. Three regions:

  - `:387-460` -- the permission compound, the type dispatch on `post.url`
    (the EXISTING url), the scheduled-post gate, and the old-file teardown.
  - `:565-661` -- the type dispatch on `url` (the NEWLY SUBMITTED one), which
    additionally builds thumbnails and `File` rows.
  - `:662-703` -- five arms of the poll and event tail whose lines all run.

ENTRY is a direct call. `edit_post` opens with `if not user:` on both
branches (`:252` SRC_API, `:316` SRC_WEB), so passing `user=` skips
`authorise_api_user` and `current_user` alike -- no request context, no
login, no token. Every test here does that.

`from_scratch=True` SWITCHES OFF `:421-459`. That block holds the
notification cleanup, the poll-vote deletes, the teardown at `:435-451`, the
tag clear and `:459`'s commit. Every test here passes `from_scratch=True`
EXCEPT the teardown tests, which are the ones aimed at that block.

THE HEAD REQUEST, AND WHY THIS FILE INVERTS THE UPLOAD FILE'S RULE.
`is_image_url` (app/utils.py:247) issues an httpx HEAD through
`mime_type_using_head`, and `edit_post` can call it twice -- `:410` on
`post.url`, `:601` on `url`. `tests/README.md` fact 229 point 3 records the
rule `tests/test_shared_post_upload.py` follows: a HEAD reporting
`image/png` and NO GET route, because an image content type makes `:601`
true and `:601` is the only one of the four arms in the
`if`/`elif`/`elif`/`else` chain at `:601`/`:619`/`:630`/`:641` that does not
call `opengraph_parse`.

THIS FILE NEEDS THE OPPOSITE on the arms it targets. A HEAD reporting
`text/html` makes `is_image_url` false (`.html` is not in
`common_image_extensions`, app/utils.py:248-249), control reaches `:619`
onward, `opengraph_parse` runs, and a GET route becomes REQUIRED rather than
forbidden. Both conventions appear in this file; they are chosen per test,
never by a module-level fixture.

THE INVERSION IS MEASURED, NOT REASONED. Sub-project 39 Task 1 ran it
directly under `app.app_context()` in this container, with the same
`respx.mock(assert_all_called=True)` router `http_mock` uses:

    text/html  -> False
    image/png  -> True

so the whole of this file's Region B strategy rests on an observation
rather than on a reading of `app/utils.py:271-273`.

`mime_type_using_head` IS `@cache.memoize`-DECORATED (app/utils.py:332), so
two tests that HEAD the SAME url with DIFFERENT content types would be a
cross-test leak under a real cache -- the second test's route would never be
fetched and `assert_all_called=True` would fail it at teardown. It is safe
here only because `TestConfig` sets `CACHE_TYPE = 'NullCache'`
(tests/conftest.py:68), which makes the memoization a no-op. The pair
`test_an_existing_image_url_retypes_the_post_as_image` /
`test_an_existing_url_that_is_neither_video_nor_image_leaves_the_type_alone`
deliberately shares one url and differs only in the content type, and that
pair is what would break first if the cache type ever changed.

`http_mock` is `assert_all_called=True` (tests/conftest.py:342), so a
registered route that is never reached fails the test at teardown, and
respx's unmatched-request error is neither `httpx.HTTPError` nor
`httpx.InvalidURL` -- it escapes `app/utils.py:345`'s handler rather than
being swallowed as `''`. Count your HEADs and your GETs.

`fixup_url` (app/utils.py:3311) RETURNS `(url, url)` for every url here. It
diverges only for YouTube domains and for peertube urls whose last 25
characters begin `/w/` (`:3330`). So `thumbnail_url == embed_url == url`,
which is why the GET for `opengraph_parse` is registered on the submitted
url itself -- and why `post.url` alone cannot tell `:640` (`post.url = url`)
apart from `:652` (`post.url = embed_url`). See TestLoopsArm for what does.
"""

from datetime import datetime, timedelta
from io import BytesIO

from PIL import Image

from app import db
from app.constants import (
    POST_STATUS_SCHEDULED, POST_TYPE_ARTICLE, POST_TYPE_EVENT,
    POST_TYPE_IMAGE, POST_TYPE_LINK, POST_TYPE_POLL, POST_TYPE_VIDEO,
    SRC_API, SRC_WEB,
)
from app.models import Domain, Event, File, Poll, PollChoice
from app.shared.post import edit_post
from tests.factories import make_community_member, make_user
from tests.test_shared_post_edit import _api_input, _make_admin, _seed, _web_form
from tests.test_shared_post_upload import chdir_upload  # noqa: F401


def _opengraph_page(http_mock, url, **tags):
    """Serve `url` as an HTML page carrying `tags` as opengraph <meta> elements.

    `:621`/`:632`/`:642`'s `opengraph_parse` (app/utils.py:2998-3007)
    delegates to `parse_page` (app/utils.py:3165-3225), which GETs the page
    and requires BOTH a 200 (app/utils.py:3195-3196) and 'text/html' in the
    Content-Type (app/utils.py:3198-3199) before it parses; either missing
    makes it return False. The header is load-bearing, not decoration.

    A keyword cannot carry a ':', so each tag is named with '_' and
    translated: `og_image_url=...` becomes `<meta property="og:image:url" ...>`.

    With NO tags this still returns a page `parse_page` reads successfully and
    finds nothing in -- an EMPTY dict, which is falsy. That is a different
    input from `_unreadable_page` below, which returns False, and the two are
    not interchangeable for the mutation pass.
    """
    meta = ''.join(f'<meta property="{name.replace("_", ":")}" content="{value}">'
                   for name, value in tags.items())
    http_mock.get(url).respond(200, headers={'Content-Type': 'text/html'},
                               text=f'<html><head>{meta}</head><body></body></html>')


def _unreadable_page(http_mock, url):
    """Serve `url` as a 404, so `parse_page` returns False at
    app/utils.py:3195-3196 and `:622`/`:633`/`:643`'s leading `if opengraph`
    is False.

    False rather than None, and the difference matters to Task 9's mutation
    pass: forcing the first conjunct true reaches `False.get('og:image', '')`
    and raises AttributeError, which is a crash-kill and not an
    assertion-kill.
    """
    http_mock.get(url).respond(404)


class TestExistingUrlTypeDispatch:
    """`:403-411` -- the type dispatch on `post.url`, the url the post ALREADY
    has, tested before `:565` ever looks at the submitted one.

    Every test here passes `url=None` in the input, so `:565`'s
    `if url and (from_scratch or url_changed):` and `:660`'s `elif url and ...`
    are both false and the whole tail from `:565` is skipped. That leaves
    `:398`'s `post.type = type` and this block as the ONLY writers of
    `post.type`, which is what makes `post.type` a witness here at all.
    """

    def test_an_existing_pixelfed_url_retypes_the_post_as_image(self, db_session):
        """`:404` true -> `:405`. Arc 404->405, statement 405.

        No `http_mock`: `:404`'s match short-circuits the elif chain before
        `:408`'s `is_video_url` (pure) and `:410`'s `is_image_url` (which would
        issue a HEAD), so this path makes no outbound request at all.

        `type=POST_TYPE_ARTICLE` is passed so `:398` writes ARTICLE and only
        `:405` can produce IMAGE.
        """
        s = _seed(url='https://pixelfed.social/p/someone/1')

        edit_post(_api_input(), s.post, POST_TYPE_ARTICLE, SRC_API,
                  user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.type == POST_TYPE_IMAGE

    def test_an_existing_loops_url_retypes_the_post_as_video(self, db_session):
        """`:404` false, `:406` true -> `:407`. Arc 406->407, statement 407.

        `loops.video` is neither pixelfed host, so `:404` is false; `:406`'s
        `startswith('https://loops.video/')` is what decides. The path has no
        video EXTENSION, so `:408`'s `is_video_url` would be false and `:409`
        cannot be the line that produces VIDEO -- which is what keeps this
        test distinct from the one below (false-witness mechanism 4).
        """
        s = _seed(url='https://loops.video/v/abc123')

        edit_post(_api_input(), s.post, POST_TYPE_ARTICLE, SRC_API,
                  user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.type == POST_TYPE_VIDEO

    def test_an_existing_video_extension_retypes_the_post_as_video(self, db_session):
        """`:408`'s `is_video_url(post.url)` true -> `:409`. Arc 408->409,
        statement 409.

        Still no `http_mock`: `is_video_url` (app/utils.py:294) is pure
        urlparse plus an extension test and issues no request. The url is
        deliberately NOT a pixelfed or loops host, so `:404` and `:406` are
        both false and `:408` is the line that decides.
        """
        s = _seed(url='https://example.com/clip.mp4')

        edit_post(_api_input(), s.post, POST_TYPE_ARTICLE, SRC_API,
                  user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.type == POST_TYPE_VIDEO

    def test_an_existing_image_url_retypes_the_post_as_image(self, db_session, http_mock):
        """`:410`'s `is_image_url(post.url)` true -> `:411`.

        THE POSITIVE CONTROL for the test below, and the reason both exist.
        The false-arm test can only assert that `post.type` is still what
        `:398` wrote, which is also what a broken `:410` that never ran would
        leave -- false-witness mechanism 1. This test differs from it in ONE
        byte of the HEAD's Content-Type and produces a different type, so the
        pair proves `:410` is actually deciding.
        """
        http_mock.head('https://example.com/page.html').respond(
            200, headers={'Content-Type': 'image/png'})
        s = _seed(url='https://example.com/page.html')

        edit_post(_api_input(), s.post, POST_TYPE_ARTICLE, SRC_API,
                  user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.type == POST_TYPE_IMAGE

    def test_an_existing_url_that_is_neither_video_nor_image_leaves_the_type_alone(
            self, db_session, http_mock):
        """`:410` false -> `:413`. Arc 410->413.

        Identical to the test above except the HEAD reports `text/html`
        instead of `image/png`, so `is_image_url` returns False at
        app/utils.py:273 (`.html` is not in `common_image_extensions`) and no
        arm of `:403-411` fires.

        `http_mock`'s `assert_all_called=True` is doing real work here on top
        of the type assertion: it proves the HEAD was actually ISSUED, so
        `:410` was reached and evaluated rather than skipped -- which is the
        part a bare "type is still ARTICLE" assertion could not tell apart
        from the elif chain never running.
        """
        http_mock.head('https://example.com/page.html').respond(
            200, headers={'Content-Type': 'text/html'})
        s = _seed(url='https://example.com/page.html')

        edit_post(_api_input(), s.post, POST_TYPE_ARTICLE, SRC_API,
                  user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.type == POST_TYPE_ARTICLE

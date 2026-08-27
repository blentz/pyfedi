"""Every url-shaped helper must answer for `None`, not raise.

This branch made a NULL `post.url` an ordinary state. `Post.new()`
(`app/models.py`) and `update_post_from_activity`
(`app/activitypub/util.py`) both store `post.url = None` for a url they will
not keep, and neither resets `post.type` -- so a VIDEO or LINK post can reach
any consumer of `post.url` with nothing in it.

The `except ValueError` guards this branch added do NOT cover that, and the
mechanism is worth stating exactly because it is easy to assume they do:

    >>> urlparse(None)
    ParseResultBytes(scheme=b'', netloc=b'', path=b'', ...)

`urlparse(None)` does not raise. It returns a result whose members are BYTES,
and the failure arrives one line later when a str method is called on them:

    >>> urlparse(None).path.endswith('.mp4')
    TypeError: endswith first arg must be bytes or a tuple of bytes, not str

`TypeError` is not a subclass of `ValueError`. Three of these helpers do not
even reach urlparse -- `domain_from_url` calls `url.lower()` and `fixup_url`
calls `len(url)` first, both `AttributeError`/`TypeError`. So the fix is an
explicit falsy check at the top of each helper, not a wider `except`: a normal
state should be answered by a conditional, not routed through exception
handling.

Each test below asserts the helper's OWN safe value -- the same one it already
returns for a url it cannot make sense of -- so a helper that started raising,
or that started returning something a caller cannot use, fails here.

`''` is asserted alongside `None` in every case. The guard is `if not url:`,
which catches both, and every one of these helpers already answered for `''`
before the guard existed: pinning both is what stops a later "fix" from
narrowing the guard to `is None` and changing the empty-string answer.

Scope, stated so the absences are not read as oversights:

- `is_local_image_url` needs no guard of its own. It opens with
  `if not is_image_url(url): return False`, so the guard on `is_image_url`
  already answers for it; `test_is_local_image_url_delegates_its_none_answer`
  proves that rather than assuming it.
- `url_needs_archive` (`app/post/util.py`) already opens with `if url:` and
  returns False for None. Pinned below so a later edit cannot quietly drop it.
- `inbox_domain` and `extract_domain_and_actor` take an INBOX or ACTOR url,
  never `post.url`, and no caller can hand them None: all five `inbox_domain`
  call sites in `app/utils.py` write `inbox_domain(x.strip())`, so a None
  would raise upstream at `.strip()` -- unchanged by this branch, and a guard
  inside the helper would be unreachable code. Left alone deliberately.
"""

import pytest

from app.models import Post
from app.post.util import url_needs_archive
from app.utils import (domain_from_url, fixup_url, is_image_url, is_local_image_url, is_video_url,
                       mimetype_from_url, remove_tracking_from_link)

FALSY = [None, '']


class TestIsVideoUrl:
    """The helper behind the live 500 -- see tests/test_post_edit_null_url.py."""

    @pytest.mark.parametrize('url', FALSY)
    def test_returns_false(self, url):
        assert is_video_url(url) is False

    def test_an_ordinary_video_url_is_still_recognised(self):
        """The over-correction guard: a check placed too wide returns False for
        everything and the test above still passes."""
        assert is_video_url('https://vid.example/clip.mp4') is True


class TestIsImageUrl:
    @pytest.mark.parametrize('url', FALSY)
    def test_returns_false_without_a_head_request(self, url, block_outbound_http):
        """`block_outbound_http` is autouse and registers no routes, so any
        outbound httpx call raises. Pre-guard, is_image_url(None) called
        mime_type_using_head(None) first; asserting under that fixture proves
        the guard returns before the HEAD request, not merely that the answer
        is False.
        """
        assert is_image_url(url) is False

    def test_an_ordinary_image_url_is_still_recognised(self, app, http_mock):
        """`app` is needed for mime_type_using_head's @cache.memoize, which
        reaches current_app. The 404 sends it to extension sniffing, which is
        the arm the falsy guard sits in front of."""
        http_mock.head('https://img.example/pic.png').respond(404)
        assert is_image_url('https://img.example/pic.png') is True


class TestIsLocalImageUrl:
    @pytest.mark.parametrize('url', FALSY)
    def test_is_local_image_url_delegates_its_none_answer(self, url):
        """No guard of its own: `if not is_image_url(url): return False` is the
        first statement, so is_image_url's guard answers for it. Without that,
        `furl(None).host` would be reached.
        """
        assert is_local_image_url(url) is False


class TestMimetypeFromUrl:
    @pytest.mark.parametrize('url', FALSY)
    def test_returns_none(self, url):
        assert mimetype_from_url(url) is None

    def test_an_ordinary_url_still_yields_its_mimetype(self):
        assert mimetype_from_url('https://ex.example/a.png') == 'image/png'


class TestDomainFromUrl:
    @pytest.mark.parametrize('url', FALSY)
    def test_returns_none_without_touching_the_database(self, url):
        """No db_session fixture on purpose. domain_from_url queries the Domain
        table for any url with a host, so a test that reached the query would
        error rather than pass -- which is what makes this assert that the
        guard returns first.
        """
        assert domain_from_url(url) is None


class TestRemoveTrackingFromLink:
    @pytest.mark.parametrize('url', FALSY)
    def test_returns_its_input_unchanged(self, url):
        assert remove_tracking_from_link(url) == url

    def test_a_youtu_be_link_is_still_rewritten(self):
        """The over-correction guard: this is the one branch that calls str
        methods on the parse result, and a guard placed too wide skips it."""
        assert remove_tracking_from_link('https://youtu.be/abc123') == 'https://youtube.com/watch?v=abc123'


class TestFixupUrl:
    @pytest.mark.parametrize('url', FALSY)
    def test_returns_its_input_twice(self, url):
        assert fixup_url(url) == (url, url)

    def test_an_ordinary_youtube_url_is_still_fixed_up(self, app):
        """The over-correction guard. `app` is needed because the youtube arm
        is reached only after the peertube length test, which queries
        `instance`."""
        thumbnail_url, embed_url = fixup_url('https://youtube.com/watch?v=abc123')
        assert thumbnail_url == 'https://youtu.be/abc123'


class TestUrlNeedsArchive:
    @pytest.mark.parametrize('url', FALSY)
    def test_returns_false(self, url):
        """Already guarded by its own `if url:`; pinned so a later edit cannot
        drop it silently."""
        assert url_needs_archive(url) is False

    def test_a_paywalled_host_still_needs_an_archive_link(self):
        assert url_needs_archive('https://www.nytimes.com/2026/an-article') is True


class TestYoutubeCanEmbed:
    """`Post.youtube_can_embed` opened with `"youtube.com" not in self.url`,
    which is TypeError for None, while its two siblings `youtube_embed` and
    `youtube_video_id` both already opened with `if self.url:`.

    NOT a live crash: all four template call sites sit inside a block that has
    already tested `post.url` (see the comment on the guard in
    `app/models.py`). Guarded because it is a public method whose two siblings
    are guarded, and asserted here rather than through a route for that reason
    -- claiming a route test for a path the templates close would overstate it.

    A bare `Post()` with no session: these three methods read only `self.url`.
    """

    @pytest.mark.parametrize('url', FALSY)
    def test_all_three_youtube_helpers_answer_for_a_falsy_url(self, url):
        post = Post(url=url)
        assert post.youtube_can_embed() is False
        assert post.youtube_embed() == ''
        assert post.youtube_video_id() == ''

    def test_an_ordinary_youtube_url_still_embeds(self):
        """The over-correction guard."""
        post = Post(url='https://youtube.com/watch?v=abc123')
        assert post.youtube_can_embed() is True
        assert post.youtube_video_id() == 'abc123'

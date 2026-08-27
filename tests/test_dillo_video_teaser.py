"""The dillo theme's `render_video` teaser must survive a NULL `post.url`.

`app/templates/themes/dillo/post/post_teaser/_macros.html` is the only theme
override of `post/post_teaser/_macros.html` in `app/templates/themes/` (nothing
else under that tree calls a method on `post.url` at all), and its
`render_video` macro opened with

    {% if post.url.endswith('.mp4') or post.url.endswith('.webm') -%}

where the main theme's counterpart (`app/templates/post/post_teaser/_macros.html`)
guards the same chain with `{% if post.url -%}` first. `None.endswith` raises
`AttributeError`, which Jinja does not swallow: the whole listing 500s, not just
the one teaser.

A VIDEO post with a NULL url is producible today. `app/activitypub/util.py`'s
microblog branch writes `post.url = None` for a peer-supplied link it cannot
parse, and `update_post_from_activity`'s Links section stores `None` rather than
`''` when an Update removes a url (tests/test_post_url_cleared_by_update.py) --
neither of which resets `post.type`, so a post that was VIDEO stays VIDEO. Local
editing reaches the same shape: `app/shared/post.py` leaves `post.url` alone when
the submitted url is empty.

Why this file drives `/home/<sort>/<view_filter>` and not `/c/<name>`:
`app/utils.py`'s theme-aware `render_template` only swaps the TOP-LEVEL template
for a themed copy, and dillo ships `index.html` but no `community/community.html`.
Jinja's `{% include %}`/`{% from %}` take a literal loader path, so the base
`community/community.html` imports the base macros no matter what theme is set --
`/c/<name>` cannot reach dillo's macros. dillo's own `index.html` includes
`themes/dillo/post/_post_teaser.html` explicitly, which is the reachable route.
`/post/<id>` is not usable here at all: `app/templates/base.html:1` calls
`csrf_token()` and TestConfig disables CSRF.

`test_the_page_really_rendered_the_dillo_theme` is what stops the other two from
passing vacuously against the main theme's already-correct macro.
"""

import pytest

from app import db
from app.constants import POST_TYPE_VIDEO
from tests.factories import make_community, make_instance, make_post, make_user

pytestmark = pytest.mark.usefixtures('site')

MP4 = 'https://vid.example/clip.mp4'


@pytest.fixture
def dillo_listing(db_session):
    """A dillo-themed viewer and two VIDEO posts: one with a url, one without.

    `User.theme` is read first by `app.utils.current_theme()`, ahead of
    `Site.default_theme`, so setting it on the viewer selects the theme without
    touching the shared Site row.

    Both posts are ordinary published posts in a community with `show_all` at its
    default, which is what `get_deduped_post_ids`' "All" view
    (`community_ids=[-1]`) selects on.
    """
    make_instance('test.piefed.local', software='piefed')
    author = make_user(make_instance('vid.example'), 'dillovidauthor')
    viewer = make_user(None, 'dilloviewer', local=True)
    viewer.theme = 'dillo'
    community = make_community('dillovideos')

    with_url = make_post(community, author, 'https://vid.example/notes/1',
                         title='a video with a url')
    with_url.type = POST_TYPE_VIDEO
    with_url.url = MP4

    without_url = make_post(community, author, 'https://vid.example/notes/2',
                            title='a video whose url was removed')
    without_url.type = POST_TYPE_VIDEO
    without_url.url = None

    db.session.commit()
    assert without_url.url is None
    return viewer, with_url, without_url


def _body(app, viewer):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(viewer.id)
        sess['_fresh'] = True
    response = client.get('/home/new/all')
    assert response.status_code == 200, f'the listing did not render: {response.status_code}'
    return response.get_data(as_text=True)


def test_the_page_really_rendered_the_dillo_theme(app, dillo_listing):
    """Without this, both tests below would pass against the main theme.

    `themes/dillo/styles.css` is emitted by `themes/dillo/base.html` only, so its
    presence proves `current_theme()` resolved to dillo and the themed
    `index.html` -- and therefore the themed `render_video` -- is what ran.
    """
    viewer, _with_url, _without_url = dillo_listing

    assert 'themes/dillo/styles.css' in _body(app, viewer)


def test_a_video_post_with_no_url_does_not_break_the_listing(app, dillo_listing):
    """The crash. Pre-fix this raises AttributeError out of the macro and the
    whole page 500s, taking the other post with it -- hence the assertion that
    the sibling post is still listed, not merely that the response was 200."""
    viewer, with_url, without_url = dillo_listing

    body = _body(app, viewer)

    assert f'/post/{without_url.id}' in body
    assert f'/post/{with_url.id}' in body, 'the crash takes the whole listing, not one teaser'


def test_an_ordinary_video_post_still_renders_its_player(app, dillo_listing):
    """The over-correction guard: a guard placed too wide (or wrapping the wrong
    span) silently drops the player for every video post, and the test above
    would still pass."""
    viewer, _with_url, _without_url = dillo_listing

    body = _body(app, viewer)

    assert f'<source src="{MP4}" type="video/mp4" />' in body

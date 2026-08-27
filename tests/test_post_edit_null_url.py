"""`GET /post/<id>/edit` must answer 401 to a non-owner, not 500, when the post
is a VIDEO whose `url` is NULL.

`app/post/routes.py`'s `post_edit` builds the form BEFORE it checks ownership:

    elif post.type == POST_TYPE_VIDEO:          # app/post/routes.py:1026
        if is_video_url(post.url):              # app/post/routes.py:1027
    ...
    if post.user_id == current_user.id:         # app/post/routes.py:1055
    ...
    else:
        abort(401)                              # app/post/routes.py:1155

So every logged-in user reaches `is_video_url(post.url)` for any VIDEO post on
the instance, whether or not they may edit it. The post's state is
peer-controllable: `app/activitypub/util.py`'s microblog branch and
`update_post_from_activity` both store `post.url = None` without resetting
`post.type`, which is the same VIDEO-with-NULL-url shape
`tests/test_dillo_video_teaser.py` asserts is producible.

`except ValueError` does not see that. `urlparse(None)` does NOT raise -- it
returns a `ParseResultBytes` whose members are `b''` -- and the crash comes one
line later, from `b''.endswith('.mp4')`:

    TypeError: endswith first arg must be bytes or a tuple of bytes, not str

`TypeError` is not a `ValueError`, so the guard this branch added to
`is_video_url` is blind to it. The fix is an explicit falsy check at the top of
the helper, not a wider `except`: `None` reaching a url-shaped helper is a
normal state in this codebase now, and a normal state should not be routed
through exception handling.

The 401 is the whole point of testing through the route rather than the helper:
a 500 here leaks the existence of a crash on state a peer controls, and it
replaces an authorisation refusal with a server error.
"""

import pytest

from app import db
from app.constants import POST_TYPE_VIDEO
from tests.factories import make_community, make_instance, make_post, make_user

pytestmark = pytest.mark.usefixtures('site')

MP4 = 'https://vid.example/clip.mp4'


@pytest.fixture
def edit_scenario(db_session):
    """An author who owns two VIDEO posts, and an unrelated logged-in viewer.

    One post has an ordinary .mp4 url, one has NULL -- the shape
    `update_post_from_activity` stores when an Update removes a url without
    changing the post's type.
    """
    make_instance('test.piefed.local', software='piefed')
    author = make_user(None, 'editauthor', local=True)
    viewer = make_user(None, 'editviewer', local=True)
    community = make_community('editvideos')

    with_url = make_post(community, author, 'https://vid.example/edit/1',
                         title='a video with a url')
    with_url.type = POST_TYPE_VIDEO
    with_url.url = MP4

    without_url = make_post(community, author, 'https://vid.example/edit/2',
                            title='a video whose url was removed')
    without_url.type = POST_TYPE_VIDEO
    without_url.url = None

    db.session.commit()
    assert without_url.url is None
    return author, viewer, with_url, without_url


def _get_edit(app, user, post):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True
    return client.get(f'/post/{post.id}/edit')


class TestNonOwnerGetsUnauthorisedNotServerError:
    def test_a_video_post_with_no_url_refuses_a_non_owner_with_401(self, app, edit_scenario):
        """The finding. Pre-fix this is 500: `is_video_url(None)` raises
        TypeError at `app/post/routes.py:1027`, twenty-eight lines before the
        ownership check that should have answered 401.
        """
        _author, viewer, _with_url, without_url = edit_scenario

        response = _get_edit(app, viewer, without_url)

        assert response.status_code == 401, (
            f'expected 401 from the ownership check, got {response.status_code}'
        )

    def test_an_ordinary_video_post_still_refuses_a_non_owner_with_401(self, app, edit_scenario):
        """The over-correction guard for the route: a fix that made the form
        builder swallow everything, or that moved the ownership check, would
        change this answer too. It is 401 before the fix and after it.
        """
        _author, viewer, with_url, _without_url = edit_scenario

        response = _get_edit(app, viewer, with_url)

        assert response.status_code == 401

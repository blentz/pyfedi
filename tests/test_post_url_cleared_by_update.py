"""What `update_post_from_activity` stores when a peer's Update removes the url.

The Links section's "no url in this Update" arm (`app/activitypub/util.py`, the
`else` of `if new_url:`) used to store `''`. It now stores `None`, matching the
column (`Post.url` is nullable with no default), matching the other
peer-supplied write in the same function (the microblog branch, ~line 2902,
which stores None for exactly this reason), and matching every post that has
never had a url.

The audit behind the change, since "align a sentinel" is the kind of edit that
quietly breaks a render path:

- Every `post.url` method call in the templates (`.endswith`, `.startswith`,
  `.replace`, `in`) sits behind a truthiness guard -- `{% if post.url %}` or
  `and post.url` -- which cannot tell `''` from `None`. There used to be one
  exception: the dillo theme's `render_video` macro
  (`app/templates/themes/dillo/post/post_teaser/_macros.html`) omitted the
  `{% if post.url %}` its main-theme counterpart
  (`app/templates/post/post_teaser/_macros.html:367`) has. That gap is now
  closed and `tests/test_dillo_video_teaser.py` is its regression suite. It was
  never reachable from HERE in any case: the macro runs only for
  `post.type == POST_TYPE_VIDEO`, and this branch sets POST_TYPE_ARTICLE on the
  line above.
- Every bare `{{ post.url }}` interpolation is gated on
  `post.type == POST_TYPE_LINK`/`VIDEO`/`IMAGE`, likewise excluded by the
  POST_TYPE_ARTICLE on the preceding line.
- Every Python consumer reads it for truth (`if post.url:`). The three that do
  not -- `app/shared/tasks/pages.py:179` and `:316`, `app/post/routes.py:1027`
  -- are all gated on POST_TYPE_LINK/VIDEO too.

Two behaviours are pinned below, and only the second discriminates a fix that
merely swapped the literal for a differently-wrong one.
"""

import pytest

from app import db
from app.constants import POST_TYPE_ARTICLE, POST_TYPE_LINK
from app.activitypub.util import update_post_from_activity
from app.models import File, Post
from tests.factories import make_community, make_instance, make_post, make_user

pytestmark = pytest.mark.usefixtures('site')

AP_ID = 'https://urlclear.example/notes/1'


def _update(post, activity_id, name='a post'):
    update_post_from_activity(post, {
        'id': activity_id,
        'object': {
            'id': post.ap_id,
            'type': 'Note',
            'name': name,
            'content': '<p>body</p>',
            'mediaType': 'text/html',
        },
    })


@pytest.fixture
def linked_post(db_session):
    """A remote POST_TYPE_LINK post carrying a url, ready to receive an Update."""
    make_instance('test.piefed.local', software='piefed')
    author = make_user(make_instance('urlclear.example'), 'urlclearauthor')
    community = make_community('urlclearfed')
    post = make_post(community, author, AP_ID, title='a post')
    post.url = 'https://urlclear.example/an-article'
    post.type = POST_TYPE_LINK
    db.session.commit()
    return post


def test_an_update_that_removes_the_url_stores_none(app, db_session, linked_post):
    """A post whose url was removed is indistinguishable from one that never had one.

    `make_post` leaves `url` at NULL, so `never_had_one.url` is the column's own
    "no url" and this asserts equality with it rather than restating the
    literal.
    """
    never_had_one = make_post(linked_post.community, linked_post.author,
                              'https://urlclear.example/notes/2', title='no url here')
    assert never_had_one.url is None

    _update(linked_post, 'https://urlclear.example/activities/1')

    stored = db.session.query(Post).filter_by(ap_id=AP_ID).one()
    assert stored.url is never_had_one.url
    assert stored.type == POST_TYPE_ARTICLE


def test_a_repeated_url_removing_update_does_not_re_enter_the_branch(
        app, db_session, linked_post):
    """The arm is idempotent, which `''` did not make it.

    `new_url` is initialised to None for every non-Event type, so a stored `''`
    made `old_url != new_url` true again on the NEXT Update and re-ran this
    whole arm: `post.image_id = None`, `calculate_cross_posts(delete_only=True)`,
    and the deletion of the old File row at the end of the function.

    Giving the post an image between the two Updates is what makes that
    observable. With `None` stored the second Update skips the arm and the image
    survives; with `''` it is cleared and its File row deleted.
    """
    _update(linked_post, 'https://urlclear.example/activities/1')
    assert db.session.query(Post).filter_by(ap_id=AP_ID).one().url is None

    image = File(source_url='https://urlclear.example/pic.png', file_name='pic.png')
    db.session.add(image)
    db.session.commit()
    image_id = image.id
    post = db.session.query(Post).filter_by(ap_id=AP_ID).one()
    post.image_id = image_id
    db.session.commit()

    _update(linked_post, 'https://urlclear.example/activities/2')

    stored = db.session.query(Post).filter_by(ap_id=AP_ID).one()
    assert stored.image_id == image_id
    assert db.session.query(File).filter_by(id=image_id).count() == 1

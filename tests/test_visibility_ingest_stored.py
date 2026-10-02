import pytest

from tests.factories import make_community, make_instance, make_post, make_site, make_user
from tests.test_visibility_ingest import FOLLOWERS, PUBLIC, note_activity


@pytest.fixture
def author(db_session):
    return make_user(make_instance('m.example'), 'alice')


def test_public_post_stores_public(db_session, author):
    from app.activitypub.util import create_post
    make_site()
    post = create_post(False, make_community(), note_activity([PUBLIC], [FOLLOWERS]), author)
    assert post.visibility == 'public'


def test_unlisted_post_stores_unlisted(db_session, author):
    from app.activitypub.util import create_post
    make_site()
    post = create_post(False, make_community(), note_activity([FOLLOWERS], [PUBLIC]), author)
    assert post.visibility == 'unlisted'


def test_unlisted_reply_stores_unlisted(db_session, author):
    from app.activitypub.util import create_post_reply
    make_site()
    community = make_community()
    parent = make_post(community, author, 'https://m.example/users/alice/statuses/9')
    activity = note_activity([FOLLOWERS], [PUBLIC])
    activity['object']['inReplyTo'] = parent.ap_id
    reply = create_post_reply(False, community, parent.ap_id, activity, author)
    assert reply.visibility == 'unlisted'


def test_update_cannot_widen_visibility(db_session, author):
    """Review focus 2: a hostile Update re-addressed to Public leaves visibility alone."""
    from app import db
    from app.activitypub.util import create_post, update_post_from_activity
    make_site()
    post = create_post(False, make_community(), note_activity([FOLLOWERS], [PUBLIC]), author)
    assert post.visibility == 'unlisted'
    update = note_activity([PUBLIC], [])
    update['type'] = 'Update'
    update['object']['content'] = '<p>edited</p>'
    update_post_from_activity(post, update)
    db.session.refresh(post)
    assert post.visibility == 'unlisted'

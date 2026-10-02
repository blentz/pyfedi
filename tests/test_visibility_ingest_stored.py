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


def test_reply_update_cannot_widen_visibility(db_session, author):
    """Deferred from Task 2: an Update re-addressed to Public leaves a followers-only reply alone."""
    from app import db
    from app.activitypub.util import create_post_reply, update_post_reply_from_activity
    make_site()
    community = make_community()
    parent = make_post(community, author, 'https://m.example/users/alice/statuses/9')
    activity = note_activity([FOLLOWERS], [])
    activity['object']['inReplyTo'] = parent.ap_id
    reply = create_post_reply(False, community, parent.ap_id, activity, author)
    assert reply.visibility == 'followers'
    update = note_activity([PUBLIC], [])
    update['type'] = 'Update'
    update['object']['content'] = '<p>edited</p>'
    update_post_reply_from_activity(reply, update)
    db.session.refresh(reply)
    assert reply.visibility == 'followers'


def test_followers_only_mention_notifies_only_the_follower(app, db_session):
    """Post.new must not hand a followers-only body to a mentioned non-follower."""
    from flask import current_app
    from app.activitypub.util import create_post
    from app.models import Notification
    from tests.factories import make_follow, make_visibility_world
    w = make_visibility_world()
    for u in (w.follower, w.stranger):
        u.ap_profile_id = f"{current_app.config['SERVER_URL']}/u/{u.user_name}".lower()
    db_session.commit()
    activity = note_activity([FOLLOWERS], [])
    activity['object']['tag'] = [{'type': 'Mention', 'href': u.ap_profile_id} for u in (w.follower, w.stranger)]
    post = create_post(False, w.community, activity, w.author)
    assert post.visibility == 'followers'
    notified = {n.user_id for n in Notification.query.filter_by(subtype='post_mention').all()}
    assert notified == {w.follower.id}

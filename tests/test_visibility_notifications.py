"""Notifications about followers-only content reach permitted viewers only (D6, Task 9)."""
import contextlib
from datetime import timedelta

import pytest
from flask import current_app

from app import cli, db
from app.activitypub.util import (create_post_reply, notify_about_post_reply, notify_about_post_task,
                                  update_post_from_activity, update_post_reply_from_activity)
from app.constants import NOTIF_COMMUNITY, NOTIF_FEED, NOTIF_POST, NOTIF_REPLY, NOTIF_TOPIC, NOTIF_USER
from app.models import Notification, PostReply, Site, Topic
from app.utils import utcnow
from tests.factories import (make_community_member, make_feed, make_feed_item, make_follow,
                             make_notification_subscription, make_post_reply, make_user, make_visibility_world)


class _LockOnlyRedis:
    def lock(self, *args, **kwargs):
        return contextlib.nullcontext()


@pytest.fixture
def world(app, db_session, monkeypatch):
    monkeypatch.setattr('app.redis_client', _LockOnlyRedis())
    return make_visibility_world()


def _notified(user):
    return Notification.query.filter_by(user_id=user.id).count()


def test_followers_only_post_notifies_a_follower_but_not_a_stranger(world):
    w = world
    for user in (w.follower, w.stranger, w.pending):
        make_notification_subscription(user, w.community.id, NOTIF_COMMUNITY)

    notify_about_post_task(w.post.id)

    assert _notified(w.follower) == 1
    assert _notified(w.stranger) == 0
    assert _notified(w.pending) == 0  # a pending follow unlocks nothing


def test_public_post_still_notifies_a_stranger(world):
    w = world
    make_notification_subscription(w.stranger, w.community.id, NOTIF_COMMUNITY)

    notify_about_post_task(w.public_post.id)

    assert _notified(w.stranger) == 1


def test_followers_only_post_by_a_followed_user_notifies_a_follower_but_not_a_stranger(world):
    w = world
    for user in (w.follower, w.stranger, w.pending):
        make_notification_subscription(user, w.author.id, NOTIF_USER)

    notify_about_post_task(w.post.id)

    assert _notified(w.follower) == 1
    assert _notified(w.stranger) == 0
    assert _notified(w.pending) == 0


def test_followers_only_post_in_a_followed_topic_notifies_a_follower_but_not_a_stranger(world):
    w = world
    topic = Topic(name='news', machine_name='news', num_communities=1, show_posts_in_children=False)
    db.session.add(topic)
    db.session.commit()
    w.community.topic_id = topic.id
    db.session.commit()
    for user in (w.follower, w.stranger, w.pending):
        make_notification_subscription(user, topic.id, NOTIF_TOPIC)

    notify_about_post_task(w.post.id)

    assert _notified(w.follower) == 1
    assert _notified(w.stranger) == 0
    assert _notified(w.pending) == 0


def test_followers_only_post_in_a_followed_feed_notifies_a_follower_but_not_a_stranger(world):
    w = world
    feed = make_feed(w.author.instance, 'newsfeed', local=True)
    make_feed_item(feed, w.community)
    for user in (w.follower, w.stranger, w.pending):
        make_notification_subscription(user, feed.id, NOTIF_FEED)

    notify_about_post_task(w.post.id)

    assert _notified(w.follower) == 1
    assert _notified(w.stranger) == 0
    assert _notified(w.pending) == 0


def test_followers_only_top_level_reply_notifies_a_follower_but_not_a_stranger(world):
    w = world
    for user in (w.follower, w.stranger):
        make_notification_subscription(user, w.public_post.id, NOTIF_POST)

    notify_about_post_reply(None, w.reply)

    assert _notified(w.follower) == 1
    assert _notified(w.stranger) == 0


def test_a_reply_is_judged_by_its_own_visibility_not_its_posts(world):
    w = world
    w.public_post.visibility = 'followers'
    w.reply.visibility = 'public'
    db.session.commit()
    make_notification_subscription(w.stranger, w.public_post.id, NOTIF_POST)

    notify_about_post_reply(None, w.reply)

    assert _notified(w.stranger) == 1


def test_followers_only_reply_to_a_comment_notifies_a_follower_but_not_a_stranger(world):
    w = world
    child = make_post_reply(w.public_post, w.author, 'followers-only answer')
    child.visibility = 'followers'
    child.parent_id = w.public_child.id
    db.session.commit()
    for user in (w.follower, w.stranger):
        make_notification_subscription(user, w.public_child.id, NOTIF_REPLY)

    notify_about_post_reply(w.public_child, child)

    assert _notified(w.follower) == 1
    assert _notified(w.stranger) == 0


def test_the_unread_digest_lists_public_posts_only_even_for_a_follower(world, monkeypatch):
    w = world
    site = db.session.get(Site, 1)
    site.name = 'Test'
    w.follower.email = 'fran@example.test'
    w.follower.email_unread = True
    w.follower.email_unread_sent = False
    w.follower.last_seen = utcnow() - timedelta(days=2)
    make_community_member(w.follower, w.community)
    w.post.title = 'FOLLOWERS-ONLY-TITLE'
    w.post.posted_at = utcnow()
    w.public_post.title = 'PUBLIC-TITLE'
    w.public_post.posted_at = utcnow()
    db.session.add(Notification(user_id=w.follower.id, title='a notification', url='/x', notif_type=NOTIF_POST,
                                created_at=utcnow() - timedelta(hours=1)))
    db.session.commit()
    sent = []
    monkeypatch.setattr('app.cli.send_email', lambda *a, **k: sent.append(k['html_body']))
    cli.register(current_app)

    result = current_app.test_cli_runner().invoke(args=['send_missed_notifs'])

    assert result.exception is None, result.exception
    assert len(sent) == 1
    assert 'PUBLIC-TITLE' in sent[0]
    assert 'FOLLOWERS-ONLY-TITLE' not in sent[0]


# --- mention notifications are gated like every other notification ---------

def _local_recipient(name, follows=None):
    """A new local user of its own (never a fixture user, whose follows would decide the outcome), optionally an
    accepted follower of `follows`."""
    user = make_user(None, name, local=True)
    user.ap_profile_id = f'https://test.piefed.local/u/{name}'
    db.session.commit()
    if follows is not None:
        make_follow(user, follows)
    return user


def _mention(user):
    return {'type': 'Mention', 'href': user.ap_profile_id}


def test_a_followers_only_reply_edit_mentions_only_a_follower(world):
    w = world
    w.reply.instance.software = 'lemmy'
    follower = _local_recipient('mia', follows=w.author)
    stranger = _local_recipient('gus')

    update_post_reply_from_activity(w.reply, {'object': {'content': 'hi', 'tag': [_mention(follower), _mention(stranger)]}})

    assert _notified(follower) == 1
    assert _notified(stranger) == 0


def test_a_followers_only_post_edit_mentions_only_a_follower(world):
    w = world
    follower, stranger = _local_recipient('mia', follows=w.author), _local_recipient('gus')
    update_post_from_activity(w.post, {'object': {'name': 't', 'content': 'x', 'type': 'Note',
                                                  'tag': [_mention(follower), _mention(stranger)]}})

    assert _notified(follower) == 1
    assert _notified(stranger) == 0


def test_a_created_followers_only_reply_mentions_only_a_follower(world, monkeypatch):
    w = world
    w.author.instance.software = 'lemmy'
    original = PostReply.new.__func__

    def new_followers_only(cls, *args, **kwargs):
        reply = original(cls, *args, **kwargs)
        reply.visibility = 'followers'
        db.session.commit()
        return reply

    monkeypatch.setattr(PostReply, 'new', classmethod(new_followers_only))
    follower, stranger = _local_recipient('mia', follows=w.author), _local_recipient('gus')
    document = {'id': 'https://m.example/create/1', 'object': {
        'id': 'https://m.example/note/1', 'type': 'Note', 'content': '<p>hello</p>',
        'to': ['https://www.w3.org/ns/activitystreams#Public'], 'cc': [],
        'attributedTo': w.author.ap_profile_id, 'inReplyTo': w.public_post.ap_id,
        'tag': [_mention(follower), _mention(stranger)]}}

    reply = create_post_reply(False, w.community, w.public_post.ap_id, document, w.author)

    assert reply is not None and reply.visibility == 'followers'
    assert _notified(follower) == 1
    assert _notified(stranger) == 0


def test_a_local_followers_only_post_mentions_only_a_follower(db_session):
    from tests.test_shared_tasks_send_post import _seed, _send
    s = _seed(body='hello @fran@test.piefed.local and @sam@test.piefed.local')
    follower = make_user(s.instance, 'fran', local=True)
    stranger = make_user(s.instance, 'sam', local=True)
    make_follow(follower, s.user)
    s.post.visibility = 'followers'
    db.session.commit()

    _send(s.post)

    assert _notified(follower) == 1
    assert _notified(stranger) == 0


def test_a_local_followers_only_reply_mentions_only_a_follower(db_session):
    from tests.test_shared_tasks_send_reply import _seed, _send
    s = _seed(body='hello @fran@test.piefed.local and @sam@test.piefed.local')
    follower = make_user(s.instance, 'fran', local=True)
    stranger = make_user(s.instance, 'sam', local=True)
    make_follow(follower, s.reply.author)
    s.reply.visibility = 'followers'
    db.session.commit()

    _send(s)

    assert _notified(follower) == 1
    assert _notified(stranger) == 0

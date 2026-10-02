"""Notifications about followers-only content reach permitted viewers only (D6, Task 9)."""
import contextlib
from datetime import timedelta

import pytest
from flask import current_app

from app import cli, db
from app.activitypub.util import notify_about_post_reply, notify_about_post_task
from app.constants import NOTIF_COMMUNITY, NOTIF_POST, NOTIF_REPLY
from app.models import Notification, Site
from app.utils import utcnow
from tests.factories import (make_community_member, make_notification_subscription,
                             make_post_reply, make_visibility_world)


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

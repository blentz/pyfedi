"""D1 (residuals WP-D): a notification, subscription name or page title that interpolates a post's title or a reply's
body names a neutral "a followers-only post" for a recipient who may not view that object."""
import contextlib
from datetime import timedelta

import pytest
from flask import current_app

from app import cli, db
from app.activitypub.routes import process_question_answer
from app.activitypub.util import notify_about_post_reply
from app.constants import NOTIF_POST, NOTIF_REPLY, SRC_API
from app.models import Notification, NotificationSubscription, Reminder
from app.shared.reply import choose_answer, subscribe_reply
from app.utils import utcnow
from app.visibility import post_title_for
from tests.factories import bearer, make_community_member, make_notification_subscription, make_post_reply, make_visibility_world
from tests.test_visibility_single_object import client_as

SECRET = 'SECRET-TITLE'
NEUTRAL = 'a followers-only post'


class _LockOnlyRedis:
    def lock(self, *args, **kwargs):
        return contextlib.nullcontext()


@pytest.fixture
def world(app, db_session, monkeypatch):
    monkeypatch.setattr('app.redis_client', _LockOnlyRedis())
    w = make_visibility_world()
    w.post.title = SECRET
    db.session.commit()
    return w


def _public_reply_on_hidden_post(w, user, body='a public answer'):
    reply = make_post_reply(w.post, user, body)
    reply.ap_id = f'https://piefed.test/comment/{reply.id}'
    db.session.commit()
    return reply


def _only_notification(user):
    return Notification.query.filter_by(user_id=user.id).one()


def test_post_title_for_names_the_title_only_to_a_viewer(world):
    w = world
    assert post_title_for(w.post, w.follower.id) == SECRET
    assert post_title_for(w.post, w.stranger.id) == NEUTRAL
    assert post_title_for(w.post, None) == NEUTRAL
    assert post_title_for(w.public_post, w.stranger.id) == w.public_post.title


def test_a_top_level_reply_notification_does_not_name_a_hidden_post(world):
    w = world
    reply = _public_reply_on_hidden_post(w, w.author)
    for user in (w.stranger, w.follower):
        make_notification_subscription(user, w.post.id, NOTIF_POST)

    notify_about_post_reply(None, reply)

    hidden = _only_notification(w.stranger)
    assert SECRET not in hidden.title and NEUTRAL in hidden.title
    assert hidden.targets['post_title'] == NEUTRAL
    shown = _only_notification(w.follower)
    assert SECRET in shown.title and shown.targets['post_title'] == SECRET


def test_a_comment_reply_notification_does_not_name_a_hidden_post_or_parent(world):
    w = world
    parent = _public_reply_on_hidden_post(w, w.stranger)
    child = make_post_reply(w.post, w.author, 'the answer')
    child.parent_id = parent.id
    db.session.commit()
    make_notification_subscription(w.stranger, parent.id, NOTIF_REPLY)

    notify_about_post_reply(parent, child)

    hidden = _only_notification(w.stranger)
    assert SECRET not in hidden.title and NEUTRAL in hidden.title


def test_a_comment_reply_notification_does_not_carry_a_hidden_parent_body(world):
    w = world
    child = make_post_reply(w.public_post, w.author, 'public answer')
    child.parent_id = w.reply.id
    db.session.commit()
    make_notification_subscription(w.stranger, w.reply.id, NOTIF_REPLY)
    make_notification_subscription(w.follower, w.reply.id, NOTIF_REPLY)

    notify_about_post_reply(w.reply, child)

    assert _only_notification(w.stranger).targets['parent_reply_body'] == ''
    assert _only_notification(w.follower).targets['parent_reply_body'] == 'secret reply'


def test_subscribing_to_a_reply_names_a_hidden_post_neutrally(world):
    w = world
    reply = _public_reply_on_hidden_post(w, w.stranger)

    subscribe_reply(reply.id, True, SRC_API, bearer(w.stranger))

    name = NotificationSubscription.query.filter_by(user_id=w.stranger.id, type=NOTIF_REPLY).one().name
    assert SECRET not in name and NEUTRAL in name


def test_a_chosen_answer_notification_names_a_hidden_post_neutrally(world, monkeypatch):
    w = world
    monkeypatch.setattr('app.shared.reply.task_selector', lambda *a, **k: None)
    w.author.unread_notifications = 0
    w.stranger.unread_notifications = 0
    reply = _public_reply_on_hidden_post(w, w.stranger)
    make_community_member(w.pending, w.community, is_moderator=True)  # a local chooser; the API refuses remote users

    choose_answer(reply.id, SRC_API, bearer(w.pending))

    notification = _only_notification(w.stranger)
    assert SECRET not in notification.title and NEUTRAL in notification.title
    assert notification.targets['post_title'] == NEUTRAL


def test_a_federated_chosen_answer_names_a_hidden_post_neutrally(world):
    w = world
    w.stranger.unread_notifications = 0
    reply = _public_reply_on_hidden_post(w, w.stranger)
    make_community_member(w.author, w.community, is_moderator=True)

    process_question_answer(w.author, False, {'id': 'https://m.example/a/1', 'object': {'object': reply.ap_id}}, True)

    assert _only_notification(w.stranger).targets['post_title'] == NEUTRAL


@pytest.mark.parametrize('reminder_type', [1, 2])
def test_a_reminder_names_a_post_that_became_hidden_neutrally(world, reminder_type):
    w = world
    reply = _public_reply_on_hidden_post(w, w.stranger)
    destination = w.post.id if reminder_type == 1 else reply.id
    db.session.add(Reminder(user_id=w.stranger.id, remind_at=utcnow() - timedelta(minutes=1),
                            reminder_type=reminder_type, reminder_destination=destination))
    db.session.commit()
    cli.register(current_app)

    result = current_app.test_cli_runner().invoke(args=['reminders'])

    assert result.exception is None, result.exception
    title = _only_notification(w.stranger).title
    assert SECRET not in title and NEUTRAL in title


def test_migrated_reply_subscriptions_name_a_hidden_post_neutrally(world):
    w = world
    reply = _public_reply_on_hidden_post(w, w.stranger)
    reply.notify_author = True
    db.session.commit()
    cli.register(current_app)

    result = current_app.test_cli_runner().invoke(args=['migrate_post_notifs'])

    assert result.exception is None, result.exception
    name = NotificationSubscription.query.filter_by(user_id=w.stranger.id, type=NOTIF_REPLY, entity_id=reply.id).one().name
    assert SECRET not in name and NEUTRAL in name


def test_the_reply_reminder_form_does_not_name_a_hidden_post(world, app):
    w = world
    reply = _public_reply_on_hidden_post(w, w.stranger)

    response = client_as(app, w.stranger).get(f'/post_reply/{reply.id}/reminder')

    assert response.status_code == 200
    assert SECRET.encode() not in response.data

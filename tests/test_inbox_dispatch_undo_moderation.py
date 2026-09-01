"""tests/test_inbox_dispatch_undo_moderation.py"""
import pytest
from sqlalchemy import inspect as sa_inspect

from app import db
from app.activitypub import routes as activitypub_routes
from app.models import ActivityPubLog, InstanceRole, User, utcnow
from tests.factories import (inbox_activity, make_community, make_community_member,
                             make_instance, make_post, make_post_reply, make_user,
                             seed_community_owner)
from tests.test_inbox_dispatch_lock_delete import record_moderation
from tests.test_inbox_dispatch_preamble import dispatch


def undo_lock_activity(actor, target_ap_id, **outer):
    """An Undo wrapping a Lock of `target_ap_id`."""
    return inbox_activity(actor, activity_type='Undo',
                          object={'type': 'Lock', 'object': target_ap_id}, **outer)


def _seed_lockable_post(host='peer.example'):
    """A community whose owner is also a moderator, plus a locked post."""
    instance = seed_community_owner(host)
    mod = make_user(instance, 'mod')
    community = make_community(host=host)
    make_community_member(mod, community, is_moderator=True)
    author = make_user(instance, 'author')
    post = make_post(community, author, f'https://{host}/post/1')
    post.comments_enabled = False
    db.session.commit()
    return instance, mod, community, author, post


def test_a_successful_post_unlock_also_logs_a_contradictory_failure(app, db_session, monkeypatch):
    """PINS FIX 3's defect. The `else` binds to `if post_reply:`, not to the
    pair, so a post unlock that SUCCEEDED logs APLOG_SUCCESS and then, because
    post_reply is None, immediately logs FAILURE 'Unlock: post not found' for
    the same activity. Two rows, contradicting each other.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, mod, community, author, post = _seed_lockable_post()
    record_moderation(monkeypatch, 'add_to_modlog')
    post_id = post.id

    dispatch(undo_lock_activity(mod, post.ap_id))

    db.session.expire_all()
    assert db.session.get(type(post), post_id).comments_enabled is True

    logs = ActivityPubLog.query.order_by(ActivityPubLog.id).all()
    assert len(logs) == 2                       # the defect
    assert logs[0].result == 'success'
    assert logs[1].result == 'failure'
    assert logs[1].exception_message == 'Unlock: post not found'


def test_the_comment_url_branch_selects_a_reply_directly(app, db_session, monkeypatch):
    """FIX 2. The membership test now runs against `target_ap_id`, the string,
    so an id containing '/comment/' selects PostReply WITHOUT first trying
    Post.get_by_ap_id. Proved by seeding a Post whose ap_id is identical to the
    reply's: if the else fallback were still running it would find that post
    first and unlock IT instead.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, mod, community, author, post = _seed_lockable_post()
    decoy = make_post(community, author, 'https://peer.example/comment/1')
    decoy.comments_enabled = False
    reply = make_post_reply(post, author)
    reply.ap_id = 'https://peer.example/comment/1'
    reply.replies_enabled = False
    db.session.commit()
    decoy_id, reply_id = decoy.id, reply.id

    record_moderation(monkeypatch, 'add_to_modlog')

    dispatch(undo_lock_activity(mod, 'https://peer.example/comment/1'))

    db.session.expire_all()
    assert db.session.get(type(reply), reply_id).replies_enabled is True
    assert db.session.get(type(decoy), decoy_id).comments_enabled is False  # decoy untouched


def test_unlocking_a_comment_records_the_reply_author_and_community(app, db_session, monkeypatch):
    """FIX 1, the twin of D97. The modlog entry now reads its author and
    community off `post_reply`, not off the always-None `post`. add_to_modlog
    is doubled so the arguments can be inspected -- which is only safe now that
    evaluating them no longer raises.

    The captured kwargs belong to the dispatcher's own task session, which is
    closed by the time dispatch() returns (test_inbox_dispatch_preamble.py's
    module docstring: a row this test commits is not visible to that session,
    and the inverse holds too -- its objects don't outlive it here). Reading
    a plain attribute like `.id` off them re-triggers a load against a closed
    session and raises DetachedInstanceError, so identity is read via
    `sa_inspect(obj).identity[0]` instead, the same pattern
    test_inbox_dispatch_lock_delete.py already uses for this exact reason.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, mod, community, author, post = _seed_lockable_post()
    reply = make_post_reply(post, author)
    reply.ap_id = 'https://peer.example/comment/1'
    reply.replies_enabled = False
    db.session.commit()
    reply_id = reply.id

    calls = record_moderation(monkeypatch, 'add_to_modlog')

    dispatch(undo_lock_activity(mod, 'https://peer.example/comment/1'))

    db.session.expire_all()
    assert db.session.get(type(reply), reply_id).replies_enabled is True

    assert len(calls['add_to_modlog']) == 1
    args, kwargs = calls['add_to_modlog'][0]
    assert args[0] == 'unlock_post_reply'
    assert sa_inspect(kwargs['target_user']).identity[0] == author.id
    assert sa_inspect(kwargs['community']).identity[0] == community.id
    assert sa_inspect(kwargs['reply']).identity[0] == reply_id


def test_unlocking_without_permission_logs_failure(app, db_session, monkeypatch):
    """The permission guard, which is NOT defective. A user who is neither
    moderator nor instance admin gets FAILURE and no unlock.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, mod, community, author, post = _seed_lockable_post()
    outsider = make_user(instance, 'outsider')
    post_id = post.id

    dispatch(undo_lock_activity(outsider, post.ap_id))

    db.session.expire_all()
    assert db.session.get(type(post), post_id).comments_enabled is False  # untouched
    logs = ActivityPubLog.query.order_by(ActivityPubLog.id).all()
    assert logs[0].result == 'failure'
    assert logs[0].exception_message == 'Unlock: Does not have permission'

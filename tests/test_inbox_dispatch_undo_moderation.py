"""tests/test_inbox_dispatch_undo_moderation.py"""
import pytest

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


def test_the_post_and_comment_url_branches_are_dead(app, db_session, monkeypatch):
    """PINS FIX 2's defect. `'/post/' in core_activity['object']` tests the
    DICT's keys, not the target string, so it can never match however the
    object id is spelled. Proved by giving the inner object an id containing
    '/comment/' while ALSO giving the dict a '/post/' key: if the test were
    against the string the '/comment/' branch would run; if against the dict,
    the '/post/' key would match. Neither happens -- the else fallback runs and
    resolves the reply -- which is only explicable if the operand is the dict
    and neither literal is among its keys.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, mod, community, author, post = _seed_lockable_post()
    reply = make_post_reply(post, author)
    reply.ap_id = 'https://peer.example/comment/1'
    reply.replies_enabled = False
    db.session.commit()

    activity = inbox_activity(mod, activity_type='Undo',
                              object={'type': 'Lock', 'object': reply.ap_id})
    assert '/post/' not in activity['object']   # the dict has no such KEY
    assert '/comment/' not in activity['object']

    record_moderation(monkeypatch, 'add_to_modlog')

    with pytest.raises(AttributeError):
        dispatch(activity)


def test_unlocking_a_comment_crashes_on_a_none_post(app, db_session, monkeypatch):
    """PINS FIX 1's defect, the exact twin of D97 (fixed in b79f43f9 in the
    Lock arm's own comment branch). The reply IS unlocked and committed first,
    then `add_to_modlog(..., target_user=post.author, community=post.community)`
    dereferences `post`, which is None on this branch.

    add_to_modlog is deliberately NOT doubled here: doubling it would swallow
    the very dereference this test exists to observe, because the arguments are
    evaluated at the call site, before any double is entered. That is the
    subtlety this docstring exists to record.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, mod, community, author, post = _seed_lockable_post()
    reply = make_post_reply(post, author)
    reply.ap_id = 'https://peer.example/comment/1'
    reply.replies_enabled = False
    db.session.commit()

    with pytest.raises(AttributeError):
        dispatch(undo_lock_activity(mod, reply.ap_id))


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

"""U1 (Loops spike 2026-10-30, ruling L3, spec 2026-10-30 §3.4): Loops v1.0.0-beta.15 drops the first activity from an
actor it has not stored yet (the new Profile is made without `status`, so `status !== 1` returns early), so a user's
first Follow of a Loops account stayed pending. PieFed re-sends that Follow once, five minutes later, if it is still
pending; for no other software."""
import pytest

from app import db
from app.models import UserFollowRequest
import app.shared.tasks.follows as follows
from app.shared.tasks.follows import follow_user, resend_pending_follow
from tests.factories import make_follow, make_instance, make_user
from tests.test_shared_tasks_follows import (PEER_INBOX, _peer_example_resolves_without_a_resolver,  # noqa: F401
                                             _peer_route, _seed, _sent_activity)


@pytest.fixture
def scheduled(monkeypatch):
    calls = []
    monkeypatch.setattr(follows.resend_pending_follow, 'apply_async',
                        lambda args, countdown: calls.append((args, countdown)))
    return calls


def _target(software):
    peer = make_instance('peer.example', software=software)
    target = make_user(peer, 'lucy')
    target.ap_inbox_url = PEER_INBOX
    db.session.commit()
    return target


def test_a_follow_of_a_loops_account_schedules_one_resend(db_session, http_mock, scheduled):
    s = _seed(with_keys=True)
    target = _target('loops')
    _peer_route(http_mock)

    follow_user(target.id, s.user.id, send_async=False)

    request = db.session.query(UserFollowRequest).one()
    assert scheduled == [((target.id, s.user.id, str(request.uuid)), 300)]


@pytest.mark.parametrize('software', ['mastodon', 'pixelfed', None])
def test_no_other_software_gets_a_resend(db_session, http_mock, scheduled, software):
    s = _seed(with_keys=True)
    target = _target(software)
    _peer_route(http_mock)

    follow_user(target.id, s.user.id, send_async=False)

    assert scheduled == []


def _pending(is_accepted=None):
    s = _seed(with_keys=True)
    target = _target('loops')
    make_follow(s.user, target, is_accepted=is_accepted)
    request = UserFollowRequest(user_id=s.user.id, follow_id=target.id)
    db.session.add(request)
    db.session.commit()
    return s, target, request


def test_a_still_pending_follow_is_sent_again_under_the_same_id(db_session, http_mock):
    s, target, request = _pending()
    route = _peer_route(http_mock)

    resend_pending_follow(target.id, s.user.id, str(request.uuid))

    follow = _sent_activity(route)
    assert follow['type'] == 'Follow' and follow['object'] == target.public_url()
    assert follow['id'].endswith(str(request.uuid)) and follow['actor'] == s.user.public_url()
    assert len(route.calls) == 1


@pytest.mark.parametrize('is_accepted', [True, False], ids=['accepted', 'refused'])
def test_an_answered_follow_is_not_sent_again(db_session, http_mock, is_accepted):
    s, target, request = _pending(is_accepted)

    resend_pending_follow(target.id, s.user.id, str(request.uuid))   # http_mock would fail on any request


def test_an_unfollowed_account_is_not_sent_again(db_session, http_mock):
    s, target, request = _pending()
    uuid = str(request.uuid)
    db.session.delete(request)
    db.session.commit()

    resend_pending_follow(target.id, s.user.id, uuid)


def test_a_follow_redone_since_is_not_sent_again_under_the_old_id(db_session, http_mock):
    s, target, request = _pending()

    resend_pending_follow(target.id, s.user.id, '00000000-0000-0000-0000-000000000000')


def test_a_follow_with_no_follower_row_is_not_sent_again(db_session, http_mock):
    s = _seed(with_keys=True)
    target = _target('loops')
    request = UserFollowRequest(user_id=s.user.id, follow_id=target.id)
    db.session.add(request)
    db.session.commit()

    resend_pending_follow(target.id, s.user.id, str(request.uuid))


def test_a_resend_to_an_account_gone_since_does_nothing(db_session, http_mock):
    s, target, request = _pending()
    target.instance.gone_forever = True
    db.session.commit()

    resend_pending_follow(target.id, s.user.id, str(request.uuid))

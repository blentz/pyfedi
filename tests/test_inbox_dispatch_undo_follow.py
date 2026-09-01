"""tests/test_inbox_dispatch_undo_follow.py"""
from datetime import timedelta

from app import db
from app.models import ActivityPubLog, CommunityJoinRequest, CommunityMember, utcnow
from tests.factories import (inbox_activity, make_community, make_community_join_request,
                             make_community_member, make_instance, make_user,
                             seed_community_owner)
from tests.test_inbox_dispatch_preamble import dispatch


def undo_follow_activity(actor, target_ap_id):
    """An Undo whose inner object is a Follow of `target_ap_id`.

    `inbox_activity` applies **fields last, so passing `object=` replaces its
    default string object with the dict the arm requires -- the arm dispatches
    on `core_activity['object']['type']`, which a string could not satisfy.
    """
    return inbox_activity(actor, activity_type='Undo',
                          object={'type': 'Follow', 'object': target_ap_id})


def test_undo_follow_of_a_community_removes_membership_and_join_request(app, db_session, monkeypatch):
    """Both `if member:` and `if join_request:` fire. subscriptions_count is
    seeded to 5 rather than left at its default so the decrement to 4 is
    evidence of the write, not of a default sitting there unexamined.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = seed_community_owner('peer.example')
    follower = make_user(instance, 'follower')
    community = make_community(host='peer.example')
    make_community_member(follower, community)
    make_community_join_request(follower, community)
    community.subscriptions_count = 5
    stale = utcnow() - timedelta(days=3)
    community.last_active = stale
    follower.last_seen = stale
    db.session.commit()
    community_id, follower_id = community.id, follower.id

    dispatch(undo_follow_activity(follower, community.ap_profile_id))

    db.session.expire_all()
    assert db.session.query(CommunityMember).filter_by(
        user_id=follower_id, community_id=community_id).first() is None
    assert db.session.query(CommunityJoinRequest).filter_by(
        user_id=follower_id, community_id=community_id).first() is None
    fresh = db.session.get(type(community), community_id)
    assert fresh.subscriptions_count == 4
    assert fresh.last_active > stale
    assert db.session.get(type(follower), follower_id).last_seen > stale

    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_undo_follow_of_a_community_with_no_membership_leaves_the_count_alone(app, db_session, monkeypatch):
    """`if member:` is false, so the decrement and both timestamps are skipped
    -- proving the guard is load-bearing rather than decoration. The join
    request is still present and still deleted, which is what distinguishes
    the two independent `if`s from a single combined one.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = seed_community_owner('peer.example')
    follower = make_user(instance, 'follower')
    community = make_community(host='peer.example')
    make_community_join_request(follower, community)
    community.subscriptions_count = 5
    stale = utcnow() - timedelta(days=3)
    community.last_active = stale
    db.session.commit()
    community_id, follower_id = community.id, follower.id

    dispatch(undo_follow_activity(follower, community.ap_profile_id))

    db.session.expire_all()
    fresh = db.session.get(type(community), community_id)
    assert fresh.subscriptions_count == 5          # untouched
    assert fresh.last_active == stale              # untouched
    assert db.session.query(CommunityJoinRequest).filter_by(
        user_id=follower_id, community_id=community_id).first() is None

    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_undo_follow_of_a_community_with_no_join_request_still_removes_membership(app, db_session, monkeypatch):
    """The mirror of the test above: `if join_request:` false, `if member:`
    true. Together the pair kills either `if` being dropped.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = seed_community_owner('peer.example')
    follower = make_user(instance, 'follower')
    community = make_community(host='peer.example')
    make_community_member(follower, community)
    community.subscriptions_count = 5
    db.session.commit()
    community_id, follower_id = community.id, follower.id

    dispatch(undo_follow_activity(follower, community.ap_profile_id))

    db.session.expire_all()
    assert db.session.query(CommunityMember).filter_by(
        user_id=follower_id, community_id=community_id).first() is None
    assert db.session.get(type(community), community_id).subscriptions_count == 4

    log = ActivityPubLog.query.one()
    assert log.result == 'success'

"""tests/test_inbox_dispatch_undo_follow.py"""
from datetime import timedelta

from app import db
from app.models import (ActivityPubLog, CommunityJoinRequest, CommunityMember,
                        FeedJoinRequest, FeedMember, UserFollower, utcnow)
from app.activitypub import routes as activitypub_routes
from tests.factories import (inbox_activity, make_community, make_community_join_request,
                             make_community_member, make_feed, make_feed_join_request,
                             make_feed_member, make_follow, make_instance, make_user,
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


def test_undo_follow_of_a_feed_removes_membership_and_join_request(app, db_session, monkeypatch):
    """The feed branch mirrors the community branch but stamps NO timestamps --
    asserted explicitly below, because the omission is the interesting
    difference between the two branches.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    follower = make_user(instance, 'follower')
    feed = make_feed(instance, 'news')
    make_feed_member(follower, feed)
    make_feed_join_request(follower, feed)
    feed.subscriptions_count = 5
    stale = utcnow() - timedelta(days=3)
    follower.last_seen = stale
    db.session.commit()
    feed_id, follower_id = feed.id, follower.id

    dispatch(undo_follow_activity(follower, feed.ap_profile_id))

    db.session.expire_all()
    assert db.session.query(FeedMember).filter_by(user_id=follower_id, feed_id=feed_id).first() is None
    assert db.session.query(FeedJoinRequest).filter_by(user_id=follower_id, feed_id=feed_id).first() is None
    assert db.session.get(type(feed), feed_id).subscriptions_count == 4
    assert db.session.get(type(follower), follower_id).last_seen == stale  # NOT stamped

    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_undo_follow_of_a_local_user_deletes_an_accepted_follower(app, db_session, monkeypatch):
    """The user branch. `make_follow(local_user, remote_user, is_accepted=True)`
    matches the filter the branch applies.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    remote = make_user(instance, 'remote')
    local = make_user(None, 'local', local=True)
    local.ap_profile_id = f"{app.config['SERVER_URL']}/u/local".lower()
    db.session.commit()
    make_follow(local, remote, is_accepted=True)
    local_id, remote_id = local.id, remote.id

    dispatch(undo_follow_activity(remote, local.ap_profile_id))

    db.session.expire_all()
    assert db.session.query(UserFollower).filter_by(
        local_user_id=local_id, remote_user_id=remote_id).first() is None

    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_undo_follow_of_a_user_with_a_pending_follow_logs_nothing(app, db_session, monkeypatch):
    """`is_accepted=True` is part of the filter, so a PENDING follow does not
    match and the branch returns having logged NOTHING -- there is no
    log_incoming_ap call outside the `if follower:` block. Asserted with
    LOG_ACTIVITYPUB_TO_DB explicitly True, so the zero is a real silence and
    not an artifact of logging being off.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    remote = make_user(instance, 'remote')
    local = make_user(None, 'local', local=True)
    local.ap_profile_id = f"{app.config['SERVER_URL']}/u/local".lower()
    db.session.commit()
    make_follow(local, remote, is_accepted=False)
    local_id, remote_id = local.id, remote.id

    dispatch(undo_follow_activity(remote, local.ap_profile_id))

    db.session.expire_all()
    assert db.session.query(UserFollower).filter_by(
        local_user_id=local_id, remote_user_id=remote_id).first() is not None  # survives
    assert ActivityPubLog.query.count() == 0


def test_undo_follow_of_an_unresolvable_target_logs_failure(app, db_session, monkeypatch):
    """`find_actor_or_create_cached` is doubled to return None so the arm
    reaches `if not target:`. Doubling is necessary rather than convenient: an
    undoubled lookup of an unknown URL attempts a real fetch, which
    block_outbound_http turns into a respx error -- an INFRASTRUCTURE failure
    that would look like a behavioural one.

    The double is scoped to the unresolvable target URL only, and delegates to
    the real function for every other call. process_inbox_request's own actor
    resolution (routes.py:871, resolving the OUTER activity's signed actor --
    `follower` here) calls this same module-level function before the Undo/
    Follow arm is ever reached; a blanket `lambda *a, **k: None` double breaks
    that earlier call too, so it never reaches the branch under test and
    instead logs 'Actor was not a user or a community' from routes.py:891.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    follower = make_user(instance, 'follower')
    target_ap_id = 'https://peer.example/c/gone'
    real_find_actor = activitypub_routes.find_actor_or_create_cached

    def fake_find_actor(actor, *args, **kwargs):
        if actor == target_ap_id:
            return None
        return real_find_actor(actor, *args, **kwargs)

    monkeypatch.setattr(activitypub_routes, 'find_actor_or_create_cached', fake_find_actor)

    dispatch(undo_follow_activity(follower, target_ap_id))

    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Unfound target'

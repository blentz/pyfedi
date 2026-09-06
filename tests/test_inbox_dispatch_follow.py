"""Sub-project 5b, Task 1 -- shared fixtures for the Follow, Accept and Reject
arms (`record_sends`), and the Follow arm's outcome table.

`record_sends` is consumed by Tasks 2-4 (the three Follow target branches):
every `send_post_request` call in this sub-project's scope -- routes.py:962,
980, 999, 1014, 1045 -- sits inside the Follow arm. Accept (1075-1148) and
Reject (1150-1191) make no outbound sends at all, so nothing downstream of
Follow needs this fixture.

The table below is derived directly from routes.py:935-1073, read line by
line for this task -- not copied from the plan or the design spec. It was
cross-checked against both afterward and no disagreement turned up; three
observations below (the User branch's silent refusals, the shared-log-call
nuance on the auto-accept split, and the if/elif vs. plain-`or` contrast
between Community's and User's guards) are not called out in either document
and are recorded here because they surfaced during the re-derivation, which
is the point of doing it independently.

Target resolution happens once, at :938
(`find_actor_or_create_cached(target_ap_id)`); which of the three branches
below runs is decided entirely by what that lookup returns.

| branch (lines)                                    | selects it                                                                                                    | writes                                                                                                                    | sends                          | logs |
|-----------------------------------------------------|----------------------------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------------------|---------------------------------|------|
| unfound target (939-941)                           | `find_actor_or_create_cached` returns falsy                                                                    | nothing                                                                                                                     | nothing                          | APLOG_FOLLOW/APLOG_FAILURE 'Could not find target of Follow' |
| Community, local_only (945-947, 954-962)           | `community.local_only` is True                                                                                 | nothing                                                                                                                     | Reject                           | APLOG_FOLLOW/APLOG_FAILURE 'Local only cannot be followed by remote users' -- BEFORE the send |
| Community, banned (949-953, 954-962)               | `community.local_only` is False AND a `CommunityBan(user_id, community_id)` row exists -- checked only when NOT local_only; this is if/elif, not two independently-evaluated alternatives | nothing                                                                                                                     | Reject                           | APLOG_FOLLOW/APLOG_FAILURE 'Remote user has been banned' -- BEFORE the send |
| Community, new member (964-981)                    | not rejected, and no existing `CommunityMember(user_id, community_id)`                                          | `CommunityMember` row; `community.subscriptions_count` +1; `community.last_active` and `user.last_seen` stamped            | Accept                           | APLOG_FOLLOW/APLOG_SUCCESS -- AFTER the send |
| Community, already a member (964-965, silent)      | `existing_member` truthy                                                                                        | nothing                                                                                                                     | nothing                           | nothing at all |
| Feed, non-public (989-999)                         | `not feed.public`                                                                                               | nothing                                                                                                                     | Reject                           | NOTHING -- no `log_incoming_ap` call anywhere on this path, unlike either Community reject reason above |
| Feed, new subscriber (1001-1016)                   | `feed_membership(user, feed) != SUBSCRIPTION_MEMBER`                                                            | `FeedMember` row; `feed.subscriptions_count` +1 (no `last_active`/`last_seen`-equivalent stamp -- Feed has no such column touched here) | Accept                           | APLOG_FOLLOW/APLOG_SUCCESS |
| Feed, already subscribed (1001, silent)            | `feed_membership(user, feed) == SUBSCRIPTION_MEMBER`                                                            | nothing                                                                                                                     | nothing                           | nothing |
| User, remote target (1021-1024)                    | `not local_user.is_local()`                                                                                     | nothing                                                                                                                     | nothing -- no Reject; this branch never sends a federated reply on any refusal | APLOG_FOLLOW/APLOG_FAILURE 'Follow request for remote user received' |
| User, blocked (1025-1027)                          | `has_blocked_user(...) or has_blocked_instance(...) or instance_banned(...)` -- a single plain `or`, all three checked unconditionally (short-circuited) and sharing ONE log message, unlike Community's if/elif with a distinct message per reason | nothing                                                                                                                     | nothing                           | APLOG_FOLLOW/APLOG_FAILURE 'Attempt to follow denied due to block' |
| User, auto-accept (1030-1071, `auto_accept` True)  | not `existing_follower`, and `local_user.ap_manually_approves_followers is False`                              | `UserFollower(is_accepted=True, is_inward=True)` via the task-local `session`; `ap_followers_url` backfilled if empty; `Notification(notif_type=NOTIF_FOLLOW)` added via `db.session` and committed separately -- a different session object than the `UserFollower` write, in the same logical write (D60's divergence made concrete); `unread_notifications` +1 | Accept                           | APLOG_FOLLOW/APLOG_SUCCESS -- the SAME log call as the manual-approval row below |
| User, manual approval (1030-1071, `auto_accept` False) | not `existing_follower`, and `ap_manually_approves_followers` anything other than the literal `False`      | `UserFollower(is_accepted=None, is_inward=True)` via `session`; `Notification(notif_type=NOTIF_FOLLOW_REQUEST)` via `db.session`; `unread_notifications` +1 | nothing                           | APLOG_FOLLOW/APLOG_SUCCESS -- logged as SUCCESS the moment the pending request is recorded, not when a human later approves it |
| User, already following (1030, silent)             | `existing_follower` truthy                                                                                      | nothing                                                                                                                     | nothing                           | nothing |

Asymmetries worth carrying into the tests that cover them:

  - Community's two reject reasons both log APLOG_FAILURE before the Reject
    is sent; Feed's one reject reason (:989) logs nothing at all, even
    though the surrounding shape (guard sets `reject_follow`, then sends a
    Reject) is otherwise identical. Registered by the design spec at
    routes.py:989-999; independently confirmed here.
  - Three "already-done" paths are uniformly silent: an existing
    `CommunityMember` (:964-965), an already-subscribed `FeedMember`
    (:1001), and an existing `UserFollower` (:1030) each write nothing,
    send nothing, and log nothing.
  - The User branch's refusals send no federated reply at all, on either
    refusal reason -- Community and Feed both notify the remote actor with
    a Reject on their own refusal paths. This asymmetry is not called out
    in the design spec's own registered-findings list.
  - `:1033`'s `is_accepted=auto_accept if auto_accept else None` can only
    ever write `True` or `None`; `UserFollower.is_accepted`'s own column
    comment (`app/models.py:3533`) documents `False` ('Rejected') as a
    third meaningful state this expression can never produce.
  - The `UserFollower` row (:1036, task-local `session`) and its
    accompanying `Notification` (:1067, `db.session`) are added and
    committed through two different session objects inside one logical
    write -- the concrete instance of 5a's D60 session divergence that this
    sub-project's own design spec calls out by line number.
"""

import pytest
from flask import current_app

from app import db
from app.activitypub import routes as activitypub_routes
from app.activitypub.signature import RsaKeys
from app.constants import NOTIF_FOLLOW, NOTIF_FOLLOW_REQUEST
from app.models import ActivityPubLog, AllowedInstances, BannedInstances, CommunityMember, \
    FeedMember, Notification, UserFollower, utcnow
from app.utils import get_setting, instance_banned, set_setting
from tests.factories import ban_user_from_community, inbox_activity, make_community, \
    make_community_member, make_feed, make_follow, make_instance, make_instance_block, \
    make_site, make_user, make_user_block
from tests.test_inbox_dispatch_preamble import dispatch


def record_sends(monkeypatch):
    """Double send_post_request at its binding site on the routes module and
    return the list it records into.

    routes.py imports send_post_request by name, so patching
    app.activitypub.signature would leave routes' copy pointing at the
    original. Same binding-site trap tests/conftest.py:442 documents.
    """
    sends = []
    monkeypatch.setattr(
        activitypub_routes, 'send_post_request',
        lambda uri, body, private_key, key_id, **kw: sends.append((uri, body, key_id)))
    return sends


# --- Task 2: Follow, Community target -- routes.py:942-981 ---


def _seed_follow_of_local_community(name='microblogs', local_only=False):
    """A local target Community (with a real RSA keypair, since routes.py:962
    and :980 sign the outbound Reject/Accept with `community.private_key`,
    which plain make_community() never sets) and a remote follower User.

    Creation order matters: make_community() hardcodes owner user_id=1 and
    instance_id=1, so the owner instance and owner user are created FIRST to
    occupy those ids -- keeping the remote follower (created second, on its
    own instance) a row distinct from the community's owner, rather than
    reusing the follower to also own the community it is following.

    The follower's `ap_fetched_at` is stamped so schedule_actor_refresh
    (app/activitypub/actor.py:134) does not fire a real actor fetch inline
    under eager Celery. The community needs no such stamp: it is LOCAL
    (default host='test.piefed.local', matching SERVER_NAME), so
    schedule_actor_refresh's own `not actor.is_local()` guard short-circuits
    before ever checking `ap_fetched_at`, and find_actor_by_url resolves it
    through find_local_community's exact-match query (app/activitypub/
    actor.py:19-21) -- a plain SELECT, no network path at all.
    """
    make_site()
    owner_instance = make_instance('owner.example')
    make_user(owner_instance, 'community_owner')
    community = make_community(name)
    community.local_only = local_only
    private_key, public_key = RsaKeys.generate_keypair()
    community.private_key = private_key
    community.public_key = public_key

    peer_instance = make_instance('peer.example')
    follower = make_user(peer_instance, 'alice')
    follower.ap_fetched_at = utcnow()

    db.session.commit()
    return follower, community


def test_a_follow_of_an_unresolvable_target_is_refused(app, db_session, monkeypatch):
    """routes.py:939-941, 'Could not find target of Follow'.

    The target ('not-a-real-target') has no '://' and no '@', so
    extract_domain_and_actor (app/activitypub/util.py:622) parses it as a
    bare path with an empty netloc, and normalise_actor_string
    (app/activitypub/util.py:4618, no '@' present) also yields an empty
    server -- validate_remote_actor (app/activitypub/actor.py:39) then
    refuses it outright before find_actor_or_create_cached's cache lookup or
    its create_if_not_found=True fallback ever run. That is the cleanest way
    to make target resolution miss without touching the network: a
    well-formed but merely-absent remote URL would instead fall through to
    create_actor_from_remote and attempt a real HTTP fetch, since
    routes.py:938 calls find_actor_or_create_cached with no
    create_if_not_found override (defaulting to True).
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    make_site()
    instance = make_instance('peer.example')
    follower = make_user(instance, 'alice')
    follower.ap_fetched_at = utcnow()
    db.session.commit()

    activity = inbox_activity(follower, activity_type='Follow', object_uri='not-a-real-target')

    dispatch(activity)

    assert ActivityPubLog.query.one().exception_message == 'Could not find target of Follow'


def test_a_follow_of_a_local_only_community_is_rejected(app, db_session, monkeypatch):
    """routes.py:945-947 sets reject_follow for a local_only community
    (independent of ban status -- this follower is never banned), then
    :956-962 sends the Reject. Asserts the log AND the full federated
    payload: type, the object.id echo of the incoming Follow's id, actor,
    and key_id -- a test that only counted sends would not notice the echo
    breaking.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    sends = record_sends(monkeypatch)
    follower, community = _seed_follow_of_local_community(local_only=True)

    activity = inbox_activity(follower, activity_type='Follow', object_uri=community.ap_profile_id)
    follow_id = activity['id']

    dispatch(activity)

    assert ActivityPubLog.query.one().exception_message == \
        'Local only cannot be followed by remote users'

    assert len(sends) == 1
    uri, body, key_id = sends[0]
    assert uri == follower.ap_inbox_url
    assert body['type'] == 'Reject'
    assert body['object']['id'] == follow_id
    assert body['actor'] == community.public_url()
    assert key_id == f'{community.public_url()}#main-key'


def test_a_follow_from_a_banned_user_is_rejected(app, db_session, monkeypatch):
    """routes.py:949-953 sets reject_follow for a `CommunityBan` row
    (independent of local_only -- this community is not local_only), then
    :956-962 sends the Reject. Same payload assertions as the local_only
    test above, for the same reason.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    sends = record_sends(monkeypatch)
    follower, community = _seed_follow_of_local_community(local_only=False)
    ban_user_from_community(follower, community)

    activity = inbox_activity(follower, activity_type='Follow', object_uri=community.ap_profile_id)
    follow_id = activity['id']

    dispatch(activity)

    assert ActivityPubLog.query.one().exception_message == 'Remote user has been banned'

    assert len(sends) == 1
    uri, body, key_id = sends[0]
    assert uri == follower.ap_inbox_url
    assert body['type'] == 'Reject'
    assert body['object']['id'] == follow_id
    assert body['actor'] == community.public_url()
    assert key_id == f'{community.public_url()}#main-key'


def test_a_follow_of_a_local_community_creates_membership_and_accepts(app, db_session, monkeypatch):
    """routes.py:964-980. Not rejected (not local_only, not banned) and no
    existing CommunityMember: a membership row is created, the community's
    subscriptions_count is incremented, community.last_active and
    user.last_seen are stamped, an Accept is sent, and the outcome is logged
    APLOG_FOLLOW/APLOG_SUCCESS -- AFTER the send, per the outcome table in
    this file's module docstring.

    `db.session.expire_all()` after dispatch() is required, not decorative:
    `community` and `follower` were loaded on this test's own db_session,
    but the dispatcher commits its writes through its own independent
    session (get_task_session()), so this test's copies are stale until
    expired -- the same pattern tests/test_inbox_dispatch_misc.py's Move
    tests use for the identical reason.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    sends = record_sends(monkeypatch)
    follower, community = _seed_follow_of_local_community()

    before = utcnow()
    activity = inbox_activity(follower, activity_type='Follow', object_uri=community.ap_profile_id)
    follow_id = activity['id']

    dispatch(activity)

    db.session.expire_all()

    member = CommunityMember.query.filter_by(user_id=follower.id, community_id=community.id).first()
    assert member is not None

    refreshed_community = db.session.get(type(community), community.id)
    refreshed_follower = db.session.get(type(follower), follower.id)
    assert refreshed_community.subscriptions_count == 1
    assert refreshed_community.last_active >= before
    assert refreshed_follower.last_seen >= before

    assert len(sends) == 1
    uri, body, key_id = sends[0]
    assert uri == follower.ap_inbox_url
    assert body['type'] == 'Accept'
    assert body['object']['id'] == follow_id
    assert body['actor'] == community.public_url()
    assert key_id == f'{community.public_url()}#main-key'

    log = ActivityPubLog.query.one()
    assert log.activity_type == 'Follow'
    assert log.result == 'success'


def test_a_follow_from_an_existing_member_writes_nothing_and_stays_silent(
        app, db_session, monkeypatch):
    """routes.py:964-965 with `existing_member` truthy: nothing is written
    (subscriptions_count unchanged), nothing is sent, and -- unlike every
    other branch this task covers -- nothing is logged at all, not even a
    failure. `ActivityPubLog.query.count() == 0` is asserted WITH logging
    enabled precisely so this assertion would fail the moment a log call
    were ever added to this path; `sends == []` covers the federation side.
    Registered as a finding by Task 9 (a silent no-op on the wire, with zero
    audit trail, is easy to mistake for the request never having arrived at
    all), not fixed here.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    sends = record_sends(monkeypatch)
    follower, community = _seed_follow_of_local_community()
    make_community_member(follower, community)

    activity = inbox_activity(follower, activity_type='Follow', object_uri=community.ap_profile_id)

    dispatch(activity)

    db.session.expire_all()
    refreshed_community = db.session.get(type(community), community.id)
    assert refreshed_community.subscriptions_count == 0
    assert ActivityPubLog.query.count() == 0
    assert sends == []


# --- Task 3: Follow, Feed target -- routes.py:983-1017 ---


def _seed_follow_of_feed(name='peerfeed', public=False, local=False, with_keys=False):
    """A target Feed and a remote follower User.

    Unlike make_community(), which hardcodes owner user_id=1 and instance_id=1
    (forcing Task 2's helper to create a throwaway owner first just to keep
    the id space clear), make_feed() takes its instance as an explicit
    argument -- so no artificial owner rows are needed here.

    The follower's `ap_fetched_at` is stamped so schedule_actor_refresh
    (app/activitypub/actor.py:134) does not fire a real actor fetch inline
    under eager Celery, the same reason Task 2's helper stamps its follower.
    make_feed() itself unconditionally stamps the feed's own `ap_fetched_at`
    to utcnow(), so that same protection covers a remote (local=False) feed
    too -- schedule_actor_refresh's staleness check never fires regardless of
    the local flag, since the timestamp is always fresh.
    """
    make_site()
    feed_instance = make_instance('feed.example')
    feed = make_feed(feed_instance, name, public=public, local=local, with_keys=with_keys)

    peer_instance = make_instance('peer.example')
    follower = make_user(peer_instance, 'alice')
    follower.ap_fetched_at = utcnow()

    db.session.commit()
    return follower, feed


def test_a_follow_of_a_non_public_feed_is_rejected_without_any_log(app, db_session, monkeypatch):
    """routes.py:989-999. `if not feed.public` sets reject_follow, and a
    Reject is sent -- but unlike either Community reject reason (:946,
    :952), NOTHING is logged: no `log_incoming_ap` call exists anywhere on
    this path. `ActivityPubLog.query.count() == 0` is asserted WITH logging
    enabled precisely so this assertion would fail the moment a log call
    were ever added here -- the same technique the already-a-member test
    above uses for the identical reason. Registered as an asymmetry by
    Task 9 (the design spec, routes.py:989-999), not fixed here.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    sends = record_sends(monkeypatch)
    follower, feed = _seed_follow_of_feed()

    activity = inbox_activity(follower, activity_type='Follow', object_uri=feed.ap_profile_id)
    follow_id = activity['id']

    dispatch(activity)

    assert len(sends) == 1
    uri, body, key_id = sends[0]
    assert uri == follower.ap_inbox_url
    assert body['type'] == 'Reject'
    assert body['object']['id'] == follow_id
    assert body['actor'] == feed.public_url()
    assert key_id == f'{feed.public_url()}#main-key'

    assert ActivityPubLog.query.count() == 0


def test_a_follow_of_a_public_feed_creates_membership_and_accepts(app, db_session, monkeypatch):
    """routes.py:1001-1016. Not rejected (feed.public is True) and
    `feed_membership` reports anything other than SUBSCRIPTION_MEMBER: a
    FeedMember row is created, feed.subscriptions_count is incremented, an
    Accept is sent, and the outcome is logged APLOG_FOLLOW/APLOG_SUCCESS.
    Unlike the Community accept path, no `last_active`/`last_seen`-equivalent
    column is stamped here -- Feed has no such field touched on this branch,
    per this file's module docstring table.

    The feed is seeded local=True, with_keys=True: :1014 signs the outbound
    Accept with `feed.private_key`, which plain make_feed() leaves unset.

    `db.session.expire_all()` after dispatch() is required for the same
    reason as the Community accept test: the dispatcher commits through its
    own independent session (get_task_session()), leaving this test's copies
    of `feed`/`follower` stale until expired.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    sends = record_sends(monkeypatch)
    follower, feed = _seed_follow_of_feed(public=True, local=True, with_keys=True)

    activity = inbox_activity(follower, activity_type='Follow', object_uri=feed.ap_profile_id)
    follow_id = activity['id']

    dispatch(activity)

    db.session.expire_all()

    member = FeedMember.query.filter_by(user_id=follower.id, feed_id=feed.id).first()
    assert member is not None

    refreshed_feed = db.session.get(type(feed), feed.id)
    assert refreshed_feed.subscriptions_count == 1

    assert len(sends) == 1
    uri, body, key_id = sends[0]
    assert uri == follower.ap_inbox_url
    assert body['type'] == 'Accept'
    assert body['object']['id'] == follow_id
    assert body['actor'] == feed.public_url()
    assert key_id == f'{feed.public_url()}#main-key'

    log = ActivityPubLog.query.one()
    assert log.activity_type == 'Follow'
    assert log.result == 'success'


def test_a_follow_from_an_existing_feed_member_writes_nothing_and_stays_silent(
        app, db_session, monkeypatch):
    """routes.py:1001, `feed_membership(user, feed) == SUBSCRIPTION_MEMBER`:
    nothing is written (subscriptions_count unchanged), nothing is sent, and
    nothing is logged -- the Feed analogue of the Community
    already-a-member silence covered above.

    `feed_membership` (app/utils.py:1661) delegates to `feed.subscribed`,
    which queries FeedMember/FeedJoinRequest directly; the test config's
    `CACHE_TYPE='NullCache'` (tests/conftest.py:68) means the function's own
    `@cache.memoize` decorator does not memoize a stale answer here, so
    seeding the FeedMember row below is enough for the guard to see it.

    The feed is public=True so this exercises the already-subscribed guard
    specifically, not the non-public reject guard above it.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    sends = record_sends(monkeypatch)
    follower, feed = _seed_follow_of_feed(public=True, local=True, with_keys=True)
    db.session.add(FeedMember(user_id=follower.id, feed_id=feed.id))
    db.session.commit()

    activity = inbox_activity(follower, activity_type='Follow', object_uri=feed.ap_profile_id)

    dispatch(activity)

    db.session.expire_all()
    refreshed_feed = db.session.get(type(feed), feed.id)
    assert refreshed_feed.subscriptions_count == 0
    assert ActivityPubLog.query.count() == 0
    assert sends == []


# --- Task 4: Follow, User target -- routes.py:1018-1072 ---


def _seed_follow_of_local_user(manually_approves=False):
    """A local target User (with a real RSA keypair, since routes.py:1045
    signs the outbound Accept with `local_user.private_key`, which plain
    make_user() never sets) and a remote follower User.

    Unlike make_community()/make_feed(), make_user() takes its instance as an
    explicit argument and hardcodes no owner id, so no throwaway rows are
    needed here to keep the id space clear -- the same reason Task 3's feed
    helper needed none.

    The local user is built local=True (ap_id None), which is what
    User.is_local() (app/models.py:1242) actually tests -- `self.ap_id is
    None or ...` short-circuits before ever touching ap_profile_id, so
    `is_local()` stays True regardless of what ap_profile_id holds.

    Plain make_user(local=True) leaves ap_profile_id None, though, and
    find_local_user (app/activitypub/actor.py:29-36) matches an incoming
    Follow's object URL against exactly that column (or alt_user_name, which
    make_user also never sets) -- a None ap_profile_id can never match a URL
    string, so the row would never be found by find_actor_or_create_cached at
    all. Production only sets ap_profile_id (and ap_public_url/ap_inbox_url)
    for a local user in finalize_user_setup (app/utils.py:2896-2899), on
    registration, which this factory-based seeding never calls, so it is set
    directly here -- the identical fix tests/test_inbox_gate_dispatch.py:651
    applies to its own local actor for the same reason, in its own words:
    "Setting ap_profile_id to a '/u/'-shaped URL under this server's own
    domain makes the DB lookup succeed while ap_id is None still makes
    is_local() True." `local_user.public_url()` (ap_public_url still unset)
    keeps falling back to `f"{SERVER_URL}/u/{user_name}"`, the same string,
    so callers that build the expected Follow-object URL from public_url()
    match what find_local_user actually looks up.

    The remote follower's `ap_fetched_at` is stamped so schedule_actor_refresh
    does not fire a real actor fetch inline under eager Celery, same as every
    other helper in this file.
    """
    make_site()
    local_instance = make_instance('local.example')
    local_user = make_user(local_instance, 'bob', local=True, with_keys=True)
    local_user.ap_profile_id = f"https://{current_app.config['SERVER_NAME']}/u/{local_user.user_name}"
    local_user.ap_manually_approves_followers = manually_approves

    peer_instance = make_instance('peer.example')
    remote_user = make_user(peer_instance, 'alice')
    remote_user.ap_fetched_at = utcnow()

    db.session.commit()
    return remote_user, local_user


def test_a_follow_of_a_remote_target_resolved_as_user_is_refused_without_reply(
        app, db_session, monkeypatch):
    """routes.py:1021-1024, `if not local_user.is_local()`. The resolved
    target is itself a remote User (find_actor_or_create_cached can return
    one for a malformed/forwarded Follow whose object names a peer's own
    user rather than one of ours) -- `local_user.is_local()` is then False
    and the request is refused.

    Unlike every Community/Feed refusal covered by Tasks 2-3, NO Reject is
    sent here: routes.py's User branch never sends a federated reply on
    either of its refusal paths (this one and the block guard below) -- an
    asymmetry this file's module docstring outcome table calls out and
    Task 9 registers as a finding, not fixed here. `sends == []` is the
    assertion that would fail the moment that changed.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    sends = record_sends(monkeypatch)
    make_site()
    target_instance = make_instance('target.example')
    target = make_user(target_instance, 'carol')
    target.ap_fetched_at = utcnow()
    peer_instance = make_instance('peer.example')
    follower = make_user(peer_instance, 'alice')
    follower.ap_fetched_at = utcnow()
    db.session.commit()

    activity = inbox_activity(follower, activity_type='Follow', object_uri=target.ap_profile_id)

    dispatch(activity)

    assert ActivityPubLog.query.one().exception_message == 'Follow request for remote user received'
    assert sends == []


def test_a_follow_blocked_by_a_user_block_is_refused_without_reply(app, db_session, monkeypatch):
    """routes.py:1025's first alternative, `local_user.has_blocked_user
    (remote_user.id)`. Seeds ONLY a UserBlock row (blocker=local_user,
    blocked=remote_user) -- no InstanceBlock, no BannedInstances row -- and
    asserts, with the production functions themselves, that the OTHER two
    alternatives are false before dispatching: 5a's Task 7 found one seeded
    row silently satisfying two alternatives of a similarly-shaped guard, so
    this is verified directly rather than assumed from the fixture's shape.

    Mutation: dropping this alternative (leaving `has_blocked_instance(...)
    or instance_banned(...)`) must be killed by THIS test specifically, not
    by either of the two below -- their fixtures leave has_blocked_user
    false.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    sends = record_sends(monkeypatch)
    remote_user, local_user = _seed_follow_of_local_user()
    make_user_block(local_user, remote_user)

    assert local_user.has_blocked_user(remote_user.id) is True
    assert local_user.has_blocked_instance(remote_user.instance_id) is False
    assert instance_banned(remote_user.instance.domain) is False

    activity = inbox_activity(remote_user, activity_type='Follow', object_uri=local_user.public_url())

    dispatch(activity)

    assert ActivityPubLog.query.one().exception_message == 'Attempt to follow denied due to block'
    assert sends == []
    assert UserFollower.query.count() == 0


def test_a_follow_blocked_by_an_instance_block_is_refused_without_reply(app, db_session, monkeypatch):
    """routes.py:1025's second alternative, `local_user.has_blocked_instance
    (remote_user.instance_id)`. Seeds ONLY an InstanceBlock row
    (user=local_user, instance=remote_user's instance) -- no UserBlock, no
    BannedInstances row -- and asserts the other two alternatives are false
    before dispatching, same reasoning as the UserBlock test above.

    Mutation: dropping this alternative (leaving `has_blocked_user(...) or
    instance_banned(...)`) must be killed by THIS test specifically.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    sends = record_sends(monkeypatch)
    remote_user, local_user = _seed_follow_of_local_user()
    make_instance_block(local_user, remote_user.instance)

    assert local_user.has_blocked_user(remote_user.id) is False
    assert local_user.has_blocked_instance(remote_user.instance_id) is True
    assert instance_banned(remote_user.instance.domain) is False

    activity = inbox_activity(remote_user, activity_type='Follow', object_uri=local_user.public_url())

    dispatch(activity)

    assert ActivityPubLog.query.one().exception_message == 'Attempt to follow denied due to block'
    assert sends == []
    assert UserFollower.query.count() == 0


def test_a_follow_blocked_by_a_banned_instance_is_refused_without_reply(app, db_session, monkeypatch):
    """routes.py:1025's third alternative, `instance_banned(remote_user.
    instance.domain)`.

    This one cannot be built the way the two tests above are: `remote_user`
    is not just some row this guard reads, it IS `user` -- the very actor
    whose signature the preamble already resolved, via `find_actor_or_create
    _cached(actor_id)` at routes.py:869, before the Follow branch is ever
    reached. That call's first step is `validate_remote_actor`
    (app/activitypub/actor.py:39), whose default (non-allowlist) branch is
    `if instance_banned(server): return False` -- the SAME instance_banned()
    check, on the SAME domain. Seeding a bare BannedInstances row for
    remote_user's own instance therefore makes the preamble refuse the actor
    OUTRIGHT ('Actor was not a user or a community', routes.py:891) before
    :1025 is ever evaluated -- confirmed by running exactly that seed and
    watching the log message change. Under the deny-list default, this
    alternative is unreachable by the very actor it is meant to check.

    The one way production lets a banned instance's actor still clear the
    preamble is `get_setting('use_allowlist', False)` True: validate_remote_
    actor's OTHER branch is `if not instance_allowed(server): return False`,
    which never consults BannedInstances at all. So this test turns
    allowlist mode on, lists BOTH this server's own domain (needed because
    the same validate_remote_actor call also gates the LOCAL follow target's
    resolution at routes.py:938) and the peer's domain in AllowedInstances
    (a real, if unusual, admin state: an instance allowlisted for delivery
    and separately blacklisted, with nothing in this codebase enforcing that
    the two tables stay mutually exclusive) -- clearing the preamble -- and
    ONLY THEN seeds the BannedInstances row :1025 itself reads. No UserBlock,
    no InstanceBlock: the other two alternatives are asserted false below.

    Mutation: dropping this alternative (leaving `has_blocked_user(...) or
    has_blocked_instance(...)`) must be killed by THIS test specifically.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    sends = record_sends(monkeypatch)
    remote_user, local_user = _seed_follow_of_local_user()

    set_setting('use_allowlist', True)
    db.session.add(AllowedInstances(domain=current_app.config['SERVER_NAME']))
    db.session.add(AllowedInstances(domain=remote_user.instance.domain))
    db.session.add(BannedInstances(domain=remote_user.instance.domain))
    db.session.commit()

    assert get_setting('use_allowlist') is True
    assert local_user.has_blocked_user(remote_user.id) is False
    assert local_user.has_blocked_instance(remote_user.instance_id) is False
    assert instance_banned(remote_user.instance.domain) is True

    activity = inbox_activity(remote_user, activity_type='Follow', object_uri=local_user.public_url())

    dispatch(activity)

    assert ActivityPubLog.query.one().exception_message == 'Attempt to follow denied due to block'
    assert sends == []
    assert UserFollower.query.count() == 0


def test_a_follow_of_a_local_user_with_auto_accept_creates_an_inward_follower_and_accepts(
        app, db_session, monkeypatch):
    """routes.py:1030-1071 with `auto_accept` True (`ap_manually_approves_
    followers` False, routes.py:1031). Not an existing follower, so a new
    `UserFollower(is_accepted=True, is_inward=True)` row is written through
    the task-local `session`; `local_user.ap_followers_url` (unset by plain
    make_user) is backfilled to `public_url() + '/followers'` (:1034-1035);
    an Accept is sent, signed with `local_user.private_key`; a
    `Notification(notif_type=NOTIF_FOLLOW, subtype='new_follower')` is added
    through `db.session` and committed; `local_user.unread_notifications` is
    incremented; and the outcome is logged APLOG_FOLLOW/APLOG_SUCCESS.

    `db.session.expire_all()` after dispatch() is required for the same
    reason as every other accept test in this file: the dispatcher commits
    through its own independent session (get_task_session()), leaving this
    test's copy of `local_user` stale until expired.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    sends = record_sends(monkeypatch)
    remote_user, local_user = _seed_follow_of_local_user(manually_approves=False)
    assert local_user.ap_followers_url is None

    activity = inbox_activity(remote_user, activity_type='Follow', object_uri=local_user.public_url())
    follow_id = activity['id']

    dispatch(activity)

    db.session.expire_all()
    refreshed_local_user = db.session.get(type(local_user), local_user.id)

    follower_row = UserFollower.query.filter_by(
        local_user_id=local_user.id, remote_user_id=remote_user.id, is_inward=True).one()
    assert follower_row.is_accepted is True

    assert refreshed_local_user.ap_followers_url == refreshed_local_user.public_url() + '/followers'
    assert refreshed_local_user.unread_notifications == 1

    notification = Notification.query.filter_by(user_id=local_user.id, author_id=remote_user.id).one()
    assert notification.notif_type == NOTIF_FOLLOW
    assert notification.subtype == 'new_follower'

    assert len(sends) == 1
    uri, body, key_id = sends[0]
    assert uri == remote_user.ap_inbox_url
    assert body['type'] == 'Accept'
    assert body['object']['id'] == follow_id
    assert body['actor'] == local_user.public_url()
    assert key_id == f'{local_user.public_url()}#main-key'

    log = ActivityPubLog.query.one()
    assert log.activity_type == 'Follow'
    assert log.result == 'success'


def test_a_follow_of_a_local_user_needing_manual_approval_records_a_request_and_sends_nothing(
        app, db_session, monkeypatch):
    """routes.py:1030-1071 with `auto_accept` False (`ap_manually_approves_
    followers` True). A new `UserFollower(is_accepted=None, is_inward=True)`
    row is written -- `is_accepted is None`, NOT False, per routes.py:1033's
    `auto_accept if auto_accept else None` (pinned separately below); NO
    Accept (or any other send) goes out; a
    `Notification(notif_type=NOTIF_FOLLOW_REQUEST, subtype='new_follower')`
    is added and `unread_notifications` incremented exactly as on the
    auto-accept path; and the outcome is STILL logged APLOG_FOLLOW/
    APLOG_SUCCESS -- the same log call the auto-accept test above hits, per
    this file's module docstring table: the pending request being recorded
    is what is being called a success here, not a human's later approval.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    sends = record_sends(monkeypatch)
    remote_user, local_user = _seed_follow_of_local_user(manually_approves=True)

    activity = inbox_activity(remote_user, activity_type='Follow', object_uri=local_user.public_url())

    dispatch(activity)

    db.session.expire_all()
    refreshed_local_user = db.session.get(type(local_user), local_user.id)

    follower_row = UserFollower.query.filter_by(
        local_user_id=local_user.id, remote_user_id=remote_user.id, is_inward=True).one()
    assert follower_row.is_accepted is None

    assert refreshed_local_user.unread_notifications == 1

    notification = Notification.query.filter_by(user_id=local_user.id, author_id=remote_user.id).one()
    assert notification.notif_type == NOTIF_FOLLOW_REQUEST
    assert notification.subtype == 'new_follower'

    assert sends == []

    log = ActivityPubLog.query.one()
    assert log.activity_type == 'Follow'
    assert log.result == 'success'


@pytest.mark.parametrize('manually_approves', [False, True])
def test_a_follow_never_records_is_accepted_false(app, db_session, monkeypatch, manually_approves):
    """routes.py:1033, `is_accepted=auto_accept if auto_accept else None`.
    UserFollower.is_accepted's own comment (app/models.py:3533) documents
    False as 'Rejected', so the column has three meaningful states and this
    expression can only ever produce two. Parametrised over both settings of
    ap_manually_approves_followers, assert the stored value is True or None
    and never False. Registered by Task 9, not fixed."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    record_sends(monkeypatch)
    remote_user, local_user = _seed_follow_of_local_user(manually_approves=manually_approves)

    activity = inbox_activity(remote_user, activity_type='Follow', object_uri=local_user.public_url())

    dispatch(activity)

    follower_row = UserFollower.query.filter_by(
        local_user_id=local_user.id, remote_user_id=remote_user.id, is_inward=True).one()
    assert follower_row.is_accepted in (True, None)
    assert follower_row.is_accepted is not False


def test_a_follow_from_an_existing_inward_follower_writes_nothing_and_stays_silent(
        app, db_session, monkeypatch):
    """routes.py:1030 with `existing_follower` truthy (a `UserFollower(
    is_inward=True)` row already linking this pair). Nothing is written
    (the row count does not change), nothing is sent, and nothing is logged
    -- the User analogue of the Community/Feed already-a-member silences
    covered by Tasks 2-3, and the third row in this file's module docstring
    table's "uniformly silent" list. `ActivityPubLog.query.count() == 0` is
    asserted WITH logging enabled for the identical reason those two tests
    give: it would fail the moment a log call were ever added to this path.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    sends = record_sends(monkeypatch)
    remote_user, local_user = _seed_follow_of_local_user()
    make_follow(local_user, remote_user, is_accepted=True, is_inward=True)

    activity = inbox_activity(remote_user, activity_type='Follow', object_uri=local_user.public_url())

    dispatch(activity)

    assert UserFollower.query.filter_by(
        local_user_id=local_user.id, remote_user_id=remote_user.id, is_inward=True).count() == 1
    assert ActivityPubLog.query.count() == 0
    assert sends == []


def test_the_follower_row_and_its_notification_both_land_under_a_direct_dispatch(
        app, db_session, monkeypatch):
    """routes.py:1036 adds the UserFollower through the task-local `session`;
    routes.py:1067-1069 adds the Notification through `db.session` and commits
    it separately.

    Under a direct dispatch() call patch_db_session makes those the same
    underlying session, so both rows land -- assert that, which is the
    behaviour today. Then record in the docstring what D60 establishes about
    the inline path, where they are NOT the same, and that this test cannot
    exhibit that divergence. Do NOT claim to have tested the divergence.
    Registered by Task 9, not fixed.

    D60 (5a) establishes that under a real signed request through the gate
    (has_request_context() True), patch_db_session does NOT patch: the
    dispatcher's `session` local stays the independent task session while
    `db.session` stays the request-scoped session, so the UserFollower write
    (via `session`) and the Notification write (via `db.session`) land in
    genuinely different session objects -- a real divergence between the two
    production paths. This test calls dispatch() directly (no request
    context), so `db.session` IS the task session for its whole duration;
    it can only show that both rows land when the two names refer to one
    object, which is what it does below. It says nothing about, and does
    not exercise, the request-context path where they refer to two.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    sends = record_sends(monkeypatch)
    remote_user, local_user = _seed_follow_of_local_user(manually_approves=False)

    activity = inbox_activity(remote_user, activity_type='Follow', object_uri=local_user.public_url())

    dispatch(activity)

    assert UserFollower.query.filter_by(
        local_user_id=local_user.id, remote_user_id=remote_user.id, is_inward=True).count() == 1
    assert Notification.query.filter_by(user_id=local_user.id, author_id=remote_user.id).count() == 1
    assert len(sends) == 1

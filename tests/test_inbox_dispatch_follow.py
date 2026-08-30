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

from app import db
from app.activitypub import routes as activitypub_routes
from app.activitypub.signature import RsaKeys
from app.models import ActivityPubLog, CommunityMember, utcnow
from tests.factories import ban_user_from_community, inbox_activity, make_community, \
    make_community_member, make_instance, make_site, make_user
from tests.test_inbox_dispatch_preamble import dispatch


def record_sends(monkeypatch):
    """Double send_post_request at its binding site on the routes module and
    return the list it records into.

    routes.py imports send_post_request by name, so patching
    app.activitypub.signature would leave routes' copy pointing at the
    original. Same binding-site trap tests/conftest.py:394 documents.
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

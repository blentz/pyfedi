"""Sub-project 5b, Task 5 -- FIX 1: the a.gup.pe string Accept cannot succeed.

routes.py:1076 states the contract in its own comment: "we have two user
variables in play -- user and requestor_user! requestor_user is the one [who]
made the follow request originally while user is the one who sent the
Accept". a.gup.pe accepts a follow by sending the follow request's own ID as
a bare string (routes.py:1077-1085) rather than embedding the Follow object
the way Lemmy does. That branch looks up the CommunityJoinRequest by the
string's last path segment and, before the fix, assigned its user to `user`
-- backwards twice per the comment above: it leaves `requestor_user` unset
(so the very next line, `if not requestor_user:` at :1091, is always true)
and it clobbers whatever `user` already held (the Accept's sender, which the
`elif user:` branch at :1130 would otherwise consume for a non-Community,
non-Feed recipient). The path therefore ALWAYS logged 'Could not find
recipient of Accept' and returned, discarding the lookup's result on every
call -- a permanently dead branch, not merely a rare miss.

Step 1 finding (recorded here, not asserted by these tests): a throwaway
probe (find_actor_or_create_cached wrapped and its calls recorded, activity
built via inbox_activity(community, activity_type='Accept', ...) against a
Community seeded with ap_fetched_at stamped) showed exactly ONE preamble
call -- find_actor_or_create_cached('https://peer.example/c/microblogs',
community_only=True, create_if_not_found=False) -> a Community instance.
Because that first lookup (routes.py:862) hit, the feed_only and unnarrowed
lookups (864, 866) never ran (see test_inbox_dispatch_preamble.py's Task 2
outcome-table comment for why a truthy `community` short-circuits both). So
for a.gup.pe's real shape -- Accept from a Community actor -- `community` is
set and `feed`/`user` are None, meaning the fixed path in every test below
reaches `if community:` at :1095, exactly as the task brief assumed. The
probe file itself was deleted after use; it changed nothing under `app/`.
"""
from datetime import timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from app import db
from app.activitypub import routes as activitypub_routes
from app.constants import APLOG_REJECT
from app.models import ActivityPubLog, CommunityJoinRequest, CommunityMember, FeedJoinRequest, \
    FeedMember, UserFollower, UserFollowRequest, utcnow
from tests.factories import inbox_activity, make_community, make_community_join_request, \
    make_community_member, make_feed, make_feed_join_request, make_follow, make_instance, \
    make_user, make_user_follow_request
from tests.test_inbox_dispatch_preamble import dispatch


def _seed_agupe_community(host='peer.example'):
    """A remote Community an a.gup.pe-shaped Accept's actor resolves to.

    ap_fetched_at is stamped so find_actor_or_create_cached's
    schedule_actor_refresh does not fire a real actor fetch inline under
    eager Celery. make_community() hardcodes owner user_id=1 and
    instance_id=1, so an instance and a local user are seeded first to
    occupy those ids (same pattern as test_inbox_dispatch_preamble.py's
    Task 2 community test).
    """
    instance = make_instance(host)
    make_user(None, 'community_owner', local=True)
    community = make_community(host=host)
    community.ap_fetched_at = utcnow()
    db.session.commit()
    return community, instance


def test_an_agupe_string_accept_admits_the_join_requests_user(app, db_session, monkeypatch):
    """routes.py:1077-1085 (fixed) + :1095-1117. a.gup.pe accepts by sending
    the follow request's ID as a bare string rather than embedding the
    Follow object. The activity's string object is a URL whose LAST path
    segment is the join request's `uuid` (make_community_join_request's
    docstring), matching :1078-1080's split-on-'/' lookup.

    Before the fix (see module docstring): the lookup's result was assigned
    to `user` instead of `requestor_user`, so :1091's `if not requestor_user:`
    was always true and the path logged 'Could not find recipient of Accept'
    without ever reaching :1095. This test asserts the CORRECTED behaviour:
    the join request's user becomes a CommunityMember of the community.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    community, instance = _seed_agupe_community()
    joiner = make_user(instance, 'joiner')
    join_request = make_community_join_request(joiner, community)

    activity = inbox_activity(
        community, activity_type='Accept',
        object=f'https://peer.example/activities/follow/{join_request.uuid}')

    dispatch(activity)

    member = db.session.query(CommunityMember).filter_by(
        user_id=joiner.id, community_id=community.id).first()
    assert member is not None
    assert ActivityPubLog.query.one().result == 'success'


def test_an_agupe_numeric_style_accept_retries_by_primary_key(app, db_session, monkeypatch):
    """routes.py:1081-1083. Old-style a.gup.pe join requests were identified
    by a bare integer rather than a uuid -- the string object's last path
    segment is the join request's integer `id`, which is not a valid uuid.
    The `.filter_by(uuid=...)` lookup at :1080 raises (Postgres rejects the
    non-uuid literal for a uuid column), :1081's `except Exception:` catches
    it, :1082 rolls back the aborted transaction, and :1083 retries with
    `.get()` against the primary key instead -- finding the SAME row by a
    different column. This test gives the activity a string object ending in
    the join request's `id`, killing that retry path specifically (a uuid
    would never reach it).
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    community, instance = _seed_agupe_community()
    joiner = make_user(instance, 'joiner')
    join_request = make_community_join_request(joiner, community)

    activity = inbox_activity(
        community, activity_type='Accept',
        object=f'https://peer.example/activities/follow/{join_request.id}')

    dispatch(activity)

    member = db.session.query(CommunityMember).filter_by(
        user_id=joiner.id, community_id=community.id).first()
    assert member is not None
    assert ActivityPubLog.query.one().result == 'success'


# --- Task 6: the rest of the Accept arm, routes.py:1086-1148 ---
#
# Outcome table, derived from source. The string-object form above (Task 5)
# is the only way into this arm that does NOT go through the dict/Follow-
# object branch below; everything from :1086 on is this task's concern.
#
#   object is a dict, type != 'Follow' (:1086 false)   -> requestor_user stays
#                                                          None -> :1092
#                                                          FAILURE 'Could not
#                                                          find recipient of
#                                                          Accept'
#   object dict, type == 'Follow', actor unresolvable   -> requestor_user None
#     (:1087)                                              -> :1092 FAILURE
#                                                          (as above)
#   object dict, type == 'Follow', actor resolved,
#     actor.banned (:1088)                               -> :1089 FAILURE
#                                                          '{ap_id} is banned'
#   object dict, type == 'Follow', actor resolved,
#     not banned                                         -> requestor_user
#                                                          set, falls through
#                                                          to :1095
#
#   if community (:1095):
#     no CommunityJoinRequest row (:1096 miss)           -> NOTHING logged
#     join request found, no CommunityMember row (:1101) -> member created,
#                                                          joined_via_feed
#                                                          copied from the
#                                                          join request,
#                                                          community.
#                                                          subscriptions_count
#                                                          += 1 UNLESS the
#                                                          joining user is a
#                                                          bot (:1108-1109),
#                                                          last_active
#                                                          stamped, :1113
#                                                          SUCCESS (no message)
#     join request found, CommunityMember already exists -> :1101 short-
#                                                          circuits the whole
#                                                          block above it;
#                                                          :1113 SUCCESS (no
#                                                          message) is the
#                                                          only effect
#     session.add(member) raises IntegrityError (:1114)  -> rollback (:1115),
#                                                          :1117 SUCCESS with
#                                                          the DISTINCT message
#                                                          "Membership already
#                                                          exists"
#   elif feed (:1118):
#     no FeedJoinRequest row                              -> NOTHING logged
#     join request found, no FeedMember row               -> member created,
#                                                          feed.
#                                                          subscriptions_count
#                                                          += 1 (no bot guard
#                                                          here, unlike the
#                                                          community branch),
#                                                          :1129 SUCCESS
#     join request found, FeedMember already exists       -> :1129 SUCCESS
#                                                          only (no IntegrityError
#                                                          guard exists on this
#                                                          branch at all)
#   elif user (:1130):
#     no UserFollowRequest row                            -> NOTHING logged
#     join request found, no matching UserFollower row
#       (existing_follow filters is_inward=False, :1136)  -> UserFollower
#                                                          created
#                                                          (is_accepted=True,
#                                                          is_inward=False),
#                                                          requestor_user.
#                                                          num_following += 1,
#                                                          :1146 SUCCESS
#     join request found, matching UserFollower exists    -> existing_follow.
#                                                          is_accepted flipped
#                                                          to True,
#                                                          num_following += 1
#                                                          regardless, :1146
#                                                          SUCCESS
#
# No disagreement with the brief found in this step -- every branch above
# matches routes.py:1086-1148 exactly as described. Findings to register
# (not fixed here):
#   - the feed branch (:1118-1129) has no try/except IntegrityError around
#     its session.add(member), unlike the community branch's :1114-1117 --
#     the two branches are not symmetric in what they defend against.
#   - the user branch's existing_follow lookup filters on is_inward=False
#     (:1136); Reject's equivalent (Task 8) does not filter on is_inward at
#     all -- an asymmetry between Accept and Reject's otherwise-parallel code.
#   - REGISTERED AND NOW FIXED as D1432/D1433. routes.py:6 imported
#     `IntegrityError` from `psycopg2`, not `sqlalchemy.exc`, so :1114's
#     `except IntegrityError:` bound to psycopg2's class -- which a real
#     duplicate-key race never raises, because SQLAlchemy's own
#     IntegrityError has no psycopg2 ancestor (`issubclass` is False). The
#     handler was dead: a second Accept for one join request took the inbox
#     request down with a 500 instead of logging "Membership already exists".
#     Both that import and the pair in app/community/util.py now come from
#     `sqlalchemy.exc`, and the test below raises the class production
#     actually produces.
#   - :1108 reads `User.query.get(join_request.user_id)` -- Flask-SQLAlchemy's
#     scoped session -- sandwiched between :1104-1106 and :1110-1111, which
#     both use the task-local `session` object. Under this file's direct-call
#     dispatch() (no request context), patch_db_session makes db.session proxy
#     to the same task-local session (see test_inbox_dispatch_preamble.py's
#     module docstring, "Request-context asymmetry"), so this happens to
#     resolve to the same underlying session in every test here -- but it is
#     still a real inconsistency in which session-access spelling the code
#     uses line to line.


def _seed_remote_feed(host='peer.example', name='peerfeed'):
    """A remote Feed an Accept's outer actor resolves to.

    make_feed() (tests/factories.py) already stamps ap_fetched_at, unlike
    make_community()/make_user(), so no extra stamping is needed here for
    find_actor_or_create_cached's schedule_actor_refresh to stay quiet.
    """
    instance = make_instance(host)
    feed = make_feed(instance, name=name)
    return feed, instance


def _stamp_remote_user(instance, name):
    """A remote User, ap_fetched_at stamped so find_actor_or_create_cached's
    schedule_actor_refresh does not fire a real actor fetch inline under
    eager Celery -- needed for every Follow-object `actor`, since that is a
    real find_actor_or_create_cached() call (unlike the string form's direct
    `session.query(User).get(...)`, which never touches the actor cache).
    """
    user = make_user(instance, name)
    user.ap_fetched_at = utcnow()
    db.session.commit()
    return user


def _follow_object(actor_ap_profile_id):
    return {'type': 'Follow', 'actor': actor_ap_profile_id}


# --- Step 2: the Follow-object form and its banned check (:1086-1093) ---

def test_a_banned_follow_object_actor_is_refused(app, db_session, monkeypatch):
    """routes.py:1087-1090. `requestor_user.banned` is checked AFTER
    find_actor_or_create_cached resolves the Follow object's actor, which
    matters because find_actor_or_create_cached's own resolution filters
    banned actors out on every path this test's harness can reach: the
    NullCache config (tests/conftest.py TestConfig.CACHE_TYPE) makes its
    cache.memoize wrapper recompute find_actor_or_create() on every call
    rather than serve a stale cached id, and find_actor_or_create's remote
    lookup (find_actor_by_url, app/activitypub/actor.py:313-314) returns
    False -- swallowed to None -- the moment it sees actor.banned. In
    production the banned check at :1088-1089 only fires for an actor that
    was cached BEFORE being banned (find_actor_or_create_cached's fast path,
    util.py:349-359, re-fetches the model by the cached primary key without
    re-running that banned filter) -- a stale-cache scenario NullCache cannot
    reproduce. find_actor_or_create_cached is therefore wrapped (not fully
    replaced -- the outer actor's own resolution at routes.py:862 still runs
    for real) to return the already-banned actor directly for this one
    lookup, so :1088-1090 can be exercised without re-deriving Redis's
    caching semantics, which is out of scope for this file.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    community, instance = _seed_agupe_community()
    follower_instance = make_instance('follower.example')
    banned_follower = _stamp_remote_user(follower_instance, 'banned_follower')
    banned_follower.banned = True
    db.session.commit()

    real_find_actor_or_create_cached = activitypub_routes.find_actor_or_create_cached

    def _stub_find_actor_or_create_cached(actor, *args, **kwargs):
        target = actor['id'] if isinstance(actor, dict) else actor
        if target == banned_follower.ap_profile_id:
            return banned_follower
        return real_find_actor_or_create_cached(actor, *args, **kwargs)

    monkeypatch.setattr(activitypub_routes, 'find_actor_or_create_cached',
                        _stub_find_actor_or_create_cached)

    activity = inbox_activity(community, activity_type='Accept',
                              object=_follow_object(banned_follower.ap_profile_id))

    dispatch(activity)

    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == f'{banned_follower.ap_id} is banned'


def test_a_follow_object_with_an_unresolvable_actor_is_refused(app, db_session, monkeypatch):
    """routes.py:1091-1093. The Follow object's actor is a string with no
    server component -- no scheme (so extract_domain_and_actor's urlparse
    yields an empty netloc) and no '@' (so normalise_actor_string's fallback
    also yields an empty server) -- so validate_remote_actor
    (app/activitypub/actor.py:58-61) returns False before any DB query or
    HTTP fetch, and find_actor_or_create_cached returns None cleanly. This
    kills the :1091 `if not requestor_user:` branch through a genuinely
    unresolvable actor, distinct from the banned-actor test above.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    community, instance = _seed_agupe_community()

    activity = inbox_activity(community, activity_type='Accept',
                              object=_follow_object('not-a-resolvable-actor'))

    dispatch(activity)

    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Could not find recipient of Accept'


# --- Step 3: the community branch, :1095-1116 ---

def test_the_community_branch_increments_subscriptions_for_a_non_bot_joiner(
        app, db_session, monkeypatch):
    """routes.py:1101-1113, non-bot half of the :1108-1109 guard. Uses the
    Follow-object form (not Task 5's string form) so this test exercises the
    same :1087 resolution the rest of Step 2/5 rely on. joined_via_feed=True
    on the join request pins that :1103-1106 actually copies the flag onto
    the new CommunityMember rather than leaving its own default.

    community.last_active is seeded to a fixed point well in the past
    before dispatch, rather than merely asserted non-None afterwards --
    Community.last_active is declared `default=utcnow` (app/models.py:561)
    and make_community() never overrides it, so a plain `is not None` check
    would already be true the moment the row is created, before :1110 ever
    runs, and would stay true even if :1110 were deleted. Asserting the
    post-dispatch value is strictly greater than the seeded stale value is
    sensitive to :1110 actually executing.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    community, instance = _seed_agupe_community()
    follower_instance = make_instance('follower.example')
    joiner = _stamp_remote_user(follower_instance, 'joiner')
    make_community_join_request(joiner, community, joined_via_feed=True)
    stale_last_active = utcnow() - timedelta(days=1)
    community.last_active = stale_last_active
    db.session.commit()

    activity = inbox_activity(community, activity_type='Accept',
                              object=_follow_object(joiner.ap_profile_id))

    dispatch(activity)

    db.session.expire_all()
    member = db.session.query(CommunityMember).filter_by(
        user_id=joiner.id, community_id=community.id).first()
    assert member is not None
    assert member.joined_via_feed is True
    assert community.subscriptions_count == 1
    assert community.last_active > stale_last_active
    log = ActivityPubLog.query.one()
    assert log.result == 'success'
    assert log.exception_message is None


def test_the_community_branch_does_not_increment_subscriptions_for_a_bot_joiner(
        app, db_session, monkeypatch):
    """routes.py:1108-1109's guard, bot half. A bot joiner still becomes a
    CommunityMember (the guard is only around the counter, not the
    membership), but community.subscriptions_count must NOT move.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    community, instance = _seed_agupe_community()
    follower_instance = make_instance('follower.example')
    joiner = _stamp_remote_user(follower_instance, 'bot_joiner')
    joiner.bot = True
    db.session.commit()
    make_community_join_request(joiner, community)

    activity = inbox_activity(community, activity_type='Accept',
                              object=_follow_object(joiner.ap_profile_id))

    dispatch(activity)

    db.session.expire_all()
    member = db.session.query(CommunityMember).filter_by(
        user_id=joiner.id, community_id=community.id).first()
    assert member is not None
    assert community.subscriptions_count == 0
    assert ActivityPubLog.query.one().result == 'success'


def test_the_community_branch_already_a_member_still_logs_success(
        app, db_session, monkeypatch):
    """routes.py:1101 false -- an existing CommunityMember short-circuits the
    entire creation block (no second row, no subscriptions_count bump), and
    :1113 SUCCESS is logged with no message. The `exception_message is None`
    assertion is what distinguishes this path from the IntegrityError path's
    distinct message below -- otherwise both are indistinguishable SUCCESS
    rows.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    community, instance = _seed_agupe_community()
    follower_instance = make_instance('follower.example')
    joiner = _stamp_remote_user(follower_instance, 'joiner')
    make_community_join_request(joiner, community)
    make_community_member(joiner, community)

    activity = inbox_activity(community, activity_type='Accept',
                              object=_follow_object(joiner.ap_profile_id))

    dispatch(activity)

    db.session.expire_all()
    members = db.session.query(CommunityMember).filter_by(
        user_id=joiner.id, community_id=community.id).all()
    assert len(members) == 1
    assert community.subscriptions_count == 0
    log = ActivityPubLog.query.one()
    assert log.result == 'success'
    assert log.exception_message is None


# --- Step 4: the IntegrityError path, :1114-1117 ---

def test_a_membership_race_is_caught_as_an_integrity_error(app, db_session, monkeypatch):
    """routes.py:1114-1117. Simulates a membership being inserted by a
    concurrent request between :1101's check and :1107's insert -- the real
    race this except clause defends against. Driven exactly as the brief
    prescribes: get_task_session() (the factory routes.py calls at :841 to
    obtain its task-local `session`) is wrapped so the session it returns has
    its own `.add` patched to raise IntegrityError the FIRST time it is
    called with a CommunityMember instance, then fall through to the real
    `.add` afterwards -- so the ActivityPubLog row this same call logs via
    the (patched-through) db.session proxy still gets written normally.

    Asserting only `result == 'success'` would not distinguish this path
    from the ordinary :1113 success above (both log SUCCESS) -- the
    assertion on `exception_message == 'Membership already exists'` is what
    proves the rollback branch specifically ran, not the ordinary one.

    D1432/D1433, registered here and since fixed: :6 imported
    `IntegrityError` from `psycopg2`, which is not what a duplicate-key race
    raises -- `sqlalchemy.exc.IntegrityError` wraps the driver error in
    `.orig` and has no psycopg2 ancestor, so `issubclass` is False and this
    handler was dead code. A second Accept for one join request took the
    whole inbox request down with a 500 instead of logging "Membership
    already exists". The import now comes from `sqlalchemy.exc`, and this
    test raises THAT class -- the one a real race produces.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    community, instance = _seed_agupe_community()
    follower_instance = make_instance('follower.example')
    joiner = _stamp_remote_user(follower_instance, 'joiner')
    make_community_join_request(joiner, community)

    real_get_task_session = activitypub_routes.get_task_session
    state = {'raised': False}

    def _get_task_session_that_fails_once():
        real_session = real_get_task_session()
        real_add = real_session.add

        def _add(instance_to_add, *args, **kwargs):
            if not state['raised'] and isinstance(instance_to_add, CommunityMember):
                state['raised'] = True
                raise IntegrityError(
                    'INSERT INTO community_member', {},
                    Exception('duplicate key value violates unique constraint'))
            return real_add(instance_to_add, *args, **kwargs)

        real_session.add = _add
        return real_session

    monkeypatch.setattr(activitypub_routes, 'get_task_session',
                        _get_task_session_that_fails_once)

    activity = inbox_activity(community, activity_type='Accept',
                              object=_follow_object(joiner.ap_profile_id))

    dispatch(activity)

    assert state['raised'] is True
    db.session.expire_all()
    member = db.session.query(CommunityMember).filter_by(
        user_id=joiner.id, community_id=community.id).first()
    assert member is None
    assert community.subscriptions_count == 0
    log = ActivityPubLog.query.one()
    assert log.result == 'success'
    assert log.exception_message == 'Membership already exists'


# --- Step 5: the feed and user branches, :1118-1147 ---

def test_the_feed_branch_creates_membership_and_increments_subscriptions(
        app, db_session, monkeypatch):
    """routes.py:1118-1129. Mirrors the community branch's happy path, but
    the feed branch has no bot guard on its subscriptions_count += 1
    (:1126) -- a bot joiner is not covered separately here because the
    source has no such guard on this branch to cover.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    feed, instance = _seed_remote_feed()
    joiner = _stamp_remote_user(instance, 'joiner')
    make_feed_join_request(joiner, feed)

    activity = inbox_activity(feed, activity_type='Accept',
                              object=_follow_object(joiner.ap_profile_id))

    dispatch(activity)

    db.session.expire_all()
    member = db.session.query(FeedMember).filter_by(
        user_id=joiner.id, feed_id=feed.id).first()
    assert member is not None
    assert feed.subscriptions_count == 1
    log = ActivityPubLog.query.one()
    assert log.result == 'success'
    assert log.exception_message is None


def test_the_feed_branch_already_a_member_still_logs_success(app, db_session, monkeypatch):
    """routes.py:1123 false -- an existing FeedMember short-circuits member
    creation and the counter bump; :1129 SUCCESS still logs. There is no
    IntegrityError guard on this branch at all (see the Step 1 finding), so
    this is the only "already exists" outcome the feed branch has.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    feed, instance = _seed_remote_feed()
    joiner = _stamp_remote_user(instance, 'joiner')
    make_feed_join_request(joiner, feed)
    existing_member = FeedMember(user_id=joiner.id, feed_id=feed.id)
    db.session.add(existing_member)
    db.session.commit()

    activity = inbox_activity(feed, activity_type='Accept',
                              object=_follow_object(joiner.ap_profile_id))

    dispatch(activity)

    db.session.expire_all()
    members = db.session.query(FeedMember).filter_by(
        user_id=joiner.id, feed_id=feed.id).all()
    assert len(members) == 1
    assert feed.subscriptions_count == 0
    assert ActivityPubLog.query.one().result == 'success'


def test_the_user_branch_creates_a_new_follower_row(app, db_session, monkeypatch):
    """routes.py:1130-1146, the no-existing-follow half. The target
    ('user', the Accept's outer actor) and the requestor (the Follow
    object's actor) are both plain remote Users under the same instance --
    ap_profile_id differs by username, so there is no collision. Note the
    asymmetry recorded in the Step 1 table: existing_follow's lookup here
    filters `is_inward=False` (:1136), as Reject's equivalent now does (D74).
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    target = _stamp_remote_user(instance, 'target')
    joiner = _stamp_remote_user(instance, 'joiner')
    make_user_follow_request(joiner, target)

    activity = inbox_activity(target, activity_type='Accept',
                              object=_follow_object(joiner.ap_profile_id))

    dispatch(activity)

    db.session.expire_all()
    follow = db.session.query(UserFollower).filter_by(
        local_user_id=joiner.id, remote_user_id=target.id, is_inward=False).first()
    assert follow is not None
    assert follow.is_accepted is True
    assert joiner.num_following == 1
    log = ActivityPubLog.query.one()
    assert log.result == 'success'
    assert log.exception_message is None


def test_the_user_branch_flips_an_existing_follow_to_accepted(app, db_session, monkeypatch):
    """routes.py:1137 false -- an existing (not-yet-accepted) UserFollower
    row has its is_accepted flipped to True rather than a second row being
    created, and requestor_user.num_following is incremented either way
    (:1144 sits outside the if/else). make_follow's own column mapping
    (local_user=follower, remote_user=followed) matches routes.py:1134-1136's
    filter exactly, so it is used directly rather than a bespoke row build.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    target = _stamp_remote_user(instance, 'target')
    joiner = _stamp_remote_user(instance, 'joiner')
    make_user_follow_request(joiner, target)
    make_follow(joiner, target, is_accepted=False, is_inward=False)

    activity = inbox_activity(target, activity_type='Accept',
                              object=_follow_object(joiner.ap_profile_id))

    dispatch(activity)

    db.session.expire_all()
    follows = db.session.query(UserFollower).filter_by(
        local_user_id=joiner.id, remote_user_id=target.id, is_inward=False).all()
    assert len(follows) == 1
    assert follows[0].is_accepted is True
    assert joiner.num_following == 1
    assert ActivityPubLog.query.one().result == 'success'


# --- Step 6: the silent no-join-request paths ---

def test_the_community_branch_is_silent_with_no_join_request(app, db_session, monkeypatch):
    """routes.py:1096-1097 miss -- no CommunityJoinRequest row exists for
    this user/community pair, so the entire community block does nothing and
    logs nothing (the Accept simply returns at :1147 having done nothing at
    all for this branch)."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    community, instance = _seed_agupe_community()
    follower_instance = make_instance('follower.example')
    joiner = _stamp_remote_user(follower_instance, 'joiner')

    activity = inbox_activity(community, activity_type='Accept',
                              object=_follow_object(joiner.ap_profile_id))

    dispatch(activity)

    assert ActivityPubLog.query.count() == 0


def test_the_feed_branch_is_silent_with_no_join_request(app, db_session, monkeypatch):
    """routes.py:1119-1120 miss -- no FeedJoinRequest row exists, so the
    feed block does nothing and logs nothing."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    feed, instance = _seed_remote_feed()
    joiner = _stamp_remote_user(instance, 'joiner')

    activity = inbox_activity(feed, activity_type='Accept',
                              object=_follow_object(joiner.ap_profile_id))

    dispatch(activity)

    assert ActivityPubLog.query.count() == 0


def test_the_user_branch_is_silent_with_no_join_request(app, db_session, monkeypatch):
    """routes.py:1131-1132 miss -- no UserFollowRequest row exists, so the
    user block does nothing and logs nothing."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    target = _stamp_remote_user(instance, 'target')
    joiner = _stamp_remote_user(instance, 'joiner')

    activity = inbox_activity(target, activity_type='Accept',
                              object=_follow_object(joiner.ap_profile_id))

    dispatch(activity)

    assert ActivityPubLog.query.count() == 0


# --- Task 7: FIX 2 -- Reject's user branch dereferences an absent join
# request, routes.py:1180-1187 ---

def test_a_reject_for_a_missing_follow_request_is_handled(app, db_session, monkeypatch):
    """routes.py:1180-1187. The community branch (:1157) and the feed branch
    (:1169) both guard their bodies with `if join_request:`. The user branch
    does not, so a Reject naming a follow request that is already gone --
    withdrawn, or already rejected -- raises AttributeError: 'NoneType' object
    has no attribute 'user_id' instead of logging.

    Peer-triggerable: nothing stops a remote instance sending a Reject for a
    request this instance has no record of.

    No UserFollowRequest row is seeded for (joiner, target) -- the request is
    simply absent, matching the docstring's "already gone" scenario without
    needing to model withdrawal or a prior rejection explicitly.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    target = _stamp_remote_user(instance, 'target')
    joiner = _stamp_remote_user(instance, 'joiner')

    activity = inbox_activity(target, activity_type='Reject',
                              object=_follow_object(joiner.ap_profile_id))

    dispatch(activity)

    assert ActivityPubLog.query.count() == 0


# --- Task 8: the rest of the Reject arm, routes.py:1150-1191 ---

def test_a_reject_with_an_unresolvable_actor_is_refused(app, db_session, monkeypatch):
    """routes.py:1152-1155. Mirrors the Accept arm's :1091-1093 (see
    test_a_follow_object_with_an_unresolvable_actor_is_refused above): a
    Follow object whose actor cannot be resolved hits `if not
    requestor_user:` and logs FAILURE, this time carrying Reject's own
    message text -- 'Could not find recipient of Reject', not '...Accept'.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    target = _stamp_remote_user(instance, 'target')

    activity = inbox_activity(target, activity_type='Reject',
                              object=_follow_object('not-a-resolvable-actor'))

    dispatch(activity)

    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Could not find recipient of Reject'


# --- Step 2: the community branch, :1157-1168 ---
#
# Two tests, each exercising one of the two `if` guards in both directions
# combined: the first has the join request present / membership absent, the
# second has the reverse (join request absent / membership present), so
# together both guards are hit on their True side and their False side.

def test_the_reject_community_branch_deletes_a_join_request_with_no_membership(
        app, db_session, monkeypatch):
    """routes.py:1158-1163, join request PRESENT / membership ABSENT. The
    `if join_request:` guard fires and deletes the row; the `if
    existing_membership:` guard is exercised on its absent side (nothing to
    delete, no crash). SUCCESS still logs.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    community, instance = _seed_agupe_community()
    follower_instance = make_instance('follower.example')
    joiner = _stamp_remote_user(follower_instance, 'joiner')
    make_community_join_request(joiner, community)

    activity = inbox_activity(community, activity_type='Reject',
                              object=_follow_object(joiner.ap_profile_id))

    dispatch(activity)

    db.session.expire_all()
    assert db.session.query(CommunityJoinRequest).filter_by(
        user_id=joiner.id, community_id=community.id).first() is None
    assert ActivityPubLog.query.one().result == 'success'


def test_the_reject_community_branch_deletes_a_membership_with_no_join_request(
        app, db_session, monkeypatch):
    """routes.py:1158-1163, join request ABSENT / membership PRESENT -- the
    reverse combination from the test above. The `if join_request:` guard is
    exercised on its absent side (no row to delete, no crash), and the `if
    existing_membership:` guard fires, deleting the CommunityMember row.
    SUCCESS still logs.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    community, instance = _seed_agupe_community()
    follower_instance = make_instance('follower.example')
    joiner = _stamp_remote_user(follower_instance, 'joiner')
    make_community_member(joiner, community)

    activity = inbox_activity(community, activity_type='Reject',
                              object=_follow_object(joiner.ap_profile_id))

    dispatch(activity)

    db.session.expire_all()
    assert db.session.query(CommunityMember).filter_by(
        user_id=joiner.id, community_id=community.id).first() is None
    assert ActivityPubLog.query.one().result == 'success'


# --- Step 3: the feed branch, :1169-1178 -- same shape as Step 2 ---

def test_the_reject_feed_branch_deletes_a_join_request_with_no_membership(
        app, db_session, monkeypatch):
    """routes.py:1170-1174, join request PRESENT / membership ABSENT. Mirrors
    the community branch's first combination above, over FeedJoinRequest and
    FeedMember.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    feed, instance = _seed_remote_feed()
    joiner = _stamp_remote_user(instance, 'joiner')
    make_feed_join_request(joiner, feed)

    activity = inbox_activity(feed, activity_type='Reject',
                              object=_follow_object(joiner.ap_profile_id))

    dispatch(activity)

    db.session.expire_all()
    assert db.session.query(FeedJoinRequest).filter_by(
        user_id=joiner.id, feed_id=feed.id).first() is None
    assert ActivityPubLog.query.one().result == 'success'


def test_the_reject_feed_branch_deletes_a_membership_with_no_join_request(
        app, db_session, monkeypatch):
    """routes.py:1170-1174, join request ABSENT / membership PRESENT -- the
    reverse combination. No make_feed_member() factory exists (see the
    Accept-arm feed tests above, which build FeedMember directly the same
    way).
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    feed, instance = _seed_remote_feed()
    joiner = _stamp_remote_user(instance, 'joiner')
    existing_member = FeedMember(user_id=joiner.id, feed_id=feed.id)
    db.session.add(existing_member)
    db.session.commit()

    activity = inbox_activity(feed, activity_type='Reject',
                              object=_follow_object(joiner.ap_profile_id))

    dispatch(activity)

    db.session.expire_all()
    assert db.session.query(FeedMember).filter_by(
        user_id=joiner.id, feed_id=feed.id).first() is None
    assert ActivityPubLog.query.one().result == 'success'


# --- Step 4: the user branch, :1179-1189 -- both sides of Task 7's guard ---
#
# The absent-join-request case is Task 7's own regression test above
# (test_a_reject_for_a_missing_follow_request_is_handled); these two cover
# the PRESENT case's two sub-outcomes.

def test_the_reject_user_branch_flips_an_existing_follow_and_decrements(
        app, db_session, monkeypatch):
    """routes.py:1179-1189, join request PRESENT / existing_follow PRESENT.
    The existing_follow query now filters is_inward=False like Accept's
    (D74); test_a_reject_leaves_the_inward_follow_between_the_same_users_alone
    below covers the inward row it must skip.

    D79, fixed: this branch now deletes the UserFollowRequest like the
    community and feed branches do, so a repeated Reject finds nothing to
    act on a second time.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    target = _stamp_remote_user(instance, 'target')
    joiner = _stamp_remote_user(instance, 'joiner')
    make_user_follow_request(joiner, target)
    make_follow(joiner, target, is_accepted=True, is_inward=False)

    activity = inbox_activity(target, activity_type='Reject',
                              object=_follow_object(joiner.ap_profile_id))

    dispatch(activity)

    db.session.expire_all()
    follow = db.session.query(UserFollower).filter_by(
        local_user_id=joiner.id, remote_user_id=target.id).first()
    assert follow.is_accepted is False
    assert joiner.num_following == 0
    assert ActivityPubLog.query.one().result == 'success'
    assert db.session.query(UserFollowRequest).filter_by(
        user_id=joiner.id, follow_id=target.id).first() is None


def test_a_reject_leaves_the_inward_follow_between_the_same_users_alone(app, db_session, monkeypatch):
    """D74, fixed. The inward row for the same pair (target follows joiner)
    has the same local_user_id/remote_user_id as joiner's outward follow of
    target. Reject's lookup did not filter on is_inward the way Accept's
    does, so it could flip that inward row instead. It now touches only the
    outward follow the Reject is about.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    target = _stamp_remote_user(instance, 'target')
    joiner = _stamp_remote_user(instance, 'joiner')
    make_user_follow_request(joiner, target)
    make_follow(joiner, target, is_accepted=True, is_inward=True)

    activity = inbox_activity(target, activity_type='Reject',
                              object=_follow_object(joiner.ap_profile_id))

    dispatch(activity)

    db.session.expire_all()
    inward = db.session.query(UserFollower).filter_by(
        local_user_id=joiner.id, remote_user_id=target.id, is_inward=True).one()
    assert inward.is_accepted is True


def test_a_reject_with_no_follower_row_leaves_num_following_alone(app, db_session, monkeypatch):
    """D75, fixed. The decrement used to run whenever a join request
    existed, whether or not an accepted follow was there to undo, so a
    Reject with no prior acceptance drove num_following to -1. It now
    moves only when an accepted follow is flipped back.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    target = _stamp_remote_user(instance, 'target')
    joiner = _stamp_remote_user(instance, 'joiner')
    make_user_follow_request(joiner, target)

    activity = inbox_activity(target, activity_type='Reject',
                              object=_follow_object(joiner.ap_profile_id))

    dispatch(activity)

    db.session.expire_all()
    assert db.session.query(UserFollower).filter_by(
        local_user_id=joiner.id, remote_user_id=target.id).first() is None
    assert joiner.num_following == 0
    assert ActivityPubLog.query.one().result == 'success'


def test_a_reject_of_a_pending_follow_leaves_num_following_alone(app, db_session, monkeypatch):
    """D75, fixed. A follow that was never accepted was never counted, so
    rejecting it flips nothing that num_following reflects."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    target = _stamp_remote_user(instance, 'target')
    joiner = _stamp_remote_user(instance, 'joiner')
    make_user_follow_request(joiner, target)
    make_follow(joiner, target, is_accepted=False, is_inward=False)

    activity = inbox_activity(target, activity_type='Reject',
                              object=_follow_object(joiner.ap_profile_id))

    dispatch(activity)

    db.session.expire_all()
    assert joiner.num_following == 0


# --- Step 5: the silently-ignored object types, :1151 ---

def test_a_reject_of_a_non_follow_object_is_silently_ignored(app, db_session, monkeypatch):
    """routes.py:1151 handles only `object['type'] == 'Follow'`; any other
    object type falls straight out of the arm without logging anything.
    Logging is enabled here specifically so a zero count proves nothing ran
    -- with logging off the count would be zero for the wrong reason and this
    test would be vacuous.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    target = _stamp_remote_user(instance, 'target')
    joiner = _stamp_remote_user(instance, 'joiner')

    activity = inbox_activity(target, activity_type='Reject',
                              object={'type': 'Undo', 'actor': joiner.ap_profile_id})

    dispatch(activity)

    assert ActivityPubLog.query.count() == 0


@pytest.mark.parametrize('activity_type', ['Accept', 'Reject'])
@pytest.mark.parametrize('malformed_object', [{'actor': 'https://follower.example/u/joiner'}, 42],
                         ids=['dict-without-type', 'int'])
def test_an_accept_or_reject_of_an_untyped_object_is_refused(
        app, db_session, monkeypatch, activity_type, malformed_object):
    """D70, fixed. Both arms read `core_activity['object']['type']` with no
    check that the object is a dict carrying 'type', so an object dict
    without one raised KeyError and a non-dict (other than Accept's string
    form) raised TypeError, uncaught, with no log row. Both are now refused
    and logged before any membership is touched.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    community, instance = _seed_agupe_community()

    activity = inbox_activity(community, activity_type=activity_type, object=malformed_object)

    dispatch(activity)

    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == f'{activity_type} object is not an activity with a type'


def test_an_agupe_string_reject_cancels_the_join_request(app, db_session, monkeypatch):
    """D70, fixed. a.gup.pe sends a Follow's ID as a bare string. The
    Reject arm used to index that string by 'type' and raise TypeError, and
    then refused it. It now looks the join request up by the string's last
    path segment, as the Accept arm does, and cancels it exactly as a
    dict-object Reject of the Follow would: join request and membership are
    both deleted.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    community, instance = _seed_agupe_community()
    joiner = make_user(instance, 'joiner')
    join_request = make_community_join_request(joiner, community)
    make_community_member(joiner, community)

    activity = inbox_activity(
        community, activity_type='Reject',
        object=f'https://peer.example/activities/follow/{join_request.uuid}')

    dispatch(activity)

    db.session.expire_all()
    assert ActivityPubLog.query.one().result == 'success'
    assert CommunityJoinRequest.query.count() == 0
    assert CommunityMember.query.filter_by(user_id=joiner.id, community_id=community.id).count() == 0


def test_an_agupe_string_reject_for_an_unknown_request_is_refused(app, db_session, monkeypatch):
    """D70, fixed. A string Reject whose last path segment names no join
    request has no one to act for, and is refused like an unresolvable
    Follow actor."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    community, instance = _seed_agupe_community()

    activity = inbox_activity(
        community, activity_type='Reject',
        object='https://peer.example/activities/follow/00000000-0000-0000-0000-000000000000')

    dispatch(activity)

    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Could not find recipient of Reject'


# --- Step 6: the Reject arm's log label ---

def test_a_reject_is_logged_as_a_reject(app, db_session, monkeypatch):
    """D68, fixed. Every log call in the Reject arm passed APLOG_ACCEPT, so
    each Reject outcome was recorded as an Accept. They now pass
    APLOG_REJECT, here on both a success and a refusal.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    community, instance = _seed_agupe_community()
    follower_instance = make_instance('follower.example')
    joiner = _stamp_remote_user(follower_instance, 'joiner')
    make_community_join_request(joiner, community)

    dispatch(inbox_activity(community, activity_type='Reject',
                            object=_follow_object(joiner.ap_profile_id)))
    dispatch(inbox_activity(community, activity_type='Reject',
                            object=_follow_object('not-a-resolvable-actor')))

    logs = ActivityPubLog.query.order_by(ActivityPubLog.id).all()
    assert [log.result for log in logs] == ['success', 'failure']
    assert all(log.activity_type == APLOG_REJECT[1] for log in logs)

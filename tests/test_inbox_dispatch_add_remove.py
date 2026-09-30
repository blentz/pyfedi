"""Sub-project 5c, Task 6 -- FIX 4: Add's auto-subscribe loop runs without a
community.

The Add arm's feed branch (routes.py, current numbering ~1400-1423) is
reached when the activity's actor resolves to a Feed rather than a Community
or User (the Announce/Accept/Reject actor-resolution preamble at
routes.py:861-870), and the inner activity's `object` carries an `id`.

FeedItem creation is correctly guarded:

    if community_to_add and isinstance(community_to_add, Community):
        ...create the FeedItem, bump feed.num_communities, commit...

but the auto-subscribe loop directly below it was NOT inside that guard, and
unconditionally read `community_to_add.ap_id` to build the actor string
`do_subscribe` needs. When `community_to_add` is `None` (the community named
by the Add could not be resolved) or is some non-Community actor, that read
raises `AttributeError: 'NoneType' object has no attribute 'ap_id'` for any
feed that has at least one local member with `feed_auto_follow` set -- which
is the column's own default (`app/models.py:1033`), so an ordinary feed
member trips it.

The fix moves the loop (its comment, the `feed_members` query, and the
`for` body) one level in, under the same guard, since the loop's entire
purpose -- subscribing a feed's members to the community just added -- has
nothing to do when no community was added.

`do_subscribe` is imported inline inside the loop
(`from app.community.routes import do_subscribe`), so it does not exist as
an attribute of `app.activitypub.routes` -- `record_moderation` (which
patches names on that module) cannot double it. It is patched directly on
`app.community.routes`, where the inline import actually resolves it.

--------------------------------------------------------------------------
Task 7 -- the rest of the Add arm.

The brief's line numbers (routes.py:1396-1468) are STALE: earlier tasks
de-indented one arm and re-indented part of another, shifting everything
after ~:1300. Re-read from source, the arm currently spans routes.py
1400-1471. Every citation below is the CURRENT line, not the brief's.

Outcome table, derived from source:

  How the arm is reached (routes.py:839-932): `core_activity['type'] ==
  'Add'` is checked at line 1400 regardless of whether the activity arrived
  directly or wrapped in an Announce. Two shapes reach it in practice:

    (a) DIRECT (not Announced): the outer activity's own actor resolves via
        the unnarrowed lookup at line 871. If that actor is a Community, it
        is intercepted at lines 875-877 ('NodeBB Topic Management') and
        NEVER reaches line 1400 at all -- so a direct Add's actor, by the
        time it reaches this arm, can only have resolved as a User (`user =
        actor`, 872-873) or been refused outright (890-892, actor neither
        User nor Community). `feed` is never set on this path (it is only
        ever set by the Announce/Accept/Reject branch, 861-870). So for a
        direct Add, line 1401's `if user is not None: mod = user` fires,
        line 1403's `if not announced and not feed:` is true (announced is
        False, feed is None), and `community = find_community(core_activity)`
        (app/activitypub/util.py:4544) searches the ACTIVITY'S OWN
        audience/cc/to/target fields for a matching Community row -- an
        entirely different lookup from the actor-resolution above.

    (b) ANNOUNCED: the outer Announce's actor resolves through lines
        861-870 (community, then feed, then user, in that priority). If a
        Community resolves, `community` is set directly and `feed`/`user`
        stay None. If a Feed resolves, `feed` is set and `community` stays
        None. Then, since `request_json['type'] == 'Announce'`, lines
        914-924 run before the core-activity dispatch: if `feed` is falsy
        (i.e. `community` was set, or neither was), `user` is
        REASSIGNED to whoever the INNER activity names as its own `actor`
        (`request_json['object']['actor']`) -- the real moderator who
        performed the action inside the community, banned-checked. `feed`
        truthy skips this reassignment (`user = None`). `announced = True`
        and `core_activity = request_json['object']` (the inner Add).
        Reaching line 1400: line 1401 fires with that reassigned `user`
        (so `mod` is the real inner actor for the community case, and stays
        unset for the feed case -- irrelevant, since the feed branch never
        reads `mod`). Line 1403's `if not announced and not feed:` is now
        FALSE (announced is True), so `community`/`feed` are NOT
        overwritten by `find_community` -- they keep whatever the preamble
        resolved.

  So: a Feed actor (Announced) reaches the FEED branch (1405); a Community
  actor (Announced) or a User actor whose activity's own audience/cc/to/
  target names a Community (direct) reaches the `elif community:` branch
  (1424); anything else -- User actor, direct, with no community
  resolvable by `find_community` -- falls to the final `else` (1469-1470).

  FEED branch (1405-1423): `community_to_add` is looked up by
  `core_activity['object']['id']` (community_only=True). If it resolves to
  a real Community (1408): a FeedItem row is created, `feed.num_communities`
  is incremented, committed (1409-1412), then (1413-1423, inside the SAME
  guard since Task 6's fix) every FeedMember of the feed is walked; the
  feed's own owner (`fm_user.id == feed.user_id` -- Feed.user_id, NOT
  FeedMember.is_owner) is skipped (1417-1418); every remaining LOCAL member
  with `feed_auto_follow` set gets `do_subscribe(actor, fm_user.id,
  joined_via_feed=True)` (1419-1423), where `actor` is
  `community_to_add.ap_id or community_to_add.name`. THE WHOLE FEED BRANCH
  NEVER CALLS log_incoming_ap -- not on the guard's success, not on its
  failure (community_to_add falsy/wrong type falls straight through with no
  else and no log). Registered as a finding (Task 6 already pinned the
  unresolvable-community case; this task pins the success case the same
  way), NOT fixed.

  `elif community:` branch (1424-1468):
    - permission guard (1425-1427): `not community.is_moderator(mod) and
      not community.is_instance_admin(mod)` -- denies and logs FAILURE
      'Does not have permission', returns, if BOTH halves are true.
    - `target = core_activity['target']` (1428) used to be read with no
      guard, so a peer omitting `target` got an uncaught KeyError (D88).
      A missing or non-string target is now refused and logged.
    - featured/sticky target (1429-1442): if `community.ap_featured_url`
      is empty, it is BACKFILLED to `community.ap_profile_id + '/featured'`
      (1429-1430) before being read into `featured_url` (1431) -- so this
      branch's own read always sees a populated value, seeded or not. The
      comparison (1433) is `target.lower() == featured_url.lower()`, i.e.
      genuinely case-insensitive on BOTH sides. On match: `Post.get_by_ap_id
      (core_activity['object'])` (exact string match, no case-folding); if
      found, `post.sticky = True`, committed, SUCCESS logged; if not found,
      FAILURE 'Cannot find: <object>' logged. Either way, returns (1442).
    - moderators-url target (1443-1467): compared with a PLAIN `==`
      (1443) -- exact, case-SENSITIVE, unlike the featured-url compare two
      lines above it. On match: the object is looked up as an actor
      (default kwargs -- creates/fetches if not already known); if found,
      an existing CommunityMember row for that user is flipped to
      is_moderator=True (1448-1449), or a new one is created with
      is_moderator=True (1450-1453) if none exists -- never duplicated.
      `add_to_modlog('add_mod', ...)` is called (1454-1455), several
      memoized caches are invalidated (1457-1462), and SUCCESS is logged
      (1463). If the actor can't be resolved: FAILURE 'Cannot find:
      <object>' (1465-1466). Either way, returns (1467).
    - neither URL matches (1468): FAILURE 'Unknown target for Add', no
      return needed (falls off the end of the `elif`, which is the last
      thing in it).

  final `else` (1469-1470): neither `community` nor `feed` was resolved by
  anything above -- FAILURE 'Add: cannot find community or feed'.
"""

import pytest

from app import db
from app.activitypub import routes as activitypub_routes
from app.constants import APLOG_ADD, SUBSCRIPTION_OWNER
from app.models import ActivityPubLog, CommunityJoinRequest, CommunityMember, FeedItem, InstanceRole, utcnow
from app.utils import community_membership
from tests.factories import (inbox_activity, make_community, make_community_join_request,
                             make_community_member, make_feed, make_feed_item, make_feed_member,
                             make_instance, make_post, make_user)
from sqlalchemy import inspect as sa_inspect

from tests.test_inbox_dispatch_follow import record_sends
from tests.test_inbox_dispatch_lock_delete import record_moderation
from tests.test_inbox_dispatch_preamble import dispatch

import app.community.routes as community_routes


def _seed_feed_with_local_auto_follow_member(host='peer.example'):
    """A Feed reachable through the preamble's actor resolution (it is the
    request's `actor`), plus one local FeedMember whose `feed_auto_follow`
    is True (the column's own default) -- the minimum needed to reach the
    unguarded `community_to_add.ap_id` read.
    """
    instance = make_instance(host)
    feed = make_feed(instance)
    member = make_user(None, 'localmember', local=True)
    make_feed_member(member, feed)
    return instance, feed, member


def test_an_add_whose_community_cannot_be_resolved_does_not_touch_feed_members(
        app, db_session, monkeypatch):
    """routes.py's Add/feed branch (current numbering ~1405-1423). The
    FeedItem creation is guarded by
    `if community_to_add and isinstance(community_to_add, Community)`, but
    (pre-fix) the feed_members loop below it is NOT -- and it reads
    `community_to_add.ap_id`, so an unresolvable community raises
    AttributeError once the feed has at least one local, auto-follow member.

    Asserts the corrected behaviour: no FeedItem is created, do_subscribe is
    never called, and no exception escapes. LOG_ACTIVITYPUB_TO_DB is turned
    on so the ActivityPubLog assertion is load-bearing (it defaults False,
    under which log_incoming_ap writes nothing regardless of the fix,
    making the same assertion vacuous) -- this branch of the Add arm calls
    no log_incoming_ap on any path, a registered finding this pins.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, feed, member = _seed_feed_with_local_auto_follow_member()
    assert member.is_local() and member.feed_auto_follow

    # Wrap, don't replace: only the Add arm's own re-lookup of the community
    # named in the Add (community_only=True, create_if_not_found defaulting
    # True) needs interception -- that is the one call that would otherwise
    # risk a real outbound fetch for an id this test deliberately makes
    # unresolvable. Both of the preamble's own probes (routes.py:862's
    # community_only=True/create_if_not_found=False miss, and :864's
    # feed_only=True/create_if_not_found=False hit) fall through to the real
    # find_actor_or_create_cached, so the preamble's feed lookup genuinely
    # resolves the seeded Feed row from the database rather than being
    # handed it by the double.
    real_find_actor_or_create_cached = activitypub_routes.find_actor_or_create_cached

    def _find(actor, create_if_not_found=True, community_only=False, feed_only=False):
        if create_if_not_found:  # the Add arm's re-lookup of an unresolvable id
            return None
        return real_find_actor_or_create_cached(
            actor, create_if_not_found=create_if_not_found,
            community_only=community_only, feed_only=feed_only)

    monkeypatch.setattr(activitypub_routes, 'find_actor_or_create_cached', _find)

    do_subscribe_calls = []
    monkeypatch.setattr(
        community_routes, 'do_subscribe',
        lambda *args, **kwargs: do_subscribe_calls.append((args, kwargs)))

    inner_add = {
        'id': f'{feed.ap_profile_id}/activities/add1',
        'type': 'Add',
        'actor': feed.ap_profile_id,
        'object': {'id': 'https://unresolvable.example/c/ghost'},
        'target': feed.ap_profile_id,
    }
    activity = inbox_activity(feed, activity_type='Announce', object=inner_add)

    dispatch(activity)

    assert FeedItem.query.count() == 0
    assert do_subscribe_calls == []
    assert ActivityPubLog.query.count() == 0


# --- Task 7: the rest of the Add arm ---
#
# See this module's docstring for the full outcome table derived from
# source, and for exactly why the brief's line numbers needed re-deriving.


def test_the_feed_branchs_success_path_subscribes_non_owners_and_logs_nothing(
        app, db_session, monkeypatch):
    """routes.py:1405-1423 -- the success path Task 6's test does not cover
    (that test's community is deliberately UNRESOLVABLE). Here
    `community_to_add` resolves to a real, already-seeded Community, so the
    FeedItem/num_communities guard body (1408-1412) and the auto-subscribe
    loop (1413-1423) both run.

    `feed.num_communities` is seeded to a nonzero baseline (3) rather than
    left at its column default (0, app/models.py:4060) before asserting it
    becomes 4 -- asserting a bare `== 1` here would be indistinguishable
    from the column's own default plus a SEPARATE, unrelated off-by-one
    (e.g. incrementing twice from a wrongly-read 0), per this campaign's
    'beware default-backed assertions' rule.

    The feed's OWNER is identified by `feed.user_id` (Feed.user_id), NOT by
    FeedMember.is_owner -- make_feed_member's `is_owner` flag is a
    DIFFERENT, unrelated column (app/models.py:4034-4035, used by
    Feed.subscribed()). Both the owner and the non-owner here are LOCAL with
    feed_auto_follow True (the column's own default, app/models.py:1033),
    so the owner being skipped is demonstrably due to the `fm_user.id ==
    feed.user_id` check (1417-1418) specifically, not because it fails the
    is_local()/feed_auto_follow test that the non-owner also has to pass.

    `community_to_add.ap_id` is the column make_community() never sets (see
    that factory's docstring) -- it stays None, so the actor string
    do_subscribe receives is `community_to_add.name` (the `ap_id if ap_id
    else name` fallback at routes.py's current :1422).

    Finally, pins the SUCCESS-path half of the finding Task 6 already pinned
    the failure-path half of: the whole feed branch calls log_incoming_ap on
    NO path at all. LOG_ACTIVITYPUB_TO_DB is turned on so the
    ActivityPubLog assertion is load-bearing (see Task 6's docstring for why
    the default-False config makes the same assertion vacuous).
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)

    instance = make_instance('peer.example')
    make_user(instance, 'community_owner')  # occupies user id=1 for make_community's hardcoded owner
    community = make_community(name='addedcomm', host='peer.example')
    community.ap_fetched_at = utcnow()
    db.session.commit()

    feed = make_feed(instance)
    feed.num_communities = 3
    db.session.commit()

    owner = make_user(instance, 'feedowner', local=True)
    feed.user_id = owner.id
    db.session.commit()
    make_feed_member(owner, feed)
    assert owner.is_local() and owner.feed_auto_follow

    nonowner = make_user(instance, 'feedmember', local=True)
    make_feed_member(nonowner, feed)
    assert nonowner.is_local() and nonowner.feed_auto_follow

    do_subscribe_calls = []
    monkeypatch.setattr(
        community_routes, 'do_subscribe',
        lambda *args, **kwargs: do_subscribe_calls.append((args, kwargs)))

    inner_add = {
        'id': f'{feed.ap_profile_id}/activities/add-success',
        'type': 'Add',
        'actor': feed.ap_profile_id,
        'object': {'id': community.ap_profile_id},
        'target': feed.ap_profile_id,
    }
    activity = inbox_activity(feed, activity_type='Announce', object=inner_add)

    dispatch(activity)

    # dispatch() runs the arm on an independent session (get_task_session());
    # this session's expire_on_commit does not fire from ANOTHER session's
    # commit, so `feed` would otherwise still read back its pre-dispatch,
    # in-memory value. Established convention (tests/test_inbox_dispatch_
    # lock_delete.py) is an explicit expire_all() before re-reading a
    # pre-existing row's attributes.
    db.session.expire_all()

    assert FeedItem.query.filter_by(feed_id=feed.id, community_id=community.id).count() == 1
    assert feed.num_communities == 4
    assert community.ap_id is None
    assert do_subscribe_calls == [((community.name, nonowner.id), {'joined_via_feed': True})]
    assert ActivityPubLog.query.count() == 0


def _seed_community_with_mod_and_admin(host='peer.example', name='modcomm'):
    """A Community with two distinguishable actors: a moderator who is NOT
    an instance admin, and an instance admin who is NOT a moderator -- the
    two fixtures Task 7's permission-guard mutants each need a DISTINCT
    killer for (see the module docstring's brief-derived outcome table).

    `Community.is_moderator` reads CommunityMember rows for the community
    (app/models.py:719-723, via `moderators()`, filtered to is_owner-or-
    is_moderator and not banned). `Community.is_instance_admin` reads
    InstanceRole scoped to the COMMUNITY'S OWN instance_id (app/models.py:
    752-759) -- make_community() hardcodes that to 1, which is why `host`'s
    Instance is created FIRST here (occupying id=1, per db_session's
    TRUNCATE ... RESTART IDENTITY) and make_community()'s owner-user slot
    (also hardcoded to user id=1) is occupied next, matching the ordering
    every other test in this campaign that calls make_community() uses.

    Both fixtures are asserted here, immediately, to establish that each one
    leaves the OTHER half of the guard's `and` false -- required before any
    mutation is run against them (see this task's report).
    """
    instance = make_instance(host)
    make_user(instance, 'community_owner')
    community = make_community(name=name, host=host)
    community.ap_fetched_at = utcnow()
    db.session.commit()

    moderator = make_user(instance, 'moduser')
    moderator.ap_fetched_at = utcnow()
    make_community_member(moderator, community, is_moderator=True)
    db.session.commit()

    admin = make_user(instance, 'adminuser')
    admin.ap_fetched_at = utcnow()
    db.session.add(InstanceRole(instance_id=community.instance_id, user_id=admin.id, role='admin'))
    db.session.commit()

    assert community.is_moderator(moderator) is True
    assert community.is_instance_admin(moderator) is False
    assert community.is_moderator(admin) is False
    assert community.is_instance_admin(admin) is True

    return instance, community, moderator, admin


def _add_from_community(community, mod, object_value, target=None, include_target=True):
    """An Announce whose actor is `community` wrapping a real Add activity
    whose OWN `actor` is `mod` -- the only construction (per the module
    docstring's outcome table, case (b)) that reaches the `elif community:`
    branch with BOTH `community` pre-resolved by the preamble's
    community_only lookup (routes.py:862) AND `mod` set to a real user, via
    routes.py's current :915 reassignment of `user` to the INNER activity's
    own `actor` field (reached because `feed` is falsy once `community`
    resolved first).

    `include_target=False` omits the `target` key entirely, for the
    unguarded-read probe.
    """
    inner_add = {
        'id': f'{community.ap_profile_id}/activities/add-{object_value[-6:] if isinstance(object_value, str) else "x"}',
        'type': 'Add',
        'actor': mod.ap_profile_id,
        'object': object_value,
    }
    if include_target:
        inner_add['target'] = target
    return inbox_activity(community, activity_type='Announce', object=inner_add)


def test_permission_denied_when_actor_is_neither_moderator_nor_instance_admin(
        app, db_session, monkeypatch):
    """routes.py's current :1425-1427 -- the guard's base case: an actor
    with NEITHER privilege is refused before `target` is ever read.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    make_user(instance, 'community_owner')
    community = make_community(name='denycomm', host='peer.example')
    community.ap_fetched_at = utcnow()
    db.session.commit()

    outsider = make_user(instance, 'outsider')
    outsider.ap_fetched_at = utcnow()
    db.session.commit()
    assert community.is_moderator(outsider) is False
    assert community.is_instance_admin(outsider) is False

    activity = _add_from_community(
        community, outsider, 'https://peer.example/post/does-not-matter',
        target=community.ap_profile_id + '/featured')

    dispatch(activity)

    log = ActivityPubLog.query.one()
    assert log.exception_message == 'Does not have permission'


def test_sticky_backfills_ap_featured_url_and_compares_case_insensitively(
        app, db_session, monkeypatch):
    """routes.py's current :1425-1437. `community.ap_featured_url` starts
    empty (make_community() never sets it -- app/models.py:587's column has
    no default, so it is None), so the backfill at :1429-1430 must run
    before the comparison can ever match anything. The activity's own
    `target` is given in a DIFFERENT case than the backfilled value, so a
    match proves the comparison at :1433 genuinely lowercases both sides
    rather than merely happening to agree.

    Uses the moderator-not-instance-admin fixture: under the MUTATION that
    drops the guard's `not community.is_moderator(mod)` half (leaving only
    `not community.is_instance_admin(mod)`), this fixture's mod (a
    moderator, NOT an admin) would be newly refused -- `post.sticky` would
    stay False and no SUCCESS would be logged, killing that mutant. This is
    the distinct killer for that half.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, moderator, admin = _seed_community_with_mod_and_admin(name='stickycomm1')
    assert community.ap_featured_url is None

    author = make_user(instance, 'postauthor')
    post = make_post(community, author, ap_id='https://peer.example/post/100')
    assert post.sticky is False

    expected_featured_url = community.ap_profile_id + '/featured'
    mismatched_case_target = expected_featured_url.upper()

    activity = _add_from_community(community, moderator, post.ap_id, target=mismatched_case_target)

    dispatch(activity)

    # See the feed-branch test above: dispatch() commits on an independent
    # session, so this session's own `community`/`post` need an explicit
    # expire before their attributes reflect that commit.
    db.session.expire_all()
    assert community.ap_featured_url == expected_featured_url
    assert post.sticky is True

    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_sticky_with_a_pre_set_featured_url_is_not_backfilled(app, db_session, monkeypatch):
    """routes.py's current :1429-1430 -- when `community.ap_featured_url`
    is already set, the backfill's `if not community.ap_featured_url:`
    guard must NOT overwrite it. A second, differently-cased target still
    matches (case-insensitive compare, :1433), covering the pre-set half of
    Step 4 alongside the backfill test above.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, moderator, admin = _seed_community_with_mod_and_admin(name='stickycomm2')

    preset_featured_url = 'https://peer.example/c/stickycomm2/FEATURED'
    community.ap_featured_url = preset_featured_url
    db.session.commit()

    author = make_user(instance, 'postauthor2')
    post = make_post(community, author, ap_id='https://peer.example/post/101')

    activity = _add_from_community(community, admin, post.ap_id, target=preset_featured_url.lower())

    dispatch(activity)

    db.session.expire_all()
    assert community.ap_featured_url == preset_featured_url  # untouched by the backfill
    assert post.sticky is True


def test_sticky_post_not_found_reports_cannot_find(app, db_session, monkeypatch):
    """routes.py's current :1434-1441 -- the target matches the featured
    URL, but `Post.get_by_ap_id` (exact string match) finds nothing.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, moderator, admin = _seed_community_with_mod_and_admin(name='stickycomm3')

    missing_object = 'https://peer.example/post/does-not-exist'
    activity = _add_from_community(
        community, moderator, missing_object, target=community.ap_profile_id + '/featured')

    dispatch(activity)

    log = ActivityPubLog.query.one()
    assert log.exception_message == 'Cannot find: ' + missing_object


def test_add_mod_creates_a_new_membership(app, db_session, monkeypatch):
    """routes.py's current :1443-1463, the no-existing-membership half.
    `add_to_modlog('add_mod', ...)`'s exact keyword arguments are asserted
    via `record_moderation`.

    Uses the instance-admin-not-moderator fixture: under the MUTATION that
    drops the guard's `not community.is_instance_admin(mod)` half (leaving
    only `not community.is_moderator(mod)`), this fixture's mod (an admin,
    NOT a moderator) would be newly refused -- no CommunityMember row would
    be created and add_to_modlog would never fire, killing that mutant.
    This is the distinct killer for that half, complementing the
    moderator-not-admin killer above.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, moderator, admin = _seed_community_with_mod_and_admin(name='addmodcomm1')
    community.ap_moderators_url = community.ap_profile_id + '/moderators'
    db.session.commit()

    calls = record_moderation(monkeypatch, 'add_to_modlog')

    candidate = make_user(instance, 'newmodcandidate')
    candidate.ap_fetched_at = utcnow()
    db.session.commit()
    assert CommunityMember.query.filter_by(community_id=community.id, user_id=candidate.id).count() == 0

    activity = _add_from_community(
        community, admin, candidate.ap_profile_id, target=community.ap_moderators_url)

    dispatch(activity)

    membership = CommunityMember.query.filter_by(community_id=community.id, user_id=candidate.id).one()
    assert membership.is_moderator is True

    assert len(calls['add_to_modlog']) == 1
    args, kwargs = calls['add_to_modlog'][0]
    assert args[0] == 'add_mod'
    # kwargs['actor'] etc. are bound to the dispatcher's OWN task session
    # (get_task_session()), which is committed and closed before dispatch()
    # returns -- reading a plain attribute off them afterward raises
    # DetachedInstanceError (the attribute is expired by that session's own
    # commit, and there is no session left to reload it from). sa_inspect(...)
    # .identity reads the primary key straight off the InstanceState, which
    # needs no session at all -- the same pattern
    # tests/test_inbox_dispatch_lock_delete.py already established for this
    # exact situation.
    assert sa_inspect(kwargs['actor']).identity[0] == admin.id
    assert sa_inspect(kwargs['target_user']).identity[0] == candidate.id
    assert sa_inspect(kwargs['community']).identity[0] == community.id

    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_add_mod_flips_an_existing_membership_rather_than_duplicating_it(
        app, db_session, monkeypatch):
    """routes.py's current :1446-1449 -- a CommunityMember row already
    exists (is_moderator=False); the Add flips it in place rather than
    inserting a second row.
    """
    instance, community, moderator, admin = _seed_community_with_mod_and_admin(name='addmodcomm2')
    community.ap_moderators_url = community.ap_profile_id + '/moderators'
    db.session.commit()

    record_moderation(monkeypatch, 'add_to_modlog')

    candidate = make_user(instance, 'flipmodcandidate')
    candidate.ap_fetched_at = utcnow()
    make_community_member(candidate, community, is_moderator=False)

    activity = _add_from_community(
        community, moderator, candidate.ap_profile_id, target=community.ap_moderators_url)

    dispatch(activity)

    memberships = CommunityMember.query.filter_by(community_id=community.id, user_id=candidate.id).all()
    assert len(memberships) == 1
    assert memberships[0].is_moderator is True


def test_add_mod_unresolvable_actor_reports_cannot_find(app, db_session, monkeypatch):
    """routes.py's current :1444-1445/1464-1466 -- the target matches the
    moderators URL, but the named object can't be resolved to an actor.

    `find_actor_or_create_cached` is wrapped (not replaced) so only the
    deliberately-unresolvable ghost URL is intercepted -- every other call
    on this path (the preamble's community_only lookup, and :915's lookup
    of the inner activity's own `actor`) still runs for real.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, moderator, admin = _seed_community_with_mod_and_admin(name='addmodcomm3')
    community.ap_moderators_url = community.ap_profile_id + '/moderators'
    db.session.commit()

    ghost_url = 'https://unresolvable.example/u/ghost'
    real_find_actor_or_create_cached = activitypub_routes.find_actor_or_create_cached

    def _find(actor, create_if_not_found=True, community_only=False, feed_only=False):
        if actor == ghost_url:
            return None
        return real_find_actor_or_create_cached(
            actor, create_if_not_found=create_if_not_found,
            community_only=community_only, feed_only=feed_only)

    monkeypatch.setattr(activitypub_routes, 'find_actor_or_create_cached', _find)

    activity = _add_from_community(
        community, moderator, ghost_url, target=community.ap_moderators_url)

    dispatch(activity)

    log = ActivityPubLog.query.one()
    assert log.exception_message == 'Cannot find: ' + ghost_url


def test_unknown_target_for_add(app, db_session, monkeypatch):
    """routes.py's current :1468 -- a target matching neither the featured
    URL nor the moderators URL.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, moderator, admin = _seed_community_with_mod_and_admin(name='unknowntargetcomm')
    community.ap_moderators_url = community.ap_profile_id + '/moderators'
    db.session.commit()

    activity = _add_from_community(
        community, moderator, 'https://peer.example/whatever',
        target='https://peer.example/c/unknowntargetcomm/something-else')

    dispatch(activity)

    log = ActivityPubLog.query.one()
    assert log.exception_message == 'Unknown target for Add'


def test_a_target_omitted_entirely_is_refused(app, db_session, monkeypatch):
    """D88, fixed. The community branch read `core_activity['target']` with no
    guard once the permission check passed, so a peer omitting `target` got an
    uncaught KeyError instead of a logged refusal. It is now refused before
    anything on the community, including the featured-URL backfill, changes.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, moderator, admin = _seed_community_with_mod_and_admin(name='notargetcomm')

    activity = _add_from_community(
        community, moderator, 'https://peer.example/whatever', include_target=False)

    dispatch(activity)

    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Add has no target'



def test_add_with_neither_community_nor_feed_resolvable_is_refused(app, db_session, monkeypatch):
    """routes.py's current :1469-1470 -- the final `else`. A DIRECT
    (not Announced) Add from a plain User actor, whose activity carries no
    audience/cc/to/target that `find_community` (app/activitypub/util.py:
    4544) can resolve against any Community row, and which is not itself a
    Feed or Community actor. Neither `community` nor `feed` is ever set.

    `object` is a dict here, not a plain string. find_community's own final
    fallback (util.py's current :4568-4581, `rj = request_json['object'] if
    'object' in request_json else request_json` followed unconditionally by
    `rj.get('type')`) assumes `rj` is dict-shaped whenever it takes that
    branch -- but the assignment does not check the type of
    `request_json['object']`, so a plain-string `object` (entirely legal for
    a direct Add, e.g. Lemmy's own sticky/add_mod activities commonly carry
    one) reaches `rj.get('type')` as a bare string and raises
    `AttributeError: 'str' object has no attribute 'get'`. That is a real,
    separate defect in a function OTHER arms of process_inbox_request also
    call, out of this task's scope (the Add arm itself, not find_community);
    noted here rather than fixed, and sidestepped by giving `object` a dict
    shape so this test exercises the Add arm's own final `else`, not
    find_community's.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    sender = make_user(instance, 'aloneuser')
    sender.ap_fetched_at = utcnow()
    db.session.commit()

    activity = inbox_activity(sender, activity_type='Add',
                              object={'id': 'https://peer.example/objects/whatever'})

    dispatch(activity)

    log = ActivityPubLog.query.one()
    assert log.exception_message == 'Add: cannot find community or feed'


# --- Task 8: FIX 5 and FIX 6 -- Remove's unguarded feed item and membership ---
#
# The Remove arm's feed branch (routes.py, current numbering) is reached the
# same way as Add's: the activity's actor resolves to a Feed (the Announce
# actor-resolution preamble), and the inner activity's `object` carries an
# `id`. Two live defects sat in that branch's auto-unsubscribe machinery,
# both unguarded lookups that can legitimately return None:
#
#   FIX 5 (current :1482-1486): `feed_item = session.query(FeedItem).
#   filter_by(feed_id=feed.id, community_id=community_to_remove.id).first()`
#   is None when the community named by the Remove was never actually in the
#   feed. `session.delete(feed_item)` then raises, and the following
#   `feed.num_communities -= 1` would have decremented for a removal that
#   never happened, had it been reached. Fixed by wrapping the delete, the
#   decrement and their commit in `if feed_item:` -- the auto-unsubscribe
#   loop directly below stays OUTSIDE that new guard, at its original
#   indentation under the `if community_to_remove and isinstance(...)` guard,
#   since it has its own reason to run (auto-leaving members) independent of
#   whether a FeedItem existed to delete.
#
#   FIX 6 (current :1498): inside that loop, `cm = session.query(
#   CommunityMember).filter_by(user_id=fm_user.id, community_id=
#   community_to_remove.id).first()` is None for a feed member who never
#   actually joined the community being removed (no CommunityMember row for
#   that pair). The next line unconditionally read `cm.joined_via_feed`,
#   raising AttributeError. Fixed by short-circuiting on `cm and
#   cm.joined_via_feed`.
#
# Both brief citations (routes.py:1478-1481 and :1494) were STALE -- earlier
# tasks' fixes shifted the arm; the current lines are :1482-1486 and :1498,
# re-derived from source rather than trusted from the brief.


def test_a_remove_for_a_community_not_in_the_feed_is_a_no_op(app, db_session, monkeypatch):
    """routes.py's current :1482-1486. The FeedItem lookup returns None when
    the community was never in the feed, and (pre-fix) session.delete(None)
    raises -- and num_communities would be decremented for a removal that
    never happened, had the delete not raised first.

    `feed.num_communities` is seeded to a nonzero baseline (3) rather than
    left at its column default (0) before asserting it stays unchanged --
    per this campaign's 'beware default-backed assertions' rule, a bare
    `== 0` here would be indistinguishable from the column's own default.

    No FeedItem row is ever created for this feed/community pair -- that
    omission is the whole point of the test. LOG_ACTIVITYPUB_TO_DB is turned
    on so the ActivityPubLog assertion is load-bearing (the feed branch
    calls log_incoming_ap on no path at all, same finding Task 6/7 already
    pinned for Add's feed branch).
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)

    instance = make_instance('peer.example')
    make_user(instance, 'community_owner')  # occupies user id=1 for make_community's hardcoded owner
    community = make_community(name='neverinfeed', host='peer.example')
    community.ap_fetched_at = utcnow()
    db.session.commit()

    feed = make_feed(instance)
    feed.num_communities = 3
    db.session.commit()

    assert FeedItem.query.filter_by(feed_id=feed.id, community_id=community.id).count() == 0

    inner_remove = {
        'id': f'{feed.ap_profile_id}/activities/remove-noop',
        'type': 'Remove',
        'actor': feed.ap_profile_id,
        'object': {'id': community.ap_profile_id},
        'target': feed.ap_profile_id,
    }
    activity = inbox_activity(feed, activity_type='Announce', object=inner_remove)

    dispatch(activity)

    # dispatch() runs the arm on an independent session (get_task_session());
    # this session's expire_on_commit does not fire from ANOTHER session's
    # commit, so `feed` would otherwise still read back its pre-dispatch,
    # in-memory value. Established convention (tests/test_inbox_dispatch_
    # lock_delete.py) is an explicit expire_all() before re-reading a
    # pre-existing row's attributes.
    db.session.expire_all()

    assert FeedItem.query.filter_by(feed_id=feed.id, community_id=community.id).count() == 0
    assert feed.num_communities == 3
    assert ActivityPubLog.query.count() == 0


def test_a_remove_skips_a_feed_member_with_no_community_membership(
        app, db_session, monkeypatch):
    """routes.py's current :1498. `cm` is None when the feed member never
    actually joined the community being removed (no CommunityMember row for
    that user/community pair), and (pre-fix) the next line's
    `cm.joined_via_feed` raises AttributeError.

    The feed's OWNER is identified by `feed.user_id` (Feed.user_id), skipped
    by the loop's `fm_user.id == feed.user_id` check before it ever reaches
    `cm` -- so the owner is given a real CommunityMember-less setup too,
    proving the stray member's skip is due to FIX 6 specifically, not
    because it never got past the owner check.

    The community is made explicitly non-local (`community.ap_id` is set to
    a peer address -- plain make_community() never sets this column, so it
    would otherwise read back local regardless of host) so that, had the
    guard not short-circuited, the arm would have gone on to attempt a
    federated Undo/Follow send; asserting `sends == []` proves the guard
    stopped the loop before that, not merely before the AttributeError.

    A FeedItem for this feed/community pair DOES exist here (unlike the
    no-op test above), so FIX 5's guard passes normally and the delete/
    decrement runs -- isolating this test to FIX 6 alone.

    `community.subscriptions_count` is seeded to a nonzero baseline (2)
    rather than left at its column default (0) before asserting it is
    unchanged -- per this campaign's 'beware default-backed assertions'
    rule, a bare `== 0` here would be indistinguishable from the column's
    own default sitting unexamined.
    """
    instance = make_instance('peer.example')
    make_user(instance, 'community_owner')  # occupies user id=1 for make_community's hardcoded owner
    community = make_community(name='straymembercomm', host='peer.example')
    community.ap_fetched_at = utcnow()
    community.ap_id = 'straymembercomm@peer.example'  # make this community non-local
    community.subscriptions_count = 2
    db.session.commit()
    assert community.is_local() is False

    feed = make_feed(instance)
    make_feed_item(feed, community)
    feed.num_communities = 1
    db.session.commit()

    owner = make_user(instance, 'feedowner', local=True)
    feed.user_id = owner.id
    db.session.commit()
    make_feed_member(owner, feed)

    stray = make_user(instance, 'strayfeedmember', local=True)
    make_feed_member(stray, feed)
    assert stray.is_local() and stray.feed_auto_leave
    assert CommunityMember.query.filter_by(user_id=stray.id, community_id=community.id).count() == 0

    sends = record_sends(monkeypatch)

    inner_remove = {
        'id': f'{feed.ap_profile_id}/activities/remove-straymember',
        'type': 'Remove',
        'actor': feed.ap_profile_id,
        'object': {'id': community.ap_profile_id},
        'target': feed.ap_profile_id,
    }
    activity = inbox_activity(feed, activity_type='Announce', object=inner_remove)

    dispatch(activity)

    db.session.expire_all()

    # FIX 6 skipped the stray member without touching its (nonexistent)
    # CommunityMember row or sending anything on its behalf.
    assert CommunityMember.query.filter_by(user_id=stray.id, community_id=community.id).count() == 0
    assert sends == []
    assert community.subscriptions_count == 2

    # FIX 5's guard still ran normally for this feed/community pair, since a
    # FeedItem genuinely existed here.
    assert FeedItem.query.filter_by(feed_id=feed.id, community_id=community.id).count() == 0
    assert feed.num_communities == 0


# --- Task 9: Remove's feed branch and its auto-unsubscribe loop ---
#
# routes.py's current numbering for this block: the guard at :1481, FIX 5's
# feed_item delete/decrement at :1482-1487, the feed_members loop opening at
# :1490-1491, the owner skip at :1493-1494, the is_local()/feed_auto_leave
# gate at :1495-1496, FIX 6's cm lookup at :1497-1499, the proceed body at
# :1500-1530 (Undo-sending at :1502-1520, the ovo.st special case inside that
# at :1505-1509, and the membership deletion/decrement/SUCCESS log at
# :1522-1530). The brief cited :1474-1522 for the whole block and :1488-1494
# / :1497-1513 / :1500-1505 / :1517-1521 for its sub-pieces; EVERY one of
# those was STALE by one line relative to source (the block now starts at
# :1473, not :1474, and everything below it is shifted the same one line) --
# 6 of 6 citations stale. Every test below cites the CURRENT line, re-derived
# by reading the arm in full rather than trusted from the brief.
#
# do_subscribe's ap_id-vs-name fallback established in Task 7's success-path
# test is NOT this arm's concern -- Remove's loop never reads
# community_to_remove.ap_id at all; the actor string it builds is
# fm_user.public_url(), read directly off the User row.


def _seed_removable_feed_community(host='peer.example', name='removecomm', instance_domain=None):
    """A Community already IN a Feed (a real FeedItem row), reachable through
    the Announce/Feed-actor preamble the same way Add's tests are. Both
    make_community()'s hardcoded instance_id=1/user_id=1 owner slots are
    occupied first, following every other test in this module.

    `instance_domain` lets a caller put the FIRST Instance row (id=1, which
    make_community() hardcodes its Community.instance_id to) on a domain
    different from the one the community's own AP identity is published on --
    needed by the ovo.st tests below, where `community_to_remove.instance.domain`
    (routes.py's current :1505) must read 'ovo.st' regardless of what host the
    community's ap_profile_id names.

    `feed.num_communities` is seeded to 1 (not left at its column default 0)
    so a test asserting it becomes 0 after the FeedItem is removed can tell
    that apart from the default plus an unrelated bug that never incremented
    it at all -- this campaign's 'beware default-backed assertions' rule.
    """
    instance = make_instance(instance_domain or host)
    make_user(instance, 'community_owner')  # occupies user id=1 for make_community's hardcoded owner
    community = make_community(name=name, host=host)
    community.ap_fetched_at = utcnow()
    db.session.commit()

    feed = make_feed(instance)
    make_feed_item(feed, community)
    feed.num_communities = 1
    db.session.commit()

    return instance, community, feed


def _make_would_proceed_feed_member(name, instance, feed, community, local=True, feed_auto_leave=None):
    """A FeedMember, paired with a real CommunityMember row for `community`,
    built so the auto-unsubscribe loop would proceed for it UNLESS the
    caller deliberately flips one thing off: it is never the feed's owner
    (callers that want the owner case set `feed.user_id` to this member's id
    themselves, afterward), it is local unless `local=False`, its
    feed_auto_leave is True (the column's own default, app/models.py) unless
    `feed_auto_leave` overrides it, and its CommunityMember row has
    joined_via_feed explicitly set True (make_community_member's factory
    default is False on this column). routes.py's current :1499 guard is
    `subscription != SUBSCRIPTION_OWNER and cm and cm.joined_via_feed` --
    THREE separate conjuncts, not two: `cm` truthy (a CommunityMember row
    exists at all) and `cm.joined_via_feed` truthy (that row was joined via
    a feed) are TWO DISTINCT conjuncts with two distinct killers, each
    needing its own dedicated test (see
    test_remove_loop_skips_a_feed_member_with_no_community_membership for
    `cm is None`, and
    test_remove_loop_skips_a_feed_member_whose_membership_was_not_joined_via_feed
    for `cm.joined_via_feed is False`) -- setting joined_via_feed True here
    satisfies both of them so this helper's callers reach 'proceed'. is_owner
    stays False (make_community_member's own default), which is what makes
    User.subscribed() read SUBSCRIPTION_MEMBER rather than SUBSCRIPTION_OWNER
    -- the guard's third, independent conjunct.
    """
    member = make_user(instance, name, local=local)
    if feed_auto_leave is not None:
        member.feed_auto_leave = feed_auto_leave
    db.session.commit()
    make_feed_member(member, feed)
    cm = make_community_member(member, community, is_moderator=False)
    cm.joined_via_feed = True
    db.session.commit()
    return member, cm


def _remove_activity_for(feed, community):
    inner_remove = {
        'id': f'{feed.ap_profile_id}/activities/remove-{community.name}',
        'type': 'Remove',
        'actor': feed.ap_profile_id,
        'object': {'id': community.ap_profile_id},
        'target': feed.ap_profile_id,
    }
    return inbox_activity(feed, activity_type='Announce', object=inner_remove)


def test_remove_loop_skips_the_feed_owner_who_would_otherwise_proceed(app, db_session, monkeypatch):
    """routes.py's current :1493-1494: `if fm_user.id == feed.user_id:
    continue`. The owner here is built with EVERY other condition set to
    the value that would let the loop proceed -- local, feed_auto_leave
    True, and a real CommunityMember row with joined_via_feed True and
    is_owner False -- so the skip proven here is demonstrably due to the
    owner check alone, not because it also fails is_local(), feed_auto_leave,
    or the CommunityMember lookup (each covered as ITS OWN isolated test
    below / in Task 8).

    `community.ap_id` is left unset (make_community()'s own default), so
    `community.is_local()` is True here -- deliberately irrelevant to this
    test, since a skip via `continue` happens before the is_local() check is
    even reached; using the local shape just keeps this test decoupled from
    the remote/Undo machinery Steps 4-5 cover separately.
    """
    instance, community, feed = _seed_removable_feed_community(name='ownerskipcomm')
    community.subscriptions_count = 1
    db.session.commit()

    owner, cm = _make_would_proceed_feed_member('feedowner', instance, feed, community)
    feed.user_id = owner.id
    db.session.commit()
    assert owner.id == feed.user_id
    assert owner.is_local() and owner.feed_auto_leave
    assert cm.joined_via_feed is True and cm.is_owner is False

    sends = record_sends(monkeypatch)

    activity = _remove_activity_for(feed, community)
    dispatch(activity)

    db.session.expire_all()

    # `continue` fired before anything below it ran: the owner's own
    # CommunityMember row is untouched, nothing was sent, and the community's
    # subscriptions_count (seeded to a nonzero baseline, not left at its
    # default 0) is unchanged.
    assert CommunityMember.query.filter_by(user_id=owner.id, community_id=community.id).count() == 1
    assert sends == []
    assert community.subscriptions_count == 1

    # FIX 5's guard (routes.py's current :1482-1487) is independent of the
    # loop and still ran normally: the FeedItem existed, so it is gone and
    # num_communities is decremented from its seeded baseline.
    assert FeedItem.query.filter_by(feed_id=feed.id, community_id=community.id).count() == 0
    assert feed.num_communities == 0


def test_remove_loop_skips_a_nonlocal_feed_member_who_would_otherwise_proceed(
        app, db_session, monkeypatch):
    """routes.py's current :1495: `if fm_user.is_local() and
    fm_user.feed_auto_leave:`. This member is NOT the feed's owner, has
    feed_auto_leave True (irrelevant here since is_local() alone already
    short-circuits the `and`) and a real CommunityMember row with
    joined_via_feed True -- every OTHER skip condition is false, isolating
    this test to the is_local() half specifically.
    """
    instance, community, feed = _seed_removable_feed_community(name='remoteskipcomm')

    owner = make_user(instance, 'feedowner', local=True)
    feed.user_id = owner.id
    db.session.commit()
    make_feed_member(owner, feed)

    remote_member, cm = _make_would_proceed_feed_member(
        'remotefeedmember', instance, feed, community, local=False)
    assert remote_member.id != feed.user_id
    assert remote_member.is_local() is False
    assert remote_member.feed_auto_leave is True
    assert cm.joined_via_feed is True

    sends = record_sends(monkeypatch)

    activity = _remove_activity_for(feed, community)
    dispatch(activity)

    db.session.expire_all()

    assert CommunityMember.query.filter_by(
        user_id=remote_member.id, community_id=community.id).count() == 1
    assert sends == []


def test_remove_loop_skips_a_member_with_feed_auto_leave_false(app, db_session, monkeypatch):
    """routes.py's current :1495: the `and fm_user.feed_auto_leave` half.
    This member is NOT the feed's owner, IS local, and has a real
    CommunityMember row with joined_via_feed True -- every OTHER skip
    condition is false, isolating this test to feed_auto_leave specifically.
    `feed_auto_leave` is set False explicitly, since the column's own default
    (app/models.py:1033) is True -- the value every OTHER test in this
    module relies on for its "would otherwise proceed" members.
    """
    instance, community, feed = _seed_removable_feed_community(name='autoleaveskipcomm')

    owner = make_user(instance, 'feedowner', local=True)
    feed.user_id = owner.id
    db.session.commit()
    make_feed_member(owner, feed)

    stayer, cm = _make_would_proceed_feed_member(
        'stayingfeedmember', instance, feed, community, feed_auto_leave=False)
    assert stayer.id != feed.user_id
    assert stayer.is_local() is True
    assert stayer.feed_auto_leave is False
    assert cm.joined_via_feed is True

    sends = record_sends(monkeypatch)

    activity = _remove_activity_for(feed, community)
    dispatch(activity)

    db.session.expire_all()

    assert CommunityMember.query.filter_by(
        user_id=stayer.id, community_id=community.id).count() == 1
    assert sends == []


def test_remove_proceeds_for_a_local_community_and_sends_nothing(app, db_session, monkeypatch):
    """Steps 1, 3 and 6. `community.ap_id` is left unset (make_community()'s
    own default), so `community_to_remove.is_local()` (routes.py's current
    :1502) is True and the whole Undo-sending block is skipped, falling
    straight to `if proceed:` -- asserted here by an empty `sends` list, not
    merely a full one elsewhere.

    Covers the removal itself (Step 1: the FeedItem is gone, num_communities
    decremented from its seeded baseline) and the deletion/log (Step 6): both
    the CommunityMember and the CommunityJoinRequest row for this member/
    community pair are gone, subscriptions_count is decremented from a
    seeded nonzero baseline (this campaign's 'beware default-backed
    assertions' rule -- Community.subscriptions_count defaults to 0, so a
    bare `== 0` afterward would be indistinguishable from that default), and
    SUCCESS is logged with the member's user_name and the community's
    ap_public_url. LOG_ACTIVITYPUB_TO_DB is turned on so that last assertion
    is load-bearing.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, feed = _seed_removable_feed_community(name='localproceedcomm')
    community.subscriptions_count = 1
    db.session.commit()
    assert community.is_local() is True

    owner = make_user(instance, 'feedowner', local=True)
    feed.user_id = owner.id
    db.session.commit()
    make_feed_member(owner, feed)

    member, cm = _make_would_proceed_feed_member('localproceedmember', instance, feed, community)
    make_community_join_request(member, community, joined_via_feed=True)

    sends = record_sends(monkeypatch)

    activity = _remove_activity_for(feed, community)
    dispatch(activity)

    db.session.expire_all()

    assert sends == []

    # Step 1: the removal itself.
    assert FeedItem.query.filter_by(feed_id=feed.id, community_id=community.id).count() == 0
    assert feed.num_communities == 0

    # Step 6: the deletion and its log.
    assert CommunityMember.query.filter_by(
        user_id=member.id, community_id=community.id).count() == 0
    assert CommunityJoinRequest.query.filter_by(
        user_id=member.id, community_id=community.id).count() == 0
    assert community.subscriptions_count == 0

    log = ActivityPubLog.query.one()
    assert log.exception_message == (
        f'{member.user_name} auto-unfollowed {community.ap_public_url} during a feed/remove')


def _seed_remote_removable_feed_community(host='peer.example', name='remoteproceedcomm',
                                          instance_domain=None):
    """`_seed_removable_feed_community`, with the community's `ap_id` moved
    off-instance so `is_local()` reads False -- plain make_community() never
    sets `ap_id`, so it would otherwise read back local regardless of host.
    `ap_inbox_url` is also set, matching the value production's
    `send_post_request(community_to_remove.ap_inbox_url, ...)` call
    (routes.py's current :1518) would actually target.
    """
    instance, community, feed = _seed_removable_feed_community(
        host=host, name=name, instance_domain=instance_domain)
    community.ap_id = f'{name}@{host}'
    community.ap_inbox_url = f'https://{host}/c/{name}/inbox'
    db.session.commit()
    assert community.is_local() is False
    return instance, community, feed


def test_remove_sends_an_undo_wrapping_a_follow_for_a_remote_community(app, db_session, monkeypatch):
    """routes.py's current :1502-1520: for a remote community whose
    instance is not gone_forever, an Undo wrapping a Follow is sent. Asserts
    the federation contract on the recorded body -- not merely a send count,
    which would show full line coverage while leaving the shape of the
    payload untested.
    """
    instance, community, feed = _seed_remote_removable_feed_community()
    community.subscriptions_count = 1
    db.session.commit()
    assert instance.gone_forever is False

    owner = make_user(instance, 'feedowner', local=True)
    feed.user_id = owner.id
    db.session.commit()
    make_feed_member(owner, feed)

    member, cm = _make_would_proceed_feed_member('undomember', instance, feed, community)

    sends = record_sends(monkeypatch)

    activity = _remove_activity_for(feed, community)
    dispatch(activity)

    db.session.expire_all()

    assert len(sends) == 1
    uri, body, key_id = sends[0]
    assert uri == community.ap_inbox_url
    assert body['type'] == 'Undo'
    assert body['object']['type'] == 'Follow'
    assert body['actor'] == member.public_url()
    assert body['object']['object'] == community.public_url()
    assert key_id == member.public_url() + '#main-key'

    # The membership is still deleted after the Undo is sent.
    assert CommunityMember.query.filter_by(
        user_id=member.id, community_id=community.id).count() == 0
    assert community.subscriptions_count == 0


def test_remove_sends_nothing_when_the_remote_instance_is_gone_forever(app, db_session, monkeypatch):
    """routes.py's current :1503: `if not community_to_remove.instance.
    gone_forever:` guards the Undo send. `gone_forever` True means no send,
    but `proceed` (set unconditionally at the current :1500, before the
    `is_local()`/`gone_forever` checks) is still True, so the membership is
    deleted regardless.
    """
    instance, community, feed = _seed_remote_removable_feed_community(name='goneforevercomm')
    instance.gone_forever = True
    community.subscriptions_count = 1
    db.session.commit()

    owner = make_user(instance, 'feedowner', local=True)
    feed.user_id = owner.id
    db.session.commit()
    make_feed_member(owner, feed)

    member, cm = _make_would_proceed_feed_member('goneforevermember', instance, feed, community)

    sends = record_sends(monkeypatch)

    activity = _remove_activity_for(feed, community)
    dispatch(activity)

    db.session.expire_all()

    assert sends == []
    assert CommunityMember.query.filter_by(
        user_id=member.id, community_id=community.id).count() == 0
    assert community.subscriptions_count == 0


def test_remove_ovo_st_uses_the_join_requests_uuid_as_the_follow_id(app, db_session, monkeypatch):
    """routes.py's current :1505-1509: when `community_to_remove.instance.
    domain == 'ovo.st'`, the generated `follow_id` is replaced with one built
    from the member's CommunityJoinRequest.uuid, if such a row exists.

    'ovo.st' is a hardcoded literal naming one specific peer instance --
    registered here as a finding, per this task's instructions, NOT fixed.

    The Instance row for 'ovo.st' is created FIRST (occupying id=1, which
    make_community() hardcodes Community.instance_id to), independently of
    the host the community's own AP identity is published on
    (`_seed_remote_removable_feed_community`'s `instance_domain` parameter) --
    otherwise `community_to_remove.instance.domain` would read back
    whatever `host` was, not 'ovo.st'.
    """
    instance, community, feed = _seed_remote_removable_feed_community(
        host='peer.example', name='ovostjoincomm', instance_domain='ovo.st')
    assert community.instance.domain == 'ovo.st'

    owner = make_user(instance, 'feedowner', local=True)
    feed.user_id = owner.id
    db.session.commit()
    make_feed_member(owner, feed)

    member, cm = _make_would_proceed_feed_member('ovostjoinmember', instance, feed, community)
    join_request = make_community_join_request(member, community, joined_via_feed=True)
    # Captured now, before dispatch(): dispatch() deletes this row on its own
    # session and commits, which expires every attribute on OUR session's
    # copy of `join_request` (Flask-SQLAlchemy's default expire_on_commit) --
    # reading `.uuid` off it afterward would try to reload a row that no
    # longer exists and raise ObjectDeletedError.
    expected_join_request_uuid = join_request.uuid

    sends = record_sends(monkeypatch)

    activity = _remove_activity_for(feed, community)
    dispatch(activity)

    expected_follow_id = f"{app.config['SERVER_URL']}/activities/follow/{expected_join_request_uuid}"
    assert len(sends) == 1
    _uri, body, _key_id = sends[0]
    assert body['object']['id'] == expected_follow_id


def test_remove_ovo_st_keeps_the_generated_follow_id_when_no_join_request_exists(
        app, db_session, monkeypatch):
    """routes.py's current :1505-1509, the other half: 'ovo.st' with NO
    CommunityJoinRequest row for this user/community pair -- the generated
    `follow_id` (routes.py's current :1504, `gibberish(15)`) is kept.
    `gibberish` is patched at its `activitypub_routes` binding site (the
    module imports it by name, `from app.utils import gibberish, ...`) to a
    fixed value so the generated id is a known, assertable string rather
    than an unpredictable random one.
    """
    instance, community, feed = _seed_remote_removable_feed_community(
        host='peer.example', name='ovostnojoincomm', instance_domain='ovo.st')
    assert community.instance.domain == 'ovo.st'

    owner = make_user(instance, 'feedowner', local=True)
    feed.user_id = owner.id
    db.session.commit()
    make_feed_member(owner, feed)

    member, cm = _make_would_proceed_feed_member('ovostnojoinmember', instance, feed, community)
    assert CommunityJoinRequest.query.filter_by(
        user_id=member.id, community_id=community.id).count() == 0

    monkeypatch.setattr(activitypub_routes, 'gibberish', lambda length=10: 'fixedgibberish')

    sends = record_sends(monkeypatch)

    activity = _remove_activity_for(feed, community)
    dispatch(activity)

    expected_follow_id = f"{app.config['SERVER_URL']}/activities/follow/fixedgibberish"
    assert len(sends) == 1
    _uri, body, _key_id = sends[0]
    assert body['object']['id'] == expected_follow_id


# --- Task 10: Remove's community branch -- the mirror of Add's ---
#
# The brief cited routes.py:1523-1570 for the whole `elif community:` branch,
# and sub-citations :1527-1529 (permission guard), :1537-1545 (sticky
# target), :1545-1565 (moderators-url target), :1528/:1563/:1566/:1568
# (the four APLOG_ADD mislabellings) and :1560-1561 (the modlog-outside-the-
# guard defect). EVERY one of those is STALE relative to source: reading the
# arm in full (see the block starting at the current :1473), the branch
# actually spans :1531-1574, with the permission guard at :1532-1534, the
# sticky target at :1540-1549, the moderators-url target at :1550-1570, the
# four mislabelled log_incoming_ap calls at :1533, :1568, :1571 and :1573,
# and the modlog-outside-`if existing_membership:` defect at :1565-1566.
# Every citation below is the CURRENT line, re-derived from source.
#
# Outcome table for the `elif community:` branch (mirrors Add's, current
# :1531-1571):
#   - permission guard (:1532-1534): `not community.is_moderator(mod) and
#     not community.is_instance_admin(mod)` -- denies and logs FAILURE
#     'Does not have permission', returns, if BOTH halves are true. Unlike
#     Add's identical guard, this one's log_incoming_ap call passes
#     APLOG_ADD, not APLOG_REMOVE -- mislabelling finding #1.
#   - `target = core_activity['target']` (:1535): a missing or non-string
#     target is refused and logged, as in Add (D88, fixed).
#   - featured/sticky target (:1536-1549): backfill and case-insensitive
#     compare identical to Add's; on match, `post.sticky = False` (the
#     opposite of Add's `= True`) is committed and SUCCESS is logged
#     correctly as APLOG_REMOVE (:1545); not-found logs FAILURE 'Cannot
#     find: <object>', also correctly APLOG_REMOVE (:1547).
#   - moderators-url target (:1550-1570): the object is resolved as an
#     actor; if found and an existing CommunityMember row exists, it is
#     flipped to is_moderator=False, several memoized caches invalidated,
#     and SUCCESS logged correctly as APLOG_REMOVE (:1564).
#     `add_to_modlog('remove_mod', ...)` (:1565-1566) sits OUTSIDE the
#     `if existing_membership:` block, at the same indentation as the
#     `if old_mod:` body -- so it fires whenever the actor resolves, even
#     when there was no membership row to flip and hence no log call at
#     all on that path. Mislabelling finding #2 (unresolvable actor,
#     :1568) sits in the `else:` here, passing APLOG_ADD.
#   - neither URL matches (:1571): FAILURE 'Unknown target for Remove',
#     mislabelling finding #3, APLOG_ADD.
#   - final `else` (:1573): neither `community` nor `feed` resolved --
#     FAILURE 'Remove: cannot find community or feed', mislabelling
#     finding #4, APLOG_ADD.
#
# All four mislabelled calls store `activity_type='Add'` (APLOG_ADD[1]) for
# what is, in every case, actually a Remove -- same class as this campaign's
# D63/D68 findings elsewhere. Registered here, NOT fixed (app/ stays closed
# for this task).


def _remove_from_community(community, mod, object_value, target=None, include_target=True):
    """`_add_from_community`'s mirror for the Remove arm's `elif community:`
    branch (current :1531-1571): an Announce whose actor is `community`,
    wrapping a real Remove activity whose OWN `actor` is `mod` -- the
    construction that reaches this branch with `community` pre-resolved by
    the preamble's community_only lookup AND `mod` set to a real user via
    routes.py's current :915 reassignment.

    `include_target=False` omits the `target` key entirely, for the
    unguarded-read probe.
    """
    inner_remove = {
        'id': f'{community.ap_profile_id}/activities/remove-{object_value[-6:] if isinstance(object_value, str) else "x"}',
        'type': 'Remove',
        'actor': mod.ap_profile_id,
        'object': object_value,
    }
    if include_target:
        inner_remove['target'] = target
    return inbox_activity(community, activity_type='Announce', object=inner_remove)


def test_remove_permission_denied_pins_the_aplog_add_mislabelling(
        app, db_session, monkeypatch):
    """routes.py's current :1532-1534 -- the guard's base case: an actor
    with NEITHER privilege is refused before `target` is ever read, mirroring
    Add's identical guard test.

    ALSO pins mislabelling finding #1: the refusal's log_incoming_ap call
    passes APLOG_ADD, not APLOG_REMOVE, so a Remove refusal is stored with
    `activity_type='Add'`. Asserting `log.activity_type == APLOG_ADD[1]`
    documents this defect -- it does NOT endorse it; the correct value would
    be 'Remove'. Same class of finding as D63 and D68.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    make_user(instance, 'community_owner')
    community = make_community(name='removedenycomm', host='peer.example')
    community.ap_fetched_at = utcnow()
    db.session.commit()

    outsider = make_user(instance, 'removeoutsider')
    outsider.ap_fetched_at = utcnow()
    db.session.commit()
    assert community.is_moderator(outsider) is False
    assert community.is_instance_admin(outsider) is False

    activity = _remove_from_community(
        community, outsider, 'https://peer.example/post/does-not-matter',
        target=community.ap_profile_id + '/featured')

    dispatch(activity)

    log = ActivityPubLog.query.one()
    assert log.exception_message == 'Does not have permission'
    # The defect: this is a Remove, but the stored activity_type is what
    # APLOG_ADD produces.
    assert log.activity_type == APLOG_ADD[1]


def test_remove_unsticky_backfills_ap_featured_url_and_compares_case_insensitively(
        app, db_session, monkeypatch):
    """routes.py's current :1532-1544. `community.ap_featured_url` starts
    empty (make_community() never sets it), so the backfill at :1536-1537
    must run before the comparison can match. The activity's own `target` is
    given in a DIFFERENT case than the backfilled value, so a match proves
    the comparison at :1540 genuinely lowercases both sides.

    `post.sticky` is seeded True (not left at its column default False)
    before asserting it becomes False -- per this campaign's 'beware
    default-backed assertions' rule, a bare `is False` afterward would be
    indistinguishable from the column's own default plus a bug that never
    touched it.

    Uses the moderator-not-instance-admin fixture: under the MUTATION that
    drops the guard's `not community.is_moderator(mod)` half (leaving only
    `not community.is_instance_admin(mod)`), this fixture's mod (a
    moderator, NOT an admin) would be newly refused -- `post.sticky` would
    stay True and no SUCCESS would be logged, a BEHAVIOURAL kill (an
    assertion on stored state, not a mocked-network probe; no send is on
    this code path at all). This is the distinct killer for that guard half.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, moderator, admin = _seed_community_with_mod_and_admin(name='removestickycomm1')
    assert community.ap_featured_url is None

    author = make_user(instance, 'removepostauthor')
    post = make_post(community, author, ap_id='https://peer.example/post/200')
    post.sticky = True
    db.session.commit()

    expected_featured_url = community.ap_profile_id + '/featured'
    mismatched_case_target = expected_featured_url.upper()

    activity = _remove_from_community(community, moderator, post.ap_id, target=mismatched_case_target)

    dispatch(activity)

    db.session.expire_all()
    assert community.ap_featured_url == expected_featured_url
    assert post.sticky is False

    log = ActivityPubLog.query.one()
    assert log.result == 'success'
    # Correctly labelled on this path -- REMOVE, not one of the four
    # mislabelled calls.
    assert log.activity_type == 'Remove'


def test_remove_unsticky_post_not_found_reports_cannot_find(app, db_session, monkeypatch):
    """routes.py's current :1541-1548 -- the target matches the featured
    URL, but `Post.get_by_ap_id` finds nothing. Correctly logged as
    APLOG_REMOVE (:1547), unlike the four mislabelled calls elsewhere in
    this arm.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, moderator, admin = _seed_community_with_mod_and_admin(name='removestickycomm2')

    missing_object = 'https://peer.example/post/does-not-exist-remove'
    activity = _remove_from_community(
        community, moderator, missing_object, target=community.ap_profile_id + '/featured')

    dispatch(activity)

    log = ActivityPubLog.query.one()
    assert log.exception_message == 'Cannot find: ' + missing_object
    assert log.activity_type == 'Remove'


def test_remove_mod_flips_an_existing_membership_to_false(app, db_session, monkeypatch):
    """routes.py's current :1550-1564, the existing-membership half.
    `add_to_modlog('remove_mod', ...)`'s exact keyword arguments are
    asserted via `record_moderation`. `CommunityMember.is_moderator` is
    seeded True (not left at its column default False) so the assertion that
    it becomes False cannot be confused with the default.

    Uses the instance-admin-not-moderator fixture: under the MUTATION that
    drops the guard's `not community.is_instance_admin(mod)` half (leaving
    only `not community.is_moderator(mod)`), this fixture's mod (an admin,
    NOT a moderator) would be newly refused -- the membership would stay
    is_moderator=True and neither add_to_modlog nor SUCCESS would fire, a
    BEHAVIOURAL kill (assertions on stored state / a recorded call list, not
    a mocked-network probe). This is the distinct killer for that guard
    half, complementing the moderator-not-admin killer above.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, moderator, admin = _seed_community_with_mod_and_admin(name='removemodcomm1')
    community.ap_moderators_url = community.ap_profile_id + '/moderators'
    db.session.commit()

    calls = record_moderation(monkeypatch, 'add_to_modlog')

    old_mod = make_user(instance, 'oldmodcandidate')
    old_mod.ap_fetched_at = utcnow()
    membership = make_community_member(old_mod, community, is_moderator=True)
    db.session.commit()
    assert membership.is_moderator is True

    activity = _remove_from_community(
        community, admin, old_mod.ap_profile_id, target=community.ap_moderators_url)

    dispatch(activity)

    db.session.expire_all()
    refreshed = CommunityMember.query.filter_by(community_id=community.id, user_id=old_mod.id).one()
    assert refreshed.is_moderator is False

    assert len(calls['add_to_modlog']) == 1
    args, kwargs = calls['add_to_modlog'][0]
    assert args[0] == 'remove_mod'
    assert sa_inspect(kwargs['actor']).identity[0] == admin.id
    assert sa_inspect(kwargs['target_user']).identity[0] == old_mod.id
    assert sa_inspect(kwargs['community']).identity[0] == community.id

    log = ActivityPubLog.query.one()
    assert log.result == 'success'
    assert log.activity_type == 'Remove'


def test_remove_mod_unresolvable_actor_reports_cannot_find(app, db_session, monkeypatch):
    """routes.py's current :1551-1552/1567-1569 -- the target matches the
    moderators URL, but the named object can't be resolved to an actor.
    Pins mislabelling finding #2: this FAILURE is logged with APLOG_ADD
    (:1568), not APLOG_REMOVE.

    `find_actor_or_create_cached` is wrapped, not replaced, so only the
    deliberately-unresolvable ghost URL is intercepted.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, moderator, admin = _seed_community_with_mod_and_admin(name='removemodcomm2')
    community.ap_moderators_url = community.ap_profile_id + '/moderators'
    db.session.commit()

    ghost_url = 'https://unresolvable.example/u/removeghost'
    real_find_actor_or_create_cached = activitypub_routes.find_actor_or_create_cached

    def _find(actor, create_if_not_found=True, community_only=False, feed_only=False):
        if actor == ghost_url:
            return None
        return real_find_actor_or_create_cached(
            actor, create_if_not_found=create_if_not_found,
            community_only=community_only, feed_only=feed_only)

    monkeypatch.setattr(activitypub_routes, 'find_actor_or_create_cached', _find)

    activity = _remove_from_community(
        community, moderator, ghost_url, target=community.ap_moderators_url)

    dispatch(activity)

    log = ActivityPubLog.query.one()
    assert log.exception_message == 'Cannot find: ' + ghost_url
    assert log.activity_type == APLOG_ADD[1]


def test_remove_mod_without_existing_membership_writes_modlog_but_logs_nothing(
        app, db_session, monkeypatch):
    """routes.py's current :1550-1566 -- `add_to_modlog('remove_mod', ...)`
    sits OUTSIDE the `if existing_membership:` block (:1550-1564), at the
    same indentation as the `if old_mod:` body it shares with it
    (:1550/:1565-1566). When the named actor resolves but has NO
    CommunityMember row for this community, `if existing_membership:` is
    False, so NEITHER `log_incoming_ap` branch inside it runs -- but
    `add_to_modlog` still fires unconditionally once `old_mod` resolves.
    Registered here as a defect, NOT fixed.

    Asserts both halves: the modlog call happened (with `old_mod`'s own
    identity, since no membership row exists to read from), and
    `ActivityPubLog.query.count() == 0` with LOG_ACTIVITYPUB_TO_DB turned
    ON, so the zero-count assertion is load-bearing rather than vacuous
    (this campaign's rule).
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, moderator, admin = _seed_community_with_mod_and_admin(name='removemodcomm3')
    community.ap_moderators_url = community.ap_profile_id + '/moderators'
    db.session.commit()

    calls = record_moderation(monkeypatch, 'add_to_modlog')

    old_mod = make_user(instance, 'membershiplessoldmod')
    old_mod.ap_fetched_at = utcnow()
    db.session.commit()
    assert CommunityMember.query.filter_by(community_id=community.id, user_id=old_mod.id).count() == 0

    activity = _remove_from_community(
        community, moderator, old_mod.ap_profile_id, target=community.ap_moderators_url)

    dispatch(activity)

    assert CommunityMember.query.filter_by(community_id=community.id, user_id=old_mod.id).count() == 0

    assert len(calls['add_to_modlog']) == 1
    args, kwargs = calls['add_to_modlog'][0]
    assert args[0] == 'remove_mod'
    assert sa_inspect(kwargs['actor']).identity[0] == moderator.id
    assert sa_inspect(kwargs['target_user']).identity[0] == old_mod.id
    assert sa_inspect(kwargs['community']).identity[0] == community.id

    assert ActivityPubLog.query.count() == 0


def test_remove_unknown_target(app, db_session, monkeypatch):
    """routes.py's current :1571 -- a target matching neither the featured
    URL nor the moderators URL. Pins mislabelling finding #3: logged with
    APLOG_ADD, not APLOG_REMOVE.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, moderator, admin = _seed_community_with_mod_and_admin(name='removeunknowntargetcomm')
    community.ap_moderators_url = community.ap_profile_id + '/moderators'
    db.session.commit()

    activity = _remove_from_community(
        community, moderator, 'https://peer.example/whatever-remove',
        target='https://peer.example/c/removeunknowntargetcomm/something-else')

    dispatch(activity)

    log = ActivityPubLog.query.one()
    assert log.exception_message == 'Unknown target for Remove'
    assert log.activity_type == APLOG_ADD[1]


def test_remove_target_omitted_entirely_is_refused(app, db_session, monkeypatch):
    """D88, fixed. Same unguarded `core_activity['target']` read as Add's, and
    the same fix: a Remove with no target is refused and logged.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, moderator, admin = _seed_community_with_mod_and_admin(name='removenotargetcomm')

    activity = _remove_from_community(
        community, moderator, 'https://peer.example/whatever-remove', include_target=False)

    dispatch(activity)

    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Remove has no target'



def test_remove_with_neither_community_nor_feed_resolvable_is_refused(
        app, db_session, monkeypatch):
    """routes.py's current :1573 -- the final `else`. A DIRECT (not
    Announced) Remove from a plain User actor whose activity carries no
    audience/cc/to/target that `find_community` can resolve against any
    Community row, and which is not itself a Feed or Community actor.
    Neither `community` nor `feed` is ever set. Pins mislabelling finding
    #4: logged with APLOG_ADD, not APLOG_REMOVE.

    `object` is a dict here, not a plain string -- same
    find_community-shape sidestep Add's equivalent test uses (see that
    test's docstring for why a bare string would exercise a different,
    unrelated defect in find_community itself).
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    sender = make_user(instance, 'removealoneuser')
    sender.ap_fetched_at = utcnow()
    db.session.commit()

    activity = inbox_activity(sender, activity_type='Remove',
                              object={'id': 'https://peer.example/objects/whatever-remove'})

    dispatch(activity)

    log = ActivityPubLog.query.one()
    assert log.exception_message == 'Remove: cannot find community or feed'
    assert log.activity_type == APLOG_ADD[1]


def test_remove_loop_skips_a_community_owner_via_the_subscription_owner_term(
        app, db_session, monkeypatch):
    """routes.py's current :1499 (`if subscription != SUBSCRIPTION_OWNER and
    cm and cm.joined_via_feed:`) has a FIFTH skip condition beyond the four
    Task 9 covered (feed-owner via `fm_user.id == feed.user_id`; non-local;
    feed_auto_leave False; and TWO SEPARATE conjuncts of the `cm and
    cm.joined_via_feed` compound -- `cm is None` (no CommunityMember row at
    all) and `cm.joined_via_feed is False` (a real row that just was not
    joined via a feed); see
    test_remove_loop_skips_a_feed_member_with_no_community_membership above
    for the first of those two, and
    test_remove_loop_skips_a_feed_member_whose_membership_was_not_joined_via_feed
    below for the second, each with its own distinct killer):
    a member who OWNS the COMMUNITY being removed is skipped by the
    `subscription != SUBSCRIPTION_OWNER` term, independent of every other
    check.

    `community_membership(user, community)` (app/utils.py) delegates to
    `User.subscribed(community_id)`, which reads `CommunityMember.is_owner`
    and returns SUBSCRIPTION_OWNER when it is True -- verified directly
    below, before relying on it, per this task's instructions.
    `make_community_member` always creates rows with `is_owner=False`
    (tests/factories.py), so this fixture flips it True on the row after
    creation.

    Every OTHER skip condition is deliberately satisfied so this term is
    demonstrably the SOLE cause of the skip: this member is NOT the feed's
    owner (a distinct user occupies `feed.user_id`, following Task 9's
    convention), IS local, has `feed_auto_leave` True (the column default),
    and has a real CommunityMember row with `joined_via_feed` True -- built
    with `_make_would_proceed_feed_member`, which sets every one of those to
    the value that would let the loop proceed, differing here only in
    `is_owner`, flipped True immediately after.
    """
    instance, community, feed = _seed_removable_feed_community(name='communityownerskipcomm')
    community.subscriptions_count = 1
    db.session.commit()

    feed_owner = make_user(instance, 'feedownerforownerskip', local=True)
    feed.user_id = feed_owner.id
    db.session.commit()
    make_feed_member(feed_owner, feed)

    community_owner, cm = _make_would_proceed_feed_member(
        'communityownermember', instance, feed, community)
    cm.is_owner = True
    db.session.commit()

    assert community_owner.id != feed.user_id
    assert community_owner.is_local() is True
    assert community_owner.feed_auto_leave is True
    assert cm.joined_via_feed is True
    assert community_membership(community_owner, community) == SUBSCRIPTION_OWNER

    sends = record_sends(monkeypatch)

    activity = _remove_activity_for(feed, community)
    dispatch(activity)

    db.session.expire_all()

    # The `subscription != SUBSCRIPTION_OWNER` term stopped the loop before
    # anything below it ran: the community-owner's own CommunityMember row is
    # untouched, nothing was sent, and subscriptions_count (seeded to a
    # nonzero baseline) is unchanged.
    assert CommunityMember.query.filter_by(
        user_id=community_owner.id, community_id=community.id).count() == 1
    assert sends == []
    assert community.subscriptions_count == 1

    # FIX 5's guard (routes.py's current :1482-1487) is independent of the
    # loop and still ran normally: the FeedItem existed, so it is gone and
    # num_communities is decremented from its seeded baseline.
    assert FeedItem.query.filter_by(feed_id=feed.id, community_id=community.id).count() == 0
    assert feed.num_communities == 0


def test_remove_loop_skips_a_feed_member_whose_membership_was_not_joined_via_feed(
        app, db_session, monkeypatch):
    """routes.py's current :1499 -- `if subscription != SUBSCRIPTION_OWNER
    and cm and cm.joined_via_feed:` has THREE conjuncts, not two. Two
    already have dedicated killers elsewhere in this module: the
    `subscription != SUBSCRIPTION_OWNER` term (the test above) and `cm`
    truthy (`test_a_remove_skips_a_feed_member_with_no_community_membership`,
    Task 8). `cm.joined_via_feed` had NONE: every other test in this module
    that reaches this guard with a truthy `cm` does so through
    `_make_would_proceed_feed_member`, which sets `joined_via_feed = True`
    unconditionally -- so dropping `and cm.joined_via_feed` from the guard
    made no test in the suite fail. Branch coverage cannot see this gap
    either: the compound condition emits a single arc, and the `cm is None`
    test already takes the guard's False arc.

    This test does NOT use `_make_would_proceed_feed_member` -- it builds
    the member and CommunityMember row directly, leaving `joined_via_feed`
    at `make_community_member`'s own factory default, which is False
    (`app/models.py:3470`'s column default; the factory never sets this
    column -- see `tests/factories.py`'s `make_community_member`). A real
    row for a member who joined this community some other way than through
    the feed. Every OTHER skip condition is deliberately satisfied -- not
    the feed's owner, local, `feed_auto_leave` True, not the community's
    owner -- so this conjunct is demonstrably the SOLE cause of the skip.

    MUTATION: routes.py:1499 changed from `if subscription !=
    SUBSCRIPTION_OWNER and cm and cm.joined_via_feed:` to `if subscription
    != SUBSCRIPTION_OWNER and cm:` (dropping only `and cm.joined_via_feed`),
    run, and confirmed this test alone catches it: under the mutant the
    member proceeds -- its CommunityMember row is deleted and
    `community.subscriptions_count` is decremented -- so both assertions
    below fail. `app/activitypub/routes.py` was restored to its exact
    pre-mutation text immediately afterward; `git diff --stat app/` was
    empty before this test file's own change was committed.
    """
    instance, community, feed = _seed_removable_feed_community(name='notviafeedcomm')
    community.subscriptions_count = 2
    db.session.commit()

    feed_owner = make_user(instance, 'feedownerfornotviafeed', local=True)
    feed.user_id = feed_owner.id
    db.session.commit()
    make_feed_member(feed_owner, feed)

    member = make_user(instance, 'notviafeedmember', local=True)
    make_feed_member(member, feed)
    cm = make_community_member(member, community, is_moderator=False)

    assert member.id != feed.user_id
    assert member.is_local() is True
    assert member.feed_auto_leave is True
    assert cm.joined_via_feed is False
    assert community_membership(member, community) != SUBSCRIPTION_OWNER

    sends = record_sends(monkeypatch)

    activity = _remove_activity_for(feed, community)
    dispatch(activity)

    db.session.expire_all()

    # The `cm.joined_via_feed` conjunct stopped the loop before anything
    # below it ran: the member's own CommunityMember row is untouched,
    # nothing was sent, and subscriptions_count (seeded to a nonzero
    # baseline) is unchanged.
    assert CommunityMember.query.filter_by(
        user_id=member.id, community_id=community.id).count() == 1
    assert sends == []
    assert community.subscriptions_count == 2

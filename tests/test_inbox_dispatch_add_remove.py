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
    - `target = core_activity['target']` (1428) is read with NO guard at
      all -- a peer that omits `target` gets an uncaught KeyError, not a
      logged refusal. Probed, not fixed.
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
from app.models import ActivityPubLog, CommunityMember, FeedItem, InstanceRole, utcnow
from tests.factories import (inbox_activity, make_community, make_community_member, make_feed,
                             make_feed_member, make_instance, make_post, make_user)
from sqlalchemy import inspect as sa_inspect

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


def test_a_target_omitted_entirely_raises_keyerror(app, db_session, monkeypatch):
    """routes.py's current :1428 -- `target = core_activity['target']` is
    read with no `.get()`, no `in` check, nothing. A peer that omits
    `target` altogether gets an uncaught KeyError, not a logged refusal.
    Probes the observed behaviour; does not fix it.
    """
    instance, community, moderator, admin = _seed_community_with_mod_and_admin(name='notargetcomm')

    activity = _add_from_community(
        community, moderator, 'https://peer.example/whatever', include_target=False)

    with pytest.raises(KeyError, match='target'):
        dispatch(activity)


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

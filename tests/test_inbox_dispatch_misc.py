"""Sub-project 5a, Task 7 -- the last arms in scope for the dispatcher: Flag
(routes.py:1344-1354), Move (:1571-1589), QuoteRequest (:1880-1884), and the
`except`/`finally` every arm unwinds through (:1885-1889).

Step 1 -- Flag, routes.py:1344-1354. `find_reported_object` and
`log_incoming_ap` run for real; only `process_report` (a real network-shaped
side effect -- it notifies every site admin) is doubled, following the same
convention as `process_upvote`/`process_downvote` elsewhere in this campaign.

Step 2 -- Move, routes.py:1571-1589. The permission guard,

    user.id == post.user_id
    or origin_community.is_moderator(user)
    or (origin_community.instance_id == user.instance_id and origin_community.is_instance_admin(user))

gets the same treatment Task 6 gave process_question_answer's guard: three
alternatives, three distinct killer tests, each seeded so ONLY its own
alternative is true. `_seed_move_scenario`'s two communities both come from
`make_community()`, which hardcodes owner `user_id=1` and `instance_id=1` --
so BOTH communities' `instance_id` is the id of the first `Instance` row this
helper creates (the `community_owner`'s instance), not a distinct "remote"
instance id. Test 3 (the instance-admin alternative) relies on exactly that:
its mover is seeded on that SAME instance, so `origin_community.instance_id
== user.instance_id` is true by construction, and an `InstanceRole` row for
that instance/user pair with role='admin' supplies `is_instance_admin`.

MUTATION, Step 2: each alternative was dropped from the guard one at a time,
run against this file, and restored immediately after, verified via `git diff
--stat app/` producing no output before this file's own change was committed.
All three were BEHAVIOURAL kills (a genuine `AssertionError` from this file's
own `Post.query.get(post.id).community_id` assertion, not
`respx.models.AllMockedAssertionError`) -- there is no network fetch on this
guard's path in these tests: both communities and the mover are already
seeded rows found by exact-URL, `ap_fetched_at`-stamped lookups, and
`announce_activity_to_followers` is monkeypatched to a no-op in every granted
test.

  - Dropping `user.id == post.user_id or` (leaving only the other two
    alternatives) was killed by
    test_a_move_by_the_post_author_moves_the_post: its mover is the post's
    author, a moderator of neither community, and not an instance admin, so
    with that alternative gone the guard is False under the mutation and the
    post never moves -- caught by this test's own assertion.
  - Dropping `origin_community.is_moderator(user) or` was killed by
    test_a_move_by_a_moderator_of_the_origin_community_moves_the_post: its
    mover is a moderator of origin_community only (not the post's author, not
    an instance admin), so the same failure shape follows.
  - Dropping ` or (origin_community.instance_id == user.instance_id and
    origin_community.is_instance_admin(user))` entirely was killed by
    test_a_move_by_an_instance_admin_of_the_origin_instance_moves_the_post:
    its mover is an instance admin of origin_community's instance only (not
    the post's author, not a community moderator).

ADDENDUM (fix round 1): the three tests above never exercised the third
alternative's OWN conjunction (`origin_community.instance_id ==
user.instance_id and origin_community.is_instance_admin(user)`)
independently, because every mover in this file up to that point was seeded
on origin_community's own instance -- the equality half was trivially True
throughout, including in the dedicated "instance admin" test. A reviewer
caught this: dropping just the equality conjunct (leaving bare
`origin_community.is_instance_admin(user)`) SURVIVED against the original
five Move tests.

Investigating required first checking what `Community.is_instance_admin`
(app/models.py:752-759) actually scopes on:

    def is_instance_admin(self, user):
        if self.instance_id:
            instance_role = InstanceRole.query.filter(
                InstanceRole.instance_id == self.instance_id,
                InstanceRole.user_id == user.id,
                InstanceRole.role == 'admin').first()
            return instance_role is not None
        else:
            return False

This IS instance-scoped -- but scoped to `self.instance_id`, the COMMUNITY's
own instance, never to `user.instance_id` at all. That is a real, easy trap:
a naive attempt to isolate the equality conjunct by seeding an admin on a
"different instance" (a second Instance, with the InstanceRole scoped to
THAT second instance -- the user's own home instance) does NOT make the
second conjunct True while the first is False, because `is_instance_admin`
would then check for a role on the COMMUNITY's instance and find none --
both conjuncts come out False, and neither single-conjunct-drop mutation is
discriminated by such a test (it degenerates into a second copy of the
unrelated-user no-op, below). The construction that actually isolates the
conjunct is the opposite: a user whose OWN account lives on a second,
unrelated instance, holding an InstanceRole scoped to ORIGIN_COMMUNITY's
instance specifically (not their own) -- an unusual database state, but one
`is_instance_admin`'s own query does not rule out, since it never reads
`user.instance_id`.

test_a_move_by_an_admin_role_scoped_to_the_origin_instance_but_whose_own_account_is_elsewhere_does_nothing
supplies exactly that actor. Re-running the mutations confirms the equality
conjunct is now covered, and reveals the OTHER conjunct
(`is_instance_admin(user)`) was already covered without a dedicated test of
its own:

  - Dropping `origin_community.instance_id == user.instance_id and`
    (leaving bare `origin_community.is_instance_admin(user)`) is killed by
    the new test above: `1 failed, 5 passed` -- a behavioural
    `AssertionError` (`assert 2 == 1`, the post's `community_id` after
    dispatch vs. `origin_community.id`), not
    `respx.models.AllMockedAssertionError`.
  - Dropping ` and origin_community.is_instance_admin(user)` (leaving bare
    `origin_community.instance_id == user.instance_id`) is killed by the
    PRE-EXISTING test_a_move_by_an_unrelated_user_does_nothing: `1 failed, 5
    passed` -- also a behavioural `assert 2 == 1`. That test's stranger
    already shares origin_community's own instance (see
    `_seed_move_scenario`'s seeding) and holds no admin role at all, which
    is exactly the shape needed to isolate this half; no new test was
    required for it.

Both mutations were restored immediately after, verified via `git diff
--stat app/` producing no output.

This arm has NO `else`: when every alternative is false, nothing is logged
and nothing happens (routes.py:1579-1589's `if` has no matching `else`
clause at all -- the whole block simply falls through to the next `if`).
test_a_move_by_an_unrelated_user_does_nothing asserts
`ActivityPubLog.query.count() == 0` WITH logging enabled (the assertion that
would fail if a refusal log were ever added) and that the post did not move.

Step 3 -- QuoteRequest, routes.py:1880-1884, and a probe of its unguarded
read. `process_quote_boost` (a real outbound-signing side effect) is doubled;
`log_incoming_ap`'s own SUCCESS call runs for real.

PROBE, routes.py:1882 (`their_post_ap = core_activity['instrument']['id']`):
fed a QuoteRequest with no 'instrument' key at all. OBSERVED: this line
raises `KeyError: 'instrument'` immediately, before `process_quote_boost` is
ever called and before `log_incoming_ap`'s SUCCESS call at :1884 is
reachable -- the same unguarded-read shape as D2 and D13 (see this
campaign's other probes, e.g. test_inbox_dispatch_preamble.py's Update/Group
probe and test_inbox_dispatch_votes.py's choice_text probe): a peer that
omits 'instrument' crashes activity processing with an uncaught 500-shaped
failure, propagating through routes.py:1885's `except Exception:
session.rollback(); raise` and out of dispatch(), rather than a logged
refusal. No ActivityPubLog row is written. Registered as a finding for Task
9; not fixed here.

Step 4 -- the except/finally every arm unwinds through, routes.py:1885-1889.

    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

Both tests drive the SAME failure (a doubled `process_upvote` raising a
distinctive exception via a Like activity, the cheapest arm in this campaign
to reach) so that the two behaviours -- rollback-then-reraise, and
close-on-every-path -- are proven independently rather than inferred from one
one shared assertion.

CORRECTION to this task's own brief: the brief's Step 4 instructs patching
`get_task_session` to capture the session it returns. The correct patch
target is `app.activitypub.routes.get_task_session`, NOT
`app.utils.get_task_session`. routes.py:39 does `from app.utils import ...
get_task_session, patch_db_session`, which binds those names into routes.py's
OWN module namespace at import time -- the same mechanism documented at
length in tests/conftest.py's `redis_double` docstring ("What matters is
WHERE THE NAME IS BOUND, not when it is called"), and the same mistake this
campaign's own commit 3b442dc7 corrected in four other tests. Patching
`app.utils.get_task_session` leaves routes.py's own bound copy of the name
untouched, so `process_inbox_request`'s `session = get_task_session()` call
at line 841 would still invoke the ORIGINAL function, and a test patching the
wrong module would go green having captured nothing -- a vacuous assertion
exactly like the ones that commit fixed. Both tests below patch
`activitypub_routes.get_task_session` and have their recorder call the real
underlying function and return its real result, so the dispatcher gets a
genuine, usable `Session` bound to `db.engine` -- only that one session
object's own `rollback`/`close` methods are individually wrapped to record
whether they were called, which proves the exact session the dispatcher used
is the one whose lifecycle methods actually ran.

All mutations in this file were run one at a time against
`app/activitypub/routes.py` and restored immediately after, verified via
`git diff --stat app/` producing no output before this file's own change was
committed.
"""
import pytest

from app import db
from app.activitypub import routes as activitypub_routes
from app.models import ActivityPubLog, InstanceRole, Post, utcnow
from tests.factories import (inbox_activity, make_community, make_community_member, make_instance,
                             make_post, make_site, make_user)
from tests.test_inbox_dispatch_preamble import dispatch


# --- Step 1: Flag, routes.py:1344-1354 ---


def _seed_flag_scenario(host='peer.example'):
    """A local-owned community (make_community()'s hardcoded owner user_id=1 /
    instance_id=1, same seeding trick as the vote/announce test files), an
    author, a reporter, and one Post by that author -- the reportable object.
    Returns (reporter, post).
    """
    make_site()
    instance = make_instance(host)
    make_user(instance, 'community_owner')
    community = make_community(host=host)
    community.ap_fetched_at = utcnow()
    author = make_user(instance, 'author')
    reporter = make_user(instance, 'reporter')
    reporter.ap_fetched_at = utcnow()
    post = make_post(community, author, ap_id=f'https://{host}/objects/1')
    db.session.commit()
    return reporter, post


def test_a_flag_of_a_found_object_reports_it_and_announces(app, db_session, monkeypatch):
    """routes.py:1345-1350 -- find_reported_object hits (a real Post lookup,
    not doubled), process_report is called and doubled here (it is a
    real network-shaped side effect -- notifying every site admin -- out of
    this arm's own scope), APLOG_REPORT/APLOG_SUCCESS is logged, and
    announce_activity_to_followers is called with is_flag=True and
    admin_instance_id=reported.author.instance_id.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    reporter, post = _seed_flag_scenario()
    author_instance_id = post.author.instance_id

    # Identifying fields are pulled out of `user`/`reported` INSIDE the
    # recorder, while the dispatcher's task session is still open. Storing
    # the bare ORM objects for attribute access after dispatch() returns
    # would be unsafe: log_incoming_ap's own commit (still to come on this
    # same path) expires every object attached to that session, and
    # `finally: session.close()` (routes.py:1889) has already run a real
    # session.close() by the time this test's own assertions execute -- an
    # expired attribute access on a detached instance raises
    # DetachedInstanceError, not a stale-but-usable value.
    report_calls = []

    def record_process_report(user, reported, core_activity, session):
        report_calls.append({'user_id': user.id, 'reported_id': reported.id,
                             'core_activity': core_activity, 'session': session})

    monkeypatch.setattr(activitypub_routes, 'process_report', record_process_report)

    announce_calls = []

    def record_announce(*args, **kwargs):
        community, user, req_json = args[0], args[1], args[2]
        announce_calls.append({'community_id': community.id, 'user_id': user.id,
                               'request_json': req_json, 'kwargs': kwargs})

    monkeypatch.setattr(activitypub_routes, 'announce_activity_to_followers', record_announce)

    activity = inbox_activity(reporter, activity_type='Flag', object=post.ap_id,
                              summary='spam')

    dispatch(activity)

    assert len(report_calls) == 1
    call = report_calls[0]
    assert call['user_id'] == reporter.id
    assert call['reported_id'] == post.id
    assert call['core_activity'] is activity
    assert call['session'] is not None

    assert ActivityPubLog.query.one().result == 'success'

    assert len(announce_calls) == 1
    announce_call = announce_calls[0]
    assert announce_call['community_id'] == post.community_id
    assert announce_call['user_id'] == reporter.id
    assert announce_call['request_json'] is activity
    assert announce_call['kwargs'] == {'is_flag': True, 'admin_instance_id': author_instance_id}


def test_a_flag_of_missing_content_is_ignored(app, db_session, monkeypatch):
    """routes.py:1351-1353 -- find_reported_object misses (no Post, no
    PostReply, no User at that ap_id), logged as
    APLOG_REPORT/APLOG_IGNORED / 'Report ignored due to missing content', and
    process_report is never called.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    make_site()
    instance = make_instance('peer.example')
    reporter = make_user(instance, 'reporter')
    reporter.ap_fetched_at = utcnow()
    db.session.commit()

    report_calls = []
    monkeypatch.setattr(activitypub_routes, 'process_report',
                         lambda *args, **kwargs: report_calls.append(args))

    ap_id = 'https://peer.example/objects/does-not-exist'
    activity = inbox_activity(reporter, activity_type='Flag', object=ap_id, summary='spam')

    dispatch(activity)

    assert len(report_calls) == 0
    row = ActivityPubLog.query.one()
    assert row.result == 'ignored'
    assert row.exception_message == 'Report ignored due to missing content'


# --- Step 2: Move, routes.py:1571-1589 ---


def _seed_move_scenario(host='peer.example'):
    """Two communities on the same host (an origin and a target -- distinct
    names so their ap_profile_ids differ), a post's author, and the post
    itself, seeded into the origin community. Both communities' instance_id
    is fixed at 1 by make_community()'s hardcoded owner seeding (see the
    module docstring) -- Test 3 below relies on that to make its own mover
    share origin_community's instance_id without needing a second Instance
    row. Returns (instance, origin_community, target_community, post,
    author).
    """
    make_site()
    instance = make_instance(host)
    make_user(instance, 'community_owner')
    origin_community = make_community('origincomm', host=host)
    origin_community.ap_fetched_at = utcnow()
    target_community = make_community('targetcomm', host=host)
    target_community.ap_fetched_at = utcnow()
    author = make_user(instance, 'author')
    post = make_post(origin_community, author, ap_id=f'https://{host}/objects/1')
    db.session.commit()
    return instance, origin_community, target_community, post, author


def test_a_move_by_the_post_author_moves_the_post(app, db_session, monkeypatch):
    """routes.py:1579-1588 -- the first alternative, `user.id == post.user_id`,
    granted alone: the mover IS the post's author, a moderator of neither
    community and not an instance admin anywhere. post.move_to(target)
    actually runs (a real community_id/instance_id change, not a mocked
    call), add_to_modlog's 'move_post' entry is written, and (since
    origin_community.is_local() is True -- make_community() never sets
    ap_id) announce_activity_to_followers is called; doubled here as a
    no-op since its own behaviour is out of this arm's scope. The SUCCESS
    log message is asserted verbatim as an extra check that this is the
    GRANTED path, not a coincidental no-op.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, origin_community, target_community, post, author = _seed_move_scenario()
    author.ap_fetched_at = utcnow()
    db.session.commit()

    monkeypatch.setattr(activitypub_routes, 'announce_activity_to_followers', lambda *a, **k: None)

    activity = inbox_activity(author, activity_type='Move', object=post.ap_id,
                              origin=origin_community.ap_profile_id,
                              target=target_community.ap_profile_id)

    dispatch(activity)

    db.session.expire_all()
    assert Post.query.get(post.id).community_id == target_community.id
    row = ActivityPubLog.query.one()
    assert row.result == 'success'
    assert row.exception_message == f'{author.user_name} moved post to {target_community.link()}'


def test_a_move_by_a_moderator_of_the_origin_community_moves_the_post(app, db_session, monkeypatch):
    """routes.py:1579, second alternative -- `origin_community.is_moderator(user)`
    -- granted alone: the mover is a CommunityMember of origin_community with
    is_moderator=True, is neither the post's author nor an instance admin.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, origin_community, target_community, post, author = _seed_move_scenario()
    mover = make_user(instance, 'moderator')
    mover.ap_fetched_at = utcnow()
    make_community_member(mover, origin_community, is_moderator=True)
    db.session.commit()

    monkeypatch.setattr(activitypub_routes, 'announce_activity_to_followers', lambda *a, **k: None)

    activity = inbox_activity(mover, activity_type='Move', object=post.ap_id,
                              origin=origin_community.ap_profile_id,
                              target=target_community.ap_profile_id)

    dispatch(activity)

    db.session.expire_all()
    assert Post.query.get(post.id).community_id == target_community.id
    assert ActivityPubLog.query.one().result == 'success'


def test_a_move_by_an_instance_admin_of_the_origin_instance_moves_the_post(app, db_session, monkeypatch):
    """routes.py:1579, third alternative -- note it requires BOTH
    `origin_community.instance_id == user.instance_id` AND
    `is_instance_admin` -- granted alone: the mover is seeded on the SAME
    instance as origin_community (see _seed_move_scenario's docstring for why
    that is instance id 1 here) and carries an InstanceRole row of role
    'admin' for that instance. The mover is neither the post's author nor a
    CommunityMember of either community.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, origin_community, target_community, post, author = _seed_move_scenario()
    mover = make_user(instance, 'admin')
    mover.ap_fetched_at = utcnow()
    db.session.add(InstanceRole(instance_id=origin_community.instance_id, user_id=mover.id, role='admin'))
    db.session.commit()

    assert mover.instance_id == origin_community.instance_id  # the guard's own precondition

    monkeypatch.setattr(activitypub_routes, 'announce_activity_to_followers', lambda *a, **k: None)

    activity = inbox_activity(mover, activity_type='Move', object=post.ap_id,
                              origin=origin_community.ap_profile_id,
                              target=target_community.ap_profile_id)

    dispatch(activity)

    db.session.expire_all()
    assert Post.query.get(post.id).community_id == target_community.id
    assert ActivityPubLog.query.one().result == 'success'


def test_a_move_by_an_admin_role_scoped_to_the_origin_instance_but_whose_own_account_is_elsewhere_does_nothing(
        app, db_session, monkeypatch):
    """routes.py:1579, third alternative's OWN conjunction --
    `origin_community.instance_id == user.instance_id and
    origin_community.is_instance_admin(user)` -- has two halves the tests
    above never exercised independently: every actor _seed_move_scenario
    builds is seeded on origin_community's OWN instance, so the equality
    half was trivially True in every test above, including the
    "instance admin" test directly above this one.

    Community.is_instance_admin (app/models.py:752-759) IS instance-scoped
    -- but scoped to the COMMUNITY's own `self.instance_id`, never to the
    acting user's `instance_id` column:

        InstanceRole.instance_id == self.instance_id
        and InstanceRole.user_id == user.id
        and InstanceRole.role == 'admin'

    So a User row's own `instance_id` and an InstanceRole row naming that
    same user can disagree about which instance they administer -- an
    unusual but perfectly representable database state, and precisely the
    state needed to make the second conjunct True while the first is False:
    a user whose OWN account is on a SECOND, unrelated instance, who
    nonetheless holds an InstanceRole scoped to ORIGIN_COMMUNITY's instance
    (not their own instance -- deliberately, since a role scoped to their
    OWN differing instance would leave is_instance_admin(user) False too,
    proving nothing about the equality conjunct specifically; see the
    module docstring's addendum).

    Under the real (unmutated) guard this is still refused: the equality
    conjunct is False (this user's account instance differs from
    origin_community's), so the third alternative is False overall, and --
    this user being neither the post's author nor a moderator of either
    community -- every alternative is False. Same silent no-op shape as
    test_a_move_by_an_unrelated_user_does_nothing: no log row, post
    unmoved.

    MUTATION killer: dropping `origin_community.instance_id ==
    user.instance_id and` from the guard (leaving bare
    `origin_community.is_instance_admin(user)`) makes the third alternative
    True for this user on its own -- their InstanceRole row alone satisfies
    it -- flipping the guard True and moving the post, caught by this
    test's own assertions. See the module docstring's addendum for the
    confirmed mutation run and for why the OTHER half
    (`is_instance_admin(user)`) is already killed by
    test_a_move_by_an_unrelated_user_does_nothing without needing a test of
    its own here.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, origin_community, target_community, post, author = _seed_move_scenario()
    other_instance = make_instance('elsewhere.example')
    foreign_admin = make_user(other_instance, 'foreign_admin')
    foreign_admin.ap_fetched_at = utcnow()
    # Scoped to ORIGIN_COMMUNITY's instance, not foreign_admin's own -- see
    # the docstring above for why that is what makes is_instance_admin
    # return True despite this account living elsewhere.
    db.session.add(InstanceRole(instance_id=origin_community.instance_id,
                                user_id=foreign_admin.id, role='admin'))
    db.session.commit()

    assert foreign_admin.instance_id != origin_community.instance_id  # the guard's own precondition

    activity = inbox_activity(foreign_admin, activity_type='Move', object=post.ap_id,
                              origin=origin_community.ap_profile_id,
                              target=target_community.ap_profile_id)

    dispatch(activity)

    db.session.expire_all()
    assert Post.query.get(post.id).community_id == origin_community.id
    assert ActivityPubLog.query.count() == 0


def test_a_move_by_an_unrelated_user_does_nothing(app, db_session, monkeypatch):
    """routes.py:1579 with every alternative false: the mover is not the
    post's author, not a moderator of origin_community, and not an instance
    admin anywhere. There is no `else` on this `if` (see the module
    docstring), so NOTHING is logged -- asserted as
    `ActivityPubLog.query.count() == 0` WITH logging enabled, which is the
    assertion that would fail if a refusal log were ever added -- and the
    post does not move.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, origin_community, target_community, post, author = _seed_move_scenario()
    mover = make_user(instance, 'stranger')
    mover.ap_fetched_at = utcnow()
    db.session.commit()

    activity = inbox_activity(mover, activity_type='Move', object=post.ap_id,
                              origin=origin_community.ap_profile_id,
                              target=target_community.ap_profile_id)

    dispatch(activity)

    db.session.expire_all()
    assert Post.query.get(post.id).community_id == origin_community.id
    assert ActivityPubLog.query.count() == 0


def test_a_move_whose_post_is_unknown_locally_is_resolved_remotely(app, db_session, monkeypatch):
    """routes.py:1575-1577 -- `Post.get_by_ap_id` misses on the activity's own
    `object` URI, so `resolve_remote_post_from_search` is called instead,
    with the '/context' suffix stripped from that URI first (Mastodon
    appends it to a boosted object's id). Doubled here (sub-project 3
    covered its own behaviour) to return the already-seeded post, moved by
    its own author so the permission guard is satisfied once resolved.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, origin_community, target_community, post, author = _seed_move_scenario()
    author.ap_fetched_at = utcnow()
    db.session.commit()

    resolve_calls = []
    post_id = post.id

    def fake_resolve(uri):
        resolve_calls.append(uri)
        # Re-queried through `Post.query`, which resolves through `db.session`
        # -- patched to the dispatcher's own task session for the duration of
        # this call (patch_db_session, routes.py:845). Returning the TEST
        # session's own `post` object instead would let `post.move_to(...)`
        # mutate an object bound to a DIFFERENT session than the one
        # routes.py's own `session.commit()` (routes.py:1581) actually
        # commits, so the change would never reach the database at all --
        # exactly mirroring how the real resolve_remote_post_from_search
        # (a plain ORM query, app/activitypub/util.py) would hand back a
        # task-session-bound row in production.
        return Post.query.get(post_id)

    monkeypatch.setattr(activitypub_routes, 'resolve_remote_post_from_search', fake_resolve)
    monkeypatch.setattr(activitypub_routes, 'announce_activity_to_followers', lambda *a, **k: None)

    unknown_ap_id = f'https://{instance.domain}/objects/unknown-locally/context'
    activity = inbox_activity(author, activity_type='Move', object=unknown_ap_id,
                              origin=origin_community.ap_profile_id,
                              target=target_community.ap_profile_id)

    dispatch(activity)

    assert resolve_calls == [f'https://{instance.domain}/objects/unknown-locally']
    db.session.expire_all()
    assert Post.query.get(post.id).community_id == target_community.id
    assert ActivityPubLog.query.one().result == 'success'


# --- Step 3: QuoteRequest, routes.py:1880-1884, and its unguarded read ---


def test_a_quote_request_delegates_and_logs_success(app, db_session, monkeypatch):
    """routes.py:1880-1884 -- process_quote_boost is doubled (a real
    outbound-signing side effect, out of this arm's own scope) and called
    with (core_activity, post_ap, their_post_ap), then
    APLOG_QUOTEBOOST/APLOG_SUCCESS is logged.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    make_site()
    instance = make_instance('peer.example')
    actor = make_user(instance, 'alice')
    actor.ap_fetched_at = utcnow()
    db.session.commit()

    calls = []
    monkeypatch.setattr(activitypub_routes, 'process_quote_boost',
                         lambda *args: calls.append(args))

    activity = inbox_activity(actor, activity_type='QuoteRequest',
                              object='https://peer.example/objects/1',
                              instrument={'id': 'https://peer.example/objects/2'})

    dispatch(activity)

    assert len(calls) == 1
    core_activity_arg, post_ap_arg, their_post_ap_arg = calls[0]
    assert core_activity_arg is activity
    assert post_ap_arg == 'https://peer.example/objects/1'
    assert their_post_ap_arg == 'https://peer.example/objects/2'
    assert ActivityPubLog.query.one().result == 'success'


def test_a_quote_request_without_an_instrument(app, db_session, monkeypatch):
    """routes.py:1882 -- `core_activity['instrument']['id']` is read
    unguarded from a peer-supplied activity, the same KeyError shape as D2
    and D13. Establish and assert the observed behaviour.

    OBSERVED: with no 'instrument' key on the activity at all, this line
    raises `KeyError: 'instrument'` immediately -- BEFORE process_quote_boost
    is ever called and before the SUCCESS log at :1884 is reachable --
    propagating uncaught through routes.py:1885's `except Exception:
    session.rollback(); raise` and out of dispatch(). No ActivityPubLog row
    is written. Registered as a finding for Task 9; not fixed here.
    """
    make_site()
    instance = make_instance('peer.example')
    actor = make_user(instance, 'alice')
    actor.ap_fetched_at = utcnow()
    db.session.commit()

    activity = inbox_activity(actor, activity_type='QuoteRequest',
                              object='https://peer.example/objects/1')

    with pytest.raises(KeyError, match='instrument'):
        dispatch(activity)

    assert ActivityPubLog.query.count() == 0


# --- Step 4: the except/finally every arm unwinds through, routes.py:1885-1889 ---


def _seed_actor_for_a_raising_dispatch(host='peer.example'):
    """A single seeded actor, resolvable through the dispatcher's general
    (non-Announce) actor lookup with no HTTP fetch. A Like activity from this
    actor is the cheapest arm in this campaign to route to a doubled
    delegate -- process_upvote, monkeypatched below to raise instead of
    running -- so that the exception fires from inside the dispatcher's own
    try block, exactly where routes.py:1885-1889 catches it.
    """
    make_site()
    instance = make_instance(host)
    actor = make_user(instance, 'alice')
    actor.ap_fetched_at = utcnow()
    db.session.commit()
    return actor


def test_an_exception_inside_the_dispatcher_rolls_back_and_re_raises(app, db_session, monkeypatch):
    """routes.py:1885-1887 -- `except Exception: session.rollback(); raise`.
    The re-raise is the point: this is a Celery task, and swallowing here
    would lose the activity silently. Driven by a doubled process_upvote
    that raises a distinctive ValueError; asserted both that the exception
    propagates (via pytest.raises with a specific match=) and that
    session.rollback() was actually called before it did, by wrapping the
    REAL session's own rollback method (see the module docstring's
    correction paragraph for why get_task_session is patched on
    activitypub_routes, not app.utils).
    """
    actor = _seed_actor_for_a_raising_dispatch()

    def boom(*args, **kwargs):
        raise ValueError('kaboom from a doubled delegate')

    monkeypatch.setattr(activitypub_routes, 'process_upvote', boom)

    real_get_task_session = activitypub_routes.get_task_session
    rollback_calls = []

    def recording_get_task_session():
        session = real_get_task_session()
        original_rollback = session.rollback

        def tracking_rollback():
            rollback_calls.append(True)
            return original_rollback()

        session.rollback = tracking_rollback
        return session

    monkeypatch.setattr(activitypub_routes, 'get_task_session', recording_get_task_session)

    activity = inbox_activity(actor, activity_type='Like')

    with pytest.raises(ValueError, match='kaboom from a doubled delegate'):
        dispatch(activity)

    assert rollback_calls == [True]


def test_the_session_is_closed_even_when_an_arm_raises(app, db_session, monkeypatch):
    """routes.py:1888-1889 -- `finally: session.close()`. Asserted on the
    session object itself, captured by patching
    `activitypub_routes.get_task_session` (see the module docstring's
    correction paragraph -- NOT `app.utils.get_task_session`, which routes.py
    never looks up again after its own module-level import) to record the
    session it returns while still returning a real, usable one: the
    recorder calls the real get_task_session(), wraps that SAME session's
    own `close` method to record whether it was called, and hands the
    dispatcher the wrapped session -- so the assertion below proves the
    EXACT session object the dispatcher used was the one actually closed,
    not merely that `close` was called on something.
    """
    actor = _seed_actor_for_a_raising_dispatch()

    def boom(*args, **kwargs):
        raise RuntimeError('boom from a doubled delegate')

    monkeypatch.setattr(activitypub_routes, 'process_upvote', boom)

    real_get_task_session = activitypub_routes.get_task_session
    captured = {}

    def recording_get_task_session():
        session = real_get_task_session()
        original_close = session.close
        close_calls = []

        def tracking_close():
            close_calls.append(True)
            return original_close()

        session.close = tracking_close
        captured['session'] = session
        captured['close_calls'] = close_calls
        return session

    monkeypatch.setattr(activitypub_routes, 'get_task_session', recording_get_task_session)

    activity = inbox_activity(actor, activity_type='Like')

    with pytest.raises(RuntimeError, match='boom from a doubled delegate'):
        dispatch(activity)

    assert captured.get('session') is not None
    assert captured['close_calls'] == [True]

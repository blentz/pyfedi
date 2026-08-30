"""Sub-project 5a, Task 1 -- the entry lever for process_inbox_request.

Tests call the dispatcher directly. That is not a testing contrivance: it is
production's own DEBUG branch, app/activitypub/routes.py:758-759

    if current_app.debug:
        process_inbox_request(request_json, store_ap_json)
    else:
        process_inbox_request.delay(request_json, store_ap_json)

so the direct call is the task's body, reached the same way a DEBUG
deployment reaches it. Task 8's seam tests drive the same arms through a real
signed POST, which is what licenses any claim that the gate reaches them.

Together, the two tests below establish the three preconditions the rest of
this sub-project depends on. Neither test alone carries all three:

  - test_the_dispatcher_runs_against_seeded_rows_and_its_log_row_is_visible
    proves the dispatcher runs to completion without a request context (Q2),
    and that a row IT writes (via log_incoming_ap) is visible to the test
    after `finally: session.close()` (Q3). Its unknown actor causes every
    find_actor_or_create_cached lookup to miss by construction, so it does
    NOT exercise whether a row this test committed is visible to the
    dispatcher's independent task session -- that assertion would pass
    identically even if seeded rows were invisible to that session.
  - test_the_dispatcher_finds_a_seeded_actor_through_its_own_session closes
    that gap: it seeds a User this test's own session commits, and the only
    way the dispatcher's preamble can reach process_announce_of_uri is by
    finding that exact row through its own independent task session (Q1).

Request-context asymmetry (Step 1, question 2): `patch_db_session`
(app/utils.py:3664) only patches `db.session` when `has_request_context()` is
false. Under a direct call like `dispatch()` below, the test has an app
context (from the `app` fixture) but no request context, so patch_db_session
DOES patch -- the dispatcher's local `session` (from `get_task_session()`)
and `db.session` become the same object for the duration of the call. Under
Task 8's seam tests, which drive the dispatcher through a real Flask request,
`has_request_context()` is true, so patch_db_session does NOT patch: the
dispatcher's `session` local stays the independent task session while
`db.session` remains the request-scoped session. This is a real behavioural
difference between two production paths (the DEBUG direct-call path and the
Celery/request-triggered path), not a test artifact -- code inside
process_inbox_request that writes via `db.session` (e.g. log_incoming_ap when
called with no explicit session) lands in a different session object
depending on which path invoked it. Task 9 decides whether that divergence
constitutes a finding.

Task 2 -- Announce/Accept/Reject actor resolution (routes.py:861-870). See
the outcome-table comment above the three tests below for the derived
lookup order. Mutant killed: dropping `create_if_not_found=False` from the
community_only lookup at line 862 (leaving `find_actor_or_create_cached
(actor_id, community_only=True)`, whose default is create_if_not_found=True)
turned 4 of this file's 5 tests red -- every one whose actor is not a
Community. Each failed not with a clean assertion failure but with
`respx.models.AllMockedAssertionError: RESPX: <Request('GET',
'https://peer.example/...')> not mocked!`, raised from deep inside
find_actor_or_create -> create_actor_from_remote -> fetch_remote_actor_data
-> get_request: with the kwarg gone, a community_only miss no longer
returns None but instead tries to CREATE the actor as a community, which
fetches it over HTTP -- blocked by the `block_outbound_http` fixture,
which errors on any unmocked request rather than letting it reach the
network. An error taking down every non-community-actor test is still a
kill of the mutant: it demonstrates the kwarg is load-bearing, and that no
test in this file would still pass unmodified if it were silently dropped.
Restored immediately after (`git diff --stat app/` confirmed empty).
"""
import pytest

from app import db
from app.activitypub import routes as activitypub_routes
from app.activitypub.routes import process_inbox_request
from app.models import ActivityPubLog, Feed, utcnow
from tests.factories import inbox_activity, make_community, make_instance, make_site, make_user


def dispatch(activity, store_ap_json=True):
    """Call the dispatcher the way production's DEBUG branch does."""
    process_inbox_request(activity, store_ap_json)


def test_the_dispatcher_runs_against_seeded_rows_and_its_log_row_is_visible(
        app, db_session, monkeypatch):
    """Proves two of the three preconditions (see module docstring): the
    dispatcher runs to completion without a request context, and a row IT
    writes (via log_incoming_ap) is visible to this test after `finally:
    session.close()`. It does NOT prove that a row this test commits is
    visible to the dispatcher's independent task session -- see
    test_the_dispatcher_finds_a_seeded_actor_through_its_own_session below for
    that. This test's actor ('https://peer.example/u/nobody') is never looked
    up successfully; make_site/make_instance/make_user here exist only so
    inbox_activity() has a real actor to template an id from before
    activity['actor'] is overwritten with the unknown one.

    An unknown actor is the cheapest activity that reaches log_incoming_ap and
    returns: routes.py:869-870, 'Actor was not a user, feed or a community'.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    make_site()
    instance = make_instance('peer.example')
    actor = make_user(instance, 'alice')

    activity = inbox_activity(actor, activity_type='Announce')
    activity['actor'] = 'https://peer.example/u/nobody'

    dispatch(activity)

    assert ActivityPubLog.query.one().exception_message == \
        'Actor was not a user, feed or a community'


def test_the_dispatcher_finds_a_seeded_actor_through_its_own_session(
        app, signing_peer, monkeypatch):
    """Proves the third precondition (see module docstring): a row this test
    commits is visible to the dispatcher's independent task session.

    `signing_peer` (tests/conftest.py) seeds a User via make_user() the normal
    way -- committed on THIS test's db.session -- with ap_fetched_at stamped
    so find_actor_or_create_cached does not call schedule_actor_refresh, which
    would fire a real actor fetch inline under eager Celery.

    Sending an Announce whose actor IS that seeded user's ap_profile_id, with
    a plain string object (inbox_activity()'s default), drives
    process_inbox_request's preamble (routes.py:861-870) to: miss the
    community lookup (find_actor_or_create_cached(..., community_only=True)
    filters out a User), miss the feed lookup (same, feed_only=True), and HIT
    the plain user lookup -- find_remote_actor's fallback query
    (app/activitypub/actor.py) runs `db.session.query(User)...`, and under a
    direct call db.session IS the dispatcher's independent task session
    (patch_db_session). That query can only find signing_peer's row if this
    test's committed row is visible on that other session/connection. A hit
    on all three lookups being a miss/miss/hit is what routes control to
    routes.py:895-899's `isinstance(request_json['object'], str)` branch,
    which calls process_announce_of_uri and returns.

    We only assert that process_announce_of_uri was REACHED -- not what it
    does with its arguments, or its delegation semantics, which is Task 4
    Step 1's contract to establish; this test's scope is proving the actor
    lookup succeeded, not that arm's full behavior. We can't assert on an
    ActivityPubLog row instead: process_announce_of_uri logs its own outcome
    on every path (see its docstring, app/activitypub/util.py) and is
    monkeypatched out below, so no such row is written here.
    """
    calls = []
    monkeypatch.setattr(activitypub_routes, 'process_announce_of_uri',
                         lambda *args, **kwargs: calls.append((args, kwargs)))

    activity = inbox_activity(signing_peer, activity_type='Announce')

    dispatch(activity)

    assert len(calls) == 1


# --- Task 2: Announce/Accept/Reject actor resolution, routes.py:861-870 ---
#
# Outcome table, derived from source (not copied from the task brief):
#
#   1st lookup (line 862): find_actor_or_create_cached(actor_id,
#     community_only=True, create_if_not_found=False) -> `community`.
#     If truthy, line 863's `if not community:` body (864-866) never runs --
#     the feed and user lookups below are skipped entirely.
#
#   2nd lookup (line 864, only reached if `community` was falsy):
#     find_actor_or_create_cached(actor_id, feed_only=True,
#     create_if_not_found=False) -> `feed`. If truthy, line 865's
#     `if not feed:` body (866) never runs -- the user lookup is skipped.
#
#   3rd lookup (line 866, only reached if both `community` and `feed` were
#     falsy): find_actor_or_create_cached(actor_id, create_if_not_found=False)
#     -> `user`. No narrowing kwarg, so this is the "find anything" lookup.
#
#   Refusal (line 867): `if not community and not feed and not user:` is
#     true only when ALL THREE lookups above missed. Logs APLOG_ANNOUNCE /
#     APLOG_FAILURE, 'Actor was not a user, feed or a community' (868) and
#     returns (870). If any one lookup hit, this condition is false and
#     control falls through to the Announce/Accept/Reject-specific handling
#     starting at line 871 (out of this task's scope), with no log written
#     by lines 861-870 themselves.
#
# This table agrees with the task brief's prose -- no correction to record
# here, unlike other enumerations in this campaign.
#
# Two things worth recording that the brief's prose does not call out:
#   - The refusal log at 868 always logs type APLOG_ANNOUNCE
#     (app/constants.py:132), even when request_json['type'] is 'Accept' or
#     'Reject' rather than 'Announce'. Not a defect this task fixes -- just
#     the exact logged shape, since the pre-existing refusal test above uses
#     an Announce and so cannot show this by itself.
#   - For an Announce whose object is a plain string (inbox_activity()'s
#     default, used by every test below), only the `community` local is ever
#     passed onward -- to process_announce_of_uri (line 899). `feed` and
#     `user` are read ONLY by the refusal check at line 867. A feed-resolved
#     and a user-resolved actor are therefore observationally IDENTICAL from
#     that point on: community=None reaches process_announce_of_uri either
#     way. process_announce_of_uri's own docstring (app/activitypub/util.py)
#     calls a None `community` argument a microblog boost and a real one a
#     community boost, so the "feed" and "user" tests below discriminate
#     outcomes by which single actor row exists to be found (proving that
#     row's lookup, and not a refusal, is what let process_announce_of_uri
#     be reached at all), rather than by asserting the discarded feed/user
#     locals, which no downstream code can distinguish once past line 867.
#
# The fourth outcome (all three miss) is already covered above by
# test_the_dispatcher_runs_against_seeded_rows_and_its_log_row_is_visible,
# whose unknown actor causes every lookup to miss by construction and
# asserts the exact refusal message from line 868. A fifth test asserting
# that same log message a second time would be pure duplication.


def test_an_announce_from_a_known_community_resolves_it_as_the_community(
        app, db_session, monkeypatch):
    """routes.py:862 -- the first lookup wins, and the feed and user lookups
    below it never run. Asserted through the outcome the community path
    produces, not by counting calls: process_announce_of_uri is monkeypatched
    to record its arguments, and the seeded Community instance flowing
    through as its `community` argument is direct evidence the
    community_only lookup at line 862 is what resolved the actor (see the
    outcome-table comment above for why a non-None `community` argument is
    only possible via that first lookup).

    The `community` argument is compared by id, not by `is`: get_task_session
    (app/utils.py:3658-3660) hands process_inbox_request a genuinely
    independent Session bound to the same engine, so the row the dispatcher
    resolves is a distinct Python object from the one this test built and
    committed on its own db_session, even though patch_db_session makes
    `db.session` refer to that same independent session for the duration of
    this direct call (see the module docstring's Q1/Q3 discussion). `is`
    fails here; `.id` is the only thing two ORM objects for the same row
    across two sessions can be expected to share.

    `host='peer.example'` keeps the community's ap_profile_id off
    SERVER_NAME ('test.piefed.local'), so find_actor_by_url's remote branch
    (app/activitypub/actor.py) is exercised rather than its local-community
    shortcut. ap_fetched_at is stamped so schedule_actor_refresh does not
    fire a real actor fetch inline under eager Celery (make_community()
    itself never sets it). make_community() hardcodes owner user_id=1 and
    instance_id=1 (see tests/test_announce_dispatch.py's
    make_owned_community for the same requirement), so an instance and a
    user are seeded first to occupy those ids.
    """
    make_site()
    instance = make_instance('peer.example')
    make_user(instance, 'community_owner')
    community = make_community(host='peer.example')
    community.ap_fetched_at = utcnow()
    db.session.commit()

    calls = []
    monkeypatch.setattr(activitypub_routes, 'process_announce_of_uri',
                         lambda *args, **kwargs: calls.append(args))

    activity = inbox_activity(community, activity_type='Announce',
                              object_uri='https://peer.example/objects/1')

    dispatch(activity)

    assert len(calls) == 1
    assert calls[0][1] is not None and calls[0][1].id == community.id


def test_an_announce_from_a_feed_falls_through_to_the_feed_lookup(
        app, db_session, monkeypatch):
    """routes.py:863-865 -- community_only finds nothing, feed_only does.

    No Community row exists with this actor's ap_profile_id, so line 862
    misses by construction; the only row that CAN satisfy any of the three
    lookups is the Feed seeded below, so process_announce_of_uri being
    reached at all (rather than the refusal at line 868) is evidence the
    feed_only lookup at line 864 is what hit. Per the outcome-table comment
    above, `community` itself is None either way once a non-community actor
    resolves -- that argument cannot distinguish "feed hit" from "user hit",
    only "some lookup hit" from "all three missed".

    There is no make_feed() factory (grep tests/factories.py), so the Feed
    row is built directly, following the pattern tests/test_ap_actor_json_feed.py's
    _child_feed helper uses. ap_fetched_at is stamped for the same
    schedule_actor_refresh reason as the community test above.
    """
    make_site()
    instance = make_instance('peer.example')
    feed = Feed(name='peerfeed', title='peerfeed', instance_id=instance.id,
               ap_id='peerfeed@peer.example', ap_domain='peer.example',
               ap_profile_id='https://peer.example/f/peerfeed',
               ap_public_url='https://peer.example/f/peerfeed',
               ap_fetched_at=utcnow())
    db.session.add(feed)
    db.session.commit()

    calls = []
    monkeypatch.setattr(activitypub_routes, 'process_announce_of_uri',
                         lambda *args, **kwargs: calls.append(args))

    activity = inbox_activity(feed, activity_type='Announce',
                              object_uri='https://peer.example/objects/1')

    dispatch(activity)

    assert len(calls) == 1
    assert calls[0][1] is None


def test_an_announce_from_a_user_falls_through_to_the_user_lookup(
        app, db_session, monkeypatch):
    """routes.py:865-866 -- both narrowed lookups miss, the wide one hits.

    No Community or Feed row exists with this actor's ap_profile_id (a plain
    make_user() row's ap_profile_id is 'https://peer.example/users/alice',
    which contains none of '/u/', '/c/' or '/f/' -- see actor.py's
    find_remote_actor, which falls through to its unconditional per-model
    query for exactly such a URL), so lines 862 and 864 both miss by
    construction and only line 866's unnarrowed lookup can find this row.
    process_announce_of_uri being reached at all is therefore evidence the
    plain lookup at line 866 is what hit, for the same reason given in the
    feed test above -- `community` is None whether a feed or a user
    resolved it, so that argument alone cannot tell the two apart.

    This overlaps in mechanism with
    test_the_dispatcher_finds_a_seeded_actor_through_its_own_session above
    (Task 1), which drives the identical lookup miss/miss/hit sequence --
    but that test's purpose is proving cross-session row visibility (via the
    signing_peer fixture, which exists for HTTP-signature tests this one
    does not need), not cataloguing the resolution table's third outcome.
    This test exists to keep that outcome documented here alongside its two
    siblings above, independent of Task 1's session-visibility concern.
    """
    make_site()
    instance = make_instance('peer.example')
    actor = make_user(instance, 'alice')
    actor.ap_fetched_at = utcnow()
    db.session.commit()

    calls = []
    monkeypatch.setattr(activitypub_routes, 'process_announce_of_uri',
                         lambda *args, **kwargs: calls.append(args))

    activity = inbox_activity(actor, activity_type='Announce',
                              object_uri='https://peer.example/objects/1')

    dispatch(activity)

    assert len(calls) == 1
    assert calls[0][1] is None

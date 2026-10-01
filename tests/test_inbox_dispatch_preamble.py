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
DOES patch -- `db.session` is reassigned to a `SessionWrapper(session)`
(app/utils.py:3678-3690) whose `__getattr__` proxies attribute access
(reads and method calls, not writes) through to the dispatcher's local
`session` (from `get_task_session()`) for the duration of the call. Under
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
lookup order.

MUTATION, round 1: dropping `create_if_not_found=False` from the
community_only lookup at line 862 (leaving `find_actor_or_create_cached
(actor_id, community_only=True)`, whose default is create_if_not_found=True)
turned 4 of this file's 5 tests red -- every one whose actor is not a
Community -- each with `respx.models.AllMockedAssertionError: RESPX:
<Request('GET', 'https://peer.example/...')> not mocked!`, raised from deep
inside find_actor_or_create -> create_actor_from_remote ->
fetch_remote_actor_data -> get_request, before any assertion in this file
runs. That is weaker evidence than a kill looks like: it shows only that
removing the kwarg causes an attempted fetch, which `block_outbound_http`
then turns into an error. It does not show that this file's assertions
would catch the guard's semantic effect -- which actor object actually
gets resolved -- because the exception fires before find_actor_or_create
can return anything at all.

MUTATION, round 2 (same drop, but letting the fetch SUCCEED): re-ran the
identical mutation with a mocked 200 response for the unfound actor's own
URL (a minimal Person document, following the recipe in
tests/test_ap_resolve_remote_post.py's serve_remote_object / this file's
conftest.py:336 http_mock, registering exactly one GET route rather than
federation_peer's webfinger+actor pair -- our fetch is a direct-URL fetch,
which never calls webfinger, so federation_peer's unused webfinger route
would fail http_mock's assert_all_called=True teardown check). Traced
through both the user-resolution and feed-resolution scenarios (temporary
scratch tests, not kept): in both, actor_json_to_model's own dedup query
(`User.ap_profile_id == activity_json['id'].lower()`, util.py:1199) finds
the SAME row this file's fixture already committed, so create_actor_from_remote
returns that existing row -- which then fails the `community_only and not
isinstance(actor_model, Community)` check and is discarded, exactly as it
would be found-then-discarded without the mutation. The narrowed feed_only
lookup at line 864 subsequently finds the real Feed (or the wide lookup at
866 finds the real User) exactly as it does today, because find_remote_actor's
per-model queries are unaffected by a discarded row from a different query.
Both scratch tests PASSED under the mutation.

CONCLUSION: with the fetch allowed to succeed, no test in this file fails.
This file's three Task 2 tests do NOT discriminate `create_if_not_found=False`'s
semantic effect on lines 862-866's actor resolution -- they only failed in
round 1 because the network was blocked, which is an infrastructure failure,
not a behavioural one. The kwarg's semantic effect (as opposed to its
network-avoidance effect) is UNTESTED by this file. Left for Task 9 to
register as a finding; not fixed here. (Caveat: an actor JSON that
DESERIALIZES to a *different* class than the row already on file -- e.g. a
Group document served at a URL this file only ever seeded as a User/Feed --
was not tried, and by the same code path (actor_json_to_model's dedup query
is keyed per-model-type, so a Group document creates a NEW Community row
sharing that URL string rather than finding the existing User/Feed) would
make `community` non-None and could plausibly make one of these tests fail;
that variant is real construction work, not test-running, and is left to
whoever registers or resolves the finding above.)

Both mutation rounds were temporary: `app/activitypub/routes.py` was
restored immediately after each, verified via `git diff --stat app/`
producing no output for `app/` before this file's own change was committed.

Task 3 -- the preamble's other branch (routes.py:871-892) and two probes of
unguarded peer-supplied input.

MUTATION, Task 3: routes.py:872 (`if actor and isinstance(actor, User):`) and
:874 (`elif actor and isinstance(actor, Community):`) each have two halves.
Dropping the `actor and` half from either line is an EQUIVALENT mutant --
find_actor_or_create_cached's return type is always `User | Community | Feed |
None` (a real ORM instance, which does not override __bool__/__len__, or
exactly None), so `actor and X` and plain `X` evaluate identically for every
value `actor` can actually take: when actor is None, `isinstance(None, ...)`
is already False, matching `None and ...`'s short-circuit to a falsy value;
when actor is a model instance, it is always truthy. Both drops (872 and 874)
were run against this file's full suite and both left all 14 tests green --
confirming equivalence empirically rather than by argument alone. Not fixed;
registered here for Task 9, since an equivalent mutant is not a test gap.

Dropping the `isinstance(actor, User)` half from 872 (leaving `if actor:`) IS
behavioural: it was run and killed 7 of this file's tests, most directly
test_add_from_a_group_actor_is_ignored_as_nodebb_topic_management (a
Community actor now satisfies the User arm too, so `user = actor` runs and
NodeBB Topic Management is never logged) -- a Community-domain kill.

Dropping the `isinstance(actor, Community)` half from 874 (leaving `elif
actor:`) was run separately (872 restored first) and killed exactly one test:
test_an_activity_from_an_actor_that_is_neither_is_refused (a Feed actor is
truthy, so it now satisfies the Community arm and falls to its own
`else: log_incoming_ap(..., 'Unexpected activity from Group')` instead of
890-892's refusal) -- a Feed-domain kill, distinct from the Community-domain
kill above, matching the brief's requirement that actor-falsy and
actor-is-a-different-class are different domains needing different killers.

All four mutations were run one at a time and `app/activitypub/routes.py` was
restored immediately after each, verified via `git diff --stat app/`
producing no output for `app/` before this file's own change was committed.

D50, fixed (`elif request_json['type'] == 'Update' and 'type' in
request_json['object']:`): a Community (Group) actor's Update whose object is
the string 'https://peer.example/some-type-of-thing' passed that membership
test on the substring "type" and then raised `TypeError: string indices must
be integers` indexing the string, with no log row. The test is now guarded
by an isinstance check, so a string object falls to the Group chain's
'Unexpected activity from Group' refusal. A membership test is never a type
test.

D49, fixed (`if isinstance(actor_id, dict): actor_id = actor_id['id']`):
a dict actor with no 'id' key (`{'type': 'Person'}`) used to raise
`KeyError: 'id'` before any log_incoming_ap call was reachable. It is now
refused with a logged failure before any actor lookup.

TASK 8 -- the seam: three signed requests, through the real /inbox route.

Every test above calls the dispatcher directly. The three tests below are the
only ones in this file that drive it through `tests.factories.signed_inbox_post`
-- a real HTTP POST to /inbox, signed with production's own signing code,
verified by production's own gate (HttpSignature.precheck, actor resolution,
HttpSignature.verify_request), reaching process_inbox_request only because
DEBUG=True selects routes.py:758-759's inline branch rather than `.delay(...)`.

What these three tests LICENSE: that a real signed peer reaches the preamble
(routes.py:839-931), the Announce unwrap (routes.py:899's
process_announce_of_uri call), and the upvote arm (routes.py:1329's
process_upvote call), for exactly three shapes -- a Like from a known User, an
Announce of a plain-string object from a known User, and (for the third test)
the same Like shape again, used to pin that the actor object the gate verified
the signature against and the actor object the arm receives are the same row.

What these three tests do NOT license: anything about Follow, Accept, Reject,
Create/Update, the moderation arms (Add/Remove/Block/etc.), or Undo. None of
these three tests sends any of those activity types; sub-project 4's gate
tests cover the gate itself, not these arms, and every OTHER test in this
file (Tasks 1-7) reaches its arm via a direct dispatch() call, not a signed
request. Slices 5b-5d cover those shapes; until they do, no test in this
repository has driven them through the gate.

Request-context session arrangement, confirmed rather than assumed (see the
asymmetry paragraph above): under all three tests below, has_request_context()
is True for the whole call -- shared_inbox() and process_inbox_request() both
run inside the one Flask request signed_inbox_post's client.post() opens -- so
patch_db_session does NOT patch db.session, exactly as reasoned above. This did
NOT produce an observable divergence in these three tests, for a reason worth
recording precisely: find_actor_or_create_cached (app/activitypub/util.py:324),
the only actor-resolution function either the gate or the preamble calls here,
never reads or writes through the `session` local process_inbox_request obtains
from get_task_session() -- every branch of it ends in a bare `db.session.get(...)`
or a query built the same way find_actor_or_create/find_actor_by_url always
build it. So the gate's call (shared_inbox, routes.py:703) and the dispatcher's
call (process_inbox_request, routes.py:871) both resolve through `db.session`
regardless of whether patch_db_session patched anything -- the request-context
divergence Task 1 identified is real for code that calls `session.query(...)`
directly (e.g. the CommunityBan and ChatMessage lookups further down in
process_inbox_request), but find_actor_or_create_cached is not such code, so
these three tests cannot exhibit it. That is a fact about which lookup these
three shapes happen to use, not a claim that the divergence never matters --
Task 9 should not read "no divergence observed here" as "no divergence exists
everywhere in this dispatcher".
"""
import pytest

from app import db
from app.activitypub import routes as activitypub_routes
from app.activitypub.routes import process_inbox_request
from app.models import ActivityPubLog, utcnow
from tests.factories import inbox_activity, make_community, make_feed, make_instance, make_site, \
    make_user, signed_inbox_post


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
    direct call `db.session` is a `SessionWrapper` (app/utils.py:3678-3690)
    proxying attribute access through to the dispatcher's independent task
    session (patch_db_session). That query can only find signing_peer's row if this
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
    reached at all (rather than the refusal at line 868) is NOT evidence
    that the feed_only lookup at line 864 is specifically what hit: dropping
    line 864 leaves this test green, because the unnarrowed lookup at line
    866 finds the same Feed row, `user` comes back truthy, and the :867
    refusal (`if not community and not feed and not user:`) is skipped
    either way. Per the outcome-table comment above, `community` itself is
    None either way once a non-community actor resolves -- that argument
    cannot distinguish "feed hit" from "user hit", only "some lookup hit"
    from "all three missed". What this test DOES uniquely prove: it kills
    the `not feed and` conjunct of the :867 refusal (with `community` also
    None, only `user` being truthy could otherwise mask a false `not feed`),
    which no other test in this file does. The genuine killer for line 864
    itself lives in tests/test_inbox_dispatch_announce.py, whose dict-shaped
    inner object makes `if not feed:` at routes.py:914 observable.

    make_feed (tests/factories.py) stamps ap_fetched_at for the same
    schedule_actor_refresh reason as the community test above.
    """
    make_site()
    instance = make_instance('peer.example')
    feed = make_feed(instance)

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


@pytest.mark.parametrize('activity_type', ['Announce', 'Accept', 'Reject'])
def test_an_unknown_actor_refusal_is_logged_as_the_activity_received(
        app, db_session, monkeypatch, activity_type):
    """D63, fixed. The actor-not-found refusal always logged APLOG_ANNOUNCE,
    so a refused Accept or Reject read as an Announce. It now names the
    activity type that arrived.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    make_site()
    instance = make_instance('peer.example')
    actor = make_user(instance, 'alice')

    activity = inbox_activity(actor, activity_type=activity_type)
    activity['actor'] = 'https://peer.example/u/nobody'

    dispatch(activity)

    log = ActivityPubLog.query.one()
    assert log.exception_message == 'Actor was not a user, feed or a community'
    assert log.activity_type == activity_type


# --- Task 3: the preamble's other branch, routes.py:871-892 ---
#
# Everything that is NOT Announce/Accept/Reject resolves exactly one actor
# via the unnarrowed find_actor_or_create_cached(actor_id) at line 871 (no
# community_only/feed_only kwarg -- it accepts whatever class comes back),
# then dispatches on that actor's class:
#
#   isinstance(actor, User)      -> user = actor, falls through (872-873)
#   isinstance(actor, Community) -> NodeBB/a.gup.pe special-casing (874-889)
#   neither (including a falsy actor) -> refused outright (890-892)


def _seed_remote_group_community(host='peer.example', name='microblogs'):
    """A Community resolvable as a Group actor via routes.py:871's
    unnarrowed lookup, with ap_id set so Community.is_local() (app/models.py:778,
    `self.ap_id is None or self.profile_id().startswith(SERVER_URL)`) is
    False. make_community() never sets ap_id, so a community built only with
    make_community() is_local()==True regardless of host -- which would route
    routes.py:1252's Update/Group handling through
    `community.is_moderator(user)` with `user` still None from the preamble
    (line 858), which falls to `is_moderator`'s no-user branch
    (app/models.py:719-721) and reads `current_user.get_id()` outside any
    request context. That is real behaviour worth registering on its own, but
    it is not what this task's Update/Group test (878-881) exists to probe,
    so this helper sidesteps it by giving the community a remote ap_id, the
    same way every other actor in this file is made unambiguously remote.

    ap_fetched_at is stamped so find_actor_or_create_cached's
    schedule_actor_refresh does not fire a real actor fetch inline under
    eager Celery. make_community() hardcodes owner user_id=1 and
    instance_id=1, so an instance and a user are seeded first to occupy those
    ids, following the pattern established above for the Announce/community
    test.
    """
    make_site()
    instance = make_instance(host)
    make_user(instance, 'community_owner')
    community = make_community(name, host=host)
    community.ap_id = f'{name}@{host}'
    community.ap_fetched_at = utcnow()
    db.session.commit()
    return community


def test_an_ordinary_activity_from_a_known_user_sets_user(app, db_session, monkeypatch):
    """routes.py:872-873 -- actor resolves as a User through the unnarrowed
    lookup at line 871 (this is not an Announce/Accept/Reject), so
    `user = actor` and control falls through the rest of the if/elif/else
    (874-892) with no log and no return, reaching the core_activity dispatch
    starting at line 930. A 'Like' activity is used so process_upvote
    (routes.py:1329, a bare module-level name in routes.py -- not imported
    from elsewhere) is the very next thing that runs; it is monkeypatched
    here so this test proves only that `user` reached it as the seeded
    actor and `announced` was False, not process_upvote's own behaviour
    (out of this task's scope).
    """
    make_site()
    instance = make_instance('peer.example')
    actor = make_user(instance, 'alice')
    actor.ap_fetched_at = utcnow()
    db.session.commit()

    calls = []
    monkeypatch.setattr(activitypub_routes, 'process_upvote',
                         lambda *args, **kwargs: calls.append(args))

    activity = inbox_activity(actor, activity_type='Like')

    dispatch(activity)

    assert len(calls) == 1
    user_arg, store_ap_json_arg, request_json_arg, announced_arg = calls[0]
    assert user_arg is not None and user_arg.id == actor.id
    assert announced_arg is False


@pytest.mark.parametrize('activity_type', ['Add', 'Remove'])
def test_add_from_a_group_actor_is_ignored_as_nodebb_topic_management(
        app, db_session, monkeypatch, activity_type):
    """routes.py:875-877 -- actor resolves as a Community (the Group case),
    and an Add or Remove activity from it is NodeBB's own topic-management
    traffic: logged and ignored without ever reaching the real Add/Remove
    handling further down (routes.py:1396/1469), which is scoped to Task 4+.
    'Remove' takes the identical arm as 'Add' (routes.py:875), hence the
    parametrisation rather than two near-duplicate tests.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    community = _seed_remote_group_community()

    activity = inbox_activity(community, activity_type=activity_type)

    dispatch(activity)

    assert ActivityPubLog.query.one().exception_message == 'NodeBB Topic Management'


def test_update_group_from_a_group_actor_is_processed_as_a_community_update(
        app, db_session, monkeypatch):
    """routes.py:878-881 -- an Update from a Community actor whose object is
    a dict with type=='Group' assigns `community = actor` with no log and no
    return here, and control falls through to routes.py:1252's Update/Group
    handling, which is the first thing downstream to actually use that
    `community` local (community.is_local() is False per
    _seed_remote_group_community, so the is_moderator/non-moderator branch at
    1253 is skipped and 1256's refresh_community_profile call is reached
    unconditionally). refresh_community_profile is monkeypatched so this
    test proves only that 881's assignment reached that call with the right
    community id, not what refreshing a profile does (out of this task's
    scope, and also would otherwise be a real network-shaped call).
    """
    community = _seed_remote_group_community()

    calls = []
    monkeypatch.setattr(activitypub_routes, 'refresh_community_profile',
                         lambda *args, **kwargs: calls.append(args))

    activity = inbox_activity(community, activity_type='Update',
                              object={'type': 'Group', 'id': community.ap_profile_id})

    dispatch(activity)

    assert len(calls) == 1
    assert calls[0][0] == community.id


def test_update_orderedcollection_from_a_group_actor_is_ignored(app, db_session, monkeypatch):
    """routes.py:882-884 -- an Update from a Community actor whose object is
    a dict with type=='OrderedCollection' is a.gup.pe's follower-count
    update: logged and ignored.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    community = _seed_remote_group_community()

    activity = inbox_activity(community, activity_type='Update',
                              object={'type': 'OrderedCollection'})

    dispatch(activity)

    assert ActivityPubLog.query.one().exception_message == 'Follower count update from a.gup.pe'


def test_any_other_update_from_a_group_actor_is_refused(app, db_session, monkeypatch):
    """routes.py:885-887 -- an Update from a Community actor whose object's
    dict type is neither 'Group' nor 'OrderedCollection' is refused outright.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    community = _seed_remote_group_community()

    activity = inbox_activity(community, activity_type='Update',
                              object={'type': 'SomeOtherType'})

    dispatch(activity)

    assert ActivityPubLog.query.one().exception_message == 'Unexpected Update activity from Group'


def test_an_unexpected_activity_type_from_a_group_actor_is_refused(app, db_session, monkeypatch):
    """The final `else` of the Community-actor chain in the preamble
    (routes.py:887-889): an actor that resolves to a Community sending an
    activity whose type is neither 'Add', 'Remove' nor 'Update'.

    'Like' is chosen because it is a type this dispatcher handles perfectly
    well from a USER actor (see test_an_ordinary_activity_from_a_known_user_sets_user
    above), so the refusal here is demonstrably about the actor being a
    Community rather than about the type being unknown. Verified against
    routes.py:874-889 that 'Like' genuinely reaches this `else`: the only
    types intercepted earlier in the chain are 'Add'/'Remove' (875, NodeBB
    topic management) and 'Update' (878, its own three-way split covered by
    the three tests above) -- every other type, 'Like' included, falls
    straight through to this `else`.

    This is NOT the sibling `'Unexpected Update activity from Group'` message
    (routes.py:885, asserted by test_any_other_update_from_a_group_actor_is_refused
    above), which is an Update whose object type is neither 'Group' nor
    'OrderedCollection'. The two messages differ by one word; only this one
    is covered here.

    make_community() hardcodes owner user_id=1 and instance_id=1 (see
    _seed_remote_group_community's docstring above), so an instance and a
    user occupy those ids first -- the same seeding this file's other
    Community-actor tests use, without which make_community()'s own commit
    fails a real foreign-key constraint on Community.user_id.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    community = _seed_remote_group_community()

    activity = inbox_activity(community, activity_type='Like',
                              object_uri='https://peer.example/post/1')

    dispatch(activity)

    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Unexpected activity from Group'


def test_an_update_from_a_group_actor_whose_object_is_a_string_containing_type(
        app, db_session, monkeypatch):
    """D50, fixed. `'type' in request_json['object']` is a membership test,
    and a membership test is never a type test: for a string object that
    contains the substring "type" it passed, and the next line indexed the
    str with a str and raised TypeError, uncaught, with no log row. The
    check now requires a dict, so this Update is refused as any other
    unexpected activity from a Group is.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    community = _seed_remote_group_community()

    activity = inbox_activity(community, activity_type='Update',
                              object='https://peer.example/some-type-of-thing')

    dispatch(activity)

    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Unexpected activity from Group'


def test_an_activity_from_an_actor_that_is_neither_is_refused(app, db_session, monkeypatch):
    """routes.py:890-892 -- actor resolves to something that is neither a
    User nor a Community for a non-Announce/Accept/Reject activity (a Feed,
    here -- find_remote_actor's fallback query at actor.py:129 finds it via
    its '/f/' ap_profile_id), and is refused outright regardless of the
    activity's own type.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    make_site()
    instance = make_instance('peer.example')
    feed = make_feed(instance)

    activity = inbox_activity(feed, activity_type='Like')

    dispatch(activity)

    assert ActivityPubLog.query.one().exception_message == 'Actor was not a user or a community'


def test_a_dict_actor_without_an_id_key_is_refused(app, db_session, monkeypatch):
    """D49, fixed. Discourse sends a dict actor (`{'id': '...', 'type':
    'Person', ...}`), which the preamble unpacks to its id. A dict with no
    'id' key used to raise KeyError there, before find_actor_or_create_cached
    or any log_incoming_ap call, so the activity vanished with no log row.
    It is now refused and logged, and no actor is looked up or created.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    make_site()
    instance = make_instance('peer.example')
    actor = make_user(instance, 'alice')

    activity = inbox_activity(actor, activity_type='Like')
    activity['actor'] = {'type': 'Person'}

    dispatch(activity)

    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Actor object has no id'


# --- Task 8: the seam -- three signed requests, through the real /inbox route ---
#
# See the module docstring's TASK 8 section for exactly what these three
# tests license and what they do not. `redis_double` is required here (and
# nowhere above) because these three are the only tests in this file that
# reach shared_inbox() itself -- every test above calls process_inbox_request
# directly, skipping shared_inbox()'s own redis_client.exists/set duplicate
# check entirely.


def test_a_signed_like_reaches_the_upvote_arm_through_the_gate(
        app, db_session, signing_peer, redis_double, monkeypatch):
    """The full path: a REAL signed POST to /inbox, through every gate check
    sub-project 4 covered (HttpSignature.precheck, actor resolution via
    find_actor_or_create_cached, HttpSignature.verify_request -- none of
    which is patched here), into process_inbox_request, out at the Like arm
    (routes.py:1327-1329).

    DEBUG=True makes shared_inbox call process_inbox_request INLINE
    (routes.py:758-759) rather than queueing it via `.delay(...)` -- the only
    way a single HTTP request can produce an assertion about an arm within
    the same test -- and is separately required for signed_inbox_post's own
    signed URI to clear `is_invalid_get_request_uri`'s '.local'-host check
    (see tests/test_inbox_gate_refusals.py's test_a_tampered_body_fails_precheck
    docstring for that second reason).

    process_upvote is monkeypatched so this test proves only that the seam
    reaches it with the right `user` -- not process_upvote's own behaviour,
    which is Task 5's contract and already covered by tests/test_inbox_dispatch_votes.py.

    Request-context note (see module docstring TASK 8 section): this drives
    the dispatcher through a real Flask request, so has_request_context() is
    True and patch_db_session does NOT patch db.session here, unlike every
    dispatch() call above in this file. That distinction turns out not to
    matter for THIS test: find_actor_or_create_cached (the only actor lookup
    either the gate or the preamble performs for this shape) always resolves
    through db.session, never through process_inbox_request's independent
    get_task_session() session, so the seeded signing_peer row is found the
    same way regardless of which session arrangement is in effect.
    """
    monkeypatch.setitem(app.config, 'DEBUG', True)

    calls = []
    monkeypatch.setattr(activitypub_routes, 'process_upvote',
                         lambda *args, **kwargs: calls.append(args))

    activity = inbox_activity(signing_peer, activity_type='Like')

    with app.test_client() as client:
        response = signed_inbox_post(client, activity, signing_peer)

    assert response.status_code == 200
    assert len(calls) == 1
    user_arg, store_ap_json_arg, request_json_arg, announced_arg = calls[0]
    assert user_arg is not None and user_arg.id == signing_peer.id
    assert announced_arg is False


def test_a_signed_announce_reaches_the_unwrap_through_the_gate(
        app, db_session, signing_peer, redis_double, monkeypatch):
    """The same path as above, for the Announce unwrap -- the preamble's
    other outcome that leads somewhere interesting (routes.py:895-899).

    `signing_peer` is a plain User, and inbox_activity()'s default object is
    a plain string, so this drives the identical miss/miss/hit lookup
    sequence as test_the_dispatcher_finds_a_seeded_actor_through_its_own_session
    (Task 1) -- community_only and feed_only both miss, the unnarrowed lookup
    at routes.py:866 hits -- except reached through a real signed POST rather
    than a direct dispatch() call, and therefore through find_actor_or_create_cached
    TWICE for the same actor_id: once by the gate (routes.py:703, to resolve
    the actor whose public_key verifies the signature) and once more by the
    preamble (routes.py:866). process_announce_of_uri is monkeypatched, for
    the same reason given in Task 1's sibling test: it logs its own outcome
    on every path, so this test would have no ActivityPubLog row to assert on
    otherwise, and its own behaviour is out of this task's scope.
    """
    monkeypatch.setitem(app.config, 'DEBUG', True)

    calls = []
    monkeypatch.setattr(activitypub_routes, 'process_announce_of_uri',
                         lambda *args, **kwargs: calls.append(args))

    activity = inbox_activity(signing_peer, activity_type='Announce')

    with app.test_client() as client:
        response = signed_inbox_post(client, activity, signing_peer)

    assert response.status_code == 200
    assert len(calls) == 1
    assert calls[0][1] is None  # `community`: None either way once a non-community actor resolves


def test_the_actor_the_gate_verified_is_the_actor_the_arm_receives(
        app, db_session, signing_peer, redis_double, monkeypatch):
    """The seam's real content. The gate verifies the HTTP signature against
    one actor object -- find_actor_or_create_cached(request_json['actor'])
    at routes.py:703, whose `.public_key` HttpSignature.verify_request checks
    the signature against -- and the dispatcher re-resolves
    request_json['actor'] a SECOND time, independently, at routes.py:871.
    Every arm downstream (process_upvote here) trusts whatever that second
    lookup returns as `user`, on the unstated assumption that it names the
    same row the gate already verified. This test pins that assumption:
    find_actor_or_create_cached is wrapped (not replaced -- the real
    resolution still has to run for the request to succeed) so this test can
    record what the GATE's call returns, and process_upvote is monkeypatched
    so this test can record what the ARM receives, and the two are compared
    by id.

    Comparison is by `.id`, matching the standard this file establishes
    above (Task 2's community test) for comparing ORM rows across sessions --
    even though, per the module docstring TASK 8 section, both calls here
    actually resolve through the SAME db.session object (the request-scoped
    one), so in this particular case the two objects may well be identical
    (`is`) too. That would be an artifact of find_actor_or_create_cached's
    implementation (a bare `db.session.get(...)`, which returns the same
    Python object for the same primary key within one session's identity
    map) and of NullCache making the memoize wrapper recompute every call
    (see tests/conftest.py's TestConfig.CACHE_TYPE) -- not a contract this
    test relies on, so `.id` equality is what is asserted.
    """
    monkeypatch.setitem(app.config, 'DEBUG', True)

    gate_actor = {}
    real_find_actor_or_create_cached = activitypub_routes.find_actor_or_create_cached

    def _recording_find_actor_or_create_cached(*args, **kwargs):
        result = real_find_actor_or_create_cached(*args, **kwargs)
        gate_actor.setdefault('actor', result)
        return result

    monkeypatch.setattr(activitypub_routes, 'find_actor_or_create_cached',
                         _recording_find_actor_or_create_cached)

    arm_calls = []
    monkeypatch.setattr(activitypub_routes, 'process_upvote',
                         lambda *args, **kwargs: arm_calls.append(args))

    activity = inbox_activity(signing_peer, activity_type='Like')

    with app.test_client() as client:
        response = signed_inbox_post(client, activity, signing_peer)

    assert response.status_code == 200
    assert len(arm_calls) == 1
    arm_user = arm_calls[0][0]

    assert gate_actor['actor'] is not None
    assert arm_user is not None
    assert arm_user.id == gate_actor['actor'].id

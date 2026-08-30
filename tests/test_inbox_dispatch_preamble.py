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
"""
import pytest

from app.activitypub import routes as activitypub_routes
from app.activitypub.routes import process_inbox_request
from app.models import ActivityPubLog
from tests.factories import inbox_activity, make_instance, make_site, make_user


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

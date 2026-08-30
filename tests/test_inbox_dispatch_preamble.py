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

from app.activitypub.routes import process_inbox_request
from app.models import ActivityPubLog
from tests.factories import inbox_activity, make_instance, make_site, make_user


def dispatch(activity, store_ap_json=True):
    """Call the dispatcher the way production's DEBUG branch does."""
    process_inbox_request(activity, store_ap_json)


def test_the_dispatcher_runs_against_seeded_rows_and_its_log_row_is_visible(
        app, db_session, monkeypatch):
    """The whole sub-project rests on three things holding at once: a row this
    test commits is visible to the dispatcher's independent task session, the
    dispatcher runs to completion without a request context, and a row IT
    writes is visible to this test after `finally: session.close()`.

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

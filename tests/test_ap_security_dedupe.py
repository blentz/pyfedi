"""S2.4: an activity id is remembered only once its signature has verified, for ACTIVITY_DEDUPE_SECONDS."""
import json

import pytest

from app.activitypub.routes import ACTIVITY_DEDUPE_SECONDS
from app.models import ActivityPubLog
from tests.conftest import unsigned_but_precheck_clean_headers
from tests.factories import inbox_activity, signed_inbox_post


@pytest.fixture
def dispatched(app, monkeypatch):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    monkeypatch.setitem(app.config, 'DEBUG', True)
    calls = []
    monkeypatch.setattr('app.activitypub.routes.process_inbox_request', lambda *a, **kw: calls.append(a))
    return calls


def test_the_window_is_a_day():
    assert ACTIVITY_DEDUPE_SECONDS == 86400


def test_a_forged_request_reusing_a_genuine_id_does_not_suppress_it(app, signing_peer, redis_double, dispatched):
    activity = inbox_activity(signing_peer)
    body = json.dumps(activity).encode('utf8')
    with app.test_client() as client:
        forged = client.post('/inbox', data=body, content_type='application/activity+json',
                             headers={'Host': app.config['SERVER_NAME'], **unsigned_but_precheck_clean_headers(body)})
        genuine = signed_inbox_post(client, activity, signing_peer)
    assert forged.status_code == 400
    assert genuine.status_code == 200
    assert len(dispatched) == 1


def test_a_verified_id_is_remembered_for_the_window(app, signing_peer, redis_double, dispatched):
    activity = inbox_activity(signing_peer)
    with app.test_client() as client:
        signed_inbox_post(client, activity, signing_peer)
    assert ACTIVITY_DEDUPE_SECONDS - 10 < redis_double.ttl(activity['id']) <= ACTIVITY_DEDUPE_SECONDS


def test_a_redelivery_verified_while_the_first_was_in_flight_is_dropped(app, signing_peer, redis_double,
                                                                        dispatched, monkeypatch):
    """The early exists() check missed it (the first delivery had not been verified yet); the post-verification
    set(nx=True) is what catches it."""
    activity = inbox_activity(signing_peer)
    redis_double.set(activity['id'], 1)
    monkeypatch.setattr(redis_double, 'exists', lambda *keys: 0)
    with app.test_client() as client:
        response = signed_inbox_post(client, activity, signing_peer)
    assert response.status_code == 200
    assert dispatched == []
    assert ActivityPubLog.query.one().exception_message == 'Already aware of this activity'

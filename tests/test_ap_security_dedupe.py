"""S2.4: an activity id is remembered only once its signature has verified, for ACTIVITY_DEDUPE_SECONDS."""
import json
from types import SimpleNamespace

import pytest

from app import db
from app.activitypub.util import ACTIVITY_DEDUPE_SECONDS, activity_dedupe_key, claim_activity
from app.models import ActivityPubLog, Relay
from app.relays import RELAY_ACCEPTED, STYLE_MASTODON
from app.relays import inbound as relay_inbound
from tests.conftest import unsigned_but_precheck_clean_headers
from tests.factories import a_keypair, inbox_activity, signed_inbox_post


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
        assert redis_double.keys() == []   # the forgery left no dedupe key
        genuine = signed_inbox_post(client, activity, signing_peer)
    assert forged.status_code == 400
    assert genuine.status_code == 200
    assert len(dispatched) == 1


def test_a_verified_id_is_remembered_for_the_window(app, signing_peer, redis_double, dispatched):
    activity = inbox_activity(signing_peer)
    with app.test_client() as client:
        signed_inbox_post(client, activity, signing_peer)
    assert ACTIVITY_DEDUPE_SECONDS - 10 < redis_double.ttl(activity_dedupe_key(activity['id'], activity['actor'])) <= ACTIVITY_DEDUPE_SECONDS


def test_a_redelivery_verified_while_the_first_was_in_flight_is_dropped(app, signing_peer, redis_double,
                                                                        dispatched, monkeypatch):
    """The early exists() check missed it (the first delivery had not been verified yet); the post-verification
    set(nx=True) is what catches it."""
    activity = inbox_activity(signing_peer)
    redis_double.set(activity_dedupe_key(activity['id'], activity['actor']), 1)
    monkeypatch.setattr(redis_double, 'exists', lambda *keys: 0)
    with app.test_client() as client:
        response = signed_inbox_post(client, activity, signing_peer)
    assert response.status_code == 200
    assert dispatched == []
    assert ActivityPubLog.query.one().exception_message == 'Already aware of this activity'


def test_a_same_host_redelivery_is_deduped(app, signing_peer, redis_double, dispatched):
    activity = inbox_activity(signing_peer)
    with app.test_client() as client:
        signed_inbox_post(client, activity, signing_peer)
        again = signed_inbox_post(client, activity, signing_peer)
    assert again.status_code == 200
    assert len(dispatched) == 1


def test_an_id_naming_another_mechanisms_key_does_not_touch_it(app, signing_peer, redis_double, dispatched):
    activity = inbox_activity(signing_peer, id='relay-refetch:1')
    with app.test_client() as client:
        signed_inbox_post(client, activity, signing_peer)
    assert redis_double.exists('relay-refetch:1') == 0
    assert redis_double.exists(activity_dedupe_key('relay-refetch:1', signing_peer.ap_profile_id)) == 1


def test_one_host_claiming_anothers_id_does_not_suppress_it(app, redis_double):
    shared_id = 'https://b.example/activities/1'
    assert claim_activity(shared_id, 'https://a.example/u/mallory')
    assert claim_activity(shared_id, 'https://b.example/u/genuine')
    assert not claim_activity(shared_id, 'https://b.example/u/genuine')


# --- R-DEDUPEUNDO: a claim whose activity was never handed on is released ---

class _Unreachable(Exception):
    pass


def _raise(*_args, **_kwargs):
    raise _Unreachable('broker down')


@pytest.mark.parametrize('failing', ['commit', 'dispatch'])
def test_a_claim_is_released_when_the_activity_is_never_handed_on(app, signing_peer, redis_double, monkeypatch,
                                                                  failing):
    """The sender retries the same id after a 5xx; it must be processed then, not dropped as already seen."""
    monkeypatch.setitem(app.config, 'DEBUG', True)   # signing to test.piefed.local needs DEBUG; dispatch is inline
    activity = inbox_activity(signing_peer)
    key = activity_dedupe_key(activity['id'], activity['actor'])
    with monkeypatch.context() as failure:
        if failing == 'commit':
            failure.setattr('app.activitypub.routes.db.session.commit', _raise)
        else:
            failure.setattr('app.activitypub.routes.process_inbox_request', _raise)
        with app.test_client() as client, pytest.raises(_Unreachable):
            signed_inbox_post(client, activity, signing_peer)
    assert redis_double.exists(key) == 0

    dispatched = []
    monkeypatch.setattr('app.activitypub.routes.process_inbox_request', lambda *a, **kw: dispatched.append(a))
    with app.test_client() as client:
        retried = signed_inbox_post(client, activity, signing_peer)
    assert retried.status_code == 200
    assert len(dispatched) == 1
    assert redis_double.exists(key) == 1


def test_a_claimed_account_deletion_is_released_when_its_dispatch_fails(app, signing_peer, redis_double, monkeypatch):
    monkeypatch.setitem(app.config, 'DEBUG', True)
    activity = inbox_activity(signing_peer, activity_type='Delete', object_uri=signing_peer.ap_profile_id)
    monkeypatch.setattr('app.activitypub.routes.process_delete_request', _raise)
    with app.test_client() as client, pytest.raises(_Unreachable):
        signed_inbox_post(client, activity, signing_peer)
    assert redis_double.exists(activity_dedupe_key(activity['id'], activity['actor'])) == 0


def test_a_relay_claim_is_released_when_its_dispatch_fails(app, site, redis_double, monkeypatch):
    private_key, public_key = a_keypair()
    relay_actor = 'https://relay.example/actor'
    db.session.add(Relay(url='https://relay.example/inbox', style=STYLE_MASTODON, inbox_url='https://relay.example/inbox',
                         actor_id=relay_actor, public_key=public_key, follow_activity_id='x', state=RELAY_ACCEPTED))
    db.session.commit()
    monkeypatch.setitem(app.config, 'DEBUG', True)
    monkeypatch.setattr(relay_inbound, 'process_relayed_announce', _raise)
    announce = {'id': f'{relay_actor}/a/1', 'type': 'Announce', 'actor': relay_actor,
                'object': 'https://other.example/notes/1'}
    sender = SimpleNamespace(private_key=private_key, ap_profile_id=relay_actor)
    with app.test_client() as client, pytest.raises(_Unreachable):
        signed_inbox_post(client, announce, sender)
    assert redis_double.exists(activity_dedupe_key(announce['id'], relay_actor)) == 0

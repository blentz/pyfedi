"""S2.2: a verified signature whose keyId names another host is accepted only via an LD signature or a relay."""
import json
import logging

import pytest

from app import db
from app.activitypub.routes import SIGNATURE_FAILURE_MESSAGES
from app.activitypub.signature import HttpSignature
from app.models import ActivityPubLog
from app.relays import RELAY_ACCEPTED
from tests.conftest import ld_signed_body
from tests.factories import a_keypair, inbox_activity
from tests.test_relays_inbound import make_relay

pytestmark = pytest.mark.usefixtures('redis_double')


@pytest.fixture
def dispatched(app, monkeypatch):
    monkeypatch.setitem(app.config, 'DEBUG', True)
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    calls = []
    monkeypatch.setattr('app.activitypub.routes.process_inbox_request', lambda *a, **kw: calls.append((a, kw)))
    return calls


def _post(app, activity, private_key, key_id):
    _uri, headers, body = HttpSignature.signed_request(f"https://{app.config['SERVER_NAME']}/inbox", activity,
                                                       private_key, key_id, send_via_async=True)
    with app.test_client() as client:
        return client.post('/inbox', data=body, headers=headers, content_type='application/activity+json')


def test_the_refusal_counts_as_a_signature_failure():
    assert 'Signature keyId host does not match actor' in SIGNATURE_FAILURE_MESSAGES


def test_a_key_id_on_another_host_is_refused_and_logged(app, signing_peer, dispatched, caplog):
    with caplog.at_level(logging.INFO):
        response = _post(app, inbox_activity(signing_peer), signing_peer.private_key, 'https://other.example/actor#main-key')
    assert response.status_code == 400 and dispatched == []
    assert ActivityPubLog.query.one().exception_message == 'Signature keyId host does not match actor'
    assert any('other.example' in r.message and 'peer.example' in r.message and r.levelno == logging.INFO
               for r in caplog.records)


@pytest.mark.parametrize('key_id', ['https://Peer.Example/users/alice#main-key',
                                    'https://peer.example:443/users/alice#main-key',
                                    'https://peer.example/users/alice/main-key'])
def test_case_port_and_path_differences_are_the_same_host(app, signing_peer, dispatched, key_id):
    """Review Focus 3 (case, default port) and Review Focus 1 (GoToSocial path keyId)."""
    response = _post(app, inbox_activity(signing_peer), signing_peer.private_key, key_id)
    assert response.status_code == 200 and len(dispatched) == 1


def test_a_valid_ld_signature_lets_it_through(app, signing_peer, dispatched, no_network_ld_signing):
    activity = json.loads(ld_signed_body(signing_peer))
    response = _post(app, activity, signing_peer.private_key, 'https://other.example/actor#main-key')
    assert response.status_code == 200 and len(dispatched) == 1


def test_an_invalid_ld_signature_does_not(app, signing_peer, dispatched, no_network_ld_signing):
    wrong_private, _ = next(pair for pair in (a_keypair() for _ in range(16)) if pair[1] != signing_peer.public_key)
    activity = json.loads(ld_signed_body(signing_peer, signing_key=wrong_private))
    response = _post(app, activity, signing_peer.private_key, 'https://other.example/actor#main-key')
    assert response.status_code == 400 and dispatched == []


def test_an_accepted_relay_signing_with_that_key_lets_it_through(app, signing_peer, dispatched):
    relay = make_relay(state=RELAY_ACCEPTED, public_key=signing_peer.public_key,
                       actor_id='https://relay.example/actor', url='https://relay.example/inbox')
    response = _post(app, inbox_activity(signing_peer), signing_peer.private_key, 'https://relay.example/actor#main-key')
    assert response.status_code == 200
    assert dispatched[0][1] == {'relay_id': relay.id}

"""Interop C1 item 4 (docs/superpowers/specs/2026-10-11-ap-interop-c1-design.md): a string `endpoints`.

ActivityPub lets `endpoints` be a URI naming a document instead of an inline object. shared_inbox_of returned
None for it, so such an actor was always delivered to at its own inbox. It now fetches the document -- only on
the actor's own host, capped, once, never retried -- and uses its `sharedInbox` only when that is on the actor's
host too. Any failure is None, and every caller falls back to the actor's own inbox exactly as before.

No route is registered for the cases that must NOT fetch: block_outbound_http (tests/conftest.py) refuses any
unmocked request, so a fetch there fails the test.
"""
import httpx
import pytest

from app import db
from app.activitypub.util import actor_json_to_model, refresh_user_profile_task, shared_inbox_of
from app.models import User
from tests.factories import peer_actor_json, peer_instance
from tests.test_ap_refresh_profiles import _person_document, _remote_user

PEER = 'peer.example'
ACTOR = f'https://{PEER}/u/alice'
OWN_INBOX = f'{ACTOR}/inbox'
ENDPOINTS = f'{ACTOR}/endpoints'
SHARED = f'https://{PEER}/inbox'


def _actor(endpoints=ENDPOINTS, actor_id=ACTOR):
    return {'id': actor_id, 'type': 'Person', 'inbox': OWN_INBOX, 'endpoints': endpoints}


def test_a_same_host_endpoints_document_is_fetched_and_its_shared_inbox_used(app, http_mock):
    route = http_mock.get(ENDPOINTS).respond(200, json={'id': ENDPOINTS, 'sharedInbox': SHARED})

    assert shared_inbox_of(_actor()) == SHARED
    assert route.call_count == 1


def test_a_shared_inbox_whose_host_differs_only_by_case_is_used(app, http_mock):
    http_mock.get(ENDPOINTS).respond(200, json={'sharedInbox': 'https://PEER.Example/inbox'})

    assert shared_inbox_of(_actor()) == 'https://PEER.Example/inbox'


@pytest.mark.parametrize('endpoints', [
    'https://elsewhere.example/endpoints',
    f'https://{PEER}.evil.example/endpoints',
    'not a url',
], ids=['other-host', 'lookalike-host', 'no-host'])
def test_an_endpoints_string_off_the_actors_host_is_not_fetched(app, endpoints):
    assert shared_inbox_of(_actor(endpoints)) is None


def test_an_actor_without_a_usable_id_never_fetches(app):
    assert shared_inbox_of({'endpoints': ENDPOINTS}) is None
    assert shared_inbox_of(_actor(actor_id=['x'])) is None


def test_a_cross_host_shared_inbox_in_the_fetched_document_is_ignored(app, http_mock):
    route = http_mock.get(ENDPOINTS).respond(200, json={'sharedInbox': 'https://elsewhere.example/inbox'})

    assert shared_inbox_of(_actor()) is None
    assert route.call_count == 1


@pytest.mark.parametrize('document', [
    {},
    {'sharedInbox': ''},
    {'sharedInbox': ['https://peer.example/inbox']},
], ids=['no-key', 'empty', 'non-string'])
def test_a_fetched_document_without_a_usable_shared_inbox_gives_none(app, http_mock, document):
    http_mock.get(ENDPOINTS).respond(200, json=document)

    assert shared_inbox_of(_actor()) is None


def test_a_non_dict_result_falls_back(app, http_mock):
    http_mock.get(ENDPOINTS).respond(200, json=[SHARED])

    assert shared_inbox_of(_actor()) is None


@pytest.mark.parametrize('status', [404, 500])
def test_a_failed_fetch_falls_back(app, http_mock, status):
    http_mock.get(ENDPOINTS).respond(status)

    assert shared_inbox_of(_actor()) is None


def test_a_transport_error_is_not_retried(app, http_mock):
    route = http_mock.get(ENDPOINTS).mock(side_effect=httpx.ConnectError('refused'))

    assert shared_inbox_of(_actor()) is None
    assert route.call_count == 1


def test_a_person_with_string_endpoints_is_created_with_the_fetched_shared_inbox(app, db_session, http_mock):
    peer_instance(PEER)
    http_mock.get(ENDPOINTS).respond(200, json={'sharedInbox': SHARED})
    document = peer_actor_json(name='alice', server=PEER, fields={'endpoints': ENDPOINTS, 'inbox': OWN_INBOX})

    user = actor_json_to_model(document, 'alice', PEER)

    assert user.ap_inbox_url == SHARED


def test_a_person_whose_endpoints_fetch_fails_gets_its_own_inbox(app, db_session, http_mock):
    peer_instance(PEER)
    http_mock.get(ENDPOINTS).respond(404)
    document = peer_actor_json(name='alice', server=PEER, fields={'endpoints': ENDPOINTS, 'inbox': OWN_INBOX})

    user = actor_json_to_model(document, 'alice', PEER)

    assert user.ap_inbox_url == OWN_INBOX


def test_a_refresh_uses_the_fetched_shared_inbox(app, db_session, http_mock):
    """Task 3's delivery_inbox_of goes through shared_inbox_of, so the refresh tasks get the fetch too."""
    user = _remote_user('wakko')
    user.ap_inbox_url = f'https://{PEER}/u/wakko/inbox'
    db.session.commit()
    user_id = user.id
    endpoints = f'https://{PEER}/u/wakko/endpoints'
    http_mock.get(endpoints).respond(200, json={'sharedInbox': SHARED})

    refresh_user_profile_task(user_id, _person_document(fields={'endpoints': endpoints}))

    db.session.expire_all()
    assert db.session.get(User, user_id).ap_inbox_url == SHARED

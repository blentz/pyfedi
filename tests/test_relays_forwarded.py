"""Mastodon-style relayed deliveries (spec: Recognising a relay delivery; What is kept)."""
import contextlib
import uuid

import pytest
from flask import request

import app as app_pkg
from app import db
from app.activitypub import routes, util
from app.activitypub.signature import LDSignature, VerificationError
from app.constants import APLOG_CREATE
from app.models import BannedInstances, Post, PostReply, utcnow
from app.relays import RELAY_ACCEPTED, RELAY_PENDING, current_relay_id
from app.relays import inbound
from tests.factories import (a_keypair, make_community, make_instance, make_post,
                             make_user, signed_inbox_post)
from tests.test_relays_inbound import ACTOR, NOTE_URI, PEM, make_relay, note

pytestmark = pytest.mark.usefixtures('site', 'redis_double')

KEY_ID = ACTOR + '#main-key'


def fake_request(key_id=KEY_ID):
    headers = {} if key_id is None else {'Signature': f'keyId="{key_id}",algorithm="rsa-sha256",signature="x"'}
    return type('FakeRequest', (), {'headers': headers})()


@pytest.fixture
def verifier(monkeypatch):
    tried = []

    def fake(request_, public_key, skip_date=False):
        tried.append(public_key)
        if public_key != PEM:
            raise VerificationError('wrong key')
        return True

    monkeypatch.setattr(inbound.HttpSignature, 'verify_request', fake)
    return tried


class TestRelayForForwarded:

    def test_an_accepted_relays_verified_signature_names_the_relay(self, app, verifier):
        relay = make_relay(state=RELAY_ACCEPTED)
        assert inbound.relay_for_forwarded(fake_request()) == relay

    def test_a_signature_that_fails_is_no_relay(self, app, verifier):
        make_relay(state=RELAY_ACCEPTED, public_key='OTHER-KEY')
        assert inbound.relay_for_forwarded(fake_request()) is None

    def test_a_pending_relay_is_ignored(self, app, verifier):
        make_relay(state=RELAY_PENDING)
        assert inbound.relay_for_forwarded(fake_request()) is None
        assert verifier == []

    def test_a_relay_with_no_key_is_ignored(self, app, verifier):
        make_relay(state=RELAY_ACCEPTED, public_key=None)
        assert inbound.relay_for_forwarded(fake_request()) is None

    def test_no_signature_header_is_no_relay(self, app, verifier):
        make_relay(state=RELAY_ACCEPTED)
        assert inbound.relay_for_forwarded(fake_request(None)) is None
        assert verifier == []

    def test_a_key_of_another_host_is_no_relay(self, app, verifier):
        make_relay(state=RELAY_ACCEPTED)
        assert inbound.relay_for_forwarded(fake_request('https://evil.example/actor#main-key')) is None
        assert verifier == []


@pytest.fixture
def bounced_author(app, monkeypatch):
    """A remote author whose own HTTP signature never verifies but whose LD signature does."""
    monkeypatch.setitem(app.config, 'DEBUG', True)
    monkeypatch.setattr(LDSignature, 'verify_signature', lambda *a, **k: True)
    instance = make_instance('other.example')
    author = make_user(instance, 'alice', with_keys=True)
    author.ap_fetched_at = utcnow()
    db.session.commit()
    return author


def post_through_the_inbox(app, author, relay_key_pair, calls, monkeypatch):
    private_key, _public = relay_key_pair
    monkeypatch.setattr(routes, 'process_inbox_request', lambda *a, **k: calls.append((a, k)))
    activity = {'id': f'{author.ap_profile_id}/a/{uuid.uuid4().hex}', 'type': 'Create',
                'actor': author.ap_profile_id, 'object': note(), 'signature': {'type': 'RsaSignature2017'}}
    relay_signer = type('Sender', (), {'private_key': private_key, 'ap_profile_id': ACTOR})()
    with app.test_client() as client:
        return signed_inbox_post(client, activity, relay_signer)


class TestSharedInbox:

    def test_a_delivery_signed_by_an_accepted_relay_is_processed_for_that_relay(self, app, bounced_author,
                                                                                  monkeypatch):
        keys = a_keypair()
        relay = make_relay(state=RELAY_ACCEPTED, public_key=keys[1])
        calls = []
        response = post_through_the_inbox(app, bounced_author, keys, calls, monkeypatch)
        assert response.status_code == 200
        assert len(calls) == 1 and calls[0][1] == {'relay_id': relay.id}

    def test_a_delivery_signed_by_a_pending_relay_is_processed_with_no_relay(self, app, bounced_author,
                                                                               monkeypatch):
        keys = a_keypair()
        make_relay(state=RELAY_PENDING, public_key=keys[1])
        calls = []
        response = post_through_the_inbox(app, bounced_author, keys, calls, monkeypatch)
        assert response.status_code == 200
        assert len(calls) == 1 and calls[0][1] == {'relay_id': None}

    def test_a_delivery_signed_by_some_other_key_is_processed_with_no_relay(self, app, bounced_author,
                                                                              monkeypatch):
        keys = a_keypair()
        other_public = next(pair[1] for pair in (a_keypair() for _ in range(10)) if pair[1] != keys[1])
        make_relay(state=RELAY_ACCEPTED, public_key=other_public)
        calls = []
        post_through_the_inbox(app, bounced_author, keys, calls, monkeypatch)
        assert len(calls) == 1 and calls[0][1] == {'relay_id': None}

    def test_the_celery_branch_passes_the_relay_too(self, app, bounced_author, monkeypatch):
        keys = a_keypair()
        relay = make_relay(state=RELAY_ACCEPTED, public_key=keys[1])
        delayed = []
        monkeypatch.setattr(routes.process_inbox_request, 'delay', lambda *a, **k: delayed.append((a, k)))
        activity = {'id': f'{bounced_author.ap_profile_id}/a/{uuid.uuid4().hex}', 'type': 'Create',
                    'actor': bounced_author.ap_profile_id, 'object': note(),
                    'signature': {'type': 'RsaSignature2017'}}
        relay_signer = type('Sender', (), {'private_key': keys[0], 'ap_profile_id': ACTOR})()
        with app.test_client() as client:
            _uri, headers, body = routes.HttpSignature.signed_request(
                f"https://{app.config['SERVER_NAME']}/inbox", activity, relay_signer.private_key,
                ACTOR + '#main-key', send_via_async=True)
            monkeypatch.setitem(app.config, 'DEBUG', False)   # signing needed DEBUG for a .local host
            client.post('/inbox', data=body, headers=headers, content_type='application/activity+json')
        assert len(delayed) == 1 and delayed[0][1] == {'relay_id': relay.id}


@pytest.fixture
def fetch(monkeypatch):
    calls = []
    monkeypatch.setattr(util, 'remote_object_to_json', lambda uri: calls.append(uri))
    return calls


@pytest.fixture
def logged(monkeypatch):
    calls = []
    monkeypatch.setattr(routes, 'log_incoming_ap',
                        lambda id, kind, result, saved, message=None, session=None: calls.append((kind, result, message)))
    return calls


@pytest.fixture
def lockless_redis(redis_double, monkeypatch):
    monkeypatch.setattr(app_pkg.redis_client, 'lock', lambda *args, **kwargs: contextlib.nullcontext(),
                        raising=False)


@pytest.fixture
def alice():
    user = make_user(make_instance('other.example'), 'alice')
    user.ap_fetched_at = utcnow()
    user.ap_domain = 'other.example'
    db.session.commit()
    return user


def create(author, obj):
    return {'id': f'{author.ap_profile_id}/a/{uuid.uuid4().hex}', 'type': 'Create',
            'actor': author.ap_profile_id, 'object': obj, 'to': obj.get('to', []) if isinstance(obj, dict) else []}


class TestProcessInboxRequestForARelay:

    def test_a_top_level_create_with_no_community_is_a_microblog_post_recording_the_relay(
            self, app, alice, fetch, logged, lockless_redis):
        relay = make_relay(state=RELAY_ACCEPTED)
        routes.process_inbox_request(create(alice, note()), False, relay_id=relay.id)
        post = Post.query.filter_by(ap_id=NOTE_URI).one()
        assert post.relay_id == relay.id and post.community.name == 'microblogs'

    def test_the_relay_context_is_reset_afterwards(self, app, alice, fetch, logged, lockless_redis):
        relay = make_relay(state=RELAY_ACCEPTED)
        routes.process_inbox_request(create(alice, note()), False, relay_id=relay.id)
        assert current_relay_id.get() is None

    def test_a_create_for_an_unknown_community_is_dropped_and_logged(self, app, alice, fetch, logged):
        relay = make_relay(state=RELAY_ACCEPTED)
        routes.process_inbox_request(create(alice, note(audience='https://other.example/c/nowhere')), False,
                                     relay_id=relay.id)
        assert Post.query.count() == 0
        assert logged[-1][2] == 'relayed post for a community this instance does not have'

    def test_a_reply_to_an_unknown_parent_is_dropped_without_a_fetch(self, app, alice, fetch, logged):
        relay = make_relay(state=RELAY_ACCEPTED)
        routes.process_inbox_request(
            create(alice, note(inReplyTo='https://other.example/notes/unknown')), False, relay_id=relay.id)
        assert Post.query.count() == 0 and PostReply.query.count() == 0
        assert fetch == []

    def test_a_reply_to_a_stored_post_is_stored(self, app, alice, fetch, logged, lockless_redis):
        relay = make_relay(state=RELAY_ACCEPTED)
        parent = make_post(make_community(), alice, 'https://other.example/notes/parent')
        routes.process_inbox_request(create(alice, note(inReplyTo=parent.ap_id)), False, relay_id=relay.id)
        assert PostReply.query.filter_by(ap_id=NOTE_URI).one().post_id == parent.id

    def test_an_announce_is_dropped(self, app, alice, fetch, logged):
        relay = make_relay(state=RELAY_ACCEPTED)
        announce = {'id': 'https://other.example/a/1', 'type': 'Announce', 'actor': alice.ap_profile_id,
                    'object': note()}
        routes.process_inbox_request(announce, False, relay_id=relay.id)
        assert Post.query.count() == 0
        assert logged[-1][2] == 'relayed boost'

    def test_a_create_for_a_stored_post_changes_nothing(self, app, alice, fetch, logged, lockless_redis):
        relay = make_relay(state=RELAY_ACCEPTED)
        existing = make_post(make_community(), alice, NOTE_URI)
        routes.process_inbox_request(create(alice, note()), False, relay_id=relay.id)
        db.session.refresh(existing)
        assert Post.query.count() == 1 and existing.relay_id is None

    def test_without_a_relay_the_relay_rules_are_not_applied(self, app, alice, fetch, logged, lockless_redis):
        routes.process_inbox_request(create(alice, note(audience='https://other.example/c/nowhere')), False)
        assert 'relayed post for a community this instance does not have' not in [m for _k, _r, m in logged]

    def test_without_a_relay_a_post_records_none(self, app, alice, fetch, logged, lockless_redis):
        routes.process_inbox_request(create(alice, note()), False)
        assert Post.query.filter_by(ap_id=NOTE_URI).one().relay_id is None


class TestModerationStillAppliesThroughARelay:

    def test_an_author_on_a_banned_instance_makes_no_post(self, app, alice, fetch, logged, lockless_redis):
        relay = make_relay(state=RELAY_ACCEPTED)
        db.session.add(BannedInstances(domain='other.example'))
        db.session.commit()
        routes.process_inbox_request(create(alice, note()), False, relay_id=relay.id)
        assert Post.query.count() == 0

    def test_a_banned_user_makes_no_post(self, app, alice, fetch, logged, lockless_redis):
        relay = make_relay(state=RELAY_ACCEPTED)
        alice.banned = True
        db.session.commit()
        routes.process_inbox_request(create(alice, note()), False, relay_id=relay.id)
        assert Post.query.count() == 0

    def test_a_create_to_a_local_only_community_makes_no_post(self, app, alice, fetch, logged, lockless_redis):
        relay = make_relay(state=RELAY_ACCEPTED)
        community = make_community('cats', host='other.example')
        community.local_only = True
        db.session.commit()
        routes.process_inbox_request(create(alice, note(audience=community.ap_profile_id)), False,
                                     relay_id=relay.id)
        assert Post.query.count() == 0


def update(author, obj):
    activity = create(author, obj)
    activity['type'] = 'Update'
    return activity


class TestOpenThreadRuleOnTheMastodonPath:

    def test_a_reply_to_a_deleted_post_is_not_stored(self, app, alice, fetch, logged, lockless_redis):
        relay = make_relay(state=RELAY_ACCEPTED)
        parent = make_post(make_community(), alice, 'https://other.example/notes/parent')
        parent.deleted = True
        db.session.commit()
        routes.process_inbox_request(create(alice, note(inReplyTo=parent.ap_id)), False, relay_id=relay.id)
        assert PostReply.query.count() == 0
        assert logged[-1][2] == 'relayed reply to a closed thread'

    def test_a_reply_in_a_private_community_is_not_stored(self, app, alice, fetch, logged, lockless_redis):
        relay = make_relay(state=RELAY_ACCEPTED)
        community = make_community()
        parent = make_post(community, alice, 'https://other.example/notes/parent')
        community.private = True
        db.session.commit()
        routes.process_inbox_request(create(alice, note(inReplyTo=parent.ap_id)), False, relay_id=relay.id)
        assert PostReply.query.count() == 0

    def test_a_reply_in_an_open_thread_is_stored(self, app, alice, fetch, logged, lockless_redis):
        relay = make_relay(state=RELAY_ACCEPTED)
        parent = make_post(make_community(), alice, 'https://other.example/notes/parent')
        routes.process_inbox_request(create(alice, note(inReplyTo=parent.ap_id)), False, relay_id=relay.id)
        assert PostReply.query.count() == 1

    def test_the_litepub_path_logs_a_closed_thread_once(self, app, alice, monkeypatch, lockless_redis):
        relay = make_relay(state=RELAY_ACCEPTED)
        parent = make_post(make_community(), alice, 'https://other.example/notes/parent')
        parent.deleted = True
        db.session.commit()
        messages = []
        monkeypatch.setattr(inbound, 'log_incoming_ap',
                            lambda id, kind, result, saved, message=None, session=None: messages.append(message))
        monkeypatch.setattr(inbound, 'remote_object_to_json', lambda uri: note(inReplyTo=parent.ap_id))
        inbound.process_relayed_announce(relay.id, NOTE_URI)
        assert messages == ['relayed reply to a closed thread']


class TestRelayedUpdate:

    def test_an_update_of_an_unknown_object_creates_nothing(self, app, alice, fetch, logged, lockless_redis):
        relay = make_relay(state=RELAY_ACCEPTED)
        routes.process_inbox_request(update(alice, note()), False, relay_id=relay.id)
        assert Post.query.count() == 0
        assert logged[-1][2] == 'relayed update of an object this instance does not have'

    def test_an_update_whose_object_is_a_bare_unknown_string_is_dropped(self, app, alice, fetch, logged):
        relay = make_relay(state=RELAY_ACCEPTED)
        routes.process_inbox_request(update(alice, NOTE_URI), False, relay_id=relay.id)
        assert logged[-1][2] == 'relayed update of an object this instance does not have'

    def test_an_update_with_an_object_that_is_neither_is_dropped(self, app, alice, fetch, logged):
        relay = make_relay(state=RELAY_ACCEPTED)
        routes.process_inbox_request(update(alice, ['x']), False, relay_id=relay.id)
        assert logged[-1][2] == 'relayed update of an object this instance does not have'

    def test_an_update_of_a_stored_post_goes_through_the_normal_path(self, app, alice, fetch, logged,
                                                                      lockless_redis):
        relay = make_relay(state=RELAY_ACCEPTED)
        post = make_post(make_community(), alice, NOTE_URI)
        routes.process_inbox_request(update(alice, note(updated='2026-09-01T00:00:00Z')), False,
                                     relay_id=relay.id)
        db.session.refresh(post)
        assert Post.query.count() == 1 and post.relay_id is None
        assert 'relayed update of an object this instance does not have' not in [m for _k, _r, m in logged]

    def test_a_delete_is_still_allowed(self, app):
        assert inbound.relayed_activity_allowed({'type': 'Delete', 'object': NOTE_URI}) == (True, '')


class TestTokenBoundBeforeTheTry:

    def test_an_early_failure_propagates_and_leaves_no_relay_set(self, app, alice, monkeypatch):
        def boom(session):
            raise RuntimeError('early')

        monkeypatch.setattr(routes, 'patch_db_session', boom)
        with pytest.raises(RuntimeError, match='early'):
            routes.process_inbox_request(create(alice, note()), False, relay_id=5)
        assert current_relay_id.get() is None


class TestMalformedStoredKey:

    def test_the_gate_answers_401_for_a_key_that_is_not_a_pem(self, app, monkeypatch):
        monkeypatch.setitem(app.config, 'DEBUG', True)
        private_key, _public = a_keypair()
        make_relay(state=RELAY_ACCEPTED, public_key='not a pem')
        monkeypatch.setattr(inbound.relay_subscribe, 'detect_relay', lambda url: {'public_key': None})
        sender = type('Sender', (), {'private_key': private_key, 'ap_profile_id': ACTOR})()
        activity = {'id': 'https://relay.example/a/1', 'type': 'Accept', 'actor': ACTOR, 'object': 'x'}
        with app.test_client() as client:
            assert signed_inbox_post(client, activity, sender).status_code == 401

    def test_a_forwarded_delivery_with_such_a_key_is_no_relay(self, app, monkeypatch):
        monkeypatch.setitem(app.config, 'DEBUG', True)
        private_key, _public = a_keypair()
        make_relay(state=RELAY_ACCEPTED, public_key='not a pem')
        activity = {'id': 'https://relay.example/a/1', 'type': 'Create', 'actor': ACTOR, 'object': 'x'}
        _uri, headers, body = routes.HttpSignature.signed_request(
            f"https://{app.config['SERVER_NAME']}/inbox", activity, private_key, KEY_ID, send_via_async=True)
        with app.test_request_context('/inbox', method='POST', data=body, headers=headers):
            assert inbound.relay_for_forwarded(request) is None

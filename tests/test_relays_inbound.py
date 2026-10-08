"""Relay traffic in the inbox (spec: Recognising a relay delivery; What is kept)."""
import uuid

import pytest
from flask import g, request

from app import db
from app.activitypub.signature import VerificationError
from app.constants import ALLOWLIST_STRONG
from app.models import Community, Post, PostReply, Relay, Site, User
from app.relays import RELAY_ACCEPTED, RELAY_PENDING, RELAY_REFUSED, STYLE_LITEPUB, STYLE_MASTODON
from app.relays import inbound
from tests.factories import (a_keypair, make_community, make_instance, make_post, make_post_reply, make_user,
                             signed_inbox_post)

pytestmark = pytest.mark.usefixtures('site')

PEM = 'THE-RELAY-KEY'
FOLLOW_ID = 'https://test.piefed.local/activities/relay-follow/abc'
ACTOR = 'https://relay.example/actor'
NOTE_URI = 'https://other.example/notes/1'


def make_relay(state=RELAY_PENDING, style=STYLE_MASTODON, public_key=PEM, follow_id=FOLLOW_ID, actor_id=ACTOR,
               url='https://relay.example/inbox'):
    relay = Relay(url=url, style=style, inbox_url=url, actor_id=actor_id, public_key=public_key,
                  follow_activity_id=follow_id, state=state)
    db.session.add(relay)
    db.session.commit()
    return relay


@pytest.fixture
def verifier(monkeypatch):
    """HttpSignature.verify_request that accepts only the expected PEM; records the keys tried."""
    tried = []

    def fake(request_, public_key, skip_date=False):
        tried.append(public_key)
        if public_key != PEM:
            raise VerificationError('wrong key')
        return True

    monkeypatch.setattr(inbound.HttpSignature, 'verify_request', fake)
    return tried


@pytest.fixture
def queued(monkeypatch):
    calls = []
    monkeypatch.setattr(inbound.process_relayed_announce, 'apply_async',
                        lambda args=None, queue=None, **kw: calls.append((args, queue)))
    return calls


def gate(app, activity):
    with app.test_request_context('/inbox', method='POST'):
        return inbound.relay_actor_gate(request, activity)


def activity(kind, obj, actor=ACTOR):
    return {'id': f'{actor}/a/{uuid.uuid4().hex}', 'type': kind, 'actor': actor, 'object': obj}


class TestAcceptAndReject:

    @pytest.mark.parametrize('state', [RELAY_PENDING, RELAY_ACCEPTED])
    @pytest.mark.parametrize('as_dict', [True, False])
    def test_an_accept_of_our_follow_marks_the_relay_accepted(self, app, verifier, state, as_dict):
        relay = make_relay(state=state)
        obj = {'id': FOLLOW_ID, 'type': 'Follow'} if as_dict else FOLLOW_ID
        assert gate(app, activity('Accept', obj)) == ('', 200)
        db.session.refresh(relay)
        assert relay.state == RELAY_ACCEPTED
        if state == RELAY_PENDING:
            assert relay.answered_at is not None

    def test_an_accept_of_another_relays_follow_is_ignored(self, app, verifier):
        relay = make_relay()
        assert gate(app, activity('Accept', 'https://test.piefed.local/activities/relay-follow/other')) == ('', 200)
        db.session.refresh(relay)
        assert relay.state == RELAY_PENDING and relay.answered_at is None

    def test_an_accept_with_an_object_that_is_neither_string_nor_dict_is_ignored(self, app, verifier):
        relay = make_relay()
        assert gate(app, activity('Accept', ['x'])) == ('', 200)
        db.session.refresh(relay)
        assert relay.state == RELAY_PENDING

    def test_an_accept_for_a_row_that_is_not_pending_is_ignored(self, app, verifier):
        relay = make_relay(state=RELAY_REFUSED)
        assert gate(app, activity('Accept', FOLLOW_ID)) == ('', 200)
        db.session.refresh(relay)
        assert relay.state == RELAY_REFUSED and relay.answered_at is None

    def test_a_reject_refuses_the_relay(self, app, verifier):
        relay = make_relay()
        assert gate(app, activity('Reject', {'id': FOLLOW_ID})) == ('', 200)
        db.session.refresh(relay)
        assert relay.state == RELAY_REFUSED and relay.answered_at is not None


class TestAnnounce:

    def test_an_announce_from_an_accepted_relay_is_queued(self, app, verifier, queued, monkeypatch):
        monkeypatch.setitem(app.config, 'DEBUG', False)
        relay = make_relay(state=RELAY_ACCEPTED)
        assert gate(app, activity('Announce', NOTE_URI)) == ('', 200)
        assert queued == [((relay.id, NOTE_URI), 'background')]

    def test_an_announced_object_with_an_id_is_queued_by_its_id(self, app, verifier, queued, monkeypatch):
        monkeypatch.setitem(app.config, 'DEBUG', False)
        relay = make_relay(state=RELAY_ACCEPTED)
        gate(app, activity('Announce', {'id': NOTE_URI, 'type': 'Note'}))
        assert queued == [((relay.id, NOTE_URI), 'background')]

    def test_an_announce_with_no_usable_object_is_acknowledged_and_nothing_queued(self, app, verifier, queued):
        make_relay(state=RELAY_ACCEPTED)
        assert gate(app, activity('Announce', ['x'])) == ('', 200)
        assert queued == []

    def test_under_debug_the_announce_is_processed_inline(self, app, verifier, monkeypatch):
        monkeypatch.setitem(app.config, 'DEBUG', True)
        relay = make_relay(state=RELAY_ACCEPTED)
        done = []
        monkeypatch.setattr(inbound, 'process_relayed_announce', lambda *args: done.append(args))
        assert gate(app, activity('Announce', NOTE_URI)) == ('', 200)
        assert done == [(relay.id, NOTE_URI)]

    def test_an_announce_from_a_pending_relay_is_left_to_the_normal_inbox(self, app, verifier, queued):
        make_relay(state=RELAY_PENDING)
        assert gate(app, activity('Announce', NOTE_URI)) is None
        assert queued == []
        assert verifier == []

    def test_other_activity_types_from_an_accepted_relay_are_ignored(self, app, verifier, queued):
        make_relay(state=RELAY_ACCEPTED)
        assert gate(app, activity('Create', {'id': NOTE_URI})) == ('', 200)
        assert queued == []

    def test_no_user_is_created_for_the_relay(self, app, verifier, queued, monkeypatch):
        monkeypatch.setitem(app.config, 'DEBUG', False)
        relay = make_relay(state=RELAY_ACCEPTED)
        gate(app, activity('Announce', NOTE_URI))
        assert User.query.filter_by(ap_profile_id=relay.actor_id).count() == 0


class TestNotARelayActor:

    def test_an_actor_that_is_not_a_relay_is_left_alone(self, app, verifier):
        make_relay()
        assert gate(app, activity('Accept', FOLLOW_ID, actor='https://other.example/u/a')) is None

    def test_an_actor_that_is_not_a_string_is_left_alone(self, app, verifier):
        assert gate(app, {'type': 'Accept', 'actor': {'id': ACTOR}, 'object': FOLLOW_ID}) is None


class TestSignature:

    def test_a_bad_signature_refetches_the_key_once_and_a_good_retry_is_handled(self, app, verifier, monkeypatch):
        relay = make_relay(public_key='STALE')
        fetched = []
        monkeypatch.setattr(inbound, 'detect_relay',
                            lambda url: fetched.append(url) or {'public_key': PEM})
        assert gate(app, activity('Accept', FOLLOW_ID)) == ('', 200)
        assert fetched == [relay.url] and verifier == ['STALE', PEM]
        db.session.refresh(relay)
        assert relay.state == RELAY_ACCEPTED and relay.public_key == PEM

    def test_a_relay_with_no_stored_key_fetches_one(self, app, verifier, monkeypatch):
        relay = make_relay(public_key=None)
        monkeypatch.setattr(inbound, 'detect_relay', lambda url: {'public_key': PEM})
        assert gate(app, activity('Accept', FOLLOW_ID)) == ('', 200)
        assert verifier == [PEM]
        db.session.refresh(relay)
        assert relay.state == RELAY_ACCEPTED

    def test_a_signature_that_still_fails_is_a_401(self, app, verifier, monkeypatch):
        relay = make_relay(public_key='STALE')
        monkeypatch.setattr(inbound, 'detect_relay', lambda url: {'public_key': 'STILL-WRONG'})
        assert gate(app, activity('Accept', FOLLOW_ID)) == ('', 401)
        db.session.refresh(relay)
        assert relay.state == RELAY_PENDING

    def test_a_relay_whose_key_cannot_be_refetched_is_a_401(self, app, verifier, monkeypatch):
        make_relay(public_key='STALE')

        def fail(url):
            raise inbound.RelayError('gone')

        monkeypatch.setattr(inbound, 'detect_relay', fail)
        assert gate(app, activity('Accept', FOLLOW_ID)) == ('', 401)

    def test_a_refetch_that_finds_no_key_is_a_401(self, app, verifier, monkeypatch):
        make_relay(public_key=None)
        monkeypatch.setattr(inbound, 'detect_relay', lambda url: {'public_key': None})
        assert gate(app, activity('Accept', FOLLOW_ID)) == ('', 401)


class TestThroughTheInbox:

    def test_a_really_signed_accept_is_handled_and_no_user_is_made(self, app, monkeypatch):
        monkeypatch.setitem(app.config, 'DEBUG', True)  # lets the test sign for a .local host
        private_key, public_key = a_keypair()
        relay = make_relay(public_key=public_key)
        sender = type('Sender', (), {'private_key': private_key, 'ap_profile_id': ACTOR})()
        with app.test_client() as client:
            response = signed_inbox_post(client, activity('Accept', FOLLOW_ID), sender)
        assert response.status_code == 200
        db.session.refresh(relay)
        assert relay.state == RELAY_ACCEPTED
        assert User.query.filter_by(ap_profile_id=ACTOR).count() == 0

    def test_a_wrongly_signed_accept_is_a_401(self, app, monkeypatch):
        monkeypatch.setitem(app.config, 'DEBUG', True)
        private_key, public_key = a_keypair()
        other_public = next(pair[1] for pair in (a_keypair() for _ in range(10)) if pair[1] != public_key)
        relay = make_relay(public_key=other_public)
        monkeypatch.setattr(inbound, 'detect_relay', lambda url: {'public_key': other_public})
        sender = type('Sender', (), {'private_key': private_key, 'ap_profile_id': ACTOR})()
        with app.test_client() as client:
            response = signed_inbox_post(client, activity('Accept', FOLLOW_ID), sender)
        assert response.status_code == 401
        db.session.refresh(relay)
        assert relay.state == RELAY_PENDING

    def test_an_unallowed_relay_host_is_refused_before_the_gate(self, app, monkeypatch):
        make_relay()
        db.session.get(Site, 1).allowlist_mode = ALLOWLIST_STRONG
        db.session.commit()
        called = []
        monkeypatch.setattr('app.activitypub.routes.relay_actor_gate', lambda *a: called.append(a))
        monkeypatch.setattr('app.activitypub.routes.instance_allowed', lambda host: False)
        with app.test_client() as client:
            response = client.post('/inbox', json=activity('Accept', FOLLOW_ID))
        assert response.status_code == 403
        assert called == []


def note(uri=NOTE_URI, **fields):
    document = {'id': uri, 'type': 'Note', 'attributedTo': 'https://other.example/users/alice',
                'content': '<p>hello relay</p>', 'published': '2026-08-20T12:00:00Z',
                'to': ['https://www.w3.org/ns/activitystreams#Public']}
    document.update(fields)
    return document


@pytest.fixture
def fetch(monkeypatch):
    calls = []

    def fake(uri):
        calls.append(uri)
        return fake.result

    fake.result = None
    monkeypatch.setattr(inbound, 'remote_object_to_json', fake)
    fake.calls = calls
    return fake


@pytest.fixture
def logged(monkeypatch):
    calls = []
    monkeypatch.setattr(inbound, 'log_incoming_ap',
                        lambda id, kind, result, saved, message=None, session=None: calls.append((kind, result, message)))
    return calls


@pytest.fixture
def alice():
    return make_user(make_instance('other.example'), 'alice')


class TestProcessRelayedAnnounce:

    def test_a_top_level_note_becomes_a_microblog_post_recording_its_relay(self, app, fetch, logged, alice):
        relay = make_relay(state=RELAY_ACCEPTED)
        fetch.result = note()
        inbound.process_relayed_announce(relay.id, NOTE_URI)
        post = Post.query.filter_by(ap_id=NOTE_URI).one()
        assert post.relay_id == relay.id
        assert post.community.name == 'microblogs'

    def test_a_note_for_a_known_community_goes_into_it(self, app, fetch, logged, alice):
        relay = make_relay(state=RELAY_ACCEPTED)
        community = make_community('cats', host='other.example')
        fetch.result = note(audience=community.ap_profile_id)
        inbound.process_relayed_announce(relay.id, NOTE_URI)
        post = Post.query.filter_by(ap_id=NOTE_URI).one()
        assert post.community_id == community.id and post.relay_id == relay.id

    def test_a_fetch_that_fails_creates_nothing_and_is_logged(self, app, fetch, logged):
        relay = make_relay(state=RELAY_ACCEPTED)
        inbound.process_relayed_announce(relay.id, NOTE_URI)
        assert Post.query.count() == 0
        assert [m for _k, _r, m in logged] == ['Could not fetch relayed object']

    def test_a_reply_to_an_unknown_parent_is_dropped(self, app, fetch, logged, alice):
        relay = make_relay(state=RELAY_ACCEPTED)
        fetch.result = note(inReplyTo='https://other.example/notes/unknown')
        inbound.process_relayed_announce(relay.id, NOTE_URI)
        assert Post.query.count() == 0 and PostReply.query.count() == 0
        assert len(logged) == 1

    def test_a_reply_to_a_known_parent_is_still_not_stored_as_a_post(self, app, fetch, logged, alice):
        relay = make_relay(state=RELAY_ACCEPTED)
        parent = make_post(make_community(), alice, 'https://other.example/notes/parent')
        fetch.result = note(inReplyTo=parent.ap_id)
        inbound.process_relayed_announce(relay.id, NOTE_URI)
        assert Post.query.filter_by(ap_id=NOTE_URI).count() == 0
        assert PostReply.query.count() == 0
        assert logged[-1][2] == 'relayed reply'

    def test_an_object_for_an_unknown_community_is_dropped(self, app, fetch, logged, alice):
        relay = make_relay(state=RELAY_ACCEPTED)
        fetch.result = note(audience='https://other.example/c/nowhere')
        inbound.process_relayed_announce(relay.id, NOTE_URI)
        assert Post.query.count() == 0

    def test_an_existing_post_is_left_alone(self, app, fetch, logged, alice):
        relay = make_relay(state=RELAY_ACCEPTED)
        existing = make_post(make_community(), alice, NOTE_URI)
        inbound.process_relayed_announce(relay.id, NOTE_URI)
        db.session.refresh(existing)
        assert fetch.calls == []
        assert Post.query.count() == 1 and existing.relay_id is None

    def test_an_error_rolls_back_and_is_raised(self, app, fetch, logged, alice, monkeypatch):
        relay = make_relay(state=RELAY_ACCEPTED)
        fetch.result = note()

        def boom(*args):
            raise RuntimeError('boom')

        monkeypatch.setattr(inbound, 'create_resolved_object', boom)
        with pytest.raises(RuntimeError):
            inbound.process_relayed_announce(relay.id, NOTE_URI)


class TestRelayedObjectAllowed:

    def test_a_top_level_object_with_no_audience_is_allowed(self, app):
        assert inbound.relayed_object_allowed({'id': NOTE_URI}) == (True, '')

    def test_an_audience_that_is_a_known_community_is_allowed(self, app, alice):
        community = make_community('cats')
        assert inbound.relayed_object_allowed({'audience': community.ap_profile_id.upper()})[0]

    def test_an_unknown_audience_is_refused(self, app):
        allowed, reason = inbound.relayed_object_allowed({'audience': 'https://other.example/c/none'})
        assert not allowed and 'community' in reason

    def test_a_reply_to_a_stored_post_is_allowed(self, app, alice):
        post = make_post(make_community(), alice, 'https://other.example/notes/p')
        assert inbound.relayed_object_allowed({'inReplyTo': post.ap_id}) == (True, '')

    def test_a_reply_to_a_stored_reply_is_allowed(self, app, alice):
        post = make_post(make_community(), alice, 'https://other.example/notes/p')
        reply = make_post_reply(post, alice)
        reply.ap_id = 'https://other.example/notes/r'
        db.session.commit()
        assert inbound.relayed_object_allowed({'inReplyTo': reply.ap_id}) == (True, '')

    def test_a_reply_to_an_unknown_object_is_refused(self, app):
        assert inbound.relayed_object_allowed({'inReplyTo': 'https://other.example/notes/none'})[0] is False

    def test_a_reply_target_given_as_a_dict_is_read_by_its_id(self, app, alice):
        post = make_post(make_community(), alice, 'https://other.example/notes/p')
        assert inbound.relayed_object_allowed({'inReplyTo': {'id': post.ap_id}})[0] is True
        assert inbound.relayed_object_allowed({'inReplyTo': {'id': 'https://x.example/none'}})[0] is False

    def test_a_reply_target_that_is_not_a_string_is_refused(self, app):
        assert inbound.relayed_object_allowed({'inReplyTo': 5})[0] is False

    def test_a_non_dict_object_is_refused(self, app):
        assert inbound.relayed_object_allowed('https://x.example/n') == (False, 'relayed object is not an object')


class TestRelayedActivityAllowed:

    def test_a_boost_is_refused(self, app):
        assert inbound.relayed_activity_allowed({'type': 'Announce'}) == (False, 'relayed boost')

    def test_a_create_is_judged_by_its_object(self, app):
        assert inbound.relayed_activity_allowed({'type': 'Create', 'object': {'id': NOTE_URI}})[0] is True
        assert inbound.relayed_activity_allowed(
            {'type': 'Create', 'object': {'audience': 'https://other.example/c/none'}})[0] is False

    @pytest.mark.parametrize('kind', ['Update', 'Delete'])
    def test_update_and_delete_are_allowed(self, app, kind):
        assert inbound.relayed_activity_allowed({'type': kind}) == (True, '')

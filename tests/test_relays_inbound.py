"""Relay traffic in the inbox (spec: Recognising a relay delivery; What is kept)."""
import contextlib
import uuid

import pytest
from flask import g, request

import app as app_pkg
from app import db
from app.activitypub.signature import VerificationError
from app.constants import ALLOWLIST_STRONG, APLOG_FAILURE, APLOG_IGNORED, APLOG_SUCCESS
from app.models import BannedInstances, Community, Post, PostReply, Relay, Site, User, utcnow
from app.relays import RELAY_ACCEPTED, RELAY_PENDING, RELAY_REFUSED, STYLE_LITEPUB, STYLE_MASTODON
from app.relays import inbound
from tests.factories import (a_keypair, make_community_ban, make_instance_ban, make_community, make_instance, make_post, make_post_reply, make_user,
                             signed_inbox_post)

pytestmark = pytest.mark.usefixtures('site', 'redis_double')

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
def logs(monkeypatch):
    calls = []
    monkeypatch.setattr(inbound, 'log_incoming_ap',
                        lambda id, kind, result, saved, message=None, session=None: calls.append((kind, result, message)))
    return calls


@pytest.fixture
def queued(monkeypatch):
    calls = []
    monkeypatch.setattr(inbound.process_relayed_announce, 'apply_async',
                        lambda args=None, queue=None, **kw: calls.append((args, queue)))
    return calls


def gate(app, activity, key_id=ACTOR + '#main-key'):
    with app.test_request_context('/inbox', method='POST',
                                  headers={'Signature': f'keyId="{key_id}",algorithm="rsa-sha256",signature="x"'}):
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

    @pytest.mark.parametrize('obj', [['x'], {'type': 'Follow'}])
    def test_an_accept_with_no_follow_id_is_honoured_for_the_pending_row(self, app, verifier, logs, obj):
        relay = make_relay()
        assert gate(app, activity('Accept', obj)) == ('', 200)
        db.session.refresh(relay)
        assert relay.state == RELAY_ACCEPTED
        assert logs[-1][1] == APLOG_SUCCESS

    def test_a_mismatched_accept_is_logged_as_ignored(self, app, verifier, logs):
        make_relay()
        gate(app, activity('Accept', 'https://test.piefed.local/activities/relay-follow/other'))
        assert logs[-1][1] == APLOG_IGNORED

    def test_an_accept_for_a_row_that_is_not_pending_is_logged_as_ignored(self, app, verifier, logs):
        make_relay(state=RELAY_REFUSED)
        gate(app, activity('Accept', FOLLOW_ID))
        assert logs[-1][1] == APLOG_IGNORED

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

    @pytest.mark.parametrize('state', [RELAY_ACCEPTED, RELAY_PENDING])
    @pytest.mark.parametrize('kind', ['Create', 'Delete', 'Follow', 'Undo'])
    def test_other_activity_types_from_a_relay_actor_are_left_to_the_normal_inbox(self, app, verifier, queued,
                                                                                   state, kind):
        make_relay(state=state)
        assert gate(app, activity(kind, {'id': NOTE_URI})) is None
        assert queued == [] and verifier == []

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
        monkeypatch.setattr(inbound.relay_subscribe, 'detect_relay',
                            lambda url: fetched.append(url) or {'public_key': PEM})
        assert gate(app, activity('Accept', FOLLOW_ID)) == ('', 200)
        assert fetched == [relay.url] and verifier == ['STALE', PEM]
        db.session.refresh(relay)
        assert relay.state == RELAY_ACCEPTED and relay.public_key == PEM

    def test_a_relay_with_no_stored_key_fetches_one(self, app, verifier, monkeypatch):
        relay = make_relay(public_key=None)
        monkeypatch.setattr(inbound.relay_subscribe, 'detect_relay', lambda url: {'public_key': PEM})
        assert gate(app, activity('Accept', FOLLOW_ID)) == ('', 200)
        assert verifier == [PEM]
        db.session.refresh(relay)
        assert relay.state == RELAY_ACCEPTED

    def test_a_signature_that_still_fails_is_a_401_and_logged(self, app, verifier, logs, monkeypatch):
        relay = make_relay(public_key='STALE')
        monkeypatch.setattr(inbound.relay_subscribe, 'detect_relay', lambda url: {'public_key': 'STILL-WRONG'})
        assert gate(app, activity('Accept', FOLLOW_ID)) == ('', 401)
        assert logs[-1][1:] == (APLOG_FAILURE, 'Relay signature did not verify')
        db.session.refresh(relay)
        assert relay.state == RELAY_PENDING

    def test_a_relay_whose_key_cannot_be_refetched_is_a_401(self, app, verifier, monkeypatch):
        make_relay(public_key='STALE')

        def fail(url):
            raise inbound.relay_subscribe.RelayError('gone')

        monkeypatch.setattr(inbound.relay_subscribe, 'detect_relay', fail)
        assert gate(app, activity('Accept', FOLLOW_ID)) == ('', 401)

    def test_a_fetch_that_finds_no_key_leaves_the_stored_key_alone(self, app, verifier, monkeypatch):
        relay = make_relay(public_key='STALE')
        monkeypatch.setattr(inbound.relay_subscribe, 'detect_relay', lambda url: {'public_key': None})
        assert gate(app, activity('Accept', FOLLOW_ID)) == ('', 401)
        db.session.refresh(relay)
        assert relay.public_key == 'STALE'

    def test_a_fetch_that_returns_the_same_key_is_a_401(self, app, verifier, monkeypatch):
        make_relay(public_key='STALE')
        monkeypatch.setattr(inbound.relay_subscribe, 'detect_relay', lambda url: {'public_key': 'STALE'})
        assert gate(app, activity('Accept', FOLLOW_ID)) == ('', 401)

    def test_a_signature_naming_another_key_causes_no_fetch(self, app, verifier, monkeypatch):
        make_relay(public_key='STALE')
        fetched = []
        monkeypatch.setattr(inbound.relay_subscribe, 'detect_relay', lambda url: fetched.append(url) or {'public_key': PEM})
        assert gate(app, activity('Accept', FOLLOW_ID), key_id='https://evil.example/actor#main-key') == ('', 401)
        assert fetched == []

    def test_two_bad_requests_within_five_minutes_fetch_once(self, app, verifier, monkeypatch):
        make_relay(public_key='STALE')
        fetched = []
        monkeypatch.setattr(inbound.relay_subscribe, 'detect_relay', lambda url: fetched.append(url) or {'public_key': 'STALE'})
        assert gate(app, activity('Accept', FOLLOW_ID)) == ('', 401)
        assert gate(app, activity('Accept', FOLLOW_ID)) == ('', 401)
        assert len(fetched) == 1


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
        monkeypatch.setattr(inbound.relay_subscribe, 'detect_relay', lambda url: {'public_key': other_public})
        sender = type('Sender', (), {'private_key': private_key, 'ap_profile_id': ACTOR})()
        with app.test_client() as client:
            response = signed_inbox_post(client, activity('Accept', FOLLOW_ID), sender)
        assert response.status_code == 401
        db.session.refresh(relay)
        assert relay.state == RELAY_PENDING

    def test_a_relay_request_whose_digest_does_not_match_its_body_fails_the_precheck(self, app, monkeypatch):
        monkeypatch.setitem(app.config, 'DEBUG', True)
        private_key, public_key = a_keypair()
        relay = make_relay(public_key=public_key)
        sender = type('Sender', (), {'private_key': private_key, 'ap_profile_id': ACTOR})()
        tampered = b'{"id": "x", "type": "Accept", "actor": "%s", "object": "%s"}' % (ACTOR.encode(), FOLLOW_ID.encode())
        with app.test_client() as client:
            response = signed_inbox_post(client, activity('Accept', FOLLOW_ID), sender, body=tampered)
        assert response.status_code == 400
        db.session.refresh(relay)
        assert relay.state == RELAY_PENDING

    def test_an_unallowed_relay_host_is_refused_before_the_gate(self, app, monkeypatch):
        make_relay()
        db.session.get(Site, 1).allowlist_mode = ALLOWLIST_STRONG
        db.session.commit()
        called = []
        monkeypatch.setattr('app.relays.inbound.relay_actor_gate', lambda *a: called.append(a))
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
def lockless_redis(redis_double, monkeypatch):
    """fakeredis without lupa cannot release a lock (EVALSHA); PostReply.new takes one."""
    monkeypatch.setattr(app_pkg.redis_client, 'lock', lambda *args, **kwargs: contextlib.nullcontext(),
                        raising=False)


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

    def test_a_reply_to_a_stored_post_is_kept_under_it(self, app, fetch, logged, alice, lockless_redis):
        relay = make_relay(state=RELAY_ACCEPTED)
        parent = make_post(make_community(), alice, 'https://other.example/notes/parent')
        fetch.result = note(inReplyTo=parent.ap_id)
        inbound.process_relayed_announce(relay.id, NOTE_URI)
        reply = PostReply.query.filter_by(ap_id=NOTE_URI).one()
        assert reply.post_id == parent.id
        assert Post.query.filter_by(ap_id=NOTE_URI).count() == 0

    def test_a_reply_to_a_stored_reply_is_kept_in_that_thread(self, app, fetch, logged, alice, lockless_redis):
        relay = make_relay(state=RELAY_ACCEPTED)
        parent = make_post(make_community(), alice, 'https://other.example/notes/parent')
        parent_reply = make_post_reply(parent, alice)
        parent_reply.ap_id = 'https://other.example/notes/pr'
        db.session.commit()
        fetch.result = note(inReplyTo=parent_reply.ap_id)
        inbound.process_relayed_announce(relay.id, NOTE_URI)
        reply = PostReply.query.filter_by(ap_id=NOTE_URI).one()
        assert reply.post_id == parent.id and reply.parent_id == parent_reply.id

    def test_a_reply_to_a_parent_given_as_a_dict_is_kept(self, app, fetch, logged, alice, lockless_redis):
        relay = make_relay(state=RELAY_ACCEPTED)
        parent = make_post(make_community(), alice, 'https://other.example/notes/parent')
        fetch.result = note(inReplyTo={'id': parent.ap_id})
        inbound.process_relayed_announce(relay.id, NOTE_URI)
        assert PostReply.query.filter_by(ap_id=NOTE_URI).count() == 1

    def test_an_object_for_an_unknown_community_is_dropped(self, app, fetch, logged, alice):
        relay = make_relay(state=RELAY_ACCEPTED)
        fetch.result = note(audience='https://other.example/c/nowhere')
        inbound.process_relayed_announce(relay.id, NOTE_URI)
        assert Post.query.count() == 0

    def test_the_post_is_created_with_no_announce_id(self, app, fetch, logged, alice):
        relay = make_relay(state=RELAY_ACCEPTED)
        fetch.result = note()
        inbound.process_relayed_announce(relay.id, NOTE_URI)
        assert Post.query.filter_by(ap_id=NOTE_URI).one().ap_announce_id is None

    def test_an_object_whose_canonical_id_is_already_stored_is_left_alone(self, app, fetch, logged, alice):
        relay = make_relay(state=RELAY_ACCEPTED)
        existing = make_post(make_community(), alice, 'https://other.example/notes/canonical')
        fetch.result = note(uri='https://other.example/notes/canonical')
        inbound.process_relayed_announce(relay.id, 'https://other.example/@alice/1')
        db.session.refresh(existing)
        assert Post.query.count() == 1 and existing.relay_id is None

    def test_a_refused_creation_is_logged(self, app, fetch, logged, alice, monkeypatch):
        relay = make_relay(state=RELAY_ACCEPTED)
        fetch.result = note()
        monkeypatch.setattr(inbound, 'create_resolved_object', lambda *args: None)
        inbound.process_relayed_announce(relay.id, NOTE_URI)
        assert logged[-1] == (inbound.APLOG_ANNOUNCE, inbound.APLOG_IGNORED, 'relayed object was refused')

    def test_an_existing_post_is_left_alone(self, app, fetch, logged, alice):
        relay = make_relay(state=RELAY_ACCEPTED)
        existing = make_post(make_community(), alice, NOTE_URI)
        inbound.process_relayed_announce(relay.id, NOTE_URI)
        db.session.refresh(existing)
        assert fetch.calls == []
        assert Post.query.count() == 1 and existing.relay_id is None

    def test_an_existing_reply_is_not_updated(self, app, fetch, logged, alice, lockless_redis):
        relay = make_relay(state=RELAY_ACCEPTED)
        parent = make_post(make_community(), alice, 'https://other.example/notes/parent')
        reply = make_post_reply(parent, alice, body='original')
        reply.ap_id = NOTE_URI
        db.session.commit()
        fetch.result = note(inReplyTo=parent.ap_id, updated='2026-09-01T00:00:00Z', content='<p>changed</p>')
        inbound.process_relayed_announce(relay.id, NOTE_URI)
        db.session.refresh(reply)
        assert fetch.calls == [] and reply.body == 'original'

    def test_an_error_rolls_back_and_is_raised(self, app, fetch, logged, alice, monkeypatch):
        relay = make_relay(state=RELAY_ACCEPTED)
        fetch.result = note()

        def boom(*args):
            raise RuntimeError('boom')

        monkeypatch.setattr(inbound, 'create_resolved_object', boom)
        with pytest.raises(RuntimeError):
            inbound.process_relayed_announce(relay.id, NOTE_URI)


def _delete_post(post, author, community):
    post.deleted = True


def _lock_post(post, author, community):
    post.comments_enabled = False


def _archive_post(post, author, community):
    post.archived = '/archive/x'


def _private_community(post, author, community):
    community.private = True


def _local_only_community(post, author, community):
    community.local_only = True


def _banned_community(post, author, community):
    community.banned = True


def _banned_author(post, author, community):
    author.banned = True


def _comment_banned_author(post, author, community):
    author.ban_comments = True


def _banned_author_instance(post, author, community):
    db.session.add(BannedInstances(domain=author.instance.domain))


def _author_banned_from_the_communitys_instance(post, author, community):
    make_instance_ban(author, community.instance)


def _banned_from_community(post, author, community):
    make_community_ban(author, community)


class TestRelayedRepliesAreRefusedWhereTheNormalPathRefuses:

    @pytest.mark.parametrize('breakage', [
        _delete_post, _lock_post, _archive_post, _private_community, _local_only_community, _banned_community,
        _banned_author, _comment_banned_author, _banned_author_instance, _banned_from_community,
        _author_banned_from_the_communitys_instance])
    def test_no_reply_is_created(self, app, fetch, logged, alice, lockless_redis, breakage):
        relay = make_relay(state=RELAY_ACCEPTED)
        community = make_community()
        parent = make_post(community, alice, 'https://other.example/notes/parent')
        author = make_user(alice.instance, 'carol')   # not alice: she owns the community, and owners are never refused
        breakage(parent, author, community)
        db.session.commit()
        fetch.result = note(inReplyTo=parent.ap_id, attributedTo=author.ap_profile_id)
        inbound.process_relayed_announce(relay.id, NOTE_URI)
        assert PostReply.query.count() == 0

    def test_a_reply_to_a_deleted_reply_is_not_created(self, app, fetch, logged, alice, lockless_redis):
        relay = make_relay(state=RELAY_ACCEPTED)
        parent = make_post(make_community(), alice, 'https://other.example/notes/parent')
        parent_reply = make_post_reply(parent, alice)
        parent_reply.ap_id = 'https://other.example/notes/pr'
        parent_reply.deleted = True
        db.session.commit()
        fetch.result = note(inReplyTo=parent_reply.ap_id)
        inbound.process_relayed_announce(relay.id, NOTE_URI)
        assert PostReply.query.filter_by(ap_id=NOTE_URI).count() == 0

    def test_a_reply_to_a_locked_reply_is_not_created(self, app, fetch, logged, alice, lockless_redis):
        relay = make_relay(state=RELAY_ACCEPTED)
        parent = make_post(make_community(), alice, 'https://other.example/notes/parent')
        parent_reply = make_post_reply(parent, alice)
        parent_reply.ap_id = 'https://other.example/notes/pr'
        parent_reply.replies_enabled = False
        db.session.commit()
        fetch.result = note(inReplyTo=parent_reply.ap_id)
        inbound.process_relayed_announce(relay.id, NOTE_URI)
        assert PostReply.query.filter_by(ap_id=NOTE_URI).count() == 0


class TestReplyTargetOpen:

    def test_an_ordinary_post_and_reply_are_open(self, app, alice):
        post = make_post(make_community(), alice, 'https://other.example/notes/parent')
        reply = make_post_reply(post, alice)
        assert inbound._reply_target_open(post) and inbound._reply_target_open(reply)

    @pytest.mark.parametrize('breakage', [_delete_post, _private_community, _local_only_community,
                                          _banned_community])
    def test_a_closed_post_is_not_open(self, app, alice, breakage):
        community = make_community()
        post = make_post(community, alice, 'https://other.example/notes/parent')
        breakage(post, alice, community)
        db.session.commit()
        assert not inbound._reply_target_open(post)

    def test_a_reply_under_a_closed_post_is_not_open(self, app, alice):
        community = make_community()
        post = make_post(community, alice, 'https://other.example/notes/parent')
        reply = make_post_reply(post, alice)
        post.deleted = True
        db.session.commit()
        assert not inbound._reply_target_open(reply)

    def test_a_deleted_reply_is_not_open(self, app, alice):
        post = make_post(make_community(), alice, 'https://other.example/notes/parent')
        reply = make_post_reply(post, alice)
        reply.deleted = True
        db.session.commit()
        assert not inbound._reply_target_open(reply)


class TestRelayedObjectAllowed:

    def test_a_top_level_object_with_no_audience_is_allowed(self, app):
        assert inbound.relayed_object_allowed({'id': NOTE_URI}) == (True, '')

    def test_an_audience_that_is_a_known_community_is_allowed(self, app, alice):
        community = make_community('cats')
        assert inbound.relayed_object_allowed({'audience': community.ap_profile_id.upper()})[0]

    def test_an_unknown_audience_is_refused(self, app):
        allowed, reason = inbound.relayed_object_allowed({'audience': 'https://other.example/c/none'})
        assert not allowed and 'community' in reason

    def test_an_audience_that_is_a_deleted_community_is_refused(self, app, alice):
        community = make_community('gone')
        community.ap_deleted_at = utcnow()
        db.session.commit()
        allowed, reason = inbound.relayed_object_allowed({'audience': community.ap_profile_id})
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

    def test_a_delete_is_allowed(self, app):
        assert inbound.relayed_activity_allowed({'type': 'Delete'}) == (True, '')

    def test_an_update_of_a_stored_object_is_allowed(self, app, alice):
        post = make_post(make_community(), alice, 'https://other.example/notes/p')
        assert inbound.relayed_activity_allowed({'type': 'Update', 'object': post.ap_id}) == (True, '')
        assert inbound.relayed_activity_allowed({'type': 'Update', 'object': {'id': post.ap_id}}) == (True, '')

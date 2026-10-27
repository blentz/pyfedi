"""The Accept or Reject of a Follow goes to the follower's own `inbox`, not its shared inbox (spec 2026-10-14 §6.2,
§10 A12). PeerTube ignores one at its shared inbox ("Need to accept on explicit inbox."), so a subscription stays
pending. Mastodon and Lemmy take either. Everything else is still delivered to the shared inbox (`ap_inbox_url`).

The own inbox is stored when the actor is created and on the next refresh of an actor created before the column
existed; until then the answer goes where it went before, to `ap_inbox_url`."""
import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect

from app import db
from app.activitypub.util import INBOX_URL_MAX, actor_json_to_model, own_inbox_of
from app.models import User
from tests.factories import inbox_activity, make_follow, make_instance, make_user, peer_actor_json, peer_instance
from tests.test_ap_interop_actor_update import _actor_update, _known_remote_user
from tests.test_ap_refresh_profiles import PEER, _person_document
from tests.test_inbox_dispatch_follow import _seed_follow_of_feed, _seed_follow_of_local_community, \
    _seed_follow_of_local_user, record_sends
from tests.test_inbox_dispatch_preamble import dispatch
from tests.test_user_files_notifications import a_follow_request, csrf, env  # noqa: F401 (env is a fixture)

OWN = 'https://peer.example/users/alice/own-inbox'
MIGRATION = Path(__file__).resolve().parent.parent / 'migrations' / 'versions' / 'b7e3c9a1f5d2_user_own_inbox.py'


def _with_own_inbox(user, own_inbox=OWN):
    user.ap_own_inbox_url = own_inbox
    db.session.commit()
    return user


def _stored_before_a12(user):
    """An actor stored before ap_own_inbox_url existed."""
    return _with_own_inbox(user, None)


# --- which inbox answers a follow ---

def test_a_follower_whose_own_inbox_is_unknown_is_answered_at_its_delivery_inbox(app, db_session):
    user = _stored_before_a12(make_user(make_instance('peer.example'), 'alice'))
    assert user.follow_answer_inbox() == user.ap_inbox_url


def test_a_follower_whose_own_inbox_is_known_is_answered_there(app, db_session):
    user = _with_own_inbox(make_user(make_instance('peer.example'), 'alice'))
    assert user.follow_answer_inbox() == OWN
    assert user.ap_inbox_url != OWN


# --- community follows ---

def test_the_accept_of_a_community_follow_goes_to_the_followers_own_inbox(app, db_session, monkeypatch):
    sends = record_sends(monkeypatch)
    follower, community = _seed_follow_of_local_community()
    _with_own_inbox(follower)

    dispatch(inbox_activity(follower, activity_type='Follow', object_uri=community.ap_profile_id))

    assert [(uri, body['type']) for uri, body, _ in sends] == [(OWN, 'Accept')]


def test_the_reject_of_a_community_follow_goes_to_the_followers_own_inbox(app, db_session, monkeypatch):
    sends = record_sends(monkeypatch)
    follower, community = _seed_follow_of_local_community(local_only=True)
    _with_own_inbox(follower)

    dispatch(inbox_activity(follower, activity_type='Follow', object_uri=community.ap_profile_id))

    assert [(uri, body['type']) for uri, body, _ in sends] == [(OWN, 'Reject')]


# --- feed follows ---

def test_the_accept_of_a_feed_follow_goes_to_the_followers_own_inbox(app, db_session, monkeypatch):
    sends = record_sends(monkeypatch)
    follower, feed = _seed_follow_of_feed(public=True, local=True, with_keys=True)
    _with_own_inbox(follower)

    dispatch(inbox_activity(follower, activity_type='Follow', object_uri=feed.ap_profile_id))

    assert [(uri, body['type']) for uri, body, _ in sends] == [(OWN, 'Accept')]


def test_a_follow_of_a_private_feed_gets_no_answer_in_any_inbox(app, db_session, monkeypatch):
    """It was a Reject to the follower's own inbox; since per-domain feeds Task 7 (review M1) a private feed answers
    a Follow as a feed nobody holds does: not at all."""
    sends = record_sends(monkeypatch)
    follower, feed = _seed_follow_of_feed(public=False, local=True, with_keys=True)
    _with_own_inbox(follower)

    dispatch(inbox_activity(follower, activity_type='Follow', object_uri=feed.ap_profile_id))

    assert sends == []


# --- follows of a local user ---

def test_the_accept_of_a_user_follow_goes_to_the_followers_own_inbox(app, db_session, monkeypatch):
    sends = record_sends(monkeypatch)
    remote_user, local_user = _seed_follow_of_local_user(manually_approves=False)
    _with_own_inbox(remote_user)

    dispatch(inbox_activity(remote_user, activity_type='Follow', object_uri=local_user.public_url()))

    assert [(uri, body['type']) for uri, body, _ in sends] == [(OWN, 'Accept')]


def test_the_re_sent_accept_of_a_user_follow_goes_to_the_followers_own_inbox(app, db_session, monkeypatch):
    sends = record_sends(monkeypatch)
    remote_user, local_user = _seed_follow_of_local_user()
    _with_own_inbox(remote_user)
    make_follow(local_user, remote_user, is_accepted=True, is_inward=True)

    dispatch(inbox_activity(remote_user, activity_type='Follow', object_uri=local_user.public_url()))

    assert [(uri, body['type']) for uri, body, _ in sends] == [(OWN, 'Accept')]


@pytest.mark.parametrize('answer,activity_type', [('accept', 'Accept'), ('reject', 'Reject')])
def test_a_follow_request_answered_by_hand_goes_to_the_followers_own_inbox(app, site, env, answer, activity_type):
    client, owner, _ = env
    remote = _with_own_inbox(make_user(make_instance('peer.example', software='piefed'), 'alice'))
    a_follow_request(owner, remote)
    token = csrf(app, client)

    with patch('app.user.routes.send_post_request') as send:
        client.post(f'/user/follow_request/{remote.id}/{answer}', data={'csrf_token': token})

    assert send.call_args.args[0] == OWN
    assert send.call_args.args[1]['type'] == activity_type


# --- where the own inbox comes from ---

@pytest.mark.parametrize('document,expected', [
    ({'inbox': OWN}, OWN),
    ({}, None),
    ({'inbox': ''}, None),
    ({'inbox': ['https://peer.example/inbox']}, None),
    ({'inbox': 'https://peer.example/' + 'x' * INBOX_URL_MAX}, None),
], ids=['inbox', 'absent', 'empty', 'not-a-string', 'too-long'])
def test_own_inbox_of(document, expected):
    assert own_inbox_of(document) == expected


def test_a_person_is_created_with_its_own_inbox_beside_the_shared_one(app, db_session):
    peer_instance(PEER)
    document = peer_actor_json(name='alice', server=PEER, fields={
        'inbox': f'https://{PEER}/u/alice/inbox', 'endpoints': {'sharedInbox': f'https://{PEER}/inbox'}})

    user = actor_json_to_model(document, 'alice', PEER)

    assert user.ap_inbox_url == f'https://{PEER}/inbox'
    assert user.ap_own_inbox_url == f'https://{PEER}/u/alice/inbox'


def test_a_refresh_stores_the_own_inbox_of_an_actor_created_without_one(app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    user = _stored_before_a12(_known_remote_user())
    user_id = user.id

    dispatch(_actor_update(user, _person_document(fields={
        'inbox': f'https://{PEER}/u/wakko/inbox', 'endpoints': {'sharedInbox': f'https://{PEER}/inbox'}})))

    db.session.expire_all()
    user = db.session.get(User, user_id)
    assert user.ap_inbox_url == f'https://{PEER}/inbox'
    assert user.ap_own_inbox_url == f'https://{PEER}/u/wakko/inbox'


def test_a_refresh_without_a_usable_own_inbox_keeps_the_stored_one(app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    user = _with_own_inbox(_known_remote_user())
    user_id = user.id

    dispatch(_actor_update(user, _person_document(fields={'endpoints': {'sharedInbox': f'https://{PEER}/inbox'}})))

    db.session.expire_all()
    user = db.session.get(User, user_id)
    assert user.ap_inbox_url == f'https://{PEER}/inbox'
    assert user.ap_own_inbox_url == OWN


def test_a_refresh_naming_an_off_host_inbox_keeps_the_stored_own_inbox(app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    user = _with_own_inbox(_known_remote_user())
    user_id = user.id

    dispatch(_actor_update(user, _person_document(fields={'inbox': 'https://elsewhere.example/inbox'})))

    db.session.expire_all()
    assert db.session.get(User, user_id).ap_own_inbox_url == OWN


# --- the migration ---

def _load_migration():
    spec = importlib.util.spec_from_file_location('user_own_inbox_migration', MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _columns(connection):
    return {column['name'] for column in inspect(connection).get_columns('user')}


def test_the_migration_adds_the_column_and_reverses(app, db_session):
    migration = _load_migration()
    db.session.close()
    with db.engine.connect() as connection:
        assert 'ap_own_inbox_url' in _columns(connection)
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
        assert 'ap_own_inbox_url' not in _columns(connection)
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        assert 'ap_own_inbox_url' in _columns(connection)
        connection.rollback()


# --- a follower stored before ap_own_inbox_url: fetched once, synchronously, before the answer (A12) ---

def _fetch_returns(monkeypatch, document):
    calls = []

    def fake(uri, retry_delay=3):
        calls.append(uri)
        return document
    monkeypatch.setattr('app.activitypub.util.remote_object_to_json', fake)
    return calls


def _follower_document(follower, **fields):
    return {'id': follower.ap_profile_id, 'type': 'Person', 'inbox': OWN,
            'endpoints': {'sharedInbox': 'https://peer.example/inbox'}, **fields}


def test_an_unknown_own_inbox_is_fetched_before_the_answer(app, db_session, monkeypatch):
    sends = record_sends(monkeypatch)
    follower, community = _seed_follow_of_local_community()
    follower_id = _stored_before_a12(follower).id
    calls = _fetch_returns(monkeypatch, _follower_document(follower))

    dispatch(inbox_activity(follower, activity_type='Follow', object_uri=community.ap_profile_id))

    assert calls == [follower.ap_public_url]
    assert [(uri, body['type']) for uri, body, _ in sends] == [(OWN, 'Accept')]
    db.session.expire_all()
    assert db.session.get(User, follower_id).ap_own_inbox_url == OWN


@pytest.mark.parametrize('document', [
    None,
    'not a document',
    {'id': 'https://peer.example/users/someone-else', 'inbox': OWN},
    {'inbox': 'https://elsewhere.example/inbox'},
    {'endpoints': {'sharedInbox': 'https://elsewhere.example/inbox'}},
    {'inbox': None, 'endpoints': {'sharedInbox': 'https://peer.example/inbox'}},
], ids=['fetch-failed', 'not-a-dict', 'another-actor', 'off-host-inbox', 'off-host-shared-inbox', 'no-own-inbox'])
def test_a_failed_fetch_answers_at_the_shared_inbox_as_before(app, db_session, monkeypatch, document):
    sends = record_sends(monkeypatch)
    follower, community = _seed_follow_of_local_community()
    follower_id = _stored_before_a12(follower).id
    if isinstance(document, dict):
        document = {**_follower_document(follower), **document}
    _fetch_returns(monkeypatch, document)

    dispatch(inbox_activity(follower, activity_type='Follow', object_uri=community.ap_profile_id))

    assert [(uri, body['type']) for uri, body, _ in sends] == [(follower.ap_inbox_url, 'Accept')]
    db.session.expire_all()
    assert db.session.get(User, follower_id).ap_own_inbox_url is None


def test_a_known_own_inbox_is_not_fetched(app, db_session, monkeypatch):
    sends = record_sends(monkeypatch)
    follower, community = _seed_follow_of_local_community()
    _with_own_inbox(follower)
    calls = _fetch_returns(monkeypatch, None)

    dispatch(inbox_activity(follower, activity_type='Follow', object_uri=community.ap_profile_id))

    assert calls == []
    assert [uri for uri, _, _ in sends] == [OWN]


def test_a_follow_request_answered_by_hand_fetches_an_unknown_own_inbox(app, site, env, monkeypatch):
    client, owner, _ = env
    remote = _stored_before_a12(make_user(make_instance('peer.example', software='piefed'), 'alice'))
    a_follow_request(owner, remote)
    _fetch_returns(monkeypatch, _follower_document(remote))
    token = csrf(app, client)

    with patch('app.user.routes.send_post_request') as send:
        client.post(f'/user/follow_request/{remote.id}/accept', data={'csrf_token': token})

    assert send.call_args.args[0] == OWN

"""A remote actor's inbox and sharedInbox are trusted only on the actor's own host (owner ruling, 2026-10-11,
interop C1 items 2 and 4).

The actor's own host is the host of its `id` (`ap_profile_id` once stored), compared with host_of as
shared_inbox_of already compared the string-endpoints document: urlparse's hostname, so userinfo and the port
are not part of it and case does not matter. A trailing dot is a different host to urlparse, and it is refused
rather than normalised: the strict reading only ever refuses a peer, never trusts one.

Before this, a peer's actor document could name any URL as its inbox or shared inbox, and everything this
instance sent that actor -- a follow, a reply, a private message -- was then posted to a host the actor does not
speak for. On a refresh or an Update an off-host value keeps the stored inbox and is logged. On creation an
off-host sharedInbox falls back to the actor's own inbox, and an off-host inbox refuses the actor.
"""
import logging

import pytest

from app import db
from app.activitypub.util import actor_json_to_model, delivery_inbox_of, refresh_community_profile_task, \
    refresh_feed_profile_task, shared_inbox_of
from app.models import Community, Feed, User
from tests.factories import peer_actor_json, peer_instance
from tests.test_ap_actor_json_feed import _owned_feed, _peer_with_one_owner
from tests.test_ap_interop_actor_update import _actor_update, _known_remote_user
from tests.test_ap_refresh_profiles import PEER, _feed_document, _group_document, _person_document, \
    _remote_community, _remote_feed
from tests.test_inbox_dispatch_preamble import dispatch

ELSEWHERE = 'https://elsewhere.example/inbox'
OLD_INBOX = f'https://{PEER}/u/wakko/inbox'
ACTOR = f'https://{PEER}/u/alice'

# URLs whose host is not the actor's, though a reader might take it for it.
OFF_HOST = [
    ELSEWHERE,
    f'https://{PEER}@elsewhere.example/inbox',        # userinfo naming the actor's host
    f'https://{PEER}.elsewhere.example/inbox',        # the actor's host as a subdomain
    f'https://elsewhere.example/{PEER}/inbox',        # the actor's host in the path
    f'https://{PEER}./inbox',                         # a trailing dot: another host to urlparse
    f'https://[@{PEER}/inbox',                        # urlparse refuses it while httpx reaches PEER (D32)
]
OFF_HOST_IDS = ['other-host', 'userinfo', 'subdomain', 'path', 'trailing-dot', 'unparseable']

# URLs on the actor's host, written differently.
ON_HOST = [
    f'https://{PEER}/inbox',
    f'https://PEER.Example/inbox',                    # case
    f'https://{PEER}:8443/inbox',                     # a port
    f'https://elsewhere.example@{PEER}/inbox',        # userinfo naming another host
]
ON_HOST_IDS = ['plain', 'case', 'port', 'userinfo']


# --- shared_inbox_of and delivery_inbox_of ---

@pytest.mark.parametrize('url', OFF_HOST, ids=OFF_HOST_IDS)
def test_an_off_host_shared_inbox_is_not_the_actors(url):
    assert shared_inbox_of({'id': ACTOR, 'endpoints': {'sharedInbox': url}}) is None


@pytest.mark.parametrize('url', ON_HOST, ids=ON_HOST_IDS)
def test_an_on_host_shared_inbox_is_the_actors(url):
    assert shared_inbox_of({'id': ACTOR, 'endpoints': {'sharedInbox': url}}) == url


def test_a_shared_inbox_of_an_actor_without_a_host_is_not_trusted():
    assert shared_inbox_of({'endpoints': {'sharedInbox': f'https://{PEER}/inbox'}}) is None
    assert shared_inbox_of({'id': ['x'], 'endpoints': {'sharedInbox': f'https://{PEER}/inbox'}}) is None


@pytest.mark.parametrize('fields', [
    {'inbox': ELSEWHERE},
    {'inbox': OLD_INBOX, 'endpoints': {'sharedInbox': ELSEWHERE}},
    {'endpoints': {'sharedInbox': ELSEWHERE}},
], ids=['inbox', 'shared-inbox', 'shared-inbox-alone'])
def test_delivery_inbox_of_keeps_the_stored_inbox_for_any_off_host_value(app, caplog, fields):
    with caplog.at_level(logging.WARNING):
        assert delivery_inbox_of(fields, f'https://{PEER}/u/wakko') is None
    assert any('elsewhere.example' in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize('url', ON_HOST, ids=ON_HOST_IDS)
def test_delivery_inbox_of_takes_an_on_host_inbox(app, url):
    assert delivery_inbox_of({'inbox': url}, f'https://{PEER}/u/wakko') == url


def test_delivery_inbox_of_measures_the_stored_actor_not_the_document(app):
    """The document's own `id` is the peer's word; the stored actor is not."""
    document = {'id': 'https://elsewhere.example/u/wakko', 'inbox': ELSEWHERE}
    assert delivery_inbox_of(document, f'https://{PEER}/u/wakko') is None


# --- a pushed Update{Person} ---

@pytest.mark.parametrize('fields', [
    {'inbox': ELSEWHERE, 'endpoints': {'sharedInbox': ELSEWHERE}},
    {'inbox': ELSEWHERE},
    {'inbox': f'https://{PEER}/u/wakko/new-inbox', 'endpoints': {'sharedInbox': ELSEWHERE}},
] + [{'inbox': url} for url in OFF_HOST[1:]],
    ids=['both', 'inbox', 'shared-inbox'] + OFF_HOST_IDS[1:])
def test_an_actor_update_with_an_off_host_inbox_keeps_the_old_one(app, db_session, monkeypatch, caplog, fields):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    user = _known_remote_user()
    user_id = user.id

    with caplog.at_level(logging.WARNING):
        dispatch(_actor_update(user, _person_document(fields={**fields, 'name': 'Renamed'})))

    db.session.expire_all()
    user = db.session.get(User, user_id)
    assert user.ap_inbox_url == OLD_INBOX
    assert user.title == 'Renamed'   # the rest of the Update is still applied
    assert any('kept the stored inbox' in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize('url', ON_HOST, ids=ON_HOST_IDS)
def test_an_actor_update_with_an_on_host_inbox_applies_it(app, db_session, monkeypatch, url):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    user = _known_remote_user()
    user_id = user.id

    dispatch(_actor_update(user, _person_document(fields={'inbox': OLD_INBOX, 'endpoints': {'sharedInbox': url}})))

    db.session.expire_all()
    assert db.session.get(User, user_id).ap_inbox_url == url


# --- a fetched community refresh, and the feed refresh ---

@pytest.mark.parametrize('fields,expected', [
    ({'inbox': ELSEWHERE, 'endpoints': {'sharedInbox': ELSEWHERE}}, 'https://peer.example/c/memes/old-inbox'),
    ({'inbox': 'https://peer.example/c/memes/inbox', 'endpoints': {'sharedInbox': ELSEWHERE}},
     'https://peer.example/c/memes/old-inbox'),
    ({'inbox': ELSEWHERE, 'endpoints': {'sharedInbox': 'https://peer.example/inbox'}},
     'https://peer.example/c/memes/old-inbox'),
    ({'inbox': 'https://peer.example/c/memes/inbox', 'endpoints': {'sharedInbox': 'https://peer.example/inbox'}},
     'https://peer.example/inbox'),
], ids=['off-host', 'off-host-shared-inbox', 'off-host-inbox-on-host-shared-inbox', 'on-host'])
def test_a_fetched_community_refresh_trusts_only_its_own_host(app, db_session, http_mock, fields, expected):
    community = _remote_community()
    community.ap_inbox_url = 'https://peer.example/c/memes/old-inbox'
    db.session.commit()
    community_id = community.id
    http_mock.get(community.ap_public_url).respond(200, json=_group_document(fields=fields))

    refresh_community_profile_task(community_id)

    db.session.expire_all()
    community = db.session.get(Community, community_id)
    assert community.ap_inbox_url == expected
    assert community.title == 'Memes, refreshed'


def test_a_community_refresh_keeps_its_inbox_when_the_fetched_endpoints_document_names_an_off_host_shared_inbox(
        app, db_session, http_mock):
    """The endpoints document is on the actor's host, so it is fetched; the sharedInbox it names is not, so the
    refresh keeps the stored inbox rather than falling back to the document's own on-host `inbox`."""
    community = _remote_community()
    community.ap_inbox_url = 'https://peer.example/c/memes/old-inbox'
    db.session.commit()
    community_id = community.id
    endpoints_url = 'https://peer.example/c/memes/endpoints'
    http_mock.get(community.ap_public_url).respond(200, json=_group_document(
        fields={'inbox': 'https://peer.example/c/memes/inbox', 'endpoints': endpoints_url}))
    http_mock.get(endpoints_url).respond(200, json={'sharedInbox': ELSEWHERE})

    refresh_community_profile_task(community_id)

    db.session.expire_all()
    community = db.session.get(Community, community_id)
    assert community.ap_inbox_url == 'https://peer.example/c/memes/old-inbox'
    assert community.title == 'Memes, refreshed'


def test_a_feed_refresh_keeps_its_inbox_against_an_off_host_one(app, db_session, http_mock):
    feed = _remote_feed()
    feed.ap_inbox_url = 'https://peer.example/f/news/old-inbox'
    db.session.commit()
    feed_id = feed.id

    refresh_feed_profile_task(feed_id, _feed_document(fields={'inbox': ELSEWHERE}))

    db.session.expire_all()
    assert db.session.get(Feed, feed_id).ap_inbox_url == 'https://peer.example/f/news/old-inbox'


# --- creation ---

def test_a_person_created_with_an_off_host_shared_inbox_takes_its_own_inbox(app, db_session):
    peer_instance(PEER)
    own_inbox = f'{ACTOR}/inbox'
    document = peer_actor_json(name='alice', server=PEER,
                               fields={'inbox': own_inbox, 'endpoints': {'sharedInbox': ELSEWHERE}})

    user = actor_json_to_model(document, 'alice', PEER)

    assert user is not None
    assert user.ap_inbox_url == own_inbox


def test_a_group_created_with_an_off_host_shared_inbox_takes_its_own_inbox(app, db_session):
    peer_instance(PEER)
    document = peer_actor_json('Group', name='memes', server=PEER,
                               fields={'endpoints': {'sharedInbox': ELSEWHERE}})

    community = actor_json_to_model(document, '!memes', PEER)

    assert community is not None
    assert community.ap_inbox_url == f'https://{PEER}/c/memes/inbox'


def test_a_feed_created_with_an_off_host_shared_inbox_takes_its_own_inbox(app, db_session, http_mock):
    _peer_with_one_owner(http_mock)

    feed = actor_json_to_model(_owned_feed(fields={'endpoints': {'sharedInbox': ELSEWHERE}}), '~news', PEER)

    assert feed is not None
    assert feed.ap_inbox_url == f'https://{PEER}/f/news/inbox'


@pytest.mark.parametrize('url', OFF_HOST, ids=OFF_HOST_IDS)
def test_a_person_with_an_off_host_inbox_is_not_created(app, db_session, caplog, url):
    peer_instance(PEER)
    document = peer_actor_json(name='alice', server=PEER, fields={'inbox': url})

    with caplog.at_level(logging.WARNING):
        assert actor_json_to_model(document, 'alice', PEER) is None

    assert db.session.query(User).filter_by(user_name='alice').count() == 0
    assert any('inbox on another host' in record.getMessage() for record in caplog.records)


def test_a_group_with_an_off_host_inbox_is_not_created(app, db_session):
    peer_instance(PEER)
    document = peer_actor_json('Group', name='memes', server=PEER,
                               fields={'inbox': ELSEWHERE, 'endpoints': {'sharedInbox': f'https://{PEER}/inbox'}})

    assert actor_json_to_model(document, '!memes', PEER) is None
    assert db.session.query(Community).count() == 0


def test_a_feed_with_an_off_host_inbox_is_not_created(app, db_session, http_mock):
    _peer_with_one_owner(http_mock)

    assert actor_json_to_model(_owned_feed(fields={'inbox': ELSEWHERE}), '~news', PEER) is None
    assert db.session.query(Feed).count() == 0


@pytest.mark.parametrize('url', ON_HOST, ids=ON_HOST_IDS)
def test_a_person_with_an_on_host_inbox_is_created(app, db_session, url):
    peer_instance(PEER)
    document = peer_actor_json(name='alice', server=PEER, fields={'inbox': url})

    user = actor_json_to_model(document, 'alice', PEER)

    assert user is not None
    assert user.ap_inbox_url == url

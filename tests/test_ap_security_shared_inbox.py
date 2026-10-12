"""Security track S1.2: an actor whose `endpoints` lacks a usable `sharedInbox`
is still created, with its own inbox. `activity_json['endpoints']['sharedInbox']`
was read whenever `endpoints` was present, so a dict without the key raised
KeyError and a string `endpoints` raised TypeError -- the actor was never
created, at all three creation sites.
"""
import pytest

from app import db
from app.activitypub.util import actor_json_to_model, shared_inbox_of
from app.models import Community, Feed, User
from tests.factories import peer_actor_json, peer_instance
from tests.test_ap_actor_json_feed import _owned_feed, _peer_with_one_owner

PEER = 'peer.example'
SHARED = f'https://{PEER}/inbox'

# (label, endpoints value, expected shared inbox)
SHAPES = [
    ('dict with sharedInbox', {'sharedInbox': SHARED}, SHARED),
    ('dict without sharedInbox', {'oauthTokenEndpoint': f'https://{PEER}/token'}, None),
    # Interop C1 item 4: a string on the actor's own host is now fetched (tests/test_ap_interop_string_endpoints.py).
    # One on another host is still never fetched, so this row keeps its expectation without an HTTP route.
    ('cross-host string endpoints', 'https://elsewhere.example/endpoints', None),
    ('non-string sharedInbox', {'sharedInbox': ['x']}, None),
    ('empty sharedInbox', {'sharedInbox': ''}, None),
]


@pytest.mark.parametrize('label,endpoints,expected', SHAPES, ids=[s[0] for s in SHAPES])
def test_shared_inbox_of(label, endpoints, expected):
    assert shared_inbox_of({'id': f'https://{PEER}/u/a', 'endpoints': endpoints}) == expected


def test_shared_inbox_of_without_endpoints_is_none():
    assert shared_inbox_of({'inbox': f'https://{PEER}/u/a/inbox'}) is None


@pytest.mark.parametrize('label,endpoints,expected', SHAPES, ids=[s[0] for s in SHAPES])
def test_a_person_is_created_whatever_shape_endpoints_takes(app, db_session, label, endpoints, expected):
    peer_instance(PEER)
    own_inbox = f'https://{PEER}/u/alice/inbox'
    document = peer_actor_json(name='alice', server=PEER,
                               fields={'endpoints': endpoints, 'inbox': own_inbox})
    user = actor_json_to_model(document, 'alice', PEER)
    assert user is not None
    assert user.ap_inbox_url == (expected or own_inbox)
    assert db.session.query(User).filter_by(user_name='alice').count() == 1


@pytest.mark.parametrize('label,endpoints,expected', SHAPES, ids=[s[0] for s in SHAPES])
def test_a_group_is_created_whatever_shape_endpoints_takes(app, db_session, label, endpoints, expected):
    peer_instance(PEER)
    document = peer_actor_json('Group', name='memes', server=PEER, fields={'endpoints': endpoints})
    community = actor_json_to_model(document, '!memes', PEER)
    assert community is not None
    assert community.ap_inbox_url == (expected or f'https://{PEER}/c/memes/inbox')
    assert db.session.query(Community).count() == 1


@pytest.mark.parametrize('label,endpoints,expected', SHAPES, ids=[s[0] for s in SHAPES])
def test_a_feed_is_created_whatever_shape_endpoints_takes(app, db_session, http_mock, label, endpoints, expected):
    _peer_with_one_owner(http_mock)
    document = _owned_feed(fields={'endpoints': endpoints})
    feed = actor_json_to_model(document, '~news', PEER)
    assert feed is not None
    assert feed.ap_inbox_url == (expected or f'https://{PEER}/f/news/inbox')
    assert db.session.query(Feed).count() == 1


def test_a_person_with_unusable_endpoints_and_no_inbox_stores_an_empty_inbox(app, db_session):
    peer_instance(PEER)
    document = peer_actor_json(name='alice', server=PEER, fields={'endpoints': {}})
    user = actor_json_to_model(document, 'alice', PEER)
    assert user.ap_inbox_url == ''

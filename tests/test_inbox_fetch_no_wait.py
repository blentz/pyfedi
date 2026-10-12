"""D775 (owner ruling): all inbox processing fetches without retrying; retry is for housekeeping Celery tasks only.

C1 residual R1 fix round 1. remote_object_to_json retries a transport error after sleeping 3 s, which its
housekeeping callers keep. The inbox-processing callers -- resolve_remote_post (an Announce of a URL; nodebb
replies), process_microblog_announce and the relays' process_relayed_announce -- pass retry_delay=0.

Relay detection (detect_relay, _lists_us_as_follower) is reached from the admin relay page and the relay CLI
(add_relay, retry_relay) and from inbox signature verification (_relay_key). No housekeeping task calls it, so it
never retries.
"""
from unittest.mock import Mock

import httpx
import pytest

from app import db
from app.activitypub.util import find_actor_or_create, process_microblog_announce, resolve_remote_post
from app.models import Relay
from app.relays import RELAY_ACCEPTED, RELAY_PENDING, STYLE_MASTODON
from app.relays import inbound, subscribe
from tests.factories import make_community, make_follow, make_instance, make_user, seed_community_owner

pytestmark = pytest.mark.usefixtures('site')

OBJECT = 'https://remote.test/notes/1'
RELAY_INBOX = 'https://relay.example/inbox'
RELAY_ACTOR = 'https://relay.example/actor'


@pytest.fixture
def sleeps(monkeypatch):
    """Every sleep remote_object_to_json or get_request could reach."""
    slept = Mock()
    monkeypatch.setattr('app.utils.sleep', slept)
    monkeypatch.setattr('app.activitypub.util.time.sleep', slept)
    return slept


def refused():
    return httpx.ConnectError('no route')


def make_relay():
    relay = Relay(url=RELAY_INBOX, style=STYLE_MASTODON, inbox_url=RELAY_INBOX, actor_id=RELAY_ACTOR,
                  public_key='PEM', follow_activity_id='https://test.piefed.local/activities/relay-follow/abc',
                  state=RELAY_ACCEPTED)
    db.session.add(relay)
    db.session.commit()
    return relay


# --- inbox processing --------------------------------------------------------


def test_resolve_remote_post_fetches_once(app, db_session, http_mock, sleeps):
    seed_community_owner('remote.test')   # the Instance and User make_community's columns name
    community = make_community('cats', host='remote.test')
    route = http_mock.get(OBJECT).mock(side_effect=refused())

    assert resolve_remote_post(OBJECT, community, None, False) is None
    assert route.call_count == 1
    sleeps.assert_not_called()


def test_process_microblog_announce_fetches_once(app, db_session, http_mock, sleeps):
    booster = make_user(make_instance('m.example'), 'booster')
    make_follow(make_user(None, 'localuser', local=True), booster)
    route = http_mock.get(OBJECT).mock(side_effect=refused())
    announce = {'id': f'{booster.ap_public_url}/statuses/1/activity', 'type': 'Announce',
                'actor': booster.ap_public_url, 'object': OBJECT}

    assert process_microblog_announce(announce, 'a1', False) is None
    assert route.call_count == 1
    sleeps.assert_not_called()


def test_process_relayed_announce_fetches_once(app, db_session, redis_double, http_mock, sleeps):
    relay = make_relay()
    route = http_mock.get(OBJECT).mock(side_effect=refused())

    inbound.process_relayed_announce(relay.id, OBJECT)

    assert route.call_count == 1
    sleeps.assert_not_called()


# --- relay detection -----------------------------------------------------------


def test_adding_a_relay_fetches_its_actor_once(app, db_session, http_mock, sleeps, monkeypatch):
    """add_relay: the admin relay page (and the relay CLI)."""
    route = http_mock.get(RELAY_ACTOR).mock(side_effect=refused())
    monkeypatch.setattr(subscribe, 'send_relay_follow', lambda relay: None)

    with app.test_request_context():
        relay = subscribe.add_relay(RELAY_INBOX)

    assert relay.actor_id is None
    assert route.call_count == 1
    sleeps.assert_not_called()


def test_retrying_a_relay_asks_each_url_once(app, db_session, http_mock, sleeps, monkeypatch):
    """retry_relay: the admin relay page (and the relay CLI). detect_relay, then _lists_us_as_follower."""
    relay = make_relay()
    relay.state = RELAY_PENDING
    db.session.commit()
    route = http_mock.get(RELAY_ACTOR).mock(side_effect=refused())
    monkeypatch.setattr(subscribe, 'send_relay_follow', lambda relay: None)

    with app.test_request_context():
        subscribe.retry_relay(relay)

    assert route.call_count == 2   # once for detect_relay, once for _lists_us_as_follower
    assert relay.actor_id == RELAY_ACTOR   # a failed fetch does not erase what is known
    sleeps.assert_not_called()


def test_an_inbox_key_refetch_fetches_the_relay_actor_once(app, db_session, http_mock, sleeps):
    """_relay_key: signature verification on the relay inbox."""
    relay = make_relay()
    route = http_mock.get(RELAY_ACTOR).mock(side_effect=refused())

    with app.test_request_context():
        assert inbound._relay_key(relay) is None

    assert route.call_count == 1
    sleeps.assert_not_called()


FEED = 'https://remote.test/f/news'


def test_an_inbox_feed_actor_asks_for_its_owners_once(app, db_session, http_mock, sleeps):
    """find_actor_or_create meeting a Feed on the inbox reaches actor_json_to_model's owners fetch (review I1)."""
    make_instance('remote.test')
    http_mock.get(FEED).respond(200, json={'id': FEED, 'type': 'Feed', 'preferredUsername': 'news',
                                           'inbox': f'{FEED}/inbox', 'attributedTo': f'{FEED}/owners',
                                           'following': f'{FEED}/following'})
    owners = http_mock.get(f'{FEED}/owners').mock(side_effect=refused())

    try:
        find_actor_or_create(FEED)
    except httpx.HTTPError:
        pass   # actor_json_to_model does not catch it; what matters is the one request and no sleep
    assert owners.call_count == 1
    sleeps.assert_not_called()

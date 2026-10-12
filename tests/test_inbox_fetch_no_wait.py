"""D775 (owner ruling): all inbox processing fetches without retrying; retry is for housekeeping Celery tasks only.

C1 residual R1 fix round 1. remote_object_to_json retries a transport error after sleeping 3 s, which its
housekeeping callers keep. The inbox-processing callers -- resolve_remote_post (an Announce of a URL; nodebb
replies), process_microblog_announce and the relays' process_relayed_announce -- pass retry_delay=0.
"""
from unittest.mock import Mock

import httpx
import pytest

from app import db
from app.activitypub.util import process_microblog_announce, resolve_remote_post
from app.models import Relay
from app.relays import RELAY_ACCEPTED, STYLE_MASTODON
from app.relays import inbound
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


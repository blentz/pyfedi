"""Interop D24 proactive sync: the instance actor (/actor) follows and unfollows synced channels."""
import pytest
from flask import current_app

from app import db
from app.discovery import SYNC_NONE, SYNC_PENDING
from app.discovery import instance_actor
from app.discovery.instance_answers import instance_actor_url
from app.discovery.instance_actor import follow_activity, send_instance_follow, \
    send_instance_undo
from app.models import DiscoverySync, Site
from tests.factories import make_community, make_instance, make_user

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture(autouse=True)
def founder(db_session):
    make_user(make_instance('test.piefed.local', software='piefed'), 'founder', local=True)


@pytest.fixture
def sent(monkeypatch):
    calls = []
    monkeypatch.setattr(instance_actor, 'send_post_request',
                        lambda uri, body, private_key, key_id, timeout=10: calls.append(
                            dict(uri=uri, body=body, private_key=private_key, key_id=key_id)) or True)
    return calls


@pytest.fixture
def synced(db_session):
    instance = make_instance('tube.example', software='peertube')
    community = make_community('zqchan', host='tube.example')
    community.instance_id = instance.id
    community.ap_inbox_url = 'https://tube.example/video-channels/zqchan/inbox'
    row = DiscoverySync(community_id=community.id, follow_target='https://tube.example/video-channels/ZqChan')
    db.session.add(row)
    db.session.commit()
    return row, community


def test_the_follow_comes_from_the_instance_actor_and_names_the_target_with_its_case(synced, sent):
    row, community = synced
    server = current_app.config['SERVER_URL']

    assert send_instance_follow(row, community) is True

    assert len(sent) == 1
    call = sent[0]
    assert call['uri'] == 'https://tube.example/video-channels/zqchan/inbox'
    assert call['key_id'] == f'{server}/actor#main-key'
    assert call['private_key'] == db.session.get(Site, 1).private_key
    assert call['body'] == {'actor': f'{server}/actor', 'to': ['https://tube.example/video-channels/ZqChan'],
                            'object': 'https://tube.example/video-channels/ZqChan', 'type': 'Follow',
                            'id': f'{server}/activities/follow/{row.follow_uuid}'}
    assert row.follow_state == SYNC_PENDING and row.followed_at is not None


def test_a_resend_keeps_the_same_uuid(synced, sent):
    row, community = synced
    send_instance_follow(row, community)
    first = row.follow_uuid

    send_instance_follow(row, community)

    assert row.follow_uuid == first
    assert sent[0]['body']['id'] == sent[1]['body']['id']


def test_an_offline_instance_gets_no_follow_and_stays_none(synced, sent):
    row, community = synced
    community.instance.dormant = True
    db.session.commit()

    assert send_instance_follow(row, community) is False

    assert sent == [] and row.follow_state == SYNC_NONE


def test_a_community_with_no_inbox_gets_no_follow(synced, sent):
    row, community = synced
    community.ap_inbox_url = None
    db.session.commit()

    assert send_instance_follow(row, community) is False
    assert sent == []


def test_a_community_with_no_instance_gets_no_follow(synced, sent):
    row, community = synced
    community.instance_id = None
    db.session.commit()

    assert send_instance_follow(row, community) is False
    assert sent == []


def test_undo_wraps_the_stored_follow(synced, sent):
    row, community = synced
    send_instance_follow(row, community)

    send_instance_undo(row, community)

    undo = sent[1]['body']
    assert undo['type'] == 'Undo' and undo['actor'] == instance_actor_url()
    assert undo['object'] == follow_activity(row)
    assert undo['id'].startswith(f"{current_app.config['SERVER_URL']}/activities/undo/")
    assert sent[1]['uri'] == community.ap_inbox_url


def test_undo_without_a_follow_sends_nothing(synced, sent):
    row, community = synced
    send_instance_undo(row, community)
    assert sent == []


def test_undo_without_an_inbox_sends_nothing(synced, sent):
    row, community = synced
    row.follow_uuid = 'abc'
    community.ap_inbox_url = None
    send_instance_undo(row, community)
    assert sent == []


def test_an_undo_transport_failure_is_logged_not_raised(synced, monkeypatch, caplog):
    row, community = synced
    row.follow_uuid = 'abc'

    def boom(*args, **kwargs):
        raise OSError('broker down')
    monkeypatch.setattr(instance_actor, 'send_post_request', boom)

    send_instance_undo(row, community)

    assert 'undo' in caplog.text.lower()

"""Interop D24 proactive sync: a poll re-walks an outbox newest-first and stops at the first post already here."""
from types import SimpleNamespace

import pytest

from app import db
from app.community import util
from app.community.util import BACKFILL_NO_OUTBOX, BACKFILL_UNREADABLE, retrieve_mods_and_backfill
from app.discovery import backfill
from app.discovery.backfill import run_backfill
from app.models import Post
from tests.factories import make_community, make_instance, make_post, make_user

pytestmark = pytest.mark.usefixtures('site')

OUTBOX = 'https://tube.example/video-channels/zqchan/outbox'


@pytest.fixture(autouse=True)
def founder(db_session):
    make_user(make_instance('test.piefed.local', software='piefed'), 'founder', local=True)


@pytest.fixture
def channel(db_session):
    community = make_community('zqchan', host='tube.example')
    community.ap_profile_id = 'https://tube.example/video-channels/zqchan'
    community.ap_outbox_url = OUTBOX
    db.session.commit()
    return community


def announces(*video_ids):
    return {'type': 'OrderedCollection', 'orderedItems': [
        {'type': 'Announce', 'id': f'{v}/announce', 'object': v} for v in video_ids]}


def test_stop_at_known_fetches_nothing_past_the_first_stored_video(channel, monkeypatch):
    author = make_user(channel.instance, 'zqauthor')
    make_post(channel, author, 'https://tube.example/videos/watch/2')
    fetched = []

    def remote(url):
        fetched.append(url)
        return announces('https://tube.example/videos/watch/3', 'https://tube.example/videos/watch/2',
                         'https://tube.example/videos/watch/1') if url == OUTBOX else None
    monkeypatch.setattr(util, 'remote_object_to_json', remote)

    retrieve_mods_and_backfill(channel.id, 'tube.example', 'zqchan', None, stop_at_known=True)

    assert fetched == [OUTBOX, 'https://tube.example/videos/watch/3']


def test_without_stop_at_known_the_walk_goes_past_stored_videos(channel, monkeypatch):
    author = make_user(channel.instance, 'zqauthor')
    make_post(channel, author, 'https://tube.example/videos/watch/2')
    fetched = []

    def remote(url):
        fetched.append(url)
        return announces('https://tube.example/videos/watch/2', 'https://tube.example/videos/watch/1') \
            if url == OUTBOX else None
    monkeypatch.setattr(util, 'remote_object_to_json', remote)

    retrieve_mods_and_backfill(channel.id, 'tube.example', 'zqchan', None)

    assert 'https://tube.example/videos/watch/1' in fetched


def lemmy_item(post_id, activity_id):
    return {'type': 'Announce', 'id': activity_id, 'object': {
        'type': 'Create', 'object': {'id': post_id, 'attributedTo': 'https://l.example/u/zqauthor'}}}


def castopod_item(post_id, activity_id):
    return {'type': 'Create', 'id': activity_id, 'object': {
        'id': post_id, 'attributedTo': 'https://l.example/u/zqauthor'}}


@pytest.mark.parametrize('shape, known_id, new_id', [
    (lemmy_item, 'https://l.example/post/9', 'https://l.example/post/8'),
    (castopod_item, 'https://pod.example/@pod/episodes/9', 'https://pod.example/@pod/episodes/8')])
@pytest.mark.parametrize('stop_at_known, expected_calls', [(True, 0), (False, 2)])
def test_stop_at_known_reads_lemmy_and_castopod_shapes(channel, monkeypatch, shape, known_id, new_id,
                                                      stop_at_known, expected_calls):
    channel.ap_profile_id = 'https://l.example/c/zqchan'   # not a PeerTube channel url
    db.session.commit()
    author = make_user(channel.instance, 'zqauthor')
    make_post(channel, author, known_id)
    items = [shape(known_id, 'a1'), shape(new_id, 'a2')]
    created = []
    monkeypatch.setattr(util, 'remote_object_to_json',
                        lambda url: {'type': 'OrderedCollection', 'orderedItems': items} if url == OUTBOX else None)
    monkeypatch.setattr(util, 'find_actor_or_create', lambda *a, **k: SimpleNamespace(is_local=lambda: False))
    monkeypatch.setattr(util, 'can_create_post', lambda *a, **k: True)
    monkeypatch.setattr(util, 'create_post', lambda *a, **k: created.append(a) or None)

    retrieve_mods_and_backfill(channel.id, 'l.example', 'zqchan', None, stop_at_known=stop_at_known)

    assert len(created) == expected_calls


@pytest.mark.parametrize('item, by_reference, expected', [
    ('https://tube.example/x', False, None),
    ({'object': 'https://tube.example/v/1'}, True, 'https://tube.example/v/1'),
    ({'object': {'id': 'x'}}, True, None),
    ({'type': 'Announce'}, False, None),
    ({'object': 5}, False, None),
    ({'object': {'object': {'id': 7}}}, False, None),
    ({'object': {'id': 7}}, False, None),
    ({'object': {'object': 'https://l.example/post/9', 'id': 'https://l.example/activities/create/1'}}, False,
     'https://l.example/post/9'),
    ({'object': {'object': 'not-a-dict', 'id': 'https://l.example/p/1'}}, False, 'not-a-dict')])
def test_stored_object_id(item, by_reference, expected):
    from app.community.util import _stored_object_id
    assert _stored_object_id(item, by_reference) == expected


def test_an_unreadable_outbox_is_reported(channel, monkeypatch):
    monkeypatch.setattr(util, 'remote_object_to_json', lambda url: None)
    assert retrieve_mods_and_backfill(channel.id, 'tube.example', 'zqchan', None) == BACKFILL_UNREADABLE


def test_an_empty_outbox_is_not_an_error(channel, monkeypatch):
    monkeypatch.setattr(util, 'remote_object_to_json', lambda url: {'type': 'OrderedCollection', 'totalItems': 0})
    assert retrieve_mods_and_backfill(channel.id, 'tube.example', 'zqchan', None) is None


def test_an_unreadable_first_page_is_reported(channel, monkeypatch):
    monkeypatch.setattr(util, 'remote_object_to_json',
                        lambda url: {'type': 'OrderedCollection', 'first': OUTBOX + '?page=1'} if url == OUTBOX else None)
    assert retrieve_mods_and_backfill(channel.id, 'tube.example', 'zqchan', None) == BACKFILL_UNREADABLE


def test_a_community_with_no_outbox_is_reported(channel, monkeypatch):
    channel.ap_outbox_url = None
    db.session.commit()
    monkeypatch.setattr(util, 'remote_object_to_json', lambda url: None)
    assert retrieve_mods_and_backfill(channel.id, 'tube.example', 'zqchan', None) == BACKFILL_NO_OUTBOX


def test_a_community_with_no_outbox_still_gets_its_featured_posts(channel, monkeypatch):
    channel.ap_outbox_url = None
    channel.ap_featured_url = 'https://tube.example/video-channels/zqchan/featured'
    db.session.commit()
    fetched = []
    monkeypatch.setattr(util, 'remote_object_to_json', lambda url: fetched.append(url))

    assert retrieve_mods_and_backfill(channel.id, 'tube.example', 'zqchan', None) == BACKFILL_NO_OUTBOX
    assert fetched == [channel.ap_featured_url]


def test_run_backfill_passes_stop_at_known_and_returns_the_outcome(channel, monkeypatch):
    calls = []
    monkeypatch.setattr(backfill, 'remote_object_to_json', lambda url: {'type': 'Group'})
    monkeypatch.setattr(backfill, 'retrieve_mods_and_backfill',
                        lambda cid, server, name, json, stop_at_known=False: calls.append(stop_at_known) or 'x')

    assert run_backfill(channel.id, stop_at_known=True) == 'x'
    assert calls == [True]


def test_run_backfill_of_a_missing_community_returns_none(db_session):
    assert run_backfill(999999) is None

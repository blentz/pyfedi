"""C4: the inbox sends media objects through process_new_content."""
import pytest

from app import db
from app.activitypub import routes as activitypub_routes
from app.models import ActivityPubLog
from tests.factories import inbox_activity, make_community, make_post, make_user, seed_community_owner
from tests.test_inbox_dispatch_lock_delete import record_moderation
from tests.test_inbox_dispatch_preamble import dispatch


def create_activity(actor, obj, *, activity_type='Create', **outer):
    return inbox_activity(actor, activity_type=activity_type, object=obj, **outer)


@pytest.fixture
def peer(app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = seed_community_owner('peer.example')
    community = make_community(host='peer.example')
    author = make_user(instance, 'author')
    db.session.commit()
    monkeypatch.setattr(activitypub_routes, 'find_community', lambda request_json, author=None: community)
    monkeypatch.setattr(activitypub_routes, 'ensure_domains_match', lambda activity: True)
    return instance, community, author


@pytest.mark.parametrize('kind', ['Audio', 'Document', 'Image', 'Video'])
def test_a_create_of_each_media_type_reaches_create_post(peer, monkeypatch, kind):
    _, community, author = peer
    calls = record_moderation(monkeypatch, 'create_post')
    dispatch(create_activity(author, {'type': kind, 'id': f'https://peer.example/m/{kind}',
                                      'url': 'https://peer.example/files/1', 'name': 'x'}))
    assert len(calls['create_post']) == 1


def test_update_of_an_unknown_media_object_is_ignored(peer, monkeypatch):
    _, _, author = peer
    calls = record_moderation(monkeypatch, 'create_post', 'update_post_from_activity')
    dispatch(create_activity(author, {'type': 'Video', 'id': 'https://peer.example/videos/watch/404',
                                      'name': 'v'}, activity_type='Update'))
    assert calls['create_post'] == [] and calls['update_post_from_activity'] == []
    assert ActivityPubLog.query.one().exception_message == 'Update of unknown media object'


@pytest.mark.parametrize('kind', ['Audio', 'Document', 'Image'])
def test_a_create_without_a_usable_url_is_refused(peer, monkeypatch, kind):
    _, _, author = peer
    calls = record_moderation(monkeypatch, 'create_post')
    dispatch(create_activity(author, {'type': kind, 'id': f'https://peer.example/m/{kind}',
                                      'url': 'javascript:alert(1)', 'name': 'x'}))
    assert calls['create_post'] == []
    assert ActivityPubLog.query.one().exception_message == 'Unacceptable media object'


def test_a_media_reply_still_goes_down_the_reply_path(peer, monkeypatch):
    _, community, author = peer
    parent = make_post(community, author, 'https://peer.example/post/1')
    db.session.commit()
    calls = record_moderation(monkeypatch, 'create_post', 'create_post_reply')
    dispatch(create_activity(author, {'type': 'Image', 'id': 'https://peer.example/m/r',
                                      'url': 'https://peer.example/files/1.png', 'inReplyTo': parent.ap_id,
                                      'content': '<p>reply</p>'}))
    assert calls['create_post'] == []


@pytest.mark.parametrize('kind, announced', [('Video', 0), ('Page', 1), ('Note', 1), ('Audio', 1)])
def test_an_update_of_a_stored_video_is_not_re_announced(peer, monkeypatch, kind, announced):
    """PeerTube sends an Update for every view-count change; the old Video arm never announced them."""
    _, community, author = peer
    post = make_post(community, author, f'https://peer.example/m/{kind}-stored')
    db.session.commit()
    calls = record_moderation(monkeypatch, 'update_post_from_activity', 'announce_activity_to_followers')
    dispatch(create_activity(author, {'type': kind, 'id': post.ap_id, 'name': 'renamed',
                                      'url': 'https://peer.example/files/1', 'content': '<p>x</p>'},
                             activity_type='Update'))
    assert len(calls['update_post_from_activity']) == 1
    assert len(calls['announce_activity_to_followers']) == announced

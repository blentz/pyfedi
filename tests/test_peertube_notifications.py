"""Notifications in PeerTube's API (plan W2.2a; spec 2026-11-09-peertube-login-design §3, ruling P3): only the four
kinds with a truthful PeerTube type, about videos where a video is the subject; read and read-all; the unread total
the app's badge reads."""
import uuid

import pytest

from app import db
from app.constants import (NOTIF_BAN, NOTIF_COMMUNITY, NOTIF_FOLLOW, NOTIF_MENTION, NOTIF_MESSAGE, NOTIF_POST,
                           NOTIF_REPLY, POST_TYPE_VIDEO)
from app.models import Notification, UserMute
from app.oauth_server import tokens
from app.shared.media import with_media
from tests import peertube_app_contract as contract
from tests.factories import make_community, make_file, make_post, make_post_reply, make_user


@pytest.fixture
def world(app, api_baseline):
    person, other = make_user(None, 'ptnotif', local=True), make_user(None, 'ptnotifother', local=True)
    community = make_community('ptnotif')
    community.show_all = True
    db.session.commit()
    local, _secret = tokens.local_client()
    access, _refresh = tokens.issue_token(local, person, tokens.LOCAL_CLIENT_SCOPE)
    return {'person': person, 'other': other, 'community': community, 'local': local, 'client': app.test_client(),
            'server': app.config['SERVER_URL'], 'headers': {'Authorization': f'Bearer {access}'}}


def _post(world, video=True):
    post = make_post(world['community'], world['other'], ap_id=f"{world['server']}/post/ptn{uuid.uuid4().hex[:8]}",
                     title='Notified')
    if video:
        post.type = POST_TYPE_VIDEO
        post.url = f"{world['server']}/static/media/posts/ab/cd/abcd{uuid.uuid4().hex[:11]}.mp4"
        with_media(post, status='ready', uuid=str(uuid.uuid4()), mime='video/mp4', bytes=1, duration_s=3,
                   width=640, height=360, fps=30, codec='h264')
    db.session.commit()
    return post


def _notify(world, kind, subtype='', read=False, **targets):
    notification = Notification(title='t', url='/x', user_id=world['person'].id, author_id=world['other'].id,
                                notif_type=kind, subtype=subtype, targets=targets, read=read)
    db.session.add(notification)
    db.session.commit()
    return notification


def _list(world, **query):
    response = world['client'].get('/api/v1/users/me/notifications', headers=world['headers'], query_string=query)
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()


def _check(item):
    assert contract.problems(item, contract.NOTIFICATION) == [] and item['type'] in contract.NOTIFICATION_TYPES
    assert contract.problems(item, contract.NOTIFICATION_OBJECTS[item['type']]) == []


def test_each_of_the_four_kinds_is_its_peertube_type(world):
    video = _post(world)
    reply = make_post_reply(video, world['other'])
    new_video = _notify(world, NOTIF_COMMUNITY, post_id=video.id)
    comment = _notify(world, NOTIF_POST, comment_id=reply.id)
    answer = _notify(world, NOTIF_REPLY, comment_id=reply.id)
    mention = _notify(world, NOTIF_MENTION, subtype='comment_mention', comment_id=reply.id)
    follow = _notify(world, NOTIF_FOLLOW)
    body = _list(world)
    types = {item['id']: item['type'] for item in body['data']}
    assert types == {new_video.id: 1, comment.id: 2, answer.id: 2, mention.id: 11, follow.id: 10}
    assert body['total'] == 5
    for item in body['data']:
        _check(item)
    item = next(item for item in body['data'] if item['id'] == comment.id)
    assert item['comment']['video']['id'] == video.id and item['comment']['account']['name'] == 'ptnotifother'


def test_what_has_no_truthful_type_or_is_not_about_a_video_is_left_out(world):
    text_post = _post(world, video=False)
    text_reply = make_post_reply(text_post, world['other'])
    for kind, targets in ((NOTIF_COMMUNITY, {'post_id': text_post.id}), (NOTIF_POST, {'comment_id': text_reply.id}),
                          (NOTIF_MESSAGE, {}), (NOTIF_BAN, {'community_id': world['community'].id})):
        _notify(world, kind, **targets)
    _notify(world, NOTIF_MENTION, subtype='post_mention', post_id=_post(world).id)
    assert _list(world) == {'total': 0, 'data': []}


def test_unread_filters_and_a_count_of_zero_is_the_badges_total(world):
    video = _post(world)
    _notify(world, NOTIF_COMMUNITY, post_id=video.id)
    _notify(world, NOTIF_FOLLOW, read=True)
    assert _list(world, unread='true')['total'] == 1
    badge = _list(world, start=0, count=0, unread='true')
    assert badge == {'total': 1, 'data': []}
    assert _list(world, count=0) == {'total': 2, 'data': []}
    assert _list(world, start=1, count=1)['total'] == 2 and len(_list(world, start=1, count=1)['data']) == 1


def _post_read(world, path, **kwargs):
    return world['client'].post(path, headers=world['headers'], **kwargs)


@pytest.mark.parametrize('how', ['json', 'form-brackets', 'form'])
def test_read_marks_the_persons_own_and_recounts(world, how):
    first, second = _notify(world, NOTIF_FOLLOW), _notify(world, NOTIF_FOLLOW)
    theirs = Notification(title='t', url='/x', user_id=world['other'].id, author_id=world['person'].id,
                          notif_type=NOTIF_FOLLOW, read=False)
    db.session.add(theirs)
    db.session.commit()
    ids = [first.id, theirs.id]
    kwargs = {'json': {'ids': ids}} if how == 'json' else {'data': {'ids[]' if how == 'form-brackets' else 'ids': ids}}
    assert _post_read(world, '/api/v1/users/me/notifications/read', **kwargs).status_code == 204
    db.session.expire_all()
    assert db.session.get(Notification, first.id).read and not db.session.get(Notification, second.id).read
    assert not db.session.get(Notification, theirs.id).read
    assert world['person'].unread_notifications == 1


@pytest.mark.parametrize('body', [{'ids': []}, {'ids': 'x'}, {'ids': ['a']}, {'ids': list(range(101))}, {}],
                         ids=['empty', 'not-a-list', 'not-ids', 'too-many', 'none'])
def test_read_refuses_what_is_not_a_list_of_ids(world, body):
    response = _post_read(world, '/api/v1/users/me/notifications/read', json=body)
    assert response.status_code == 400 and response.get_json()['code'] == 'invalid_request'


def test_read_all_marks_every_one(world):
    for _ in range(3):
        _notify(world, NOTIF_FOLLOW)
    assert _post_read(world, '/api/v1/users/me/notifications/read-all').status_code == 204
    db.session.expire_all()
    assert Notification.query.filter_by(user_id=world['person'].id, read=False).count() == 0
    assert world['person'].unread_notifications == 0


def test_a_muted_authors_notifications_are_left_out_and_an_avatar_is_a_whole_url(world):
    world['other'].avatar = make_file(file_path='app/static/media/users/ab/cd/other.png')
    db.session.commit()
    follow = _notify(world, NOTIF_FOLLOW)
    [item] = _list(world)['data']
    assert item['id'] == follow.id
    assert item['actorFollow']['follower']['avatars'][0]['fileUrl'].endswith('/static/media/users/ab/cd/other.png')
    assert item['actorFollow']['follower']['avatars'][0]['fileUrl'].startswith('http')
    db.session.add(UserMute(muter_id=world['person'].id, muted_id=world['other'].id, notifications=True))
    db.session.commit()
    assert _list(world) == {'total': 0, 'data': []}

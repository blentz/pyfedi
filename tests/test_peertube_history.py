"""Watch history in PeerTube's API (plan W2.2a; spec 2026-11-09-peertube-login-design §3, ruling P5): read marks of
video posts. Watching marks a video read (at most once in ten minutes), the history lists read videos newest first,
and removing history takes read marks away."""
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from app import db
from app.constants import POST_TYPE_VIDEO
from app.models import read_posts, utcnow
from app.oauth_server import tokens
from app.shared.media import with_media
from app.shared.post import mark_post_read
from tests import peertube_app_contract as contract
from tests.factories import make_community, make_post, make_user


@pytest.fixture
def world(app, api_baseline):
    person, author = make_user(None, 'pthist', local=True), make_user(None, 'pthistauthor', local=True)
    community = make_community('pthist')
    community.show_all = True
    db.session.commit()
    local, _secret = tokens.local_client()
    access, _refresh = tokens.issue_token(local, person, tokens.LOCAL_CLIENT_SCOPE)
    return {'person': person, 'author': author, 'community': community, 'local': local, 'client': app.test_client(),
            'server': app.config['SERVER_URL'], 'headers': {'Authorization': f'Bearer {access}'}}


def _video(world, title='History video'):
    post = make_post(world['community'], world['author'], ap_id=f"{world['server']}/post/pth{uuid.uuid4().hex[:8]}",
                     title=title)
    post.type = POST_TYPE_VIDEO
    post.url = f"{world['server']}/static/media/posts/ab/cd/abcd{uuid.uuid4().hex[:11]}.mp4"
    with_media(post, status='ready', uuid=str(uuid.uuid4()), mime='video/mp4', bytes=1, duration_s=3, width=640,
               height=360, fps=30, codec='h264')
    db.session.commit()
    return post


def _call(world, method, path, **kwargs):
    return getattr(world['client'], method)(path, headers=world['headers'], **kwargs)


def _read_at(world, post):
    return db.session.execute(select(read_posts.c.interacted_at).where(
        read_posts.c.user_id == world['person'].id, read_posts.c.read_post_id == post.id)).scalar()


def _history(world):
    response = _call(world, 'get', '/api/v1/users/me/history/videos')
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()


def _set_read_at(world, post, moment):
    db.session.execute(read_posts.update().where(read_posts.c.user_id == world['person'].id,
                                                 read_posts.c.read_post_id == post.id).values(interacted_at=moment))
    db.session.commit()


def test_watching_marks_the_video_read_and_lists_it_newest_first(world):
    older, newer = _video(world, 'Older'), _video(world, 'Newer')
    for post in (older, newer):
        assert _call(world, 'post', f'/api/v1/videos/{post.id}/views', json={'currentTime': 3}).status_code == 204
    _set_read_at(world, older, utcnow() - timedelta(hours=1))
    body = _history(world)
    assert [video['id'] for video in body['data']] == [newer.id, older.id] and body['total'] == 2
    assert all(contract.problems(video, contract.VIDEO) == [] for video in body['data'])


def test_a_view_marks_at_most_once_in_ten_minutes(world):
    post = _video(world)
    _call(world, 'post', f'/api/v1/videos/{post.id}/views')
    first = _read_at(world, post)
    _call(world, 'post', f'/api/v1/videos/{post.id}/views')
    assert _read_at(world, post) == first
    _set_read_at(world, post, utcnow() - timedelta(minutes=11))
    _call(world, 'post', f'/api/v1/videos/{post.id}/views')
    assert _read_at(world, post) > utcnow() - timedelta(minutes=1)


def test_a_signed_out_view_is_taken_and_marks_nothing(world):
    post = _video(world)
    assert world['client'].post(f'/api/v1/videos/{post.id}/views').status_code == 204
    assert world['client'].post('/api/v1/videos/999999/views').status_code == 404


def test_a_read_post_that_is_no_video_is_not_history(world):
    text_post = make_post(world['community'], world['author'], ap_id=f"{world['server']}/post/{uuid.uuid4().hex}")
    mark_post_read([text_post.id], True, world['person'].id)
    assert _history(world)['total'] == 0


def test_forgetting_one_takes_its_read_mark_away(world):
    post = _video(world)
    _call(world, 'post', f'/api/v1/videos/{post.id}/views')
    assert _call(world, 'delete', f'/api/v1/users/me/history/videos/{post.id}').status_code == 204
    assert _read_at(world, post) is None and _history(world)['total'] == 0
    assert _call(world, 'delete', '/api/v1/users/me/history/videos/999999').status_code == 404


@pytest.mark.parametrize('how', ['json', 'form', 'zulu', 'offset'])
def test_removing_before_a_date_keeps_the_later_ones(world, how):
    old, recent = _video(world, 'Old'), _video(world, 'Recent')
    for post in (old, recent):
        _call(world, 'post', f'/api/v1/videos/{post.id}/views')
    _set_read_at(world, old, utcnow() - timedelta(days=2))
    cut = utcnow() - timedelta(days=1)
    stamp = {'json': cut.isoformat(), 'form': cut.isoformat(), 'zulu': cut.strftime('%Y-%m-%dT%H:%M:%S.000Z'),
             'offset': (cut + timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M:%S+02:00')}[how]
    kwargs = {'data': {'beforeDate': stamp}} if how == 'form' else {'json': {'beforeDate': stamp}}
    assert _call(world, 'post', '/api/v1/users/me/history/videos/remove', **kwargs).status_code == 204
    assert _read_at(world, old) is None and _read_at(world, recent) is not None


def test_removing_with_no_date_clears_the_video_history_only(world):
    post = _video(world)
    _call(world, 'post', f'/api/v1/videos/{post.id}/views')
    text_post = make_post(world['community'], world['author'], ap_id=f"{world['server']}/post/{uuid.uuid4().hex}")
    mark_post_read([text_post.id], True, world['person'].id)
    assert _call(world, 'post', '/api/v1/users/me/history/videos/remove').status_code == 204
    assert _read_at(world, post) is None and _read_at(world, text_post) is not None
    assert _call(world, 'post', '/api/v1/users/me/history/videos/remove', json={}).status_code == 204   # nothing


def test_a_date_that_is_no_date_is_refused(world):
    response = _call(world, 'post', '/api/v1/users/me/history/videos/remove', json={'beforeDate': 'yesterday'})
    assert response.status_code == 400 and response.get_json()['code'] == 'invalid_request'

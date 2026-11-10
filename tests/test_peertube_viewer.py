"""PeerTube's reads as the person a token names (plan W2.2a; spec 2026-11-09-peertube-login-design §7): a token lets
the app browse a private domain, the person's own blocks apply, and a private community they belong to is theirs to
watch; a request with no token reads as before, as a signed-out reader."""
import uuid

import pytest

from app import db
from app.constants import POST_TYPE_VIDEO
from app.models import UserBlock
from app.oauth_server import tokens
from app.shared.media import with_media
from tests import test_login_gate as gate_tests
from tests.factories import make_community, make_community_member, make_post, make_user

private = gate_tests.private   # the private-instance fixture


def _video(server, community, author):
    post = make_post(community, author, ap_id=f'{server}/post/ptv{uuid.uuid4().hex[:8]}', title='Viewer video')
    post.type = POST_TYPE_VIDEO
    post.url = f'{server}/static/media/posts/ab/cd/abcd{uuid.uuid4().hex[:11]}.mp4'
    with_media(post, status='ready', uuid=str(uuid.uuid4()), mime='video/mp4', bytes=1, duration_s=3, width=640,
               height=360, fps=30, codec='h264')
    db.session.commit()
    return post


def _token(person) -> dict:
    local, _secret = tokens.local_client()
    access, _refresh = tokens.issue_token(local, person, tokens.LOCAL_CLIENT_SCOPE)
    return {'Authorization': f'Bearer {access}'}


@pytest.fixture
def world(app, api_baseline):
    person, author = make_user(None, 'ptviewer', local=True), make_user(None, 'ptviewerauthor', local=True)
    community = make_community('ptviewer')
    community.show_all = True
    db.session.commit()
    return {'person': person, 'author': author, 'community': community, 'client': app.test_client(),
            'server': app.config['SERVER_URL'], 'headers': _token(person)}


def _ids(response):
    assert response.status_code == 200, response.get_data(as_text=True)
    return [video['id'] for video in response.get_json()['data']]


def test_a_token_lets_the_app_browse_a_private_domain(app, private):
    person, author = make_user(None, 'ptviewerprivate', local=True), make_user(None, 'ptvpauthor', local=True)
    private.community.show_all = True
    post = _video(app.config['SERVER_URL'], private.community, author)
    client = app.test_client()
    assert post.id in _ids(client.get('/api/v1/videos', query_string={'sort': '-publishedAt'},
                                      headers=_token(person)))
    assert client.get(f'/api/v1/videos/{post.id}', headers=_token(person)).status_code == 200
    refused = client.get('/api/v1/videos')
    assert refused.status_code == 401 and refused.mimetype == 'application/problem+json'


def test_a_bad_token_is_a_401_the_app_refreshes_on(world):
    response = world['client'].get('/api/v1/videos', headers={'Authorization': 'Bearer not-a-token'})
    assert response.status_code == 401 and response.get_json()['code'] == 'invalid_token'


def test_the_persons_blocks_leave_videos_out(world):
    post = _video(world['server'], world['community'], world['author'])
    assert post.id in _ids(world['client'].get('/api/v1/videos', query_string={'sort': '-publishedAt'},
                                               headers=world['headers']))
    db.session.add(UserBlock(blocker_id=world['person'].id, blocked_id=world['author'].id))
    db.session.commit()
    assert post.id not in _ids(world['client'].get('/api/v1/videos', query_string={'sort': '-publishedAt'},
                                                   headers=world['headers']))
    assert post.id in _ids(world['client'].get('/api/v1/videos', query_string={'sort': '-publishedAt'}))


def test_a_member_of_a_private_community_can_open_its_video_and_nobody_else_can(world):
    secret = make_community('ptviewersecret')
    secret.private = True
    secret.show_all = True
    make_community_member(world['person'], secret)
    db.session.commit()
    post = _video(world['server'], secret, world['author'])
    assert world['client'].get(f'/api/v1/videos/{post.id}', headers=world['headers']).status_code == 200
    assert world['client'].get(f'/api/v1/videos/{post.id}').status_code == 404
    outsider = make_user(None, 'ptvieweroutsider', local=True)
    assert world['client'].get(f'/api/v1/videos/{post.id}', headers=_token(outsider)).status_code == 404

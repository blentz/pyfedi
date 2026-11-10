"""Subscriptions in PeerTube's API (plan W2.2a; spec 2026-11-09-peertube-login-design §3, V2): a channel is a community,
subscribing joins it and unsubscribing leaves it through the shared functions; the subscriptions' videos are the
joined communities' videos as the person sees them."""
import uuid

import pytest

from app import db
from app.constants import POST_TYPE_VIDEO
from app.models import CommunityMember, utcnow
from app.oauth_server import tokens
from app.shared.media import with_media
from tests import peertube_app_contract as contract
from tests.factories import make_community, make_community_ban, make_community_member, make_post, make_user


@pytest.fixture
def world(app, api_baseline):
    person, author = make_user(None, 'ptsub', local=True), make_user(None, 'ptsubauthor', local=True)
    joined, other = make_community('ptsubjoined'), make_community('ptsubother')
    for community in (joined, other):
        community.show_all = True
    make_community_member(person, joined)
    db.session.commit()
    local, _secret = tokens.local_client()
    access, _refresh = tokens.issue_token(local, person, tokens.LOCAL_CLIENT_SCOPE)
    return {'person': person, 'author': author, 'joined': joined, 'other': other, 'local': local,
            'headers': {'Authorization': f'Bearer {access}'}, 'client': app.test_client(),
            'server': app.config['SERVER_URL']}


def _call(world, method, path, **kwargs):
    return getattr(world['client'], method)(path, headers=world['headers'], **kwargs)


def _member(world, community) -> bool:
    return CommunityMember.query.filter_by(user_id=world['person'].id, community_id=community.id).first() is not None


def _video(world, community):
    post = make_post(community, world['author'], ap_id=f"{world['server']}/post/pts{uuid.uuid4().hex[:8]}")
    post.type = POST_TYPE_VIDEO
    post.url = f"{world['server']}/static/media/posts/ab/cd/abcd{uuid.uuid4().hex[:11]}.mp4"
    with_media(post, status='ready', uuid=str(uuid.uuid4()), mime='video/mp4', bytes=1, duration_s=3, width=640,
               height=360, fps=30, codec='h264')
    db.session.commit()
    return post


def test_the_list_is_the_joined_communities_as_channels(world):
    body = _call(world, 'get', '/api/v1/users/me/subscriptions').get_json()
    assert body['total'] == 1 and [channel['id'] for channel in body['data']] == [world['joined'].id]
    assert contract.problems(body['data'][0], contract.CHANNEL) == []


@pytest.mark.parametrize('how', ['handle', 'handle-with-host', 'actor-url', 'form'])
def test_subscribing_joins_the_community(world, how):
    other = world['other']
    uri = {'handle': other.name, 'handle-with-host': f"{other.name}@{world['server'].split('//')[1]}",
           'actor-url': other.public_url(), 'form': other.name}[how]
    response = _call(world, 'post', '/api/v1/users/me/subscriptions',
                     **({'data': {'uri': uri}} if how == 'form' else {'json': {'uri': uri}}))
    assert response.status_code == 204, response.get_data(as_text=True)
    assert _member(world, other)
    assert _call(world, 'post', '/api/v1/users/me/subscriptions', json={'uri': uri}).status_code == 204   # twice
    assert CommunityMember.query.filter_by(user_id=world['person'].id, community_id=other.id).count() == 1


@pytest.mark.parametrize('uri', ['nobody-here', '', 'https://elsewhere.example/c/nope', 'x' * 300])
def test_subscribing_to_no_channel_is_a_404(world, uri):
    response = _call(world, 'post', '/api/v1/users/me/subscriptions', json={'uri': uri})
    assert response.status_code == 404 and response.get_json()['code'] == 'not_found'


def test_a_shared_refusal_is_a_400_with_its_reason(world, monkeypatch):
    def refuse(*args, **kwargs):
        raise Exception('You are banned from this community')
    monkeypatch.setattr('app.shared.community.join_community', refuse)
    response = _call(world, 'post', '/api/v1/users/me/subscriptions', json={'uri': world['other'].name})
    assert response.status_code == 400 and 'banned' in response.get_json()['detail']


def test_unsubscribing_leaves_and_a_moderator_is_told_to_step_down(world):
    assert _call(world, 'delete', f"/api/v1/users/me/subscriptions/{world['joined'].name}").status_code == 204
    assert not _member(world, world['joined'])
    assert _call(world, 'delete', f"/api/v1/users/me/subscriptions/{world['joined'].name}").status_code == 204
    make_community_member(world['person'], world['other'], is_moderator=True)
    response = _call(world, 'delete', f"/api/v1/users/me/subscriptions/{world['other'].name}")
    assert response.status_code == 400 and 'moderator' in response.get_json()['detail']
    assert _call(world, 'delete', '/api/v1/users/me/subscriptions/nobody-here').status_code == 404


def test_exist_answers_each_uri_however_the_app_sends_them(world):
    joined, other = world['joined'].name, world['other'].name
    repeated = _call(world, 'get', f'/api/v1/users/me/subscriptions/exist?uris={joined}&uris={other}').get_json()
    assert repeated == {joined: True, other: False}
    bracketed = _call(world, 'get', f'/api/v1/users/me/subscriptions/exist?uris[]={joined},nobody-here').get_json()
    assert bracketed == {joined: True, 'nobody-here': False}


def test_the_subscriptions_videos_are_the_joined_communities_videos(world):
    mine, theirs = _video(world, world['joined']), _video(world, world['other'])
    body = _call(world, 'get', '/api/v1/users/me/subscriptions/videos').get_json()
    ids = [video['id'] for video in body['data']]
    assert mine.id in ids and theirs.id not in ids
    assert all(contract.problems(video, contract.VIDEO) == [] for video in body['data'])


def test_changing_subscriptions_needs_a_token_that_may_write(world):
    reader, _refresh = tokens.issue_token(world['local'], world['person'], 'read')
    response = world['client'].post('/api/v1/users/me/subscriptions', json={'uri': world['other'].name},
                                    headers={'Authorization': f'Bearer {reader}'})
    assert response.status_code == 403 and not _member(world, world['other'])


def test_a_person_banned_from_a_community_cannot_rejoin_it(world):
    """The shared join refuses it for every caller (security review 2026-11-09): the alpha API refused it in its own
    route, and this one did not."""
    make_community_ban(world['person'], world['other'])
    response = _call(world, 'post', '/api/v1/users/me/subscriptions', json={'uri': world['other'].name})
    assert response.status_code == 400 and 'banned' in response.get_json()['detail']
    assert not _member(world, world['other'])


def test_a_community_deleted_on_its_home_server_takes_no_new_members(world):
    world['other'].ap_deleted_at = utcnow()
    db.session.commit()
    response = _call(world, 'post', '/api/v1/users/me/subscriptions', json={'uri': world['other'].name})
    assert response.status_code == 400 and not _member(world, world['other'])

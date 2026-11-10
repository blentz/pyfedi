"""The signed-in person in PeerTube's API (plan W2.2a; spec 2026-11-09-peertube-login-design §3): users/me with every
field PeerTube's own app requires, its owned communities as channels, refusals in PeerTube's shape, and the display
name and description it may change. Also W2.1's videos, accounts and channels against the same app's models."""
import uuid

import pytest

from app import db
from app.constants import POST_TYPE_VIDEO
from app.models import OAuthToken
from app.oauth_server import tokens
from app.shared.media import with_media
from tests import peertube_app_contract as contract
from tests import test_login_gate as gate_tests
from tests.factories import make_community, make_community_member, make_post, make_user

private = gate_tests.private   # the private-instance fixture


@pytest.fixture
def world(app, api_baseline):
    person = make_user(None, 'ptme', local=True)
    owned, moderated = make_community('ptmeowned'), make_community('ptmemoderated')
    make_community_member(person, owned).is_owner = True
    make_community_member(person, moderated, is_moderator=True)
    db.session.commit()
    local, _secret = tokens.local_client()
    access, _refresh = tokens.issue_token(local, person, tokens.LOCAL_CLIENT_SCOPE)
    return {'person': person, 'owned': owned, 'moderated': moderated, 'token': access, 'local': local,
            'client': app.test_client(), 'server': app.config['SERVER_URL']}


def _me(world, token=None, method='get', **kwargs):
    headers = {'Authorization': f"Bearer {token}"} if token != '' else {}
    if token is None:
        headers = {'Authorization': f"Bearer {world['token']}"}
    return getattr(world['client'], method)('/api/v1/users/me', headers=headers, **kwargs)


def _refused(response, status):
    assert response.status_code == status, response.get_data(as_text=True)
    assert response.mimetype == 'application/problem+json' and response.get_json()['status'] == status


def test_me_has_every_field_peertubes_app_requires(world):
    response = _me(world)
    assert response.status_code == 200, response.get_data(as_text=True)
    body = response.get_json()
    assert contract.problems(body, contract.USER) == []
    assert body['username'] == 'ptme' and body['account']['name'] == 'ptme' and body['role']['id'] == 2
    assert body['videoQuota'] == -1 and body['noWelcomeModal'] is True


def test_its_channels_are_the_communities_it_owns(world):
    assert [channel['id'] for channel in _me(world).get_json()['videoChannels']] == [world['owned'].id]


def test_an_admin_is_an_administrator(world, monkeypatch):
    monkeypatch.setattr(type(world['person']), 'is_admin', lambda self: True)
    assert _me(world).get_json()['role'] == {'id': 0, 'label': 'Administrator'}


@pytest.mark.parametrize('token', ['', 'not-a-token'], ids=['none', 'unknown'])
def test_no_token_or_an_unknown_one_is_a_401_in_peertubes_shape(world, token):
    _refused(_me(world, token), 401)


def test_a_revoked_token_is_refused(world):
    tokens.revoke(OAuthToken.query.filter_by(user_id=world['person'].id).one())
    _refused(_me(world), 401)


def test_on_a_private_instance_a_token_is_let_in(app, private):
    person = make_user(None, 'ptmeprivate', local=True)
    local, _secret = tokens.local_client()
    access, _refresh = tokens.issue_token(local, person, tokens.LOCAL_CLIENT_SCOPE)
    client = app.test_client()
    assert client.get('/api/v1/users/me', headers={'Authorization': f'Bearer {access}'}).status_code == 200
    _refused(client.get('/api/v1/users/me'), 401)


def test_put_changes_the_display_name_and_description_and_takes_the_rest(world):
    response = _me(world, method='put', json={'displayName': 'Tess Video', 'description': 'I make *videos*',
                                              'autoPlayVideo': False, 'theme': 'dark'})
    assert response.status_code == 204, response.get_data(as_text=True)
    person = world['person']
    db.session.refresh(person)
    assert person.title == 'Tess Video' and person.about == 'I make *videos*' and '<em>videos</em>' in person.about_html
    body = _me(world).get_json()
    assert body['account']['displayName'] == 'Tess Video' and body['autoPlayVideo'] is True


def test_put_as_a_form_and_with_nothing_to_change(world):
    assert _me(world, method='put', data={'displayName': 'Formed'}).status_code == 204
    db.session.refresh(world['person'])
    assert world['person'].title == 'Formed'
    assert _me(world, method='put', json={'noWelcomeModal': True}).status_code == 204
    db.session.refresh(world['person'])
    assert world['person'].title == 'Formed'


@pytest.mark.parametrize('fields', [{'displayName': 'x' * 121}, {'description': 'x' * 5001}, {'displayName': 7}],
                         ids=['long-name', 'long-description', 'not-text'])
def test_put_refuses_what_it_cannot_take(world, fields):
    response = _me(world, method='put', json=fields)
    assert response.status_code == 400 and response.get_json()['code'] == 'invalid_request'


def test_put_needs_a_token_that_may_write(world):
    reader, _refresh = tokens.issue_token(world['local'], world['person'], 'read')
    _refused(_me(world, reader, method='put', json={'displayName': 'Nope'}), 403)


# W2.1's entities, as the official app decodes them

def _video(world):
    post = make_post(world['owned'], world['person'], ap_id=f"{world['server']}/post/ptme{uuid.uuid4().hex[:8]}",
                     title='A contract video')
    post.type = POST_TYPE_VIDEO
    post.url = f"{world['server']}/static/media/posts/ab/cd/abcd{uuid.uuid4().hex[:11]}.mp4"
    with_media(post, status='ready', uuid=str(uuid.uuid4()), mime='video/mp4', bytes=1, duration_s=3, width=640,
               height=360, fps=30, codec='h264')
    world['owned'].show_all = True
    db.session.commit()
    return post


def test_a_video_its_account_and_its_channel_decode_in_peertubes_app(world):
    post = _video(world)
    listed = world['client'].get('/api/v1/videos', query_string={'sort': '-publishedAt'}).get_json()['data']
    item = next(item for item in listed if item['id'] == post.id)
    whole = world['client'].get(f'/api/v1/videos/{post.id}').get_json()
    for video in (item, whole):
        assert contract.problems(video, contract.VIDEO) == [] and video['privacy']['id'] in contract.PRIVACIES
    account = world['client'].get('/api/v1/accounts/ptme').get_json()
    channel = world['client'].get(f"/api/v1/video-channels/{world['owned'].name}").get_json()
    assert contract.problems(account, contract.ACCOUNT) == [] and contract.problems(channel, contract.CHANNEL) == []


def test_the_email_goes_only_to_the_peertube_login_that_asked_with_the_password(world):
    """Another app's token (a Mastodon app's, by its own consent screen) reads users/me too, but was never trusted
    with the address: it gets an empty one (security review 2026-11-09)."""
    world['person'].email = 'ptme@example.com'
    db.session.commit()
    assert _me(world).get_json()['email'] == 'ptme@example.com'
    other, _secret = tokens.register_app('Tusky', ['https://app.example/cb'], 'read write follow', None)
    access, _refresh = tokens.issue_token(other, world['person'], 'read')
    body = _me(world, access).get_json()
    assert body['email'] == '' and contract.problems(body, contract.USER) == []

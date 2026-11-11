"""Pixelfed's own routes for its official app (plan W1.4a; spec 2026-11-10-pixelfed-app-design §2, §3): its config
and compose limits, explore's trending photos, an account by name and mutual followers, a post's report, and the
answers for what PieFed has none of yet (stories W1.4c, chats W1.4b) or at all (Pixelfed's push relay, collections,
computed trends)."""
import uuid

import pytest

from app import db
from app.constants import POST_TYPE_IMAGE
from app.models import File, Report, UserFollower
from app.oauth_server import tokens
from tests.factories import make_community, make_file, make_post, make_user
from tests import test_login_gate as gate_tests

private = gate_tests.private   # the private-instance fixture


@pytest.fixture
def world(app, api_baseline):
    registered, _secret = tokens.register_app('Pixelfed', ['https://app.example/cb'], 'read write follow push', None)
    me, author = make_user(None, 'pfme', local=True), make_user(None, 'pfauthor', local=True)
    community = make_community('pfphotos')
    db.session.commit()
    access, _refresh = tokens.issue_token(registered, me, 'read write follow push')
    return {'me': me, 'author': author, 'community': community, 'client': app.test_client(),
            'headers': {'Authorization': f'Bearer {access}'}}


def _get(world, path, **query):
    return world['client'].get(path, headers=world['headers'], query_string=query)


def _photo(world, title='A photo', votes=1):
    picture = make_file(file_path=f'app/static/media/posts/aa/{uuid.uuid4().hex[:8]}.webp')
    post = make_post(world['community'], world['author'], ap_id=f'https://test.piefed.local/post/{uuid.uuid4().hex}',
                     title=title)
    post.type, post.image_id, post.up_votes, post.score = POST_TYPE_IMAGE, picture.id, votes, votes
    db.session.commit()
    return post


def test_the_v2_1_config_says_a_pixelfed_the_app_takes(world):
    """Pixelfed's app (2.0.1) refuses a server whose /api/v2.1/config version is below 0.14.4; read before login."""
    config = world['client'].get('/api/v2.1/config').get_json()
    assert config['version'] == '0.15.2' and config['oauth'] is True
    assert config['limits']['account']['max_bio_length'] == 1000
    assert config['limits']['media']['max_album_limit'] >= 1 and config['features']['video'] is True


def test_the_config_and_compose_limits(world):
    config = _get(world, '/api/v2/config').get_json()
    assert config['version'] == '0.15.2' and config['account']['max_bio_length'] == 1000
    assert config['uploader']['album_limit'] >= 1 and 'image/jpeg' in config['uploader']['mime_types']
    compose = _get(world, '/api/v1.1/compose/settings').get_json()
    assert compose['max_media_attachments'] >= 1 and compose['max_altext_length'] == 1500


@pytest.mark.parametrize('path,query', [('/api/v1.1/discover/posts/trending', {'range': 'daily'}),
                                        ('/api/v1.1/discover/posts/trending', {'range': 'yearly'}),
                                        ('/api/v1.1/discover/posts/trending', {'range': 'nonsense'}),
                                        ('/api/v1.1/discover/posts/network/trending', {})])
def test_trending_is_the_most_voted_photos_as_pixelfeds_statuses(world, path, query):
    _photo(world, 'Liked', votes=5)
    text = make_post(world['community'], world['author'], ap_id=f'https://test.piefed.local/post/{uuid.uuid4()}',
                     title='Words only')
    text.up_votes = 9
    db.session.commit()
    listed = _get(world, path, _pe=1, **query).get_json()
    assert [status['pf_type'] for status in listed] == ['photo']


@pytest.mark.parametrize('path', ['/api/v1.1/discover/posts/hashtags', '/api/v1.1/discover/accounts/popular',
                                  '/api/v1.1/collections/self', '/api/pixelfed/v1/app/settings'])
def test_what_is_not_kept_here_is_an_empty_list(world, path):
    assert _get(world, path).get_json() == []


def test_no_stories_yet_and_no_pixelfed_push(world):
    assert _get(world, '/api/v1.2/stories/carousel').get_json() == {'self': [], 'nodes': []}
    assert _get(world, '/api/v1.1/nag/state').get_json() == {'active': False}
    assert _get(world, '/api/v1.1/push/state').status_code == 404
    pushed = world['client'].post('/api/v1.1/push/update', headers=world['headers'], json={})
    assert pushed.status_code == 404


@pytest.mark.parametrize('name', ['pfauthor', '@pfauthor', 'pfauthor@test.piefed.local'])
def test_an_account_by_its_name(world, name):
    assert _get(world, f'/api/v1.1/accounts/username/{name}').get_json()['username'] == 'pfauthor'


def test_nobody_by_that_name_is_an_empty_list(world):
    assert _get(world, '/api/v1.1/accounts/username/nobodyhere').get_json() == []


def test_mutual_followers_are_whom_i_follow_who_follow_them(world):
    friend, stranger = make_user(None, 'pffriend', local=True), make_user(None, 'pfstranger', local=True)
    for follower, followed in ((world['me'], friend), (friend, world['author']), (stranger, world['author'])):
        db.session.add(UserFollower(local_user_id=follower.id, remote_user_id=followed.id, is_inward=False,
                                    is_accepted=True))
    db.session.commit()
    mutuals = _get(world, f"/api/v1.1/accounts/mutuals/{world['author'].id}").get_json()
    assert [person['username'] for person in mutuals] == ['pffriend']
    assert _get(world, f'/api/v1.1/accounts/mutuals/{2 ** 31}').get_json() == []


def _report(world, **fields):
    return world['client'].post('/api/v1.1/report', headers=world['headers'], json=fields)


def test_a_post_is_reported(world):
    post = _photo(world)
    response = _report(world, object_id=str(post.id), object_type='post', report_type='spam')
    assert response.status_code == 200 and response.get_json()['code'] == 200
    assert Report.query.filter_by(reporter_id=world['me'].id, suspect_post_id=post.id).count() == 1


def test_reports_that_are_not_of_a_post_or_not_to_be_had(world, monkeypatch):
    assert _report(world, object_id=str(world['author'].id), object_type='user').status_code == 400
    assert _report(world, object_id='999999', object_type='post').status_code == 404
    assert _report(world, object_id='x', object_type='post').status_code == 404
    own = make_post(world['community'], world['me'], ap_id=f'https://test.piefed.local/post/{uuid.uuid4()}')
    assert _report(world, object_id=str(own.id), object_type='post').get_json()['error_code'] == \
        'ERROR_NO_SELF_REPORTS'

    def refused(*_args, **_kwargs):
        raise Exception('Already reported')
    monkeypatch.setattr('app.mastodon.pixelfed.shared_post.report_post', refused)
    response = _report(world, object_id=str(_photo(world).id), object_type='post', report_type='spam')
    assert response.status_code == 400 and response.get_json()['error'] == 'Already reported'


def test_the_avatar_is_removed(world):
    picture = make_file(file_path='app/static/media/users/aa/avatar.webp')
    world['me'].avatar_id = picture.id
    db.session.commit()
    deleting = world['client'].delete('/api/v1.1/accounts/avatar', headers=world['headers'])
    assert deleting.status_code == 200 and deleting.get_json()['username'] == 'pfme'
    assert world['me'].avatar_id is None and db.session.get(File, picture.id) is None
    again = world['client'].delete('/api/v1.1/accounts/avatar', headers=world['headers'])
    assert again.status_code == 200


def test_writes_need_a_token(world):
    assert world['client'].post('/api/v1.1/report', json={}).status_code == 401
    assert world['client'].get('/api/v1.1/compose/settings').status_code == 401


def test_a_hidden_account_is_answered_as_nobody(world, monkeypatch):
    monkeypatch.setattr('app.mastodon.pixelfed.reads_users.refuse_hidden_person', lambda *_args: (_ for _ in ()).throw(
        Exception('hidden here')))
    assert _get(world, '/api/v1.1/accounts/username/pfauthor').get_json() == []


def test_the_app_reads_its_config_from_a_private_instance_before_it_signs_in(private, app):
    response = app.test_client().get('/api/v2.1/config')
    assert response.status_code == 200 and response.get_json()['version'] == '0.15.2'

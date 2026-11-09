"""Media from a Mastodon app (plan W1.3): upload, read and describe an image; attach it to a status, which makes a
post a gallery and puts the picture in a comment's body."""
import io
from itertools import count

import pytest
from PIL import Image
from werkzeug.exceptions import RequestEntityTooLarge

import app.mastodon.media as media_module
from app import db
from app.constants import COMMUNITY_KIND_MICROBLOGS, POST_TYPE_GALLERY, POST_TYPE_IMAGE
from app.mastodon import ids
from app.models import File
from app.oauth_server import tokens
from tests.factories import make_community, make_post, make_user

_SERIAL = count()
pytestmark = pytest.mark.usefixtures('private_static_tree')   # uploads land in a scratch app/static


@pytest.fixture
def world(app, api_baseline):
    registered, _secret = tokens.register_app('Tusky', ['https://app.example/cb'], 'read write', None)
    me = make_user(None, 'uploader', local=True, with_keys=True)
    other = make_user(None, 'otheruploader', local=True, with_keys=True)
    microblogs = make_community('microblogs')
    microblogs.kind = COMMUNITY_KIND_MICROBLOGS
    community = make_community('mediaplace')
    db.session.commit()
    mine, _refresh = tokens.issue_token(registered, me, 'read write')
    theirs, _refresh = tokens.issue_token(registered, other, 'read write')
    return {'me': me, 'community': community, 'token': mine, 'their_token': theirs, 'app': registered,
            'client': app.test_client()}


def _png(width=40, height=20) -> io.BytesIO:
    data = io.BytesIO()
    Image.new('RGB', (width, height), 'orange').save(data, format='PNG')
    data.seek(0)
    return data


def _upload(world, token=None, name='pie.png', content=None, **form):
    return world['client'].post('/api/v2/media', content_type='multipart/form-data',
                                headers={'Authorization': f"Bearer {token or world['token']}"},
                                data={'file': (content or _png(), name), **form})


def _our_picture_is_an_image(http_mock):
    """A one-picture post is a link to the upload, which the link check asks for its type (as the alpha API's)."""
    http_mock.head(url__startswith='https://test.piefed.local/static/media/').respond(
        200, headers={'Content-Type': 'image/png'})


def _call(world, method, path, token=None, **kwargs):
    return getattr(world['client'], method)(path, headers={'Authorization': f"Bearer {token or world['token']}"},
                                            **kwargs)


def test_an_image_upload_is_answered_processed_with_its_description(world):
    response = _upload(world, description='  an orange pie  ')
    assert response.status_code == 200, response.get_data(as_text=True)
    media = response.get_json()
    assert media['type'] == 'image' and media['url'].startswith('http') and media['description'] == 'an orange pie'
    assert db.session.get(File, int(media['id'])).alt_text == 'an orange pie'


@pytest.mark.parametrize('name, content', [('clip.mp4', io.BytesIO(b'not an image')), ('', io.BytesIO(b''))],
                         ids=['video', 'nothing'])
def test_what_is_not_an_image_is_refused(world, name, content):
    response = _upload(world, name=name, content=content)
    assert response.status_code == 422 and response.get_json()['error']


def test_an_upload_is_read_and_described_by_its_uploader_alone(world):
    media_id = _upload(world).get_json()['id']
    assert _call(world, 'get', f'/api/v1/media/{media_id}').status_code == 200
    described = _call(world, 'put', f'/api/v1/media/{media_id}', json={'description': 'pie, top view'})
    assert described.get_json()['description'] == 'pie, top view'
    assert _call(world, 'put', f'/api/v1/media/{media_id}', data={'focus': '0,0'}).status_code == 200   # kept
    assert db.session.get(File, int(media_id)).alt_text == 'pie, top view'
    assert _call(world, 'get', f'/api/v1/media/{media_id}', token=world['their_token']).status_code == 404
    assert _call(world, 'put', f'/api/v1/media/{media_id}', token=world['their_token'],
                 json={'description': 'mine'}).status_code == 404
    assert _call(world, 'get', '/api/v1/media/99999999999').status_code == 404


def test_attached_pictures_make_a_gallery_post_in_order(world):
    first, second = _upload(world, description='one').get_json(), _upload(world, description='two').get_json()
    response = _call(world, 'post', '/api/v1/statuses',
                     json={'status': 'Two pies. Both orange.', 'media_ids': [second['id'], first['id']]})
    assert response.status_code == 200, response.get_data(as_text=True)
    post = ids.find_status(response.get_json()['id'])
    assert post.type == POST_TYPE_GALLERY and post.gallery_count == 2
    assert [file.alt_text for file in post.album_images()] == ['two', 'one']
    assert [media['description'] for media in response.get_json()['media_attachments']] == ['two', 'one']


def test_pictures_alone_need_no_text(world, http_mock):
    _our_picture_is_an_image(http_mock)
    media = _upload(world).get_json()
    response = _call(world, 'post', '/api/v1/statuses', data={'media_ids[]': [media['id']]})
    assert response.status_code == 200, response.get_data(as_text=True)
    post = ids.find_status(response.get_json()['id'])   # one picture: an image post, as the alpha API's upload makes
    assert post.type == POST_TYPE_IMAGE and not post.image.alt_text
    assert [attached['type'] for attached in response.get_json()['media_attachments']] == ['image']


def test_a_comment_carries_its_pictures_in_its_body(world):
    post = make_post(world['community'], world['me'], ap_id=f'https://test.piefed.local/post/m{next(_SERIAL)}')
    media = _upload(world, description='a [crumb]').get_json()
    response = _call(world, 'post', '/api/v1/statuses',
                     json={'status': 'Look', 'in_reply_to_id': ids.status_id(post), 'media_ids': [media['id']]})
    reply = ids.find_status(response.get_json()['id'])
    # the alt text's ']' is dropped, so it cannot close the image's brackets early
    assert reply.body == f"Look\n\n![a [crumb]({media['url']})" or reply.body.startswith('Look\n\n![a [crumb](')
    assert media['url'].split('/')[-1] in reply.body


@pytest.mark.parametrize('media_ids', [['999999'], 'x', ['1', '2', '3', '4', '5']], ids=['not-theirs', 'not-a-list',
                                                                                    'too-many'])
def test_attachments_that_are_not_ones_own_or_too_many_are_refused(world, media_ids):
    response = _call(world, 'post', '/api/v1/statuses', json={'status': 'hi', 'media_ids': media_ids})
    assert response.status_code == 422


def test_anothers_upload_cannot_be_attached(world):
    theirs = _upload(world, token=world['their_token']).get_json()
    response = _call(world, 'post', '/api/v1/statuses', json={'status': 'hi', 'media_ids': [theirs['id']]})
    assert response.status_code == 422


def test_one_described_picture_keeps_its_description(world, http_mock):
    _our_picture_is_an_image(http_mock)
    media = _upload(world, description='a lone pie').get_json()
    response = _call(world, 'post', '/api/v1/statuses', json={'status': 'Pie.', 'media_ids': [media['id']]})
    assert ids.find_status(response.get_json()['id']).image.alt_text == 'a lone pie'


def test_an_upload_the_shared_store_aborts_keeps_its_status(world, monkeypatch):
    def too_big(*args, **kwargs):
        raise RequestEntityTooLarge()
    monkeypatch.setattr(media_module, 'process_upload', too_big)
    response = _upload(world)
    assert response.status_code == 413 and 'error' in response.get_json()


def test_an_upload_fault_is_logged_and_its_text_never_sent(world, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError('/app/app/static/media/posts: disk quota exceeded')
    monkeypatch.setattr(media_module, 'process_upload', fail)
    response = _upload(world)
    assert response.status_code == 500 and response.get_json() == {'error': 'Something went wrong'}

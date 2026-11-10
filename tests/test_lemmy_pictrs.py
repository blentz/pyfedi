"""Uploading an image as Lemmy does, for Jerboa (plan W3; spec 2026-11-10-lemmy-v3-design §2): images[] posted to
/pictrs/image with the token as the jwt cookie are stored by alpha's upload, each named by a signed token that
/pictrs/image/<token> redirects from, to that image only; a post linking one stores the image's own URL; the upload
shares alpha's limit."""
import io
import uuid

import pytest

from app import cache, db, limiter
from app.models import Post
from tests import lemmy_app_contract as contract
from tests import test_login_gate as gate_tests
from tests import test_onion_guard_limits as limit_tests
from tests.factories import make_community, make_user

private = gate_tests.private   # the private-instance fixture
limited_app = limit_tests.limited_app   # an app whose limiter counts
STORED = 'https://test.piefed.local/static/media/posts/ab/cd/abcdefghijklmno.png'


@pytest.fixture
def stored(monkeypatch):
    names = []

    def store(image_file, user=None, **_kwargs):
        names.append(image_file.filename)
        return STORED
    monkeypatch.setattr('app.api.alpha.utils.upload.process_upload', store)
    return names


@pytest.fixture
def world(app, api_baseline, monkeypatch, stored):
    monkeypatch.setitem(app.config, 'ENABLE_ALPHA_API', 'true')
    cache.clear()
    person = make_user(None, 'lemmypictrs', local=True, with_keys=True)
    person.verified = True
    db.session.commit()
    return {'person': person, 'jwt': person.encode_jwt_token(), 'client': app.test_client(), 'stored': stored,
            'flask': app,
            'community': make_community('lemmypictrs')}


def _images(count=1):
    return {'images[]': [(io.BytesIO(b'\x89PNG...'), 'test.jpg') for _ in range(count)]}


def _with_cookie(client, flask_app, jwt):
    """As Jerboa's library sends it: the token as the jwt cookie (the test client sends its own jar's cookies only)."""
    client.set_cookie('jwt', jwt, domain=flask_app.config['SERVER_NAME'])
    return client


def _upload(world, count=1):
    return _with_cookie(world['client'], world['flask'], world['jwt']).post(
        '/pictrs/image', data=_images(count), content_type='multipart/form-data')


def test_an_image_posted_as_jerboa_posts_it_is_stored_and_found_again(world):
    response = _upload(world)
    body = response.get_json()
    assert response.status_code == 200 and contract.problems(body, 'UploadImageResponse') == []
    assert body['msg'] == 'ok' and world['stored'] == ['test.jpg']
    found = world['client'].get(f"/pictrs/image/{body['files'][0]['file']}")
    assert found.status_code == 302 and found.headers['Location'] == STORED


def test_the_token_in_a_header_works_too(world):
    response = world['client'].post('/pictrs/image', data=_images(), content_type='multipart/form-data',
                                    headers={'Authorization': f"Bearer {world['jwt']}"})
    assert response.status_code == 200


def test_an_upload_needs_a_login_and_a_few_images(world):
    refused = world['client'].post('/pictrs/image', data=_images(), content_type='multipart/form-data')
    assert refused.status_code == 401 and refused.get_json() == {'error': 'incorrect_login'}
    for count in (0, 11):
        response = _upload(world, count)
        assert response.status_code == 400 and response.get_json() == {'error': 'invalid_request'}


def test_a_token_this_server_did_not_sign_leads_nowhere(world):
    response = world['client'].get('/pictrs/image/aHR0cHM6Ly9ldmlsLmV4YW1wbGUv.forged')
    assert response.status_code == 404 and response.get_json() == {'error': 'not_found'}


def test_a_post_linking_an_upload_stores_the_images_own_url(world, monkeypatch):
    monkeypatch.setattr('app.shared.post.is_image_url', lambda url: url.endswith('.png'))   # no fetching its type
    token = _upload(world).get_json()['files'][0]['file']
    headers = {'Authorization': f"Bearer {world['jwt']}"}
    for url, kept in ((f'https://test.piefed.local/pictrs/image/{token}', STORED),
                      ('https://test.piefed.local/pictrs/image/forged.token', 'https://test.piefed.local/pictrs/image/forged.token'),
                      ('https://example.com/elsewhere', 'https://example.com/elsewhere')):
        response = world['client'].post('/api/v3/post', headers=headers, json={
            'name': f'Linked {uuid.uuid4().hex[:6]}', 'community_id': world['community'].id, 'url': url})
        assert response.status_code == 200, response.get_data(as_text=True)
        post = db.session.get(Post, response.get_json()['post_view']['post']['id'])
        assert post.url == kept
        edited = world['client'].put('/api/v3/post', headers=headers, json={'post_id': post.id, 'url': url})
        assert edited.status_code == 200 and db.session.get(Post, post.id).url == kept


def test_an_upload_works_on_a_private_instance(private, app, stored):
    person = make_user(None, 'lemmyprivatepictrs', local=True)
    client = _with_cookie(app.test_client(), app, person.encode_jwt_token())
    response = client.post('/pictrs/image', data=_images(), content_type='multipart/form-data')
    assert response.status_code == 200, response.get_data(as_text=True)


def test_uploads_share_alphas_limit(limited_app, api_baseline, monkeypatch, stored):
    monkeypatch.setitem(limited_app.config, 'ENABLE_ALPHA_API', 'true')
    with limited_app.app_context():
        person = make_user(None, 'lemmyuploadlimit', local=True)
        headers, away = {'Authorization': f'Bearer {person.encode_jwt_token()}'}, {'REMOTE_ADDR': '203.0.113.4'}
        client = limited_app.test_client()
        limiter.reset()
        limiter.enabled = True
        try:
            codes = [client.post('/api/alpha/upload/image', data={'file': (io.BytesIO(b'x'), 'a.png')},
                                 content_type='multipart/form-data', headers=headers, environ_base=away).status_code
                     for _ in range(15)]
            codes.append(client.post('/pictrs/image', data=_images(), content_type='multipart/form-data',
                                     headers=headers, environ_base=away).status_code)
        finally:
            limiter.reset()
            limiter.enabled = False
    assert codes == [200] * 15 + [429]

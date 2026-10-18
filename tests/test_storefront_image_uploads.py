"""The community and user image uploads answer with a URL on the storefront they were asked on, and saving that URL
stores the canonical one (onion storefront spec §3.5), as the general image upload does."""
import io
from unittest.mock import patch

import pytest
from flask import g

from app import db
from app.models import File, Site
from tests.test_api_user_settings import settings
from tests.test_storefront_api import CANONICAL, ONION, onion  # noqa: F401  (the fixture registers abc.onion)
from tests.factories import make_instance, make_user

pytestmark = pytest.mark.usefixtures('site')

PATH = '/static/media/{}/zz/yy/zzyyuploaded.png'


@pytest.mark.parametrize('route, destination', [('community_image', 'communities'), ('user_image', 'users')])
@pytest.mark.parametrize('origin', [f'http://{ONION}', f'https://{CANONICAL}'])
def test_the_upload_answers_with_a_url_on_the_storefront(app, onion, monkeypatch, route, destination, origin):
    monkeypatch.setitem(app.config, 'ENABLE_ALPHA_API', 'true')
    g.site = db.session.get(Site, 1)
    user = make_user(make_instance(CANONICAL, software='piefed'), 'uploader', local=True)
    stored = f'https://{CANONICAL}' + PATH.format(destination)
    with patch('app.api.alpha.utils.upload.process_upload', return_value=stored):
        response = app.test_client().post(
            f'/api/alpha/upload/{route}', base_url=origin, headers={'Authorization': f'Bearer {user.encode_jwt_token()}'},
            data={'file': (io.BytesIO(b'png'), 'a.png')}, content_type='multipart/form-data')
    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json()['url'] == f'{origin}{PATH.format(destination)}'


@pytest.mark.parametrize('field', ['avatar', 'cover'])
def test_saving_an_onion_image_url_stores_the_canonical_one(app, onion, api_baseline, field):
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    user = api_baseline.user2
    with patch('app.api.alpha.utils.user.make_image_sizes'):
        settings(user, **{field: f'http://{ONION}' + PATH.format('users')})
    image_id = user.avatar_id if field == 'avatar' else user.cover_id
    assert db.session.get(File, image_id).source_url == f'https://{CANONICAL}' + PATH.format('users')

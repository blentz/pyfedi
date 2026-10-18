"""API and script configuration follow the storefront (onion storefront spec §3.4, Task 4c).

Identifiers a client may federate or compare (`ap_id`, `actor_id`) stay on the canonical origin. Addresses a client
shows or fetches (media, the site icon, the OpenAPI server) and the notification stream a page opens follow the
host the request named.
"""
import logging
from datetime import timedelta
from unittest.mock import patch

import pytest
from flask import current_app, g

from app import cache, create_app, db
from app.models import File, Language, Site, SiteDomain
from app.storefront import clear_domain_cache
from tests.conftest import TestConfig
from app.utils import utcnow
from tests.factories import make_community, make_community_member, make_instance, make_post, make_user

ONION = 'abc.onion'
CANONICAL = 'test.piefed.local'
MEDIA = 'app/static/media/posts/ab/cd/abcdefghijklmno.jpg'
THUMB = 'app/static/media/posts/ab/cd/abcdefghijklmno_t.webp'

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture
def onion(db_session):
    if SiteDomain.query.filter_by(domain=CANONICAL).first() is None:   # the canonical host stays served
        db.session.add(SiteDomain(domain=CANONICAL, scheme='https', is_default=True))
    db.session.add(SiteDomain(domain=ONION, scheme='http', federates=False))
    db.session.commit()
    clear_domain_cache()
    cache.clear()
    yield ONION
    clear_domain_cache()


@pytest.fixture
def restores_the_shared_logger():
    logger = logging.getLogger('app')
    before = list(logger.handlers)
    yield
    for handler in list(logger.handlers):
        if handler not in before:
            logger.removeHandler(handler)
            handler.close()


def get_json(client, path, host):
    scheme = 'http' if host == ONION else 'https'
    response = client.get(path, base_url=f'{scheme}://{host}')
    assert response.status_code == 200, (path, host, response.status_code)
    return response.get_json()


@pytest.mark.parametrize('host, origin', [(ONION, f'http://{ONION}'), (CANONICAL, f'https://{CANONICAL}')])
def test_openapi_servers_follow_the_storefront(onion, restores_the_shared_logger, host, origin):
    docs = create_app(type('DocsConfig', (TestConfig,), {'SERVE_API_DOCS': True}))
    servers = get_json(docs.test_client(), '/api/alpha/swagger.json', host)['servers']
    assert servers[0] == {'url': origin, 'description': 'This instance'}
    assert [server['url'] for server in servers[1:]] == ['https://piefed.social', 'https://crust.piefed.social']
    # the shared spec was not edited: the next request names its own storefront
    other = ONION if host == CANONICAL else CANONICAL
    other_origin = f'http://{ONION}' if other == ONION else f'https://{CANONICAL}'
    assert get_json(docs.test_client(), '/api/alpha/swagger.json', other)['servers'][0]['url'] == other_origin


@pytest.fixture
def api(app, onion, api_baseline, monkeypatch):
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    monkeypatch.setitem(current_app.config, 'ENABLE_ALPHA_API', 'true')
    for code, name in [('en', 'English'), ('und', 'Undetermined')]:
        db.session.add(Language(code=code, name=name))
    db.session.commit()
    db.session.get(Site, 1).language_id = Language.query.filter_by(code='en').one().id
    community = make_community('picturesque')
    author = api_baseline.user2
    make_community_member(author, community)
    image = File(file_path=MEDIA, thumbnail_path=THUMB,
                 source_url=f'https://{CANONICAL}/static/media/posts/ab/cd/abcdefghijklmno.jpg')
    db.session.add(image)
    db.session.commit()
    post = make_post(community, author, f'https://{CANONICAL}/post/1', title='a picture')
    post.type = 3   # POST_TYPE_IMAGE
    post.image_id = image.id
    db.session.commit()
    return post


def test_api_post_view_on_storefront(app, api):
    body = get_json(app.test_client(), f'/api/alpha/post?id={api.id}', ONION)['post_view']['post']
    assert body['ap_id'] == f'https://{CANONICAL}/post/1'
    for field in ('url', 'thumbnail_url', 'small_thumbnail_url'):
        assert body[field].startswith(f'http://{ONION}/static/media/'), (field, body[field])


@pytest.mark.parametrize('host, origin', [(ONION, f'http://{ONION}'), (CANONICAL, f'https://{CANONICAL}')])
def test_site_view_icon_follows_storefront_and_actor_id_stays_canonical(app, onion, monkeypatch, host, origin):
    monkeypatch.setitem(current_app.config, 'ENABLE_ALPHA_API', 'true')
    g.site = db.session.get(Site, 1)
    body = get_json(app.test_client(), '/api/alpha/site', host)['site']
    assert body['icon'] == f'{origin}/static/images/piefed_logo_icon_t_75.png'
    assert body['actor_id'] == f'https://{CANONICAL}/'


def test_instance_chooser_logo_follows_storefront(app, onion, monkeypatch):
    monkeypatch.setitem(current_app.config, 'ENABLE_ALPHA_API', 'true')
    g.site = db.session.get(Site, 1)
    with patch('app.api.alpha.routes.get_setting', return_value=True):
        body = get_json(app.test_client(), '/api/alpha/site/instance_chooser', ONION)
    assert body['logo_url'] == f'http://{ONION}/static/images/piefed_logo_icon_t_75.png'


@pytest.mark.parametrize('configured, expected', [
    (f'https://{CANONICAL}', f'http://{ONION}'),           # this instance's own origin follows the storefront
    ('https://stream.example', 'https://stream.example'),  # a separate stream host is not ours to move
])
def test_stream_urls_relative_or_storefront(app, onion, configured, expected):
    user = make_user(make_instance(CANONICAL, software='piefed'), 'streamer', local=True)
    client = app.test_client()
    with client.session_transaction(base_url=f'http://{ONION}') as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True
    g.pop('_login_user', None)
    with patch.dict(app.config, {'NOTIF_SERVER': configured}):
        text = client.get('/', base_url=f'http://{ONION}').get_data(as_text=True)
    assert f'{expected}/notifications/stream?user_id=' in text
    if expected != configured:
        assert f'{configured}/notifications/stream' not in text


def test_add_local_hint_names_the_storefront(app, onion):
    user = make_user(make_instance(CANONICAL, software='piefed'), 'maker', local=True)
    user.verified = True
    user.private_key = 'k'
    user.created = utcnow() - timedelta(days=30)
    db.session.commit()
    client = app.test_client()
    with client.session_transaction(base_url=f'http://{ONION}') as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True
    g.pop('_login_user', None)
    with patch.dict(app.config, {'WTF_CSRF_ENABLED': True}):   # the form renders its token
        response = client.get('/community/add_local', base_url=f'http://{ONION}')
    assert response.status_code == 200, response.headers.get('Location')
    assert f'Only people using {ONION} can post or reply' in response.get_data(as_text=True)

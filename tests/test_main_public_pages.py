"""The public pages this instance serves to anyone: robots.txt, security.txt,
the sitemap, the licensing file, the PWA manifest, the service worker, the
instance actor, and the static pages beside them.

Sub-project 86, slice A -- `app/main/routes.py`'s public surface. These are
the endpoints a crawler, a fediverse peer or a browser reaches without an
account, and none of them had a test.
"""
import json

import pytest
from flask import current_app, g

from app import db
from app.constants import POST_STATUS_REVIEWING
from app.models import CmsPage, Instance, Site
from tests.factories import make_community, make_post, make_user


@pytest.fixture
def env(app, api_baseline):
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    return SimpleNamespace(client=app.test_client(), site=g.site,
                           reader=api_baseline.user3,
                           baseline=api_baseline)


class TestWhatCrawlersAskFor:
    def test_robots_txt(self, env):
        response = env.client.get('/robots.txt')
        assert response.status_code == 200
        assert response.mimetype == 'text/plain'

    def test_robots_txt_names_the_licence_file_when_ai_is_not_welcome(
            self, env, monkeypatch):
        """`use_rsl` is `not ALLOW_AI_CRAWLERS`, and the template points
        crawlers at /rsl.xml when it is set."""
        monkeypatch.setitem(current_app.config, 'ALLOW_AI_CRAWLERS', False)
        with_licence = env.client.get('/robots.txt').get_data(as_text=True)
        monkeypatch.setitem(current_app.config, 'ALLOW_AI_CRAWLERS', True)
        without = env.client.get('/robots.txt').get_data(as_text=True)
        assert with_licence != without

    def test_security_txt(self, env):
        response = env.client.get('/.well-known/security.txt')
        assert response.status_code == 200
        assert response.mimetype == 'text/plain'

    def test_the_sitemap(self, env):
        # `Site.private_instance` defaults to True, and a private instance
        # publishes no sitemap (R222).
        env.site.private_instance = False
        community = make_community('probeland')
        post = make_post(community, env.reader,
                         ap_id='https://test.piefed.local/y/1')
        post.instance_id = 1
        post.indexable = True
        db.session.commit()
        response = env.client.get('/sitemap.xml')
        assert response.status_code == 200
        assert response.mimetype == 'text/xml'

    def test_the_licence_file_is_served_when_ai_is_not_welcome(
            self, env, monkeypatch):
        monkeypatch.setitem(current_app.config, 'ALLOW_AI_CRAWLERS', False)
        response = env.client.get('/rsl.xml')
        assert response.status_code == 200
        assert response.mimetype == 'text/xml'

    def test_the_licence_file_is_absent_when_ai_is_welcome(self, env,
                                                           monkeypatch):
        monkeypatch.setitem(current_app.config, 'ALLOW_AI_CRAWLERS', True)
        assert env.client.get('/rsl.xml').status_code == 404


class TestWhatBrowsersAskFor:
    def test_the_service_worker(self, env):
        response = env.client.get('/service_worker.js')
        assert response.status_code == 200
        assert 'max-age=86400' in response.headers['Cache-Control']

    @pytest.mark.parametrize('path', ['/manifest.json',
                                      '/static/manifest.json'])
    def test_the_manifest(self, env, path):
        response = env.client.get(path)
        assert response.status_code == 200
        assert json.loads(response.get_data(as_text=True))['name']

    def test_the_manifest_for_an_ios_browser(self, env):
        """The manifest is built per platform: iOS gets a different one."""
        response = env.client.get('/manifest.json', headers={
            'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac '
                          'OS X) AppleWebKit/605.1.15'})
        assert response.status_code == 200
        assert json.loads(response.get_data(as_text=True))['name']

    def test_the_keyboard_shortcuts_page(self, env):
        assert env.client.get('/keyboard_shortcuts').status_code == 200


class TestTheStaticPages:
    def test_the_about_page(self, env):
        response = env.client.get('/about')
        assert response.status_code == 200

    def test_the_about_page_can_be_replaced_by_a_cms_page(self, env):
        db.session.add(CmsPage(url='/about', title='About us',
                               body='<p>hello</p>', body_html='<p>hello</p>'))
        db.session.commit()
        response = env.client.get('/about')
        assert response.status_code == 200

    def test_the_privacy_page(self, env):
        response = env.client.get('/privacy')
        assert response.status_code == 200

    def test_a_cms_privacy_page_is_served_instead(self, env):
        db.session.add(CmsPage(url='/privacy', title='Privacy',
                               body='ours', body_html='<p>ours</p>'))
        db.session.commit()
        response = env.client.get('/privacy')
        assert response.status_code == 200
        assert 'ours' in response.get_data(as_text=True)

    def test_the_login_link_redirects_to_the_real_one(self, env):
        response = env.client.get('/login')
        assert response.status_code == 302
        assert '/auth/login' in response.headers['Location']


class TestTheInstanceActor:
    def test_the_actor(self, env):
        response = env.client.get('/actor')
        assert response.status_code == 200
        assert response.content_type == 'application/activity+json'
        actor = response.get_json()
        assert actor['type'] == 'Application'
        assert actor['id'].endswith('/actor')
        assert actor['publicKey']['owner'] == actor['id']
        assert actor['endpoints']['sharedInbox'].endswith('/inbox')

    def test_the_actor_carries_the_instance_key(self, env):
        env.site.public_key = 'a public key'
        db.session.commit()
        actor = env.client.get('/actor').get_json()
        assert actor['publicKey']['publicKeyPem'] == 'a public key'


class TestTheDiagnosticEndpoints:
    """Every `/test_*` route is `@debug_mode_only`, which is a 403 in
    production. These rows are what says so."""

    @pytest.mark.parametrize('path', [
        '/test', '/test_email', '/test_redis', '/test_ip', '/test_s3',
        '/test_hashing', '/test_ldap', '/test_ldap_login',
        '/test_libretranslate',
    ])
    def test_a_diagnostic_endpoint_is_closed_in_production(self, env, path):
        assert env.client.get(path).status_code == 403

    @pytest.mark.parametrize('path', ['/find_voters', '/replay_inbox'])
    def test_an_endpoint_that_needs_an_account(self, env, path):
        response = env.client.get(path)
        assert response.status_code in (302, 401, 403)

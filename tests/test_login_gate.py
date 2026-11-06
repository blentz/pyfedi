"""On a storefront that requires login, a logged-out request is refused unless its endpoint is listed (spec M11, plan S1-S3).

The table of leaks in docs/superpowers/plans/2026-11-06-monetization-phase-3.md is what this was written against.
"""
import re

import pytest

from app import db
from app.login_gate import FEDERATION, OPEN
from app.models import Site
from tests.factories import make_community, make_instance, make_post, make_site, make_user

SECRET = 'SECRET TITLE'
SIDEBAR = 'SECRET SIDEBAR'


@pytest.fixture
def private(app, db_session, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setitem(app.config, 'ENABLE_ALPHA_API', 'true')
    make_site()
    site = db.session.get(Site, 1)
    site.private_instance = True
    make_instance('test.piefed.local', software='piefed')
    peer = make_instance('peer.example', software='lemmy')
    author = make_user(peer, 'author')
    author.ap_profile_id = 'https://peer.example/u/author'
    author.ap_domain = 'peer.example'
    community = make_community('general')
    community.private = False
    community.description_html = f'<p>{SIDEBAR}</p>'
    post = make_post(community, author, 'https://peer.example/p/1', title=SECRET)
    db.session.commit()
    return SimpleNamespace(site=site, community=community, post=post)


def filled_in(rule) -> str:
    """A URL for the rule, with something plausible in each argument."""
    def value(match):
        converter = match.group(1) or ''
        if converter.startswith('int'):
            return '1'
        if converter.startswith('any('):
            return re.search(r'any\((\w+)', converter).group(1)
        return 'x'
    return re.sub(r'<(?:([^:<>]+(?:\([^)]*\))?):)?\w+>', value, rule.rule)


class TestTheLeaksItWasWrittenFor:
    @pytest.mark.parametrize('url', ['/api/alpha/post/list', '/api/alpha/post?id=1', '/api/alpha/search?q=SECRET&type_=Posts',
                                     '/api/alpha/community/list', '/api/alpha/community?name=general'],
                             ids=['post-list', 'post', 'search', 'community-list', 'community'])
    def test_the_api_refuses_a_logged_out_caller(self, app, private, url):
        response = app.test_client().get(url)

        assert response.status_code == 401
        assert response.get_json() == {'error': 'incorrect_login'}
        assert SECRET not in response.get_data(as_text=True)

    def test_a_token_opens_the_api(self, app, private):
        member = make_user(None, 'member', local=True)
        member.verified = True
        db.session.commit()

        response = app.test_client().get('/api/alpha/post/list',
                                         headers={'Authorization': f'Bearer {member.encode_jwt_token()}'})

        assert response.status_code == 200
        assert SECRET in response.get_data(as_text=True)

    def test_a_bad_token_opens_nothing(self, app, private):
        response = app.test_client().get('/api/alpha/post/list', headers={'Authorization': 'Bearer nonsense'})

        assert response.status_code == 401

    def test_the_sidebar_redirects_to_login(self, app, private):
        response = app.test_client().get(f'/community/get_sidebar/{private.community.id}')

        assert response.status_code == 302
        assert '/auth/login' in response.headers['Location']

    def test_an_activitypub_header_opens_no_page(self, app, private):
        """Anybody can send the header. A view that isn't federation's answered it as if it were."""
        response = app.test_client().get('/instance/peer.example/posts', headers={'Accept': 'application/activity+json'})

        assert response.status_code == 302
        assert SECRET not in response.get_data(as_text=True)


class TestWhatStaysOpen:
    def test_an_app_can_still_find_out_how_to_log_in(self, app, private):
        assert app.test_client().get('/api/alpha/site').status_code == 200

    def test_the_login_page_and_about_page_stay_open(self, app, private, monkeypatch):
        monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', True)
        client = app.test_client()

        assert client.get('/auth/login').status_code == 200
        assert client.get('/about').status_code == 200

    def test_a_menu_fetched_by_htmx_gets_nothing_to_swap_in(self, app, private):
        response = app.test_client().get('/communities_menu', headers={'HX-Request': 'true'})

        assert response.status_code == 401
        assert response.get_data() == b''

    def test_a_federating_instance_still_answers_other_servers(self, app, private):
        client = app.test_client()

        assert client.get('/.well-known/nodeinfo').status_code == 200
        assert client.get('/', headers={'Accept': 'application/activity+json'}).status_code == 200

    def test_a_feed_keeps_its_404(self, app, private):
        assert app.test_client().get('/index/feed').status_code == 404

    def test_a_cms_page_is_not_refused(self, app, private):
        """No route means no endpoint, and the 404 handler is what serves a CMS page."""
        assert app.test_client().get('/no/such/page').status_code == 404

    def test_a_member_gets_through(self, app, private):
        member = make_user(None, 'member', local=True)
        client = app.test_client()
        with client.session_transaction() as session:
            session['_user_id'] = str(member.id)
            session['_fresh'] = True

        assert client.get(f'/community/get_sidebar/{private.community.id}').status_code == 200


class TestAPublicInstance:
    def test_it_refuses_nothing(self, app, private):
        private.site.private_instance = False
        db.session.commit()

        assert app.test_client().get('/api/alpha/post/list').status_code == 200


class TestEveryRoute:
    def test_every_listed_endpoint_exists(self, app):
        endpoints = {rule.endpoint for rule in app.url_map.iter_rules()}

        assert OPEN - endpoints == set()
        assert FEDERATION - endpoints == set()

    def test_every_other_route_refuses_a_logged_out_request(self, app, private):
        """Walks the URL map, every method. A new route passes this by being refused, unless somebody lists it above."""
        client = app.test_client()
        served = []
        for rule in app.url_map.iter_rules():
            if rule.endpoint in OPEN or rule.endpoint in FEDERATION or rule.endpoint.startswith('activitypub.'):
                continue
            for method in sorted(rule.methods - {'OPTIONS', 'HEAD'}):
                status = client.open(filled_in(rule), method=method).status_code
                if status not in (302, 401, 404):
                    served.append((rule.rule, method, status))

        assert served == []

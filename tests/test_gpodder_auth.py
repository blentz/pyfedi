"""gpodder.net sync's login (plan W4a; spec docs/superpowers/specs/2026-11-12-gpodder-sync-design.md §3, ruling G1):
an app password is a token of a named app, made in settings and shown once, of the scope `gpodder` alone; the apps
log in with it by HTTP Basic, and AntennaPod carries the session cookie its login sets."""
import base64
import re

import pytest

import app as app_pkg
from app import db
from app.models import OAuthToken, Site
from app.oauth_scopes import GPODDER_SCOPE, SCOPES
from app.gpodder import auth as gpodder_auth
from app.oauth_server import tokens
from tests.factories import make_user
from tests.test_admin_routes_entry import csrf, login

PAGE = '/user/settings/apps'
NEW = '/user/settings/apps/passwords'


@pytest.fixture
def world(app, api_baseline):
    alice = make_user(None, 'gpalice', local=True)
    alice.set_password('the account password')
    bob = make_user(None, 'gpbob', local=True)
    db.session.commit()
    password = tokens.issue_app_password(alice, 'Phone')
    return {'alice': alice, 'bob': bob, 'password': password, 'client': app.test_client(), 'flask': app}


def _basic(name: str, password: str) -> dict:
    return {'Authorization': 'Basic ' + base64.b64encode(f'{name}:{password}'.encode()).decode()}


def _cookie(world):
    return world['client'].get_cookie('sessionid', domain=world['flask'].config['SERVER_NAME'], path='/api/2/')


def _login(world, name='gpalice', password=None, path_name=None):
    return world['client'].post(f'/api/2/auth/{path_name or name}/login.json',
                                headers=_basic(name, password or world['password']))


def test_an_app_password_is_a_token_of_its_own_app_and_scope_with_no_refresh(world):
    token = tokens.token_for(world['password'])
    assert token.user_id == world['alice'].id and token.scope == GPODDER_SCOPE and token.refresh_token_hash is None
    assert token.app.name == 'Phone' and token.app.redirect_uri_list() == []


def test_no_oauth_client_may_ask_for_the_gpodder_scope(world):
    assert GPODDER_SCOPE not in SCOPES
    response = world['client'].post('/api/v1/apps', data={'client_name': 'A', 'scopes': GPODDER_SCOPE,
                                                          'redirect_uris': 'urn:ietf:wg:oauth:2.0:oob'})
    assert response.status_code == 422


def test_an_app_password_is_no_bearer_token(world):
    response = world['client'].get('/api/v1/accounts/verify_credentials',
                                    headers={'Authorization': f"Bearer {world['password']}"})
    assert response.status_code == 403


def test_login_with_an_app_password_sets_the_session_cookie(world):
    response = _login(world)
    assert response.status_code == 200, response.get_data(as_text=True)
    cookie = _cookie(world)
    assert cookie is not None and cookie.http_only


def test_the_name_is_matched_whatever_its_case(world):
    assert _login(world, name='GPAlice').status_code == 200


def test_the_session_cookie_signs_in_what_follows(world):
    _login(world)
    assert world['client'].post('/api/2/auth/gpalice/logout.json').status_code == 200
    assert _cookie(world) is None


def test_basic_signs_in_any_call_without_a_login_first(world):
    response = world['client'].post('/api/2/auth/gpalice/logout.json', headers=_basic('gpalice', world['password']))
    assert response.status_code == 200


@pytest.mark.parametrize('case', ['account-password', 'wrong', 'someone-elses-name', 'nobody', 'no-credentials'])
def test_a_login_that_does_not_check_out_is_refused_with_a_basic_challenge(world, case):
    if case == 'account-password':
        response = _login(world, password='the account password')
    elif case == 'wrong':
        response = _login(world, password='not it')
    elif case == 'someone-elses-name':
        response = _login(world, name='gpbob')
    elif case == 'nobody':
        response = _login(world, name='nobody-here')
    else:
        response = world['client'].post('/api/2/auth/gpalice/login.json')
    assert response.status_code == 401
    assert response.headers['WWW-Authenticate'].startswith('Basic')
    assert _cookie(world) is None


def test_the_path_must_name_the_person_signed_in(world):
    assert _login(world, path_name='gpbob').status_code == 401
    _login(world)
    assert world['client'].post('/api/2/auth/gpbob/logout.json').status_code == 401


def test_revoking_the_app_ends_its_password_and_its_session(world):
    _login(world)
    token = tokens.token_for(world['password'])
    tokens.revoke_app_for(world['alice'], token.app_id)
    assert world['client'].post('/api/2/auth/gpalice/logout.json').status_code == 401
    assert _login(world).status_code == 401


def test_a_wrong_password_beside_a_good_session_is_refused(world):
    _login(world)
    assert _login(world, password='not it').status_code == 401


def test_a_forged_session_cookie_is_refused(world):
    world['client'].set_cookie('sessionid', 'not-signed', domain=world['flask'].config['SERVER_NAME'], path='/api/2/')
    assert world['client'].post('/api/2/auth/gpalice/logout.json').status_code == 401


def test_a_person_who_cannot_sign_in_cannot_sync(world):
    world['alice'].banned = True
    db.session.commit()
    assert _login(world).status_code == 401


def test_a_private_instance_leaves_sync_to_its_own_login(world):
    db.session.get(Site, 1).private_instance = True
    db.session.commit()
    assert _login(world).status_code == 200
    assert world['client'].post('/api/2/auth/gpbob/login.json').status_code == 401


def test_an_unknown_gpodder_call_is_not_found(world):
    assert world['client'].get('/api/2/tags/10.json', headers=_basic('gpalice', world['password'])).status_code == 404


class TestSettings:
    @pytest.fixture
    def signed_in(self, world):
        login(world['client'], world['alice'])
        return world

    def test_the_page_offers_a_new_app_password(self, signed_in):
        page = signed_in['client'].get(PAGE).get_data(as_text=True)
        assert NEW in page and 'name="name"' in page

    def test_a_new_app_password_is_shown_once_and_listed_as_an_app(self, signed_in):
        token = csrf(signed_in['flask'], signed_in['client'])
        response = signed_in['client'].post(NEW, data={'csrf_token': token, 'name': '  Kasts on my laptop  '})
        assert response.status_code == 200
        page = response.get_data(as_text=True)
        made = OAuthToken.query.filter_by(user_id=signed_in['alice'].id).order_by(OAuthToken.id.desc()).first()
        assert made.scope == GPODDER_SCOPE and made.app.name == 'Kasts on my laptop'
        shown = [word for word in re.findall(r'[\w-]{30,}', page) if tokens.token_for(word) is not None]
        assert shown, 'the new password is on the page'
        assert response.headers['Cache-Control'] == 'no-store'
        again = signed_in['client'].get(PAGE).get_data(as_text=True)
        assert 'Kasts on my laptop' in again and shown[0] not in again

    @pytest.mark.parametrize('name', ['', '   ', 'x' * 51], ids=['empty', 'blank', 'too-long'])
    def test_a_name_is_needed_and_short(self, signed_in, name):
        before = OAuthToken.query.count()
        token = csrf(signed_in['flask'], signed_in['client'])
        response = signed_in['client'].post(NEW, data={'csrf_token': token, 'name': name})
        assert response.status_code == 400 and OAuthToken.query.count() == before

    def test_making_one_needs_the_csrf_token(self, signed_in):
        before = OAuthToken.query.count()
        response = signed_in['client'].post(NEW, data={'name': 'Phone'})
        assert response.status_code in (400, 403) and OAuthToken.query.count() == before

    def test_making_one_needs_a_signed_in_person(self, world):
        response = world['client'].post(NEW, data={'name': 'Phone'})
        assert response.status_code == 302 and '/auth/login' in response.headers['Location']


def _signed(world, value) -> str:
    with world['flask'].test_request_context():
        return gpodder_auth._serializer().dumps(value)


def _with_cookie(world, value):
    world['client'].set_cookie('sessionid', value, domain=world['flask'].config['SERVER_NAME'], path='/api/2/')
    return world['client'].post('/api/2/auth/gpalice/logout.json')


@pytest.mark.parametrize('header', ['Bearer {password}', 'Basic ' + base64.b64encode(b'gpalice:').decode(),
                                    'Basic ' + base64.b64encode(b':{password}').decode()],
                         ids=['bearer', 'no-password', 'no-name'])
def test_only_basic_with_a_name_and_password_signs_in(world, header):
    response = world['client'].post('/api/2/auth/gpalice/login.json',
                                    headers={'Authorization': header.format(password=world['password'])})
    assert response.status_code == 401


def test_a_signed_cookie_must_name_an_app_password(world):
    registered, _secret = tokens.register_app('Tusky', ['https://app.example/cb'], 'read write', None)
    bearer, _refresh = tokens.issue_token(registered, world['alice'], 'read write')
    assert _with_cookie(world, _signed(world, tokens.token_for(bearer).id)).status_code == 401
    assert _with_cookie(world, _signed(world, 'not an id')).status_code == 401
    assert _with_cookie(world, _signed(world, tokens.token_for(world['password']).id)).status_code == 200


def test_an_app_password_is_this_domains_alone(world):
    _login(world)
    token = tokens.token_for(world['password'])
    token.domain = 'elsewhere.example'
    db.session.commit()
    assert world['client'].post('/api/2/auth/gpalice/logout.json').status_code == 401
    assert _login(world).status_code == 401


class TestFailureLimit:
    """Failed sign-ins per address are limited, checked before a password is tried, so it cannot be guessed rapidly;
    good calls, which Kasts and gPodder make with Basic on every request, are never counted."""

    @pytest.fixture
    def few(self, world, monkeypatch):
        monkeypatch.setattr(gpodder_auth, 'MAX_FAILURES', 3)
        return world

    def test_too_many_failures_refuse_even_the_right_password(self, few):
        for _ in range(3):
            assert _login(few, password='a guess').status_code == 401
        response = _login(few)
        assert response.status_code == 429 and int(response.headers['Retry-After']) > 0

    def test_failures_against_one_account_lock_out_no_other_from_the_address(self, few):
        """Onion clients without a session share one address key, as people behind one NAT share an address: the
        count is per account named, so guessing at bob locks out bob alone, not alice beside him."""
        bob_password = tokens.issue_app_password(few['bob'], 'Phone')
        for _ in range(3):
            assert _login(few, name='gpbob', password='a guess').status_code == 401
        assert _login(few, name='gpbob', password=bob_password).status_code == 429
        assert _login(few).status_code == 200

    def test_good_sign_ins_are_not_counted(self, few):
        for _ in range(5):
            assert _login(few).status_code == 200
        assert _login(few, password='a guess').status_code == 401
        assert _login(few).status_code == 200

    def test_a_failure_count_expires(self, few):
        _login(few, password='a guess')
        keys = app_pkg.redis_client.keys('gpodder_login_fail:*')
        assert len(keys) == 1 and 0 < app_pkg.redis_client.ttl(keys[0]) <= gpodder_auth.FAILURE_WINDOW

    def test_the_session_cookie_is_limited_as_basic_is(self, few):
        few['client'].set_cookie('sessionid', 'not-signed', domain=few['flask'].config['SERVER_NAME'], path='/api/2/')
        for _ in range(3):
            assert few['client'].post('/api/2/auth/gpalice/logout.json').status_code == 401
        assert few['client'].post('/api/2/auth/gpalice/logout.json').status_code == 429

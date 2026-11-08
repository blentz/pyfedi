"""GET and POST /oauth/authorize: the person signs in here, sees what the app asks for, and allows or denies it
(plan W0.2 step 3; spec docs/superpowers/specs/2026-11-07-mastodon-client-api-design.md §3.1, §5)."""
from base64 import urlsafe_b64encode
from hashlib import sha256
from urllib.parse import parse_qs, urlsplit

import pytest

from app.models import OAuthCode
from app.oauth_server import tokens
from tests.factories import make_user
from tests.test_admin_routes_entry import csrf, login

CALLBACK = 'https://app.example/cb'
OOB = 'urn:ietf:wg:oauth:2.0:oob'
VERIFIER = 'a' * 50
CHALLENGE = urlsafe_b64encode(sha256(VERIFIER.encode()).digest()).rstrip(b'=').decode()


@pytest.fixture
def world(app, api_baseline):
    registered, _secret = tokens.register_app('Phanpy', [CALLBACK, OOB], 'read write follow push',
                                              'https://phanpy.social')
    user = make_user(None, 'oauthconsent', local=True)
    client = app.test_client()
    return {'app': registered, 'user': user, 'client': client, 'flask': app}


def _query(world, **overrides):
    query = {'response_type': 'code', 'client_id': world['app'].client_id, 'redirect_uri': CALLBACK,
             'scope': 'read write', 'state': 'xyz'}
    query.update(overrides)
    return {name: value for name, value in query.items() if value is not None}


def _signed_in(world):
    login(world['client'], world['user'])
    return world['client']


def _approve(world, approve=True, **overrides):
    client = _signed_in(world)
    token = csrf(world['flask'], client)
    field = 'approve' if approve else 'deny'
    return client.post('/oauth/authorize', query_string=_query(world, **overrides),
                       data={'csrf_token': token, field: 'y'})


def _location_query(response):
    return parse_qs(urlsplit(response.headers['Location']).query)


def test_a_person_not_signed_in_is_sent_to_log_in_and_back(world):
    response = world['client'].get('/oauth/authorize', query_string=_query(world))
    assert response.status_code == 302
    location = urlsplit(response.headers['Location'])
    assert location.path == '/auth/login'
    back = parse_qs(location.query)['next'][0]
    assert back.startswith('/oauth/authorize') or '/oauth/authorize?' in back
    assert world['app'].client_id in back


def test_the_consent_page_names_the_app_and_what_it_asks_for(world):
    response = _signed_in(world).get('/oauth/authorize', query_string=_query(world))
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert 'Phanpy' in page and 'https://phanpy.social' in page and 'oauthconsent' in page
    assert 'name="csrf_token"' in page
    assert 'read' in page and 'write' in page
    assert response.headers['X-Frame-Options'] == 'DENY'
    assert "frame-ancestors 'none'" in response.headers['Content-Security-Policy']


@pytest.mark.parametrize('overrides', [{'client_id': 'nobody'}, {'client_id': None},
                                       {'redirect_uri': 'https://evil.example/cb'},
                                       {'redirect_uri': CALLBACK + '/more'}, {'redirect_uri': None}],
                         ids=['unknown-app', 'no-app', 'other-redirect', 'longer-redirect', 'no-redirect-of-two'])
def test_an_app_or_redirect_that_does_not_check_out_is_never_redirected_to(world, overrides):
    """No open redirect: until the app and its redirect are known good, the error is shown here."""
    response = _signed_in(world).get('/oauth/authorize', query_string=_query(world, **overrides))
    assert response.status_code == 400
    assert 'Location' not in response.headers


@pytest.mark.parametrize('overrides, error', [
    ({'scope': 'read admin:read'}, 'invalid_scope'),
    ({'scope': 'read:statuses write:media'}, None),
    ({'response_type': 'token'}, 'unsupported_response_type'),
    ({'code_challenge': CHALLENGE, 'code_challenge_method': 'plain'}, 'invalid_request'),
], ids=['unknown-scope', 'narrower-scopes', 'implicit-flow', 'plain-pkce'])
def test_a_bad_request_from_a_known_app_goes_back_to_it_with_the_error(world, overrides, error):
    response = _signed_in(world).get('/oauth/authorize', query_string=_query(world, **overrides))
    if error is None:
        assert response.status_code == 200
        return
    assert response.status_code == 302
    assert response.headers['Location'].startswith(CALLBACK + '?')
    query = _location_query(response)
    assert query['error'] == [error] and query['state'] == ['xyz']


def test_a_scope_the_app_did_not_register_is_refused(world):
    narrow, _secret = tokens.register_app('Reader', [CALLBACK], 'read', None)
    response = _signed_in(world).get('/oauth/authorize',
                                     query_string=_query(world, client_id=narrow.client_id, scope='read write'))
    assert _location_query(response)['error'] == ['invalid_scope']


def test_an_app_with_one_redirect_may_leave_it_out(world):
    single, _secret = tokens.register_app('Single', [CALLBACK], 'read', None)
    response = _signed_in(world).get('/oauth/authorize',
                                     query_string=_query(world, client_id=single.client_id, redirect_uri=None,
                                                         scope='read'))
    assert response.status_code == 200


def test_allowing_sends_the_app_a_code_for_this_person(world):
    response = _approve(world)
    assert response.status_code == 302
    assert response.headers['Location'].startswith(CALLBACK + '?')
    query = _location_query(response)
    assert query['state'] == ['xyz']
    code = OAuthCode.query.one()
    assert code.code_hash == tokens.digest(query['code'][0])
    assert code.user_id == world['user'].id and code.app_id == world['app'].id
    assert code.scope == 'read write' and code.redirect_uri == CALLBACK
    assert code.code_challenge is None


def test_a_pkce_challenge_is_kept_with_the_code(world):
    _approve(world, code_challenge=CHALLENGE, code_challenge_method='S256')
    code = OAuthCode.query.one()
    assert code.code_challenge == CHALLENGE and code.code_challenge_method == 'S256'


def test_no_scope_asked_for_means_read(world):
    _approve(world, scope=None)
    assert OAuthCode.query.one().scope == 'read'


def test_denying_tells_the_app_and_makes_no_code(world):
    response = _approve(world, approve=False)
    assert response.status_code == 302
    query = _location_query(response)
    assert query['error'] == ['access_denied'] and query['state'] == ['xyz']
    assert OAuthCode.query.count() == 0


def test_a_post_without_the_csrf_token_makes_no_code(world):
    client = _signed_in(world)
    response = client.post('/oauth/authorize', query_string=_query(world), data={'approve': 'y'})
    assert response.status_code == 400
    assert OAuthCode.query.count() == 0


def test_an_oob_app_is_shown_the_code_to_copy(world):
    response = _approve(world, redirect_uri=OOB)
    assert response.status_code == 200
    assert 'Location' not in response.headers
    code = OAuthCode.query.one()
    page = response.get_data(as_text=True)
    shown = [word for word in page.replace('<', ' ').replace('>', ' ').split() if tokens.digest(word) == code.code_hash]
    assert shown, 'the code is on the page'
    assert response.headers['X-Frame-Options'] == 'DENY'


def test_an_oob_app_that_is_denied_is_told_here(world):
    response = _approve(world, approve=False, redirect_uri=OOB)
    assert response.status_code == 200
    assert OAuthCode.query.count() == 0
    assert 'denied' in response.get_data(as_text=True).lower()

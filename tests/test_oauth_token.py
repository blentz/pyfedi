"""POST /oauth/token: an app trades a code, a refresh token or its own credentials for an access token (plan W0.2
step 4; spec docs/superpowers/specs/2026-11-07-mastodon-client-api-design.md §3.1, §5)."""
from base64 import b64encode, urlsafe_b64encode
from hashlib import sha256

import pytest

from app import db
from app.models import OAuthCode, OAuthToken
from app.oauth_server import server, tokens
from tests.factories import make_user
from tests.test_login_gate import private  # noqa: F401  the private-instance fixture

CALLBACK = 'https://app.example/cb'
VERIFIER = 'v' * 60
CHALLENGE = urlsafe_b64encode(sha256(VERIFIER.encode()).digest()).rstrip(b'=').decode()


@pytest.fixture
def world(app, api_baseline):
    registered, secret = tokens.register_app('Tusky', [CALLBACK], 'read write follow push', None)
    user = make_user(None, 'oauthtoken', local=True)
    return {'app': registered, 'secret': secret, 'user': user, 'client': app.test_client()}


def _code(world, scope='read write', redirect_uri=CALLBACK, challenge=None, user=None):
    return tokens.issue_code(world['app'], user or world['user'], redirect_uri, scope, challenge,
                             'S256' if challenge else None)


def _exchange(world, the_code, **fields):
    data = {'grant_type': 'authorization_code', 'code': the_code, 'redirect_uri': CALLBACK,
            'client_id': world['app'].client_id, 'client_secret': world['secret']}
    data.update(fields)
    return world['client'].post('/oauth/token', data={name: value for name, value in data.items() if value is not None})


def test_a_code_becomes_a_token_as_mastodon_answers(world):
    response = _exchange(world, _code(world))
    assert response.status_code == 200, response.get_data(as_text=True)
    body = response.get_json()
    assert body['token_type'] == 'Bearer' and body['scope'] == 'read write'
    assert isinstance(body['created_at'], int)
    assert 'expires_in' not in body
    stored = tokens.token_for(body['access_token'])
    assert stored.user_id == world['user'].id and stored.app_id == world['app'].id and stored.scope == 'read write'
    assert tokens.refresh_token_for(body['refresh_token']) is stored
    assert OAuthCode.query.count() == 0


def test_the_app_may_authenticate_with_basic(world):
    basic = b64encode(f"{world['app'].client_id}:{world['secret']}".encode()).decode()
    response = world['client'].post('/oauth/token', headers={'Authorization': f'Basic {basic}'},
                                    data={'grant_type': 'authorization_code', 'code': _code(world),
                                          'redirect_uri': CALLBACK})
    assert response.status_code == 200, response.get_data(as_text=True)


def test_the_request_may_be_json(world):
    response = world['client'].post('/oauth/token', json={
        'grant_type': 'authorization_code', 'code': _code(world), 'redirect_uri': CALLBACK,
        'client_id': world['app'].client_id, 'client_secret': world['secret']})
    assert response.status_code == 200, response.get_data(as_text=True)


def test_a_json_body_that_is_not_an_object_is_refused(world):
    response = world['client'].post('/oauth/token', json=['authorization_code'])
    assert response.status_code == 400


def test_a_code_works_once(world):
    code = _code(world)
    assert _exchange(world, code).status_code == 200
    again = _exchange(world, code)
    assert again.status_code == 400 and again.get_json()['error'] == 'invalid_grant'


@pytest.mark.parametrize('fields, status, error', [
    ({'client_secret': 'wrong'}, 401, 'invalid_client'),
    ({'client_id': 'nobody'}, 400, 'invalid_client'),   # 401 is for a client that tried Basic (RFC 6749 5.2)
    ({'redirect_uri': 'https://app.example/other'}, 400, 'invalid_grant'),
    ({'code': 'not-a-code'}, 400, 'invalid_grant'),
    ({'grant_type': 'password', 'username': 'oauthtoken', 'password': 'x'}, 400, 'unsupported_grant_type'),
], ids=['wrong-secret', 'unknown-app', 'other-redirect', 'unknown-code', 'password-grant'])
def test_a_bad_exchange_is_refused(world, fields, status, error):
    response = _exchange(world, _code(world), **fields)
    assert response.status_code == status, response.get_data(as_text=True)
    assert response.get_json()['error'] == error
    assert OAuthToken.query.count() == 0


def test_another_apps_code_is_refused(world):
    other, other_secret = tokens.register_app('Other', [CALLBACK], 'read', None)
    response = _exchange(world, _code(world), client_id=other.client_id, client_secret=other_secret)
    assert response.get_json()['error'] == 'invalid_grant'


def test_an_expired_code_is_refused(world, monkeypatch):
    code = _code(world)
    monkeypatch.setattr(tokens, 'CODE_LIFETIME_SECONDS', -1)
    assert _exchange(world, code).get_json()['error'] == 'invalid_grant'


def test_a_code_asked_for_without_a_redirect_is_exchanged_without_one(world):
    response = _exchange(world, _code(world, redirect_uri=''), redirect_uri=None)
    assert response.status_code == 200, response.get_data(as_text=True)


@pytest.mark.parametrize('verifier, error', [(None, 'invalid_request'), ('w' * 60, 'invalid_grant')],
                         ids=['no-verifier', 'wrong-verifier'])
def test_a_pkce_code_needs_its_verifier(world, verifier, error):
    response = _exchange(world, _code(world, challenge=CHALLENGE), code_verifier=verifier)
    assert response.get_json()['error'] == error


def test_a_pkce_code_with_its_verifier_is_exchanged(world):
    response = _exchange(world, _code(world, challenge=CHALLENGE), code_verifier=VERIFIER)
    assert response.status_code == 200, response.get_data(as_text=True)


def test_an_app_without_its_secret_must_use_pkce(world):
    """A public client: PKCE stands in for the secret (RFC 9700)."""
    assert _exchange(world, _code(world, challenge=CHALLENGE), client_secret=None,
                     code_verifier=VERIFIER).status_code == 200
    refused = _exchange(world, _code(world), client_secret=None)
    assert refused.status_code == 400 and refused.get_json()['error'] == 'invalid_request'


@pytest.mark.parametrize('change', ['banned', 'deleted', 'unverified'])
def test_no_token_for_a_person_who_can_no_longer_sign_in(world, change):
    code = _code(world)
    user = world['user']
    if change == 'unverified':
        user.verified = False
    else:
        setattr(user, change, True)
    db.session.commit()
    response = _exchange(world, code)
    assert response.get_json()['error'] == 'invalid_grant'
    assert OAuthToken.query.count() == 0


def _refresh(world, refresh_token, **fields):
    data = {'grant_type': 'refresh_token', 'refresh_token': refresh_token, 'client_id': world['app'].client_id,
            'client_secret': world['secret']}
    data.update(fields)
    return world['client'].post('/oauth/token', data={name: value for name, value in data.items() if value is not None})


def test_a_refresh_token_gives_a_new_pair_and_ends_the_old(world):
    first = _exchange(world, _code(world)).get_json()
    response = _refresh(world, first['refresh_token'])
    assert response.status_code == 200, response.get_data(as_text=True)
    second = response.get_json()
    assert second['access_token'] != first['access_token'] and second['refresh_token'] != first['refresh_token']
    assert second['scope'] == 'read write'
    assert tokens.token_for(first['access_token']) is None
    assert tokens.token_for(second['access_token']).user_id == world['user'].id
    assert _refresh(world, first['refresh_token']).get_json()['error'] == 'invalid_grant'


def test_a_refresh_token_is_the_apps_own(world):
    first = _exchange(world, _code(world)).get_json()
    other, other_secret = tokens.register_app('Other', [CALLBACK], 'read', None)
    response = _refresh(world, first['refresh_token'], client_id=other.client_id, client_secret=other_secret)
    assert response.get_json()['error'] == 'invalid_grant'


def test_no_refresh_for_a_banned_person(world):
    first = _exchange(world, _code(world)).get_json()
    world['user'].banned = True
    db.session.commit()
    assert _refresh(world, first['refresh_token']).get_json()['error'] == 'invalid_grant'


def _app_token(world, **fields):
    data = {'grant_type': 'client_credentials', 'client_id': world['app'].client_id, 'client_secret': world['secret']}
    data.update(fields)
    return world['client'].post('/oauth/token', data={name: value for name, value in data.items() if value is not None})


def test_an_app_gets_a_read_only_token_of_its_own(world):
    response = _app_token(world)
    assert response.status_code == 200, response.get_data(as_text=True)
    body = response.get_json()
    assert body['scope'] == 'read' and 'refresh_token' not in body
    stored = tokens.token_for(body['access_token'])
    assert stored.user_id is None and stored.app_id == world['app'].id


@pytest.mark.parametrize('fields, error', [({'scope': 'read write'}, 'invalid_scope'),
                                           ({'client_secret': None}, 'invalid_client')],
                         ids=['write', 'no-secret'])
def test_an_apps_own_token_is_read_only_and_needs_its_secret(world, fields, error):
    response = _app_token(world, **fields)
    assert response.get_json()['error'] == error
    assert OAuthToken.query.count() == 0


def test_tokens_are_issued_on_a_private_instance(app, private):
    registered, secret = tokens.register_app('Tusky', [CALLBACK], 'read', None)
    response = app.test_client().post('/oauth/token', data={
        'grant_type': 'client_credentials', 'client_id': registered.client_id, 'client_secret': secret})
    assert response.status_code == 200, response.get_data(as_text=True)


def test_a_json_field_that_is_not_a_string_is_left_out(world):
    response = world['client'].post('/oauth/token', json={
        'grant_type': 'authorization_code', 'code': _code(world), 'redirect_uri': CALLBACK,
        'client_id': world['app'].client_id, 'client_secret': world['secret'], 'extra': 5})
    assert response.status_code == 200, response.get_data(as_text=True)


def test_a_refresh_needs_the_apps_secret(world):
    """Every app here has one; a stolen refresh token is no use without it."""
    first = _exchange(world, _code(world)).get_json()
    response = _refresh(world, first['refresh_token'], client_secret=None)
    assert response.status_code in (400, 401) and response.get_json()['error'] == 'invalid_client'
    assert tokens.token_for(first['access_token']) is not None


def test_two_refreshes_at_once_give_one_new_pair(world, monkeypatch):
    """The other request claims the old token after this one checked it: this one gets nothing."""
    first = _exchange(world, _code(world)).get_json()
    checked = server.RefreshTokenGrant.validate_token_request

    def checked_then_lost_the_race(grant):
        checked(grant)
        tokens.claim(tokens.refresh_token_for(first['refresh_token']))

    monkeypatch.setattr(server.RefreshTokenGrant, 'validate_token_request', checked_then_lost_the_race)
    response = _refresh(world, first['refresh_token'])
    assert response.status_code == 400 and response.get_json()['error'] == 'invalid_grant'
    assert OAuthToken.query.count() == 1


def test_a_refresh_refused_for_its_scope_keeps_the_old_token(world):
    first = _exchange(world, _code(world, scope='read')).get_json()
    response = _refresh(world, first['refresh_token'], scope='read write')
    assert response.get_json()['error'] == 'invalid_scope'
    assert tokens.token_for(first['access_token']) is not None

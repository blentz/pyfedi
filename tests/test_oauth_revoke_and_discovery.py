"""POST /oauth/revoke ends a token (RFC 7009); GET /.well-known/oauth-authorization-server says where everything is
(RFC 8414), which Mastodon 4.3+ apps read (plan W0.2 step 5; spec 2026-11-07-mastodon-client-api-design §3.1)."""
import pytest

from app.oauth_scopes import SCOPES
from app.oauth_server import tokens
from tests.factories import make_user
from tests.test_login_gate import private  # noqa: F401  the private-instance fixture


@pytest.fixture
def world(app, api_baseline):
    registered, secret = tokens.register_app('Tusky', ['https://app.example/cb'], 'read write', None)
    user = make_user(None, 'oauthrevoke', local=True)
    access, refresh = tokens.issue_token(registered, user, 'read write')
    return {'app': registered, 'secret': secret, 'access': access, 'refresh': refresh, 'client': app.test_client()}


def _revoke(world, token, **fields):
    data = {'token': token, 'client_id': world['app'].client_id, 'client_secret': world['secret']}
    data.update(fields)
    return world['client'].post('/oauth/revoke', data={name: value for name, value in data.items() if value is not None})


@pytest.mark.parametrize('which, hint', [('access', None), ('access', 'access_token'), ('refresh', None),
                                         ('refresh', 'refresh_token')],
                         ids=['access', 'access-hinted', 'refresh', 'refresh-hinted'])
def test_revoking_either_token_ends_both(world, which, hint):
    response = _revoke(world, world[which], token_type_hint=hint)
    assert response.status_code == 200 and response.get_json() == {}
    assert tokens.token_for(world['access']) is None
    assert tokens.refresh_token_for(world['refresh']) is None


def test_revoking_a_token_nobody_knows_is_not_an_error(world):
    """RFC 7009 2.2: the client cannot do anything about it."""
    assert _revoke(world, 'nothing-like-a-token').status_code == 200
    assert tokens.token_for(world['access']) is not None


def test_an_app_cannot_revoke_another_apps_token(world):
    other, other_secret = tokens.register_app('Other', ['https://other.example/cb'], 'read', None)
    response = _revoke(world, world['access'], client_id=other.client_id, client_secret=other_secret)
    assert response.status_code == 400
    assert tokens.token_for(world['access']) is not None


def test_revoking_needs_the_apps_credentials(world):
    assert _revoke(world, world['access'], client_secret='wrong').status_code == 401
    assert tokens.token_for(world['access']) is not None


def test_revoking_works_on_a_private_instance(app, private):
    registered, secret = tokens.register_app('Tusky', ['https://app.example/cb'], 'read', None)
    access, _refresh = tokens.issue_token(registered, None, 'read', with_refresh=False)
    response = app.test_client().post('/oauth/revoke', data={'token': access, 'client_id': registered.client_id,
                                                              'client_secret': secret})
    assert response.status_code == 200
    assert tokens.token_for(access) is None


def test_the_metadata_says_where_everything_is(app, api_baseline):
    response = app.test_client().get('/.well-known/oauth-authorization-server')
    assert response.status_code == 200
    body = response.get_json()
    base = app.config['SERVER_URL']
    assert body['issuer'] == base + '/'
    assert body['authorization_endpoint'] == base + '/oauth/authorize'
    assert body['token_endpoint'] == base + '/oauth/token'
    assert body['revocation_endpoint'] == base + '/oauth/revoke'
    assert body['app_registration_endpoint'] == base + '/api/v1/apps'
    assert set(body['scopes_supported']) == SCOPES
    assert body['response_types_supported'] == ['code']
    assert body['response_modes_supported'] == ['query']
    assert body['code_challenge_methods_supported'] == ['S256']
    assert set(body['grant_types_supported']) == {'authorization_code', 'refresh_token', 'client_credentials'}
    assert set(body['token_endpoint_auth_methods_supported']) == {'client_secret_basic', 'client_secret_post', 'none'}


def test_the_metadata_is_open_on_a_private_instance(app, private):
    assert app.test_client().get('/.well-known/oauth-authorization-server').status_code == 200

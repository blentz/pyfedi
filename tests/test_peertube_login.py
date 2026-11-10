"""PeerTube's login (plan W2.2a; spec 2026-11-09-peertube-login-design, rulings P1, P2): a domain's local client, the
same on every ask; the password grant for that client only, with the API login's checks; refusals in PeerTube's
problem+json with the `code` its apps read; refresh and logout."""
import pytest
from flask import abort as flask_abort
from werkzeug.exceptions import Forbidden

from app import db, limiter
from app.models import OAuthToken, SiteDomain
from app.oauth_server import tokens
from app.peertube import auth as peertube_auth
from app.storefront import clear_domain_cache
from tests.factories import make_user
from tests import peertube_app_contract as contract
from tests import test_login_gate as gate_tests
from tests import test_onion_guard_limits as limit_tests

private = gate_tests.private   # the private-instance fixture
limited_app = limit_tests.limited_app   # an app whose limiter counts

PASSWORD = 'a-good-password'


@pytest.fixture
def world(app, api_baseline):
    person = make_user(None, 'ptlogin', local=True)
    person.set_password(PASSWORD)
    db.session.commit()
    return {'person': person, 'client': app.test_client(), 'flask': app}


def _local(client, base_url=None):
    kwargs = {'base_url': base_url} if base_url else {}
    response = client.get('/api/v1/oauth-clients/local', **kwargs)
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()


def _token(client, local, username='ptlogin', password=PASSWORD, base_url=None, **extra):
    kwargs = {'base_url': base_url} if base_url else {}
    return client.post('/api/v1/users/token', data={'client_id': local['client_id'],
                                                    'client_secret': local['client_secret'], 'grant_type': 'password',
                                                    'username': username, 'password': password, **extra}, **kwargs)


def _refused(response, code, status=400):
    assert response.status_code == status, response.get_data(as_text=True)
    assert response.mimetype == 'application/problem+json'
    body = response.get_json()
    assert body['code'] == code and body['status'] == status and body['detail'] and body['type'].endswith(code)
    assert 'access_token' not in body


def test_the_local_client_is_the_same_on_every_ask_and_its_secret_works(world):
    first, again = _local(world['client']), _local(world['client'])
    assert first == again and first['client_id'].startswith('peertube-local-') and first['client_secret']
    registered = tokens.OAuthApp.query.filter_by(client_id=first['client_id']).one()
    assert registered.check_client_secret(first['client_secret'])


def test_each_domain_has_its_own_local_client(world):
    db.session.add(SiteDomain(domain='ptlogin.example', scheme='https', federates=True))
    db.session.commit()
    clear_domain_cache()
    here, there = _local(world['client']), _local(world['client'], base_url='https://ptlogin.example')
    assert here['client_id'] != there['client_id'] and here['client_secret'] != there['client_secret']


def test_a_local_client_made_under_an_old_secret_key_takes_the_new_one(world, monkeypatch):
    before = _local(world['client'])
    monkeypatch.setitem(world['flask'].config, 'SECRET_KEY', 'a-new-secret-key')
    after = _local(world['client'])
    assert after['client_id'] != before['client_id'] or after['client_secret'] != before['client_secret']
    assert _token(world['client'], after).status_code == 200


def test_config_has_every_field_peertubes_app_requires(world):
    """Its add-instance screen decodes the whole config, and refuses an API level below 7.2.0 (D4)."""
    body = world['client'].get('/api/v1/config').get_json()
    assert contract.problems(body, contract.CONFIG) == []
    assert tuple(int(part) for part in body['serverVersion'].split('.')) >= (7, 2, 0)


def test_the_password_grant_gives_a_bearer_pair_that_never_expires_and_acts_for_the_person(world):
    response = _token(world['client'], _local(world['client']))
    assert response.status_code == 200, response.get_data(as_text=True)
    body = response.get_json()
    assert contract.problems(body, contract.TOKEN) == [] and body['token_type'] == 'Bearer'

    assert 'expires_in' not in body   # P2: never expiring, as M6 rules for every app
    stored = tokens.token_for(body['access_token'])
    assert stored.user_id == world['person'].id and stored.scope == tokens.LOCAL_CLIENT_SCOPE


def test_a_scope_asked_for_within_the_clients_is_the_one_given(world):
    response = _token(world['client'], _local(world['client']), scope='read')
    assert tokens.token_for(response.get_json()['access_token']).scope == 'read'


@pytest.mark.parametrize('username, password', [('ptlogin', 'wrong-password'), ('nobody-here', PASSWORD)])
def test_a_wrong_password_and_an_unknown_name_are_the_same_invalid_grant(world, username, password):
    _refused(_token(world['client'], _local(world['client']), username, password), 'invalid_grant')
    assert OAuthToken.query.filter_by(user_id=world['person'].id).count() == 0


@pytest.mark.parametrize('field', ['username', 'password'])
def test_a_missing_name_or_password_is_an_invalid_request(world, field):
    _refused(_token(world['client'], _local(world['client']), **{field: ''}), 'invalid_request')


@pytest.mark.parametrize('state', ['banned', 'unverified', 'deleted'])
def test_a_person_who_cannot_sign_in_gets_no_token(world, state):
    person = world['person']
    if state == 'banned':
        person.banned = True
    elif state == 'unverified':
        person.verified = False
    else:
        person.deleted = True
    db.session.commit()
    _refused(_token(world['client'], _local(world['client'])), 'invalid_grant')


def test_another_app_may_not_use_the_password_grant(world):
    registered, secret = tokens.register_app('Tusky', ['https://app.example/cb'], 'read write follow', None)
    other = {'client_id': registered.client_id, 'client_secret': secret}
    _refused(_token(world['client'], other), 'unauthorized_client')


def test_another_domains_local_client_may_not_log_in_here(world):
    db.session.add(SiteDomain(domain='ptother.example', scheme='https', federates=True))
    db.session.commit()
    clear_domain_cache()
    theirs = _local(world['client'], base_url='https://ptother.example')
    _refused(_token(world['client'], theirs), 'unauthorized_client')


def test_a_wrong_client_secret_is_invalid_client(world):
    local = _local(world['client'])
    _refused(_token(world['client'], {**local, 'client_secret': 'not-it'}), 'invalid_client')


def test_refresh_ignores_the_apps_extra_response_type_and_rotates_the_pair(world):
    local = _local(world['client'])
    first = _token(world['client'], local).get_json()
    response = world['client'].post('/api/v1/users/token', data={
        'client_id': local['client_id'], 'client_secret': local['client_secret'], 'grant_type': 'refresh_token',
        'refresh_token': first['refresh_token'], 'response_type': 'code'})
    assert response.status_code == 200, response.get_data(as_text=True)
    second = response.get_json()
    assert second['token_type'] == 'Bearer' and second['refresh_token'] != first['refresh_token']
    assert tokens.token_for(first['access_token']) is None and tokens.token_for(second['access_token']) is not None
    again = world['client'].post('/api/v1/users/token', data={
        'client_id': local['client_id'], 'client_secret': local['client_secret'], 'grant_type': 'refresh_token',
        'refresh_token': first['refresh_token']})
    _refused(again, 'invalid_grant')   # the official app's cue to log in again


def test_logout_revokes_the_token(world):
    access = _token(world['client'], _local(world['client'])).get_json()['access_token']
    response = world['client'].post('/api/v1/users/revoke-token', headers={'Authorization': f'Bearer {access}'})
    assert response.status_code == 200
    assert tokens.token_for(access) is None


def test_an_app_can_log_in_on_a_private_instance(app, private):
    person = make_user(None, 'ptprivate', local=True)
    person.set_password(PASSWORD)
    db.session.commit()
    client = app.test_client()
    assert client.get('/api/v1/config').status_code == 200   # the app reads it to add the instance
    assert _token(client, _local(client), 'ptprivate').status_code == 200


def test_password_guesses_are_limited_as_the_api_login_is(limited_app, api_baseline):
    with limited_app.app_context():
        person = make_user(None, 'ptlimited', local=True)
        person.set_password(PASSWORD)
        db.session.commit()
        client = limited_app.test_client()
        local = _local(client)
        limiter.reset()
        limiter.enabled = True
        try:
            codes = [_token(client, local, 'ptlimited', 'wrong').status_code for _ in range(21)]
            last = _token(client, local, 'ptlimited')
        finally:
            limiter.reset()
            limiter.enabled = False
    assert codes[:20] == [400] * 20 and codes[20] == 429
    _refused(last, 'too_many_requests', 429)


@pytest.mark.parametrize('how', ['json-body', 'query-string', 'oauth-token-endpoint'])
def test_no_form_of_the_request_and_no_other_endpoint_escapes_the_limit(limited_app, api_baseline, how):
    """The limit is the password grant's own: Authlib also reads a JSON body or the query, and /oauth/token reaches
    the same grant, so a limit put on one route for one form could be walked round (security review 2026-11-09)."""
    with limited_app.app_context():
        person = make_user(None, f'ptlimit{how.replace("-", "")}', local=True)
        person.set_password(PASSWORD)
        db.session.commit()
        client = limited_app.test_client()
        local = _local(client)
        fields = {'client_id': local['client_id'], 'client_secret': local['client_secret'], 'grant_type': 'password',
                  'username': person.user_name, 'password': 'wrong'}
        limiter.reset()
        limiter.enabled = True
        try:
            if how == 'json-body':
                codes = [client.post('/api/v1/users/token', json=fields).status_code for _ in range(21)]
            elif how == 'query-string':
                codes = [client.post('/api/v1/users/token', query_string=fields).status_code for _ in range(21)]
            else:
                codes = [client.post('/oauth/token', data=fields).status_code for _ in range(21)]
        finally:
            limiter.reset()
            limiter.enabled = False
    assert 429 in codes[:21] and codes.index(429) == 20, codes


def test_two_first_asks_at_once_share_one_local_client(world, monkeypatch):
    """The second to insert finds the first's row: a unique client_id refuses its own."""
    made, secret = tokens.local_client()
    monkeypatch.setattr(type(tokens.OAuthApp.query), 'first', lambda self: None)   # as if it had not been made yet
    again, again_secret = tokens.local_client()
    assert again.id == made.id and again_secret == secret


def test_a_local_client_whose_secret_changed_is_given_the_new_one(world, monkeypatch):
    made, _secret = tokens.local_client()
    derived = tokens._derived
    monkeypatch.setattr(tokens, '_derived', lambda purpose: 'a-new-secret' if purpose == 'peertube-client-secret'
                        else derived(purpose))
    again, secret = tokens.local_client()
    assert again.id == made.id and secret == 'a-new-secret' and again.check_client_secret('a-new-secret')


def test_the_onion_gates_wait_is_a_429_in_peertubes_shape(world, monkeypatch):
    def wait(username, password):
        flask_abort(429)
    monkeypatch.setattr('app.oauth_server.server.log_in_with_password', wait)
    _refused(_token(world['client'], _local(world['client'])), 'too_many_requests', 429)


def test_another_http_refusal_of_the_token_endpoint_is_passed_through(world, monkeypatch):
    def forbidden():
        raise Forbidden()
    monkeypatch.setattr(peertube_auth.server, 'create_token_response', forbidden)
    assert _token(world['client'], _local(world['client'])).status_code == 403

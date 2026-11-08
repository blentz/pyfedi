"""An API route takes an OAuth token only for what its scopes allow, and only while its person can sign in; on a
private instance the login gate lets a person's token through, not an app's own (plan W0.2 step 6; spec
docs/superpowers/specs/2026-11-07-mastodon-client-api-design.md §3.1, §5)."""
from datetime import timedelta

import pytest
from flask import g

from app import db
from app.models import utcnow
from app.oauth_server import tokens
from app.oauth_server.protect import require_oauth
from tests.factories import make_user
from tests.test_login_gate import private  # noqa: F401  the private-instance fixture


@pytest.fixture
def world(app, api_baseline):
    registered, _secret = tokens.register_app('Tusky', ['https://app.example/cb'], 'read write follow', None)
    user = make_user(None, 'oauthprotect', local=True)
    return {'app': registered, 'user': user, 'flask': app}


def _call(world, view, token):
    headers = {'Authorization': f'Bearer {token}'} if token is not None else {}
    with world['flask'].test_request_context('/api/v1/test', headers=headers):
        world['flask'].preprocess_request()   # the before_request hooks: the site and the storefront
        result = view()
        return result if isinstance(result, tuple) else (result, 200)


def _person_token(world, scope='read write'):
    access, _refresh = tokens.issue_token(world['app'], world['user'], scope)
    return access


@require_oauth('read:statuses')
def _reader():
    return {'user': g.oauth_user.id, 'token': g.oauth_token.id}


@require_oauth('write:statuses')
def _writer():
    return {'user': g.oauth_user.id}


@require_oauth('read', user_required=False)
def _app_reader():
    return {'user': g.oauth_user.id if g.oauth_user else None}


def test_a_token_with_the_scope_gets_in_as_its_person(world):
    body, status = _call(world, _reader, _person_token(world))
    assert status == 200 and body['user'] == world['user'].id


def test_a_token_without_the_scope_is_refused(world):
    body, status = _call(world, _writer, _person_token(world, scope='read'))
    assert status == 403
    assert body.get_json()['error'] == 'This action is outside the authorized scopes'


@pytest.mark.parametrize('token', [None, '', 'not-a-token'], ids=['no-header', 'empty', 'unknown'])
def test_no_valid_token_is_refused(world, token):
    body, status = _call(world, _reader, token)
    assert status == 401
    assert body.get_json()['error'] == 'The access token is invalid'
    assert 'Bearer' in body.headers['WWW-Authenticate']


def test_a_header_that_is_not_bearer_is_refused(world):
    with world['flask'].test_request_context('/api/v1/test', headers={'Authorization': 'Basic abc'}):
        response, status = _reader()
    assert status == 401


def test_a_revoked_token_is_refused(world):
    access = _person_token(world)
    tokens.revoke(tokens.token_for(access))
    assert _call(world, _reader, access)[1] == 401


@pytest.mark.parametrize('change', ['banned', 'deleted', 'password'])
def test_a_token_stops_working_when_its_person_cannot_sign_in(world, change):
    access = _person_token(world)
    if change == 'password':
        world['user'].password_updated_at = utcnow() + timedelta(seconds=5)
    else:
        setattr(world['user'], change, True)
    db.session.commit()
    assert _call(world, _reader, access)[1] == 401


def test_an_apps_own_token_is_refused_where_a_person_is_needed(world):
    access, _none = tokens.issue_token(world['app'], None, 'read', with_refresh=False)
    body, status = _call(world, _reader, access)
    assert status == 422
    assert body.get_json()['error'] == 'This method requires an authenticated user'


def test_an_apps_own_token_gets_in_where_none_is_needed(world):
    access, _none = tokens.issue_token(world['app'], None, 'read', with_refresh=False)
    body, status = _call(world, _app_reader, access)
    assert status == 200 and body['user'] is None


def test_use_is_recorded_at_most_once_an_hour(world):
    access = _person_token(world)
    token = tokens.token_for(access)
    assert token.last_used_at is None
    _call(world, _reader, access)
    first = tokens.token_for(access).last_used_at
    assert first is not None
    _call(world, _reader, access)
    assert tokens.token_for(access).last_used_at == first
    token = tokens.token_for(access)
    token.last_used_at = utcnow() - timedelta(hours=2)
    db.session.commit()
    _call(world, _reader, access)
    assert tokens.token_for(access).last_used_at > utcnow() - timedelta(minutes=1)


def _private_world(app):
    registered, _secret = tokens.register_app('Tusky', ['https://app.example/cb'], 'read', None)
    user = make_user(None, 'oauthmember', local=True)
    return registered, user


def test_an_oauth_route_keeps_its_own_login_gate():
    """The login gate lets it check the token itself, scope included (app.login_gate._has_own_gate)."""
    assert _reader.has_own_login_gate is True


def test_on_a_private_instance_a_persons_token_gets_in(app, private):
    registered, user = _private_world(app)
    access, _refresh = tokens.issue_token(registered, user, 'read')
    with app.test_request_context('/api/v1/test', headers={'Authorization': f'Bearer {access}'}):
        app.preprocess_request()
        assert isinstance(_app_reader(), dict)


def test_on_a_private_instance_an_apps_own_token_gets_nothing(app, private):
    registered, _user = _private_world(app)
    access, _none = tokens.issue_token(registered, None, 'read', with_refresh=False)
    with app.test_request_context('/api/v1/test', headers={'Authorization': f'Bearer {access}'}):
        app.preprocess_request()
        response, status = _app_reader()
    assert status == 401


@pytest.mark.parametrize('scope', ['read', 'push'])
def test_an_oauth_token_does_not_open_the_alpha_api_of_a_private_instance(app, private, scope):
    """The alpha API does not take OAuth tokens: one would get in as nobody, whatever its scope."""
    registered, user = _private_world(app)
    access, _refresh = tokens.issue_token(registered, user, scope)
    response = app.test_client().get('/api/alpha/post/list', headers={'Authorization': f'Bearer {access}'})
    assert response.status_code == 401

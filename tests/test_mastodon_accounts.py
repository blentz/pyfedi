"""Mastodon's accounts for a client app: verify_credentials, accounts/:id and accounts/lookup (plan W1.1; spec
docs/superpowers/specs/2026-11-07-mastodon-client-api-design.md §3.2; the fields Tusky requires are in plan W1.0)."""
import re

import pytest

from app import db
from app.models import UserExtraField
from app.oauth_server import tokens
from tests.factories import make_instance, make_user
from tests.test_login_gate import private  # noqa: F401  the private-instance fixture

RFC3339 = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$')
TUSKY_REQUIRED = ('id', 'username', 'acct', 'created_at', 'note', 'url', 'avatar', 'header')


@pytest.fixture
def world(app, api_baseline):
    registered, _secret = tokens.register_app('Tusky', ['https://app.example/cb'], 'read write follow push', None)
    me = make_user(None, 'mastome', local=True)
    me.title = 'Me Myself'
    me.about = 'Hello **there**'
    me.about_html = '<p>Hello <strong>there</strong></p>'
    me.extra_fields.append(UserExtraField(label='Site', text='https://me.example'))
    remote = make_user(make_instance('peer.example'), 'farfaraway')
    remote.ap_id = 'farfaraway@peer.example'
    remote.ap_domain = 'peer.example'
    remote.ap_profile_id = 'https://peer.example/u/farfaraway'
    db.session.commit()
    access, _refresh = tokens.issue_token(registered, me, 'read write follow')
    return {'app': registered, 'me': me, 'remote': remote, 'token': access, 'client': app.test_client(),
            'flask': app}


def _get(world, path, token=True, **query):
    headers = {'Authorization': f"Bearer {world['token'] if token is True else token}"} if token else {}
    return world['client'].get(path, headers=headers, query_string=query)


def test_verify_credentials_is_the_signed_in_account_with_its_source(world):
    response = _get(world, '/api/v1/accounts/verify_credentials')
    assert response.status_code == 200, response.get_data(as_text=True)
    account = response.get_json()
    for field in TUSKY_REQUIRED:
        assert account[field] is not None, field
    me = world['me']
    assert account['id'] == str(me.id) and account['username'] == 'mastome' and account['acct'] == 'mastome'
    assert account['display_name'] == 'Me Myself'
    assert account['note'] == '<p>Hello <strong>there</strong></p>'
    assert RFC3339.match(account['created_at'])
    assert account['url'].startswith('http') and account['avatar'].startswith('http')
    assert account['header'].startswith('http') and account['avatar_static'] == account['avatar']
    assert account['bot'] is False and account['locked'] is False and account['group'] is False
    assert account['emojis'] == [] and account['roles'] == []
    assert account['fields'] == [{'name': 'Site', 'value': 'https://me.example', 'verified_at': None}]
    assert account['source']['note'] == 'Hello **there**'
    assert account['source']['fields'] == [{'name': 'Site', 'value': 'https://me.example', 'verified_at': None}]
    assert account['source']['privacy'] == 'public' and account['source']['sensitive'] is False
    assert isinstance(account['followers_count'], int) and isinstance(account['statuses_count'], int)


def test_verify_credentials_needs_a_persons_token_that_reads_accounts(world):
    assert _get(world, '/api/v1/accounts/verify_credentials', token=None).status_code == 401
    app_token, _none = tokens.issue_token(world['app'], None, 'read', with_refresh=False)
    assert _get(world, '/api/v1/accounts/verify_credentials', token=app_token).status_code == 422
    writer, _refresh = tokens.issue_token(world['app'], world['me'], 'write')
    assert _get(world, '/api/v1/accounts/verify_credentials', token=writer).status_code == 403
    narrow, _refresh = tokens.issue_token(world['app'], world['me'], 'read:accounts')
    assert _get(world, '/api/v1/accounts/verify_credentials', token=narrow).status_code == 200


def test_an_account_by_id_has_no_source(world):
    response = _get(world, f"/api/v1/accounts/{world['remote'].id}", token=None)
    assert response.status_code == 200, response.get_data(as_text=True)
    account = response.get_json()
    assert account['acct'] == 'farfaraway@peer.example' and account['username'] == 'farfaraway'
    assert account['uri'] == 'https://peer.example/u/farfaraway'
    assert 'source' not in account


@pytest.mark.parametrize('account_id', ['999999', '99999999999999999999'], ids=['nobody', 'huge'])
def test_an_account_nobody_holds_is_not_found(world, account_id):
    response = _get(world, f'/api/v1/accounts/{account_id}')
    assert response.status_code == 404
    assert response.get_json() == {'error': 'Record not found'}


def test_an_id_that_is_not_a_number_is_not_found(world):
    """No route takes it: the ids handed out are numbers."""
    assert _get(world, '/api/v1/accounts/abc').status_code == 404


def test_a_deleted_account_is_not_found(world):
    world['remote'].deleted = True
    db.session.commit()
    assert _get(world, f"/api/v1/accounts/{world['remote'].id}").status_code == 404


@pytest.mark.parametrize('acct, name', [('mastome', 'mastome'), ('@mastome', 'mastome'),
                                        ('farfaraway@peer.example', 'farfaraway'),
                                        ('@FarFarAway@Peer.Example', 'farfaraway')],
                         ids=['local', 'local-at', 'remote', 'remote-case'])
def test_lookup_finds_an_account_by_its_acct(world, acct, name):
    response = _get(world, '/api/v1/accounts/lookup', token=None, acct=acct)
    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json()['username'] == name


@pytest.mark.parametrize('acct', ['nobody', 'nobody@peer.example', '', 'a@b@c'], ids=['local', 'remote', 'empty',
                                                                                    'two-ats'])
def test_lookup_of_nobody_is_not_found(world, acct):
    assert _get(world, '/api/v1/accounts/lookup', acct=acct).status_code == 404


def test_a_bad_token_is_refused_even_where_none_is_needed(world):
    assert _get(world, f"/api/v1/accounts/{world['me'].id}", token='not-a-token').status_code == 401


def test_on_a_private_instance_accounts_need_a_persons_token(app, private):
    registered, _secret = tokens.register_app('Tusky', ['https://app.example/cb'], 'read', None)
    member = make_user(None, 'mastomember', local=True)
    access, _refresh = tokens.issue_token(registered, member, 'read')
    client = app.test_client()
    assert client.get(f'/api/v1/accounts/{member.id}').status_code == 401
    response = client.get(f'/api/v1/accounts/{member.id}', headers={'Authorization': f'Bearer {access}'})
    assert response.status_code == 200, response.get_data(as_text=True)

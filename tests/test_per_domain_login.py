"""A login name means the account on the storefront's own domain, or the hosted domain a full handle names (plan B task 3)."""
import logging
from types import SimpleNamespace
from urllib.parse import urlsplit
from unittest.mock import patch

import pytest
from flask import g

from app import db
from app.auth.util import find_user, split_handle
from app.mentions import mentioned_users_found_by, stored_user
from app.models import SiteDomain, User
from app.nntp.nntpserver import NNTPAuthenticationError
from app.nntp.server import _authenticate
from app.shared.auth import api_log_user_in
from app.storefront import clear_domain_cache, default_domain, domain_by_id
from app.utils import user_name_is_taken
from tests.factories import make_community, make_instance, make_user
from tests.test_passkeys import _passkey, authenticated
from tests.test_storefront_api import ONION

pytestmark = pytest.mark.usefixtures('site')

CLEARNET = {'REMOTE_ADDR': '203.0.113.9'}
ONION_URL = f'http://{ONION}'
HELL = 'test.piefed.local'
LOGIN = {'timezone': 'UTC', 'submit': 'Log In'}


@pytest.fixture
def world(db_session, monkeypatch):
    """The default domain and an onion, and one `wakko` on each with its own password. The second row is inserted
    straight in: creation refuses a name any domain holds until plan C, but the database allows it."""
    monkeypatch.setattr('app.auth.routes.render_login_form', lambda form: 'the login form')
    monkeypatch.setattr('app.auth.routes.onion_proof_refused', lambda form: False)
    local = make_instance(HELL, software='piefed')
    make_user(local, 'burnseat', local=True)   # id 1 is exempt from every ban check
    hell = db.session.query(SiteDomain).filter_by(domain=HELL).first()
    if hell is None:
        hell = SiteDomain(domain=HELL, scheme='https', is_default=True)
        db.session.add(hell)
    onion = SiteDomain(domain=ONION, scheme='http', federates=False)
    db.session.add(onion)
    db.session.commit()
    clear_domain_cache()
    accounts = {}
    for domain, password in ((hell, 'hell password'), (onion, 'onion password')):
        user = make_user(local, 'wakko', local=True) if domain is hell else User(
            user_name='wakko', email='wakko-onion@example.com', instance_id=local.id, verified=True, banned=False)
        if domain is hell:
            user.email = 'wakko-hell@example.com'
        user.site_domain_id = domain.id
        user.verified = True
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        accounts[domain.domain] = user
    yield SimpleNamespace(hell=accounts[HELL], onion=accounts[ONION], hell_domain=hell, onion_domain=onion)
    clear_domain_cache()


def flashes(client):
    with client.session_transaction() as session:
        return list(session.get('_flashes', []))


def login(app, typed, password, base_url=None, client=None):
    g.pop('_login_user', None)   # the test's app context outlives a request, and a login would stay signed in
    return (client or app.test_client()).post('/auth/login', data={'user_name': typed, 'password': password, **LOGIN},
                                  base_url=base_url, environ_base=CLEARNET)


def logged_in(response):
    return response.status_code == 302 and urlsplit(response.headers['Location']).path != '/auth/login'


def failures(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith('auth: login failed')]


def test_split_handle(app, world):
    onion = domain_by_id(world.onion_domain.id)
    assert split_handle('wakko', onion) == ('wakko', onion)
    assert split_handle(' @wakko ', onion) == ('wakko', onion)
    assert split_handle(f'wakko@{HELL}', onion) == ('wakko', default_domain())
    assert split_handle('wakko@nowhere.example', onion) == ('wakko', None)


def test_a_bare_name_on_the_onion_is_the_onion_account(app, world):
    with patch('app.auth.util.sync_user_to_ldap'):
        assert logged_in(login(app, 'wakko', 'onion password', ONION_URL))
        assert not logged_in(login(app, 'wakko', 'hell password', ONION_URL))
        assert logged_in(login(app, 'wakko', 'hell password'))
        assert not logged_in(login(app, 'wakko', 'onion password'))
    with app.test_request_context(base_url=ONION_URL):
        app.preprocess_request()
        assert find_user('wakko').id == world.onion.id
    with app.test_request_context(base_url=f'https://{HELL}'):
        app.preprocess_request()
        assert find_user('Wakko').id == world.hell.id


def test_a_full_handle_on_the_onion_reaches_the_hell_cloud_account(app, world):
    with patch('app.auth.util.sync_user_to_ldap'):
        assert logged_in(login(app, f'wakko@{HELL}', 'hell password', ONION_URL))
        assert logged_in(login(app, f'@wakko@{ONION}', 'onion password'))
        assert not logged_in(login(app, f'wakko@{HELL}', 'onion password', ONION_URL))


def test_a_full_handle_for_an_unhosted_domain_fails_like_a_wrong_password(app, world, caplog):
    caplog.set_level(logging.DEBUG)
    with patch('app.auth.util.sync_user_to_ldap'):
        client = app.test_client()
        login(app, 'wakko', 'a wrong password', client=client)
        wrong_flash = flashes(client)
        wrong_lines = failures(caplog)
        caplog.clear()
        client = app.test_client()
        unhosted = login(app, 'wakko@nowhere.example', 'hell password', client=client)
    assert not logged_in(unhosted)
    assert flashes(client) == wrong_flash and wrong_flash
    assert failures(caplog) == wrong_lines == ['auth: login failed ip=203.0.113.9']
    assert 'wakko' not in repr([(r.msg, r.args) for r in caplog.records if r.getMessage().startswith('auth:')])


def test_an_email_logs_in_on_any_storefront(app, world):
    with patch('app.auth.util.sync_user_to_ldap'):
        assert logged_in(login(app, 'wakko-onion@example.com', 'onion password'))
        assert logged_in(login(app, 'wakko-hell@example.com', 'hell password', ONION_URL))


def test_passkey_options_and_verify_resolve_per_domain(app, world):
    onion_key = _passkey(world.onion, passkey_id='b25pb24=')
    client = app.test_client()
    options = client.post('/auth/passkeys/login_options', json={'username': 'wakko'}, base_url=ONION_URL).get_json()
    assert options['allowCredentials'][0]['id'] == 'b25pb24'
    options = client.post('/auth/passkeys/login_options', json={'username': f'wakko@{HELL}'},
                          base_url=ONION_URL).get_json()
    assert options['allowCredentials'][0]['id'] != 'b25pb24'   # the decoy: hell's wakko has no passkey
    options = client.post('/auth/passkeys/login_options', json={'username': 'wakko@nowhere.example'}).get_json()
    assert options['allowCredentials'][0]['id'] != 'b25pb24'
    assert client.post('/auth/passkeys/login_options', json={}).get_json()['allowCredentials']
    with patch('app.auth.passkeys.parse_authentication_credential_json', return_value='CRED'), \
            patch('app.auth.passkeys.verify_authentication_response', return_value=authenticated()):
        ok = client.post('/auth/passkeys/login_verification',
                         json={'username': f'wakko@{ONION}', 'redirect': '/', 'response': {}})
        refused = client.post('/auth/passkeys/login_verification',
                              json={'username': 'wakko', 'redirect': '/', 'response': {}})
    assert ok.get_json()['verified'] is True
    assert refused.get_json()['verified'] is False
    assert onion_key.counter == 0


def test_nntp_login_takes_a_bare_default_name_or_a_full_handle(app, world):
    with app.app_context():
        assert _authenticate('wakko', 'hell password')
        assert _authenticate(f'wakko@{ONION}', 'onion password')
        for typed, password in (('wakko', 'onion password'), ('wakko@nowhere.example', 'hell password')):
            with pytest.raises(NNTPAuthenticationError):
                _authenticate(typed, password)


def test_api_login_resolves_on_the_request_storefront(app, world):
    with patch('app.shared.auth.sync_user_to_ldap'):
        with app.test_request_context('/', base_url=ONION_URL, environ_base=CLEARNET):
            app.preprocess_request()
            assert api_log_user_in({'username': 'wakko', 'password': 'onion password'})
            with pytest.raises(Exception, match='incorrect_login'):
                api_log_user_in({'username': 'wakko', 'password': 'hell password'})
            assert api_log_user_in({'username': f'wakko@{HELL}', 'password': 'hell password'})


def test_a_bare_mention_resolves_in_the_authors_home_domain(app, world):
    author = make_user(None, 'author', local=True)
    author.site_domain_id = world.onion_domain.id
    db.session.commit()
    post = SimpleNamespace(author=author, body=f'hi @wakko@{HELL}')
    assert mentioned_users_found_by(post, stored_user) == [world.onion]
    author.site_domain_id = world.hell_domain.id
    assert mentioned_users_found_by(post, stored_user) == [world.hell]
    assert stored_user('wakko') in (world.hell, world.onion)   # no home: any domain, as before


def test_a_name_taken_on_one_domain_is_free_on_another(app, world):
    assert user_name_is_taken('wakko')
    assert user_name_is_taken('wakko', world.onion_domain.id)
    other = SiteDomain(domain='other.example', scheme='https')
    db.session.add(other)
    db.session.commit()
    assert not user_name_is_taken('wakko', other.id)
    community = make_community('yakko')
    community.site_domain_id = world.onion_domain.id
    db.session.commit()
    assert user_name_is_taken('yakko', world.onion_domain.id)
    assert not user_name_is_taken('yakko', world.hell_domain.id)


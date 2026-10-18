"""A failed credential check logs `auth: login failed ip=<ip>` at WARNING, for fail2ban on the host (task 11).

Every failed check logs exactly once: web login, API login and verify_credentials, whether the account is unknown,
the password is wrong, the directory bind failed, or the account is in the hidden backoff (which has to look like a
wrong password, in the log too). On the onion the line says `ip=onion`: those requests share the proxy's address, and
no address from them may ever be bannable. The line holds neither the user name nor the email.

Not logged, because no password was checked: a refused or missing proof-of-work or wait token, a visible typed-key
wait refusal, and the session-first redirect. Nor is a correct password for a banned account.
"""
import logging
from unittest.mock import patch

import pytest
from werkzeug.exceptions import HTTPException

from app import db
from app.auth.util import validate_user_login   # a deleted row is reached by name only: find_user never returns one
from app.models import User
from app.onion_guard import record_onion_account_failure, record_onion_login_failure
from app.shared.auth import api_log_user_in
from app.utils import log_login_failure
from app.api.alpha.utils.user import post_user_verify_credentials
from tests.test_onion_guard_limits import (FROM_CADDY, LOGIN, PASSWORD, a_session, alice, clock, directory,  # noqa: F401
                                           ldap_off, login_form, onion, onion_context, onion_post, proof_given,
                                           web_login)

CLEARNET = {'REMOTE_ADDR': '203.0.113.9'}
LINE = 'auth: login failed ip='


def failures(caplog):
    return [r for r in caplog.records if r.getMessage().startswith('auth: login failed')]


def only_failure(caplog, ip):
    """The one failure line, WARNING, exactly `ip`, and nothing of the typed name in any part of the record."""
    found = failures(caplog)
    assert len(found) == 1
    record = found[0]
    assert record.levelno == logging.WARNING
    assert record.getMessage() == LINE + ip
    everything = repr((record.msg, record.args, record.__dict__))
    for secret in ('alice', 'bob', 'ghost', 'example.com', PASSWORD):
        assert secret not in everything


@pytest.fixture(autouse=True)
def captured(caplog):
    caplog.set_level(logging.DEBUG)
    return caplog


def clearnet_login(app, user_name, password):
    return app.test_client().post('/auth/login', data={'user_name': user_name, 'password': password, **LOGIN},
                                  environ_base=CLEARNET)


def test_failed_login_logs_ip_not_username(app, alice, ldap_off, caplog):
    clearnet_login(app, 'alice', 'a wrong password')
    only_failure(caplog, '203.0.113.9')


def test_an_unknown_user_logs(app, alice, ldap_off, caplog):
    clearnet_login(app, 'ghost', PASSWORD)
    only_failure(caplog, '203.0.113.9')


def test_an_email_typed_with_a_wrong_password_logs(app, alice, ldap_off, caplog):
    clearnet_login(app, 'alice@example.com', 'a wrong password')
    only_failure(caplog, '203.0.113.9')


def test_a_correct_password_logs_nothing(app, alice, ldap_off, caplog):
    response = clearnet_login(app, 'alice', PASSWORD)
    assert response.status_code == 302 and not response.headers['Location'].endswith('/auth/login')
    assert failures(caplog) == []


def test_a_correct_password_for_a_banned_account_logs_nothing(app, alice, ldap_off, caplog):
    alice.banned = True
    db.session.commit()
    clearnet_login(app, 'alice', PASSWORD)
    assert failures(caplog) == []


def test_a_deleted_account_logs(app, alice, ldap_off, caplog):
    alice.deleted = True
    with app.test_request_context('/', environ_base=CLEARNET):
        assert validate_user_login(alice, PASSWORD, '203.0.113.9') is False
    only_failure(caplog, '203.0.113.9')


def test_a_failed_bind_and_a_wrong_local_password_log_once(app, alice, directory, caplog):
    clearnet_login(app, 'alice', 'a wrong password')
    assert directory.call_count == 1
    only_failure(caplog, '203.0.113.9')


def test_a_failed_bind_for_an_unknown_name_logs_once(app, alice, directory, caplog):
    clearnet_login(app, 'ghost', 'a wrong password')
    only_failure(caplog, '203.0.113.9')


def test_a_good_bind_logs_nothing(app, alice, directory, caplog):
    clearnet_login(app, 'alice', PASSWORD)
    assert failures(caplog) == []


def test_an_onion_failure_logs_onion(app, alice, ldap_off, caplog):
    client = app.test_client()
    a_session(client)
    web_login(client, 'alice', 'a wrong password')
    only_failure(caplog, 'onion')


def test_an_onion_unknown_user_logs_onion(app, alice, ldap_off, caplog):
    client = app.test_client()
    a_session(client)
    web_login(client, 'ghost', 'a wrong password')
    only_failure(caplog, 'onion')


@pytest.mark.parametrize('typed, password', [('alice', 'a wrong password'), ('alice', PASSWORD)],
                         ids=['wrong', 'hidden-refusal-of-the-right-one'])
def test_the_hidden_refusal_logs_as_a_wrong_password_does(app, alice, ldap_off, redis_double, clock, caplog,
                                                          typed, password):
    for _ in range(6):
        record_onion_account_failure(alice.id)
    client = app.test_client()
    a_session(client)
    web_login(client, typed, password)
    only_failure(caplog, 'onion')


def test_the_hidden_refusal_with_a_directory_logs_once(app, alice, directory, redis_double, clock, caplog):
    for _ in range(6):
        record_onion_account_failure(alice.id)
    client = app.test_client()
    a_session(client)
    web_login(client, 'alice', PASSWORD)
    only_failure(caplog, 'onion')


def test_a_visible_wait_refusal_logs_nothing(app, alice, ldap_off, redis_double, clock, caplog):
    for _ in range(6):
        record_onion_login_failure('alice')
    client = app.test_client()
    a_session(client)
    web_login(client, 'alice', PASSWORD)
    assert 'Please try again' in ' '.join(m for _, m in _flashes(client))
    assert failures(caplog) == []


def _flashes(client):
    with client.session_transaction(base_url='http://abc.onion') as stored:
        return stored.get('_flashes', [])


def test_a_refused_proof_logs_nothing(app, alice, ldap_off, caplog, monkeypatch):
    monkeypatch.setattr('app.auth.routes.onion_proof_refused', lambda form: True)
    client = app.test_client()
    a_session(client)
    web_login(client, 'alice', 'a wrong password')
    assert failures(caplog) == []


def test_the_session_first_redirect_logs_nothing(app, alice, ldap_off, caplog):
    response = app.test_client().get('/auth/login', base_url='http://abc.onion',
                                     headers={'X-PieFed-Via': 'onion'}, environ_base=FROM_CADDY)
    assert response.status_code == 303
    assert failures(caplog) == []


def test_api_login_logs_a_wrong_password(app, alice, ldap_off, caplog):
    with app.test_request_context('/', environ_base=CLEARNET):
        with pytest.raises(Exception, match='incorrect_login'):
            api_log_user_in({'username': 'alice', 'password': 'a wrong password'})
    only_failure(caplog, '203.0.113.9')


def test_api_login_logs_an_unknown_user(app, alice, ldap_off, caplog):
    with app.test_request_context('/', environ_base=CLEARNET):
        with pytest.raises(Exception, match='incorrect_login'):
            api_log_user_in({'username': 'ghost', 'password': PASSWORD})
    only_failure(caplog, '203.0.113.9')


def test_api_login_logs_nothing_for_a_banned_account_with_the_right_password(app, alice, ldap_off, caplog):
    alice.banned = True
    db.session.commit()
    with app.test_request_context('/', environ_base=CLEARNET):
        with pytest.raises(Exception, match='incorrect_login'):
            api_log_user_in({'username': 'alice', 'password': PASSWORD})
    assert failures(caplog) == []


def test_api_login_hidden_refusal_logs_onion(app, alice, ldap_off, redis_double, clock, caplog):
    for _ in range(6):
        record_onion_account_failure(alice.id)
    with onion_context(app):
        with pytest.raises(Exception, match='incorrect_login'):
            api_log_user_in({'username': 'alice', 'password': PASSWORD})
    only_failure(caplog, 'onion')


def test_verify_credentials_logs_a_wrong_password(app, alice, caplog):
    with app.test_request_context('/', environ_base=CLEARNET):
        with pytest.raises(BlockingIOError):
            post_user_verify_credentials({'username': 'alice', 'password': 'a wrong password'})
    only_failure(caplog, '203.0.113.9')


def test_verify_credentials_logs_an_unknown_user(app, alice, caplog):
    with app.test_request_context('/', environ_base=CLEARNET):
        with pytest.raises(BlockingIOError):
            post_user_verify_credentials({'username': 'ghost', 'password': PASSWORD})
    only_failure(caplog, '203.0.113.9')


def test_verify_credentials_logs_onion_for_a_hidden_refusal(app, alice, redis_double, clock, caplog):
    for _ in range(6):
        record_onion_account_failure(alice.id)
    with onion_context(app):
        with pytest.raises(BlockingIOError):
            post_user_verify_credentials({'username': 'alice', 'password': PASSWORD})
    only_failure(caplog, 'onion')


def test_verify_credentials_logs_nothing_for_success_or_ban(app, alice, caplog):
    with app.test_request_context('/', environ_base=CLEARNET):
        assert post_user_verify_credentials({'username': 'alice', 'password': PASSWORD}) == {}
        alice.banned = True
        db.session.commit()
        with pytest.raises(BlockingIOError):
            post_user_verify_credentials({'username': 'alice', 'password': PASSWORD})
    assert failures(caplog) == []


def test_verify_credentials_wait_refusal_logs_nothing(app, alice, redis_double, clock, caplog):
    for _ in range(6):
        record_onion_login_failure('alice')
    with onion_context(app):
        with pytest.raises(HTTPException):
            post_user_verify_credentials({'username': 'alice', 'password': PASSWORD})
    assert failures(caplog) == []


@pytest.mark.parametrize('ip', ['2001:db8::1', '203.0.113.9', 'ABCdef:0.1'])
def test_a_plain_address_is_logged_as_it_is(app, caplog, ip):
    with app.test_request_context('/', environ_base={'REMOTE_ADDR': ip}):
        log_login_failure()
    only_failure(caplog, ip)


@pytest.mark.parametrize('ip', ['1.2.3.4\nauth: login failed ip=5.6.7.8', '1.2.3.4 x', '', '1.2.3.4\r', '::g', None])
def test_an_address_with_anything_else_is_logged_as_invalid(app, caplog, monkeypatch, ip):
    monkeypatch.setattr('app.utils.ip_address', lambda: ip)
    with app.test_request_context('/'):
        log_login_failure()
    only_failure(caplog, 'invalid')

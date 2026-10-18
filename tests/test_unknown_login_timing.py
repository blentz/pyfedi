"""A login naming no account costs what a wrong password costs (gap G1a).

When nothing matched the typed username or email, web login, API login and verify_credentials used to fail without
any password-hash work, so the response time alone told a caller which names have accounts, clearnet and onion
alike. The not-found branch now runs `verify_password_hash(DUMMY_PASSWORD_HASH, password)`, the same verification a
real check does, and then fails exactly as before. Each test counts the hash verifications by wrapping werkzeug's
`check_password_hash` where app.models calls it, and compares the answer with a wrong password's.
"""
from unittest.mock import patch

import pytest

import app.models as models
from app import db
from app.api.alpha.utils.user import post_user_verify_credentials
from app.models import DUMMY_PASSWORD_HASH, User
from app.shared.auth import api_log_user_in
from tests.factories import make_user
from tests.test_onion_guard_limits import (LOGIN, PASSWORD, a_session, alice, ldap_off, login_form,  # noqa: F401
                                           onion, proof_given, web_login)

CLEARNET = {'REMOTE_ADDR': '203.0.113.9'}


def counted():
    return patch('app.models.check_password_hash', wraps=models.check_password_hash)


def test_the_dummy_hash_uses_set_passwords_method():
    probe = User()
    probe.set_password('anything at all')
    assert DUMMY_PASSWORD_HASH.split('$')[0] == probe.password_hash.split('$')[0]


def flashed(client, **where):
    with client.session_transaction(**where) as stored:
        return stored.get('_flashes', [])


def clearnet_attempt(app, typed):
    client = app.test_client()
    response = client.post('/auth/login', data={'user_name': typed, 'password': 'a wrong password', **LOGIN},
                           environ_base=CLEARNET)
    return response, flashed(client)


def test_web_login_for_an_unknown_name_verifies_a_hash_and_answers_as_a_wrong_password(app, alice, ldap_off):
    with counted() as verified:
        unknown, unknown_flashes = clearnet_attempt(app, 'ghost')
    verified.assert_called_once_with(DUMMY_PASSWORD_HASH, 'a wrong password')

    with counted() as verified:
        wrong, wrong_flashes = clearnet_attempt(app, 'alice')
    verified.assert_called_once()

    assert (unknown.status_code, unknown.headers['Location']) == (wrong.status_code, wrong.headers['Location'])
    assert unknown_flashes == wrong_flashes and unknown_flashes


def test_onion_web_login_for_an_unknown_name_verifies_a_hash_and_answers_as_a_wrong_password(app, alice, ldap_off):
    answers = []
    for typed in ('ghost', 'alice'):
        client = app.test_client()
        a_session(client)
        with counted() as verified:
            response = web_login(client, typed, 'a wrong password')
        verified.assert_called_once()
        answers.append((response.status_code, response.headers['Location'], flashed(client, base_url='http://abc.onion')))
    assert answers[0] == answers[1]


def api_attempt(app, typed):
    with app.test_request_context('/', environ_base=CLEARNET):
        with pytest.raises(Exception) as raised:
            api_log_user_in({'username': typed, 'password': 'a wrong password'})
    return type(raised.value), str(raised.value)


def test_api_login_for_an_unknown_name_verifies_a_hash_and_answers_as_a_wrong_password(app, alice, ldap_off):
    with counted() as verified:
        unknown = api_attempt(app, 'ghost')
    verified.assert_called_once_with(DUMMY_PASSWORD_HASH, 'a wrong password')
    with counted() as verified:
        wrong = api_attempt(app, 'alice')
    verified.assert_called_once()
    assert unknown == wrong == (Exception, 'incorrect_login')


def verify_attempt(app, typed):
    with app.test_request_context('/', environ_base=CLEARNET):
        with pytest.raises(BlockingIOError) as raised:
            post_user_verify_credentials({'username': typed, 'password': 'a wrong password'})
    return str(raised.value)


@pytest.mark.parametrize('typed', ['ghost', 'ghost@example.com'], ids=['name', 'email'])
def test_verify_credentials_for_an_unknown_name_verifies_a_hash_and_answers_as_a_wrong_password(app, alice, typed):
    with counted() as verified:
        unknown = verify_attempt(app, typed)
    verified.assert_called_once_with(DUMMY_PASSWORD_HASH, 'a wrong password')
    with counted() as verified:
        wrong = verify_attempt(app, 'alice')
    verified.assert_called_once()
    assert unknown == wrong


# --------------------------------------------------------------------------
# An account with no password (OAuth or LDAP only) costs the same as well
# --------------------------------------------------------------------------

@pytest.fixture(params=[None, ''], ids=['null', 'empty'])
def passwordless(request, alice):
    """bob: an account with no stored hash, beside alice's real one."""
    bob = make_user(alice.instance, 'bob', local=True)
    bob.verified = True
    bob.password_hash = request.param
    db.session.commit()
    return bob


def test_web_login_for_a_passwordless_account_verifies_a_hash_and_answers_as_a_wrong_password(app, passwordless,
                                                                                              ldap_off):
    with counted() as verified:
        none, none_flashes = clearnet_attempt(app, 'bob')
    verified.assert_called_once_with(DUMMY_PASSWORD_HASH, 'a wrong password')
    wrong, wrong_flashes = clearnet_attempt(app, 'alice')
    assert (none.status_code, none.headers['Location'], none_flashes) == \
        (wrong.status_code, wrong.headers['Location'], wrong_flashes)


def test_api_login_for_a_passwordless_account_verifies_a_hash_and_answers_as_a_wrong_password(app, passwordless,
                                                                                              ldap_off):
    with counted() as verified:
        none = api_attempt(app, 'bob')
    verified.assert_called_once_with(DUMMY_PASSWORD_HASH, 'a wrong password')
    assert none == api_attempt(app, 'alice') == (Exception, 'incorrect_login')


def test_verify_credentials_for_a_passwordless_account_verifies_a_hash_and_answers_as_a_wrong_password(app,
                                                                                                     passwordless):
    with counted() as verified:
        none = verify_attempt(app, 'bob')
    verified.assert_called_once_with(DUMMY_PASSWORD_HASH, 'a wrong password')
    assert none == verify_attempt(app, 'alice')

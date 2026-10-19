"""`auth: login failed ip=<ip>` is for failed CREDENTIAL checks only (gap G1c and its ruling).

A failed credential check is a wrong password, an unknown name, a failed LDAP bind, or a failed NNTP credential
check. tests/test_login_failure_log.py covers the web and API arms. NNTP AUTHINFO did not log its failures at all: it
now logs them, with the connection's peer address, since there is no HTTP request to read one from.

A refusal of an account that authenticated correctly is not a failed credential check, and logs nothing: banned,
deleted or unverified, registrations closed, a declined country, a profile with no email, an email already in use.
That holds whichever way the credential was accepted -- a local password (tests/test_login_failure_log.py), an LDAP
bind, an OAuth provider's callback, or NNTP.
"""
import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app import db
from app.models import Site
from app.nntp import server as nntp
from app.nntp.nntpserver import NNTPAuthenticationError
from app.nntp.server import PieFedNNTPConnectionHandler, PieFedNNTPServer
from app.utils import log_login_failure
from tests.test_login_failure_log import CLEARNET, clearnet_login, failures, only_failure
from tests.test_onion_guard_limits import (LOGIN, PASSWORD, a_session, alice, directory, ldap_off, login_form, onion,  # noqa: F401
                                           onion_get, onion_post, proof_given)


@pytest.fixture(autouse=True)
def captured(caplog):
    caplog.set_level(logging.DEBUG)
    return caplog


# --------------------------------------------------------------------------
# log_login_failure's own address, for a login with no HTTP request
# --------------------------------------------------------------------------

@pytest.mark.parametrize('given, logged', [('198.51.100.4', '198.51.100.4'), ('2001:db8::4', '2001:db8::4'),
                                           ('', 'invalid'), ('1.2.3.4\nauth: x', 'invalid')])
def test_a_given_address_is_logged_without_a_request(app, caplog, given, logged):
    with app.app_context():
        log_login_failure(given)
    only_failure(caplog, logged)


# --------------------------------------------------------------------------
# NNTP
# --------------------------------------------------------------------------

@pytest.fixture
def peer():
    nntp._tl.peer = '198.51.100.4'
    yield '198.51.100.4'
    del nntp._tl.peer


def nntp_login(app, name, password):
    return PieFedNNTPServer.auth_user(SimpleNamespace(_app=app), name, password)


def test_the_connection_handler_records_its_peer_for_the_login(app):
    handler = object.__new__(PieFedNNTPConnectionHandler)
    handler.client_address = ('198.51.100.7', 50000)
    handler.setup()
    try:
        assert nntp._tl.peer == '198.51.100.7'
    finally:
        del nntp._tl.peer


@pytest.mark.parametrize('typed, password', [('ghost', PASSWORD), ('alice', 'a wrong password'),
                                             ('ali\x00ce', PASSWORD)], ids=['unknown', 'wrong-password', 'nul'])
def test_a_failed_nntp_credential_check_logs_its_peer(app, alice, peer, caplog, typed, password):
    with pytest.raises(NNTPAuthenticationError, match='Invalid username or password'):
        nntp_login(app, typed, password)
    only_failure(caplog, peer)


def test_a_failed_nntp_check_with_no_peer_recorded_logs_invalid(app, alice, caplog):
    with pytest.raises(NNTPAuthenticationError):
        nntp_login(app, 'alice', 'a wrong password')
    only_failure(caplog, 'invalid')


@pytest.mark.parametrize('banned, verified', [(True, True), (False, False)], ids=['banned', 'unverified'])
def test_an_nntp_account_refused_with_the_right_password_logs_nothing(app, alice, peer, caplog, banned, verified):
    alice.banned = banned
    alice.verified = verified
    db.session.commit()
    with pytest.raises(NNTPAuthenticationError):
        nntp_login(app, 'alice', PASSWORD)
    assert failures(caplog) == []


def test_an_accepted_nntp_login_logs_nothing(app, alice, peer, caplog):
    assert nntp_login(app, 'alice', PASSWORD).startswith(b'Bearer ')
    assert failures(caplog) == []


# --------------------------------------------------------------------------
# OAuth: the provider checked the credential, so nothing here is a failed check
# --------------------------------------------------------------------------

@pytest.fixture
def oauth_site(app, alice):
    site = db.session.get(Site, 1)
    site.registration_mode = 'Open'
    site.application_question = ''
    db.session.commit()
    return alice


def google_callback(app, **user_info):
    info = {'id': 'remote-1', 'email': 'someone@example.com', 'username': 'Someone', **user_info}
    with patch('app.auth.oauth_util.get_token_and_user_info', return_value=('a-token', info)), \
            patch('app.auth.oauth_util.finalize_user_setup'):
        return app.test_client().get('/auth/google_authorize', environ_base=CLEARNET)


def closed(app, account):
    db.session.get(Site, 1).registration_mode = 'Closed'
    db.session.commit()
    return google_callback(app)


def declined_country(app, account):
    with patch('app.auth.oauth_util.is_country_blocked', return_value=True):
        return google_callback(app)


def account_in_state(state):
    def refuse(app, account):
        account.google_oauth_id = 'remote-1'
        setattr(account, state, True)
        db.session.commit()
        return google_callback(app)
    return refuse


def banned_visitor(app, account):
    with patch('app.auth.oauth_util.user_ip_banned', return_value=True):
        return google_callback(app, email='brand-new@example.com')


@pytest.mark.parametrize('refusal', [
    closed, declined_country, account_in_state('banned'), account_in_state('deleted'), banned_visitor,
    lambda app, account: google_callback(app, email=None),
    lambda app, account: google_callback(app, email='alice@example.com'),
], ids=['closed', 'declined-country', 'banned', 'deleted', 'banned-visitor', 'no-email', 'email-taken'])
def test_an_oauth_refusal_after_the_provider_accepted_logs_nothing(app, oauth_site, caplog, refusal):
    refused = refusal(app, oauth_site)
    assert refused.status_code == 200 or refused.headers['Location'] == '/auth/login'
    assert failures(caplog) == []


# --------------------------------------------------------------------------
# LDAP: a bind that worked is not a failed check, whatever this instance then says
# --------------------------------------------------------------------------

def test_a_banned_account_the_directory_accepted_logs_nothing(app, alice, directory, caplog):
    alice.banned = True
    db.session.commit()
    assert clearnet_login(app, 'alice', PASSWORD).headers['Location'].endswith('/auth/login')
    assert failures(caplog) == []


def test_a_directory_name_that_cannot_be_local_logs_nothing(app, alice, directory, caplog):
    assert clearnet_login(app, 'not/a name', PASSWORD).headers['Location'].endswith('/auth/login')
    assert failures(caplog) == []


# --------------------------------------------------------------------------
# One line per registration POST and per refused probe, for fail2ban now the access log is off
# --------------------------------------------------------------------------

def event_lines(caplog, prefix):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith(prefix)]


def test_a_register_post_logs_one_attempt_line(app, db_session, caplog):
    app.test_client().post('/auth/register', data={'user_name': ''}, environ_base=CLEARNET)   # invalid form still counts
    assert event_lines(caplog, 'auth: register attempt') == ['auth: register attempt ip=203.0.113.9']


def test_a_register_get_logs_nothing(app, db_session, caplog):
    app.test_client().get('/auth/register', environ_base=CLEARNET)
    assert not event_lines(caplog, 'auth: register attempt')


def test_a_register_post_by_a_signed_in_user_still_logs(app, alice, ldap_off, caplog):
    client = app.test_client()
    client.post('/auth/login', data={'user_name': 'alice', 'password': PASSWORD, **LOGIN}, environ_base=CLEARNET)
    client.post('/auth/register', data={}, environ_base=CLEARNET)
    assert event_lines(caplog, 'auth: register attempt') == ['auth: register attempt ip=203.0.113.9']


def test_onion_requests_log_ip_onion(app, onion, caplog):
    client = app.test_client()
    a_session(client)   # session first, or the guard answers before the route runs
    onion_post(client, '/auth/register', data={})
    onion_get(client, '/.env')
    assert event_lines(caplog, 'auth: register attempt') == ['auth: register attempt ip=onion']
    assert event_lines(caplog, 'scan:') == ['scan: probe refused ip=onion']


@pytest.mark.parametrize('site, logged', [('cross-site', False), ('same-site', False), ('same-origin', False),
                                          ('none', True)])
def test_a_register_post_logs_only_when_not_page_initiated(app, db_session, caplog, site, logged):
    app.test_client().post('/auth/register', data={}, environ_base=CLEARNET, headers={'Sec-Fetch-Site': site})
    assert bool(event_lines(caplog, 'auth: register attempt')) is logged

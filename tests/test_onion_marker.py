"""Onion requests and the client IP (onion storefront spec 2026-10-17, section 3.7).

Every onion visitor reaches the app through one Tor container, so they all share its address. A request counts as
onion only when Caddy's internal listener marked it (`X-PieFed-Via: onion`) AND the TCP peer ProxyFix saw is
`ONION_PROXY_IP`. For such a request the shared address is never used: no IP ban, no honeypot ban, no geolocation,
nothing stored, and rate limits are keyed by session.

The `app` fixture is session-scoped, so every config change goes through `patch.dict` and is undone.
"""
import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import g, session

from app import cache, db, get_ip_address, limiter, rate_limit_key
from app.api.alpha.routes import is_trusted_request
from app.auth.util import get_country, handle_banned_user, update_user_session
from app.models import Instance, IpBan, Site, SiteDomain, User
from app.onion import audit_field, is_onion_host, is_onion_request
from app.shared.auth import api_log_user_in
from app.storefront import check_default_domain, clear_domain_cache
from app.utils import banned_ip_addresses, block_honey_pot, user_ip_banned
from tests.factories import make_instance, make_user

PROXY = '10.89.0.2'
TOR_CONTAINER = '10.89.0.7'
ONION_HOST = 'abc.onion'
ONION = {'X-PieFed-Via': 'onion', 'X-Forwarded-For': TOR_CONTAINER, 'Host': ONION_HOST}


def proxy_ip(app, value=PROXY):
    return patch.dict(app.config, {'ONION_PROXY_IP': value})


def onion_context(app, peer=PROXY, path='/', headers=None):
    """A request context shaped as ProxyFix leaves it: remote_addr is the last X-Forwarded-For entry and the
    original peer is kept under `werkzeug.proxy_fix.orig`. test_request_context does not run the WSGI middleware."""
    return app.test_request_context(path, headers={**ONION, **(headers or {})},
                                    environ_base={'REMOTE_ADDR': TOR_CONTAINER,
                                                  'werkzeug.proxy_fix.orig': {'REMOTE_ADDR': peer}})


# --------------------------------------------------------------------------
# The marker, through the real ProxyFix
# --------------------------------------------------------------------------

def through_proxy_fix(app, peer, headers=ONION):
    """Send one real request through the WSGI stack and answer is_onion_request() and get_ip_address() for it."""
    with app.test_client() as client:
        client.get('/robots.txt', headers=headers, environ_base={'REMOTE_ADDR': peer})
        return is_onion_request(), get_ip_address()


def test_marker_trusted_from_proxy(app, site):
    with proxy_ip(app):
        assert through_proxy_fix(app, PROXY) == (True, '')


def test_marker_ignored_from_client(app, site):
    with proxy_ip(app):
        assert through_proxy_fix(app, '203.0.113.9') == (False, TOR_CONTAINER)


def test_marker_ignored_when_unset(app, site):
    with proxy_ip(app, None):
        assert through_proxy_fix(app, PROXY) == (False, TOR_CONTAINER)
    with proxy_ip(app, ''):
        assert through_proxy_fix(app, PROXY) == (False, TOR_CONTAINER)


def test_the_proxy_without_the_header_is_clearnet(app, site):
    with proxy_ip(app):
        assert through_proxy_fix(app, PROXY, {'X-Forwarded-For': TOR_CONTAINER}) == (False, TOR_CONTAINER)
        assert through_proxy_fix(app, PROXY, {**ONION, 'X-PieFed-Via': 'Onion'}) == (False, TOR_CONTAINER)


@pytest.mark.parametrize('host, onion', [
    ('test.piefed.local', False),
    ('hell.cloud', False),
    ('abc.onion.evil.example', False),
    ('onion', False),
    ('[::1]:80', False),
    ('[::1', False),
    ('abc.onion', True),
    ('ABC.ONION:80', True),
    ('abc.onion.', True),
    ('abc.onion.:8080', True),
])
def test_the_host_must_be_an_onion(app, site, host, onion):
    """Defence in depth: the marker from Caddy's address counts only on a .onion Host. Should a public listener ever
    fail to strip a client's X-PieFed-Via, a clearnet request is still not treated as onion."""
    with proxy_ip(app):
        expected = (True, '') if onion else (False, TOR_CONTAINER)
        assert through_proxy_fix(app, PROXY, {**ONION, 'Host': host}) == expected


@pytest.mark.parametrize('host, onion', [
    ('[::1', False),            # Werkzeug's request.host already drops this; the parser must not raise on it either
    ('[fd00::1]:80', False),
    ('', False),
    ('abc.onion', True),
    ('Abc.Onion.:443', True),
])
def test_is_onion_host(host, onion):
    assert is_onion_host(host) is onion


def test_a_forwarded_for_naming_the_proxy_is_not_the_proxy(app, site):
    """X-Forwarded-For is client-writable. Only the TCP peer counts."""
    with proxy_ip(app):
        assert through_proxy_fix(app, '203.0.113.9', {**ONION, 'X-Forwarded-For': PROXY}) == (False, PROXY)


def test_the_trusted_header_does_not_outrank_the_marker(app, site):
    """TRUSTED_CLIENT_IP_HEADER names a header the CDN writes. On the onion there is no CDN and the header is the
    client's own, so the onion check comes first."""
    with proxy_ip(app), patch.dict(app.config, {'TRUSTED_CLIENT_IP_HEADER': 'CF-Connecting-IP'}):
        assert through_proxy_fix(app, PROXY, {**ONION, 'CF-Connecting-IP': '198.51.100.1'}) == (True, '')


@pytest.mark.parametrize('configured, peer, onion', [
    ('10.89.0.2', '10.89.0.2', True),
    (' 10.89.0.2 ', '10.89.0.2', True),
    ('fd00::2', 'fd00:0:0:0:0:0:0:2', True),
    ('10.89.0.2', '::ffff:10.89.0.2', True),
    ('10.89.0.0/24', '10.89.0.2', False),
    ('10.89.0.2', '10.89.0.20', False),
    ('not-an-ip', 'not-an-ip', False),
    ('10.89.0.2', '', False),
    ('10.89.0.2', None, False),
])
def test_addresses_are_compared_as_one_address(app, configured, peer, onion):
    with proxy_ip(app, configured), onion_context(app, peer):
        assert is_onion_request() is onion


def test_no_proxy_fix_environ_is_clearnet(app):
    with proxy_ip(app), app.test_request_context('/', headers=ONION, environ_base={'REMOTE_ADDR': PROXY}):
        assert is_onion_request() is False


def test_outside_a_request_is_clearnet(app):
    with proxy_ip(app):
        assert is_onion_request() is False
        assert audit_field('1.2.3.4') == '1.2.3.4'


# --------------------------------------------------------------------------
# Startup warning
# --------------------------------------------------------------------------

def add_onion_domain():
    db.session.add(SiteDomain(domain='test.piefed.local', scheme='https', is_default=True))
    db.session.add(SiteDomain(domain='abc.onion', scheme='http'))
    db.session.commit()
    clear_domain_cache()


@pytest.mark.parametrize('value', [None, '', 'not-an-ip', '10.89.0.0/24'])
def test_an_onion_domain_without_a_proxy_ip_warns(app, db_session, caplog, value):
    add_onion_domain()
    with proxy_ip(app, value), caplog.at_level(logging.WARNING, logger=app.logger.name):
        check_default_domain()
    assert 'ONION_PROXY_IP' in caplog.text


def test_an_onion_domain_with_a_proxy_ip_is_quiet(app, db_session, caplog):
    add_onion_domain()
    with proxy_ip(app), caplog.at_level(logging.WARNING, logger=app.logger.name):
        check_default_domain()
    assert caplog.text == ''


def test_no_onion_domain_is_quiet_without_a_proxy_ip(app, db_session, caplog):
    with proxy_ip(app, None), caplog.at_level(logging.WARNING, logger=app.logger.name):
        check_default_domain()
    assert caplog.text == ''


# --------------------------------------------------------------------------
# Rate-limit key
# --------------------------------------------------------------------------

def test_the_limiter_keys_by_rate_limit_key():
    assert limiter._key_func is rate_limit_key


def test_onion_rate_limit_key_is_per_session(app):
    """A sid made during this request is not yet a session: it is keyed per session only once it comes back in the
    session cookie (tests/test_onion_guard_limits.py has the round trip)."""
    with proxy_ip(app), onion_context(app):
        assert 'onion_sid' not in session
        assert rate_limit_key() == 'onion-anon'
        sid = session['onion_sid']
        assert len(sid) >= 16
        assert rate_limit_key() == 'onion-anon'
    with proxy_ip(app), onion_context(app):
        rate_limit_key()
        assert session['onion_sid'] != sid


def test_clearnet_rate_limit_key_is_the_ip(app):
    with proxy_ip(app), app.test_request_context('/', environ_base={'REMOTE_ADDR': '9.9.9.9'}):
        assert rate_limit_key() == '9.9.9.9'
        assert 'onion_sid' not in session


def test_rate_limit_key_outside_a_request(app):
    assert rate_limit_key() == ''


def test_the_skip_list_never_exempts_an_onion_request(app):
    """SKIP_RATE_LIMIT_IPS is compared with request.remote_addr, which for onion traffic is the Tor container."""
    with proxy_ip(app), patch.dict(app.config, {'SKIP_RATE_LIMIT_IPS': [TOR_CONTAINER]}):
        with onion_context(app):
            assert is_trusted_request() is False
        with app.test_request_context('/', environ_base={'REMOTE_ADDR': TOR_CONTAINER}):
            assert is_trusted_request() is True


# --------------------------------------------------------------------------
# IP bans and the honeypot
# --------------------------------------------------------------------------

def test_onion_no_ip_ban(app, site):
    for address in ('', PROXY, TOR_CONTAINER):
        db.session.add(IpBan(ip_address=address, notes='probe'))
    db.session.commit()
    cache.delete_memoized(banned_ip_addresses)
    with proxy_ip(app):
        with onion_context(app):
            assert not user_ip_banned()
        with app.test_request_context('/', environ_base={'REMOTE_ADDR': TOR_CONTAINER}):
            assert user_ip_banned()


def test_onion_honeypot_ban_is_not_applied(app, site, redis_double):
    for address in ('', PROXY, TOR_CONTAINER):
        redis_double.set(f'ban:{address}', 1)
    with proxy_ip(app), onion_context(app):
        g.site = site
        block_honey_pot()   # no abort(403)


def test_onion_honeypot_ends_session(app, site, redis_double):
    add_onion_domain()
    with proxy_ip(app):
        with app.test_client() as client:
            with client.session_transaction() as stored:
                stored['onion_sid'] = 'a-session'
                stored['marker'] = 'kept until the honeypot'
            for _ in range(4):
                response = client.get('/honey', headers=ONION, environ_base={'REMOTE_ADDR': PROXY})
                assert response.status_code == 200
                assert dict(session) == {}
    assert list(redis_double.scan_iter('ban:*')) == []
    assert list(redis_double.scan_iter('honeypot:*')) == []


def test_clearnet_honeypot_still_bans(app, site, redis_double):
    with proxy_ip(app):
        client = app.test_client()
        for _ in range(3):
            client.get('/honey', environ_base={'REMOTE_ADDR': '203.0.113.9'})
    assert redis_double.exists('ban:203.0.113.9')


# --------------------------------------------------------------------------
# Geolocation and stored IPs
# --------------------------------------------------------------------------

def test_onion_country_is_not_looked_up(app):
    with proxy_ip(app), patch.dict(app.config, {'COUNTRY_SOURCE_HEADER': 'CF-IPCountry'}), \
            patch('app.auth.util.ip2location') as located:
        with onion_context(app, headers={'CF-IPCountry': 'NZ'}):
            assert get_country('1.2.3.4', 'fallback') == 'fallback'
        located.assert_not_called()
        with app.test_request_context('/', headers={'CF-IPCountry': 'NZ'}):
            assert get_country('1.2.3.4') == 'NZ'


def test_audit_field_is_null_on_the_onion(app):
    with proxy_ip(app), onion_context(app):
        assert audit_field('') is None
        assert audit_field('NZ') is None
    with proxy_ip(app), app.test_request_context('/'):
        assert audit_field('1.2.3.4') == '1.2.3.4'
        assert audit_field('') == ''


def registration_env():
    site = db.session.get(Site, 1)
    site.private_instance = False
    site.registration_mode = 'Open'
    site.application_question = ''
    site.tos_url = None
    db.session.commit()
    local = Instance.query.filter_by(domain='test.piefed.local').first() or make_instance('test.piefed.local',
                                                                                         software='piefed')
    make_user(local, 'founder', local=True)
    db.session.commit()


REGISTRATION = {'user_name': 'newperson', 'email': '', 'real_email': 'new@example.com',
                'password': 'a-good-password', 'password2': 'a-good-password', 'timezone': 'UTC',
                'submit': 'Register'}


def test_onion_signup_ip_null(app, site):
    registration_env()
    add_onion_domain()
    with proxy_ip(app), patch.dict(app.config, {'COUNTRY_SOURCE_HEADER': 'CF-IPCountry'}), \
            patch('app.auth.forms.get_setting', return_value=False), \
            patch('app.auth.util.ip2location') as located, \
            patch('app.auth.util.send_email_verification'), patch('app.auth.util.sync_user_with_ldap'):
        client = app.test_client()
        # Session first (task 9): the cookieless request picks up the session cookie the POST then sends
        assert client.get('/auth/register', headers=ONION, environ_base={'REMOTE_ADDR': PROXY}).status_code == 303
        response = client.post('/auth/register', data=REGISTRATION, headers={**ONION, 'CF-IPCountry': 'NZ'},
                               environ_base={'REMOTE_ADDR': PROXY})
    assert response.status_code == 302
    user = User.query.filter_by(user_name='newperson').one()
    assert user.ip_address is None
    assert user.ip_address_country is None
    located.assert_not_called()


def test_clearnet_signup_ip_stored(app, site):
    registration_env()
    with proxy_ip(app), patch('app.auth.forms.get_setting', return_value=False), \
            patch('app.auth.util.send_email_verification'), patch('app.auth.util.sync_user_with_ldap'):
        response = app.test_client().post('/auth/register', data=REGISTRATION,
                                          environ_base={'REMOTE_ADDR': '203.0.113.9'})
    assert response.status_code == 302
    assert User.query.filter_by(user_name='newperson').one().ip_address == '203.0.113.9'


# --------------------------------------------------------------------------
# Logging in over the onion
# --------------------------------------------------------------------------

def login_user_with_password(banned=False):
    if not db.session.get(Instance, 1):
        make_instance('test.piefed.local', software='piefed')
    make_user(None, 'burn-the-id-1-seat', local=True)   # id 1 is exempt from every ban check
    user = make_user(None, 'onionuser', local=True)
    user.set_password('correct horse battery')
    user.banned = banned
    user.ip_address = '203.0.113.9'
    user.ip_address_country = 'NZ'
    db.session.commit()
    return user


def test_onion_api_login_stores_no_ip(app, db_session):
    user = login_user_with_password()
    with proxy_ip(app), onion_context(app), patch('app.shared.auth.sync_user_to_ldap'):
        api_log_user_in({'username': 'onionuser', 'password': 'correct horse battery'})
    db.session.refresh(user)
    assert user.ip_address is None and user.ip_address_country is None


def test_onion_api_login_of_a_banned_user_bans_no_address(app, db_session):
    login_user_with_password(banned=True)
    with proxy_ip(app), onion_context(app), pytest.raises(Exception, match='incorrect_login'):
        api_log_user_in({'username': 'onionuser', 'password': 'correct horse battery'})
    assert IpBan.query.count() == 0


def test_onion_web_login_of_a_banned_user_bans_no_address(app, db_session):
    user = login_user_with_password(banned=True)
    with proxy_ip(app), onion_context(app):
        handle_banned_user(user, '')
    assert IpBan.query.count() == 0
    with proxy_ip(app), app.test_request_context('/', environ_base={'REMOTE_ADDR': '203.0.113.9'}):
        handle_banned_user(user, '203.0.113.9')
    assert [ban.ip_address for ban in IpBan.query.all()] == ['203.0.113.9']


def test_onion_web_login_stores_no_ip(app, db_session):
    user = login_user_with_password()
    form = SimpleNamespace(timezone=SimpleNamespace(data='UTC'))
    with proxy_ip(app), onion_context(app):
        update_user_session(user, form, '', '')
    assert user.ip_address is None and user.ip_address_country is None
    with proxy_ip(app), app.test_request_context('/'):
        update_user_session(user, form, '198.51.100.4', '')
    assert user.ip_address == '198.51.100.4' and user.ip_address_country is None

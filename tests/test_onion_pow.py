"""Onion login and registration ask for a small proof-of-work, or a timed wait without JavaScript
(onion storefront spec 2026-10-17, section 3.7a A).

Every onion visitor shares Tor's address, so a scripted attacker can rotate sessions for free. The form therefore
carries a signed challenge (`issue_challenge`): the browser finds a nonce whose `sha256("<salt>:<nonce>")` has the
challenge's leading zero bits (app/static/js/onion_pow.js). Without JavaScript a `<noscript>` link goes to a wait
step whose signed token is good from 15 s to 600 s after it was issued. Either proof is checked first on the POST,
before the login delay, the account lookup, LDAP or any password check, and each is good once. Clearnet never asks.

The time in tokens is `app.onion_guard._now`, frozen by the `clock` fixture (as tests/test_onion_guard_limits.py
does). Redis is the onion store's fakeredis, `onion_redis`.
"""
import hashlib
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from bs4 import BeautifulSoup
from flask import session
from itsdangerous import URLSafeTimedSerializer

import app.onion_guard as onion_guard
from app import db
from app.admin.forms import SiteMiscForm
from app.models import SiteDomain, User
from app.onion import mint_onion_sid
from app.onion_guard import (COOKIE_CHECK, ONION_SESSION_ENDPOINTS, DEFAULT_POW_DIFFICULTY, MAX_POW_DIFFICULTY, issue_challenge, issue_wait_token,
                             leading_zero_bits, pow_difficulty, verify_pow, wait_token_ready)
from app.storefront import clear_domain_cache
from app.utils import get_setting, set_setting
from tests.factories import make_instance, make_user
from tests.test_admin_routes_entry import _language, _seed, _settings_admin, _valid_misc_payload, csrf, login

PROXY = '10.89.0.2'
TOR_CONTAINER = '10.89.0.7'
ONION_URL = 'http://abc.onion'
MARKER = {'X-PieFed-Via': 'onion', 'X-Forwarded-For': TOR_CONTAINER}
FROM_CADDY = {'REMOTE_ADDR': PROXY}
PASSWORD = 'correct horse battery'
T0 = 1_800_000_000.0

# The shared vector: tests/js/onion_pow.test.mjs asserts that solve(SHARED_SALT, SHARED_BITS) finds SHARED_NONCE.
SHARED_SALT = '00112233445566778899aabbccddeeff'
SHARED_BITS = 12
SHARED_NONCE = '11y'
DIGITS = '0123456789abcdefghijklmnopqrstuvwxyz'


# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------

@pytest.fixture
def onion(app, site, monkeypatch, onion_redis):
    monkeypatch.setitem(app.config, 'ONION_PROXY_IP', PROXY)
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', True)   # the login and register forms render their token
    if not SiteDomain.query.filter_by(domain='test.piefed.local').first():
        db.session.add(SiteDomain(domain='test.piefed.local', scheme='https', is_default=True))
    db.session.add(SiteDomain(domain='abc.onion', scheme='http', federates=False))
    db.session.commit()
    clear_domain_cache()
    yield onion_redis
    clear_domain_cache()


@pytest.fixture
def clock(monkeypatch):
    now = SimpleNamespace(t=T0)
    monkeypatch.setattr('app.onion_guard._now', lambda: now.t)
    return now


@pytest.fixture
def easy(onion):
    """Difficulty 8, so a test solves a challenge in a few hundred hashes. (The suite's cache is NullCache and each
    test starts on empty tables, so a setting needs no undoing.)"""
    set_setting('onion_pow_difficulty', 8)


@pytest.fixture
def alice(onion):
    local = make_instance('test.piefed.local', software='piefed')
    make_user(local, 'founder', local=True)   # id 1 is exempt from every ban check
    user = make_user(local, 'alice', local=True)
    user.site_domain_id = SiteDomain.query.filter_by(domain='abc.onion').first().id   # her login name is the onion's
    user.email = 'alice@example.com'
    user.verified = True
    user.set_password(PASSWORD)
    db.session.commit()
    return user


@pytest.fixture
def dora(onion):
    """An account of the DEFAULT domain: on the onion she is reached as dora@test.piefed.local."""
    local = make_instance('test.piefed.local', software='piefed')
    make_user(local, 'founder', local=True)
    user = make_user(local, 'dora', local=True)
    user.site_domain_id = SiteDomain.query.filter_by(domain='test.piefed.local').first().id
    user.email = 'dora@example.com'
    user.verified = True
    user.set_password(PASSWORD)
    db.session.commit()
    return user


def in_session(app, sid='sid-a', **data):
    """A request whose session cookie brought back onion sid `sid` (None: a cookie with no sid), as a browser's does
    once session first has run. Proofs are bound to that round-tripped sid."""
    body = {**data, **({'onion_sid': sid} if sid else {})} or {'other': 1}
    cookie = app.session_interface.get_signing_serializer(app).dumps(body)
    return app.test_request_context('/', headers={'Cookie': f'{app.config["SESSION_COOKIE_NAME"]}={cookie}'})


def base36(number):
    digits = ''
    while True:
        number, digit = divmod(number, 36)
        digits = DIGITS[digit] + digits
        if not number:
            return digits


def zero_bits(salt, nonce):
    return leading_zero_bits(hashlib.sha256(f'{salt}:{nonce}'.encode()).digest())


def solve(salt, bits):
    """The first nonce, counting 0, 1, 2 ... in base 36 as onion_pow.js does, that meets `bits`."""
    number = 0
    while zero_bits(salt, base36(number)) < bits:
        number += 1
    return base36(number)


def miss(salt, bits):
    """A nonce that does not meet `bits`."""
    number = 0
    while zero_bits(salt, base36(number)) >= bits:
        number += 1
    return base36(number)


def onion_get(client, path, **kwargs):
    return client.get(path, base_url=ONION_URL, headers=MARKER, environ_base=FROM_CADDY, **kwargs)


def onion_post(client, path, **kwargs):
    return client.post(path, base_url=ONION_URL, headers=MARKER, environ_base=FROM_CADDY, **kwargs)


def onion_page(client, path):
    """The page at `path`, after session first (a 303 that sets the cookie, then one back to `path`), as a browser
    gets it."""
    response = onion_get(client, path)
    while response.status_code == 303:
        response = onion_get(client, response.headers['Location'])
    assert response.status_code == 200
    return BeautifulSoup(response.get_data(as_text=True), 'html.parser')


def value(page, name):
    field = page.find('input', attrs={'name': name})
    return None if field is None else field.get('value', '')


def challenge(page):
    nonce = page.find('input', attrs={'name': 'pow_nonce'})
    return SimpleNamespace(token=value(page, 'pow_token'), salt=nonce['data-salt'], bits=int(nonce['data-bits']),
                           csrf=value(page, 'csrf_token'))


def login_data(csrf_token, password=PASSWORD, **proof):
    return {'user_name': 'alice', 'password': password, 'timezone': 'UTC', 'submit': 'Log In',
            'csrf_token': csrf_token, **proof}


def flashes(client):
    with client.session_transaction(base_url=ONION_URL) as stored:
        return [message for _, message in stored.get('_flashes', [])]


def logged_in(client):
    with client.session_transaction(base_url=ONION_URL) as stored:
        return '_user_id' in stored


def failure_keys(redis):
    return [key for key in redis.keys('*') if key.startswith('onion_login_fail')]


def refused(response):
    """A proof refusal: the form again, with the message, and nothing else run."""
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert 'Your browser&#39;s check did not go through' in text or "Your browser's check did not go through" in text
    return True


def wait_token_from(page):
    content = page.find('meta', attrs={'http-equiv': 'refresh'})['content']
    seconds, url = content.split(';url=', 1)
    return int(seconds), url


# --------------------------------------------------------------------------
# The hash check
# --------------------------------------------------------------------------

@pytest.mark.parametrize('digest, bits', [
    (b'\x80' + b'\x00' * 31, 0),
    (b'\x01' + b'\xff' * 31, 7),
    (b'\x00\x80' + b'\x00' * 30, 8),
    (b'\x00\x00\x0f' + b'\xff' * 29, 20),
    (b'\x00' * 32, 256),
])
def test_leading_zero_bits_is_exact(digest, bits):
    assert leading_zero_bits(digest) == bits


def test_the_shared_vector(app, onion, monkeypatch):
    """tests/js/onion_pow.test.mjs finds the same nonce for the same salt, so the two sides agree on the check."""
    assert solve(SHARED_SALT, SHARED_BITS) == SHARED_NONCE
    assert zero_bits(SHARED_SALT, SHARED_NONCE) >= SHARED_BITS
    set_setting('onion_pow_difficulty', SHARED_BITS)
    monkeypatch.setattr(onion_guard, 'token_hex', lambda size: SHARED_SALT)
    with in_session(app):
        token = issue_challenge()
        assert verify_pow(token, SHARED_NONCE)


# --------------------------------------------------------------------------
# verify_pow
# --------------------------------------------------------------------------

def payload(token):
    return URLSafeTimedSerializer(onion_guard.current_app.config['SECRET_KEY'], salt='onion-pow').loads(token)


def test_a_challenge_carries_a_fresh_salt_and_the_difficulty(app, easy):
    with in_session(app):
        first, second = payload(issue_challenge()), payload(issue_challenge())
    assert first['bits'] == 8
    assert len(first['salt']) == 32 and first['salt'] != second['salt']


def test_verify_pow_accepts_a_solution_once(app, easy, clock):
    with in_session(app):
        token = issue_challenge()
        salt = payload(token)['salt']
        nonce = solve(salt, 8)
        assert verify_pow(token, nonce)
        assert not verify_pow(token, nonce)          # reused
    redis = onion_guard.app_pkg.onion_redis_client
    assert redis.get(f'onion_pow_used:{salt}') is not None
    assert redis.ttl(f'onion_pow_used:{salt}') == 660   # the token's 600 s, plus slack for its whole-second stamp


def test_verify_pow_refuses_an_expired_challenge(app, easy, clock):
    with in_session(app):
        token = issue_challenge()
        nonce = solve(payload(token)['salt'], 8)
        clock.t += 601
        assert not verify_pow(token, nonce)
        clock.t -= 1
        assert verify_pow(token, nonce)              # 600 s is still good, and the refusal spent nothing


def test_verify_pow_refuses_a_challenge_from_the_future(app, easy, clock):
    with in_session(app):
        token = issue_challenge()
        nonce = solve(payload(token)['salt'], 8)
        clock.t -= 5
        assert not verify_pow(token, nonce)


@pytest.mark.parametrize('nonce', ['', 'a' * 33, 'ab-c', 'ab c', '١', None, 12])
def test_verify_pow_refuses_a_malformed_nonce(app, onion, nonce):
    set_setting('onion_pow_difficulty', 0)                # any well-formed nonce would do
    with in_session(app):
        token = issue_challenge()
        assert not verify_pow(token, nonce)
        assert verify_pow(token, 'a' * 32)


def test_verify_pow_refuses_an_unsolved_challenge(app, easy):
    with in_session(app):
        token = issue_challenge()
        salt = payload(token)['salt']
        assert not verify_pow(token, miss(salt, 8))
        assert verify_pow(token, solve(salt, 8))      # the miss spent nothing


@pytest.mark.parametrize('tamper', [
    lambda token: token[:-2] + ('AA' if not token.endswith('AA') else 'BB'),
    lambda token: 'x' + token,
    lambda token: '',
    lambda token: None,
])
def test_verify_pow_refuses_a_bad_signature(app, easy, tamper):
    with in_session(app):
        token = issue_challenge()
        nonce = solve(payload(token)['salt'], 8)
        assert not verify_pow(tamper(token), nonce)


def test_verify_pow_refuses_a_challenge_signed_for_something_else(app, easy):
    with in_session(app):
        other = URLSafeTimedSerializer(app.config['SECRET_KEY'], salt='onion-wait').dumps({'salt': 'ab', 'bits': 0})
        assert not verify_pow(other, '0')
        wait = issue_wait_token()
        assert not verify_pow(wait, '0')


@pytest.mark.parametrize('body', [['ab', 0], {'salt': 'ab'}, {'salt': 3, 'bits': 0}, {'salt': 'ab', 'bits': True},
                                  {'salt': 'ab', 'bits': '0'}])
def test_verify_pow_refuses_a_payload_it_did_not_shape(app, onion, body):
    set_setting('onion_pow_difficulty', 0)
    with in_session(app):
        token = URLSafeTimedSerializer(app.config['SECRET_KEY'], salt='onion-pow').dumps(body)
        assert not verify_pow(token, '0')


def test_a_challenge_easier_than_the_setting_is_refused(app, easy):
    """Raising the difficulty applies at once: a challenge issued at the old one no longer counts."""
    with in_session(app):
        token = issue_challenge()
        salt = payload(token)['salt']
        number = 0
        while not 8 <= zero_bits(salt, base36(number)) < 12:
            number += 1
        set_setting('onion_pow_difficulty', 12)
        assert not verify_pow(token, base36(number))
        set_setting('onion_pow_difficulty', 8)
        assert verify_pow(token, base36(number))


def test_difficulty_zero_takes_any_nonce_but_still_once(app, onion):
    set_setting('onion_pow_difficulty', 0)
    with in_session(app):
        token = issue_challenge()
        assert payload(token)['bits'] == 0
        assert verify_pow(token, '0')
        assert not verify_pow(token, '0')


@pytest.mark.parametrize('stored, bits', [(16, 16), (0, 0), (24, 24), (-3, 0), (99, 24), ('12', 12)])
def test_the_difficulty_setting_is_clamped(app, onion, stored, bits):
    set_setting('onion_pow_difficulty', stored)
    assert pow_difficulty() == bits


def test_the_default_difficulty_is_16(app, onion):
    assert DEFAULT_POW_DIFFICULTY == 16 and MAX_POW_DIFFICULTY == 24
    assert get_setting('onion_pow_difficulty', None) in (None, 16)
    assert pow_difficulty() == 16


# --------------------------------------------------------------------------
# The wait token
# --------------------------------------------------------------------------

def test_wait_token_ready_between_15_and_600_seconds(app, onion, clock):
    with in_session(app):
        token = issue_wait_token()
        clock.t = T0 + 14
        assert not wait_token_ready(token)
        clock.t = T0 + 15
        assert wait_token_ready(token)
        assert not wait_token_ready(token)            # used
        salt = URLSafeTimedSerializer(app.config['SECRET_KEY'], salt='onion-wait').loads(token)['salt']
        assert onion.ttl(f'onion_wait_used:{salt}') == 660
        late = issue_wait_token()
        clock.t += 601
        assert not wait_token_ready(late)
        clock.t -= 1
        assert wait_token_ready(late)


@pytest.mark.parametrize('token', ['', 'nonsense', None, 7])
def test_a_bad_wait_token_is_not_ready(app, onion, clock, token):
    with in_session(app):
        assert not wait_token_ready(token)


def test_a_challenge_is_no_wait_token(app, easy, clock):
    with in_session(app):
        token = issue_challenge()
        clock.t += 20
        assert not wait_token_ready(token)
        body = URLSafeTimedSerializer(app.config['SECRET_KEY'], salt='onion-wait').dumps(['x'])
        assert not wait_token_ready(body)


def test_wait_tokens_and_challenges_are_spent_in_their_own_namespaces(app, onion, clock, monkeypatch):
    set_setting('onion_pow_difficulty', 0)
    monkeypatch.setattr(onion_guard, 'token_hex', lambda size: 'same')
    with in_session(app):
        challenge_token, wait = issue_challenge(), issue_wait_token()
        clock.t += 15
        assert verify_pow(challenge_token, '0')
        assert wait_token_ready(wait)
    assert onion.get('onion_pow_used:same') and onion.get('onion_wait_used:same')


# --------------------------------------------------------------------------
# Bound to the session
# --------------------------------------------------------------------------

def test_a_challenge_from_another_session_is_refused(app, easy):
    with in_session(app, 'sid-a'):
        token = issue_challenge()
        nonce = solve(payload(token)['salt'], 8)
    with in_session(app, 'sid-b'):
        assert not verify_pow(token, nonce)
    with in_session(app, None):
        assert not verify_pow(token, nonce)
    with in_session(app, 'sid-a'):
        assert verify_pow(token, nonce)               # the refusals spent nothing


def test_a_wait_token_from_another_session_is_refused(app, onion, clock):
    with in_session(app, 'sid-a'):
        token = issue_wait_token()
    clock.t += 15
    with in_session(app, 'sid-b'):
        assert not wait_token_ready(token)
    with in_session(app, None):
        assert not wait_token_ready(token)
    with in_session(app, 'sid-a'):
        assert wait_token_ready(token)


def test_a_sid_minted_during_this_request_is_refused(app, onion, clock):
    """A sid this request minted has not come back from the browser: it proves nothing about a session."""
    set_setting('onion_pow_difficulty', 0)
    with in_session(app, None):
        mint_onion_sid()
        token, wait = issue_challenge(), issue_wait_token()
        clock.t += 15
        assert not verify_pow(token, '0')
        assert not wait_token_ready(wait)
    with app.test_request_context('/'):              # no cookie at all
        mint_onion_sid()
        assert not verify_pow(issue_challenge(), '0')
    with app.test_request_context('/'):              # no session at all: nothing recorded, and never ready
        wait = issue_wait_token()
        clock.t += 15
        assert not wait_token_ready(wait)
    assert len(onion.keys('onion_wait_current:*')) == 1   # the minted sid's above, none for no session


@pytest.mark.parametrize('body', [{'salt': 'ab', 'bits': 0}, {'salt': 'ab', 'bits': 0, 'sid': 7}])
def test_a_challenge_without_its_sid_is_refused(app, onion, body):
    set_setting('onion_pow_difficulty', 0)
    with in_session(app, 'sid-a'):
        token = URLSafeTimedSerializer(app.config['SECRET_KEY'], salt='onion-pow').dumps(body)
        assert not verify_pow(token, '0')


def test_a_new_wait_token_replaces_the_last(app, onion, clock):
    """One wait token outstanding per session, so one session cannot queue up many waits at once."""
    with in_session(app, 'sid-a'):
        first = issue_wait_token()
        second = issue_wait_token()
    with in_session(app, 'sid-b'):
        other = issue_wait_token()                    # another session's does not replace this one's
    clock.t += 15
    with in_session(app, 'sid-a'):
        assert not wait_token_ready(first)
        assert wait_token_ready(second)
        assert not wait_token_ready(second)           # and still good once
    with in_session(app, 'sid-b'):
        assert wait_token_ready(other)


def test_the_wait_step_is_session_first(app, onion):
    assert 'auth.onion_wait' in ONION_SESSION_ENDPOINTS
    client = app.test_client(use_cookies=False)
    response = onion_get(client, '/auth/onion_wait?form=login')
    assert response.status_code == 303
    assert response.headers['Location'] == '/auth/onion_wait?form=login&' + COOKIE_CHECK


def test_a_proof_from_another_browser_is_refused(app, alice, easy, no_login):
    page = challenge(onion_page(app.test_client(), '/auth/login'))
    other = app.test_client()
    mine = challenge(onion_page(other, '/auth/login'))
    post_refused(other, onion_guard.app_pkg.onion_redis_client, no_login,
                 login_data(mine.csrf, pow_token=page.token, pow_nonce=solve(page.salt, 8)))


# --------------------------------------------------------------------------
# Login over the onion
# --------------------------------------------------------------------------

def test_valid_solution_accepted(app, alice, easy):
    client = app.test_client()
    page = challenge(onion_page(client, '/auth/login'))
    assert page.bits == 8
    response = onion_post(client, '/auth/login',
                          data=login_data(page.csrf, pow_token=page.token, pow_nonce=solve(page.salt, 8)))
    assert response.status_code == 302
    assert logged_in(client)


def test_a_default_domain_account_logs_in_on_the_onion_with_a_proof(app, dora, easy):
    client = app.test_client()
    page = challenge(onion_page(client, '/auth/login'))
    data = {**login_data(page.csrf, pow_token=page.token, pow_nonce=solve(page.salt, 8)),
            'user_name': 'dora@test.piefed.local'}
    assert onion_post(client, '/auth/login', data=data).status_code == 302
    assert logged_in(client)


def test_a_default_domain_name_without_a_proof_is_refused_before_any_lookup(app, dora, easy, no_login):
    client = app.test_client()
    page = challenge(onion_page(client, '/auth/login'))
    post_refused(client, onion_guard.app_pkg.onion_redis_client, no_login,
                 {**login_data(page.csrf), 'user_name': 'dora@test.piefed.local'})


@pytest.fixture
def no_login(monkeypatch):
    """process_login, refused if called: a refused proof must never reach the delay, the lookup or the password."""
    called = []
    monkeypatch.setattr('app.auth.routes.process_login', lambda form: called.append(form) or 'processed')
    monkeypatch.setattr(User, 'check_password', lambda *args, **kwargs: pytest.fail('password checked'))
    return called


def post_refused(client, onion, no_login, data):
    response = onion_post(client, '/auth/login', data=data)
    assert refused(response)
    assert no_login == []
    assert failure_keys(onion) == []
    assert not logged_in(client)
    return response


def test_expired(app, alice, easy, clock, no_login):
    client = app.test_client()
    page = challenge(onion_page(client, '/auth/login'))
    clock.t += 601
    post_refused(client, onion_guard.app_pkg.onion_redis_client, no_login,
                 login_data(page.csrf, pow_token=page.token, pow_nonce=solve(page.salt, 8)))


def test_bad_signature(app, alice, easy, no_login):
    client = app.test_client()
    page = challenge(onion_page(client, '/auth/login'))
    forged = URLSafeTimedSerializer('not the key', salt='onion-pow').dumps({'salt': page.salt, 'bits': 0})
    post_refused(client, onion_guard.app_pkg.onion_redis_client, no_login,
                 login_data(page.csrf, pow_token=forged, pow_nonce='0'))


def test_reused(app, alice, easy, monkeypatch):
    client = app.test_client()
    page = challenge(onion_page(client, '/auth/login'))
    proof = {'pow_token': page.token, 'pow_nonce': solve(page.salt, 8)}
    first = onion_post(client, '/auth/login', data=login_data(page.csrf, password='wrong password', **proof))
    assert first.status_code == 302                    # a failed login still spends the proof
    assert not logged_in(client)
    called = []
    monkeypatch.setattr('app.auth.routes.process_login', lambda form: called.append(form) or 'processed')
    keys = failure_keys(onion_guard.app_pkg.onion_redis_client)
    response = onion_post(client, '/auth/login', data=login_data(page.csrf, **proof))
    assert refused(response)
    assert called == []
    assert failure_keys(onion_guard.app_pkg.onion_redis_client) == keys
    assert not logged_in(client)


@pytest.mark.parametrize('proof', ['unsolved', 'no nonce', 'no token', 'nothing'])
def test_unsolved(app, alice, easy, no_login, proof):
    client = app.test_client()
    page = challenge(onion_page(client, '/auth/login'))
    data = {'unsolved': {'pow_token': page.token, 'pow_nonce': miss(page.salt, 8)},
            'no nonce': {'pow_token': page.token},
            'no token': {'pow_nonce': solve(page.salt, 8)},
            'nothing': {}}[proof]
    post_refused(client, onion_guard.app_pkg.onion_redis_client, no_login, login_data(page.csrf, **data))


def test_a_refusal_shows_a_new_challenge(app, alice, easy, no_login):
    client = app.test_client()
    page = challenge(onion_page(client, '/auth/login'))
    response = post_refused(client, onion_guard.app_pkg.onion_redis_client, no_login, login_data(page.csrf))
    again = challenge(BeautifulSoup(response.get_data(as_text=True), 'html.parser'))
    assert again.token and again.token != page.token
    assert value(BeautifulSoup(response.get_data(as_text=True), 'html.parser'), 'user_name') == 'alice'


def visible_wait_links(page):
    """Links to the wait step a browser without JavaScript shows: not inside <noscript> (shown only when the browser
    itself has JavaScript off, not when an extension blocks it) and not under a hidden element."""
    return [a['href'] for a in page.find_all('a', href=lambda href: href and href.startswith('/auth/onion_wait'))
            if not a.find_parent('noscript') and not any(p.has_attr('hidden') for p in [a, *a.parents])]


def test_a_refused_login_offers_the_wait_step(app, alice, easy, no_login):
    """Tor Browser at Safest runs no JavaScript, so the challenge is never solved: trying again can never work, and
    the refusal must show the way that does."""
    client = app.test_client()
    page = challenge(onion_page(client, '/auth/login?next=%2Fc%2Fnews'))
    assert visible_wait_links(onion_page(client, '/auth/login')) == []
    response = post_refused(client, onion_guard.app_pkg.onion_redis_client, no_login, login_data(page.csrf))
    assert visible_wait_links(BeautifulSoup(response.get_data(as_text=True), 'html.parser')) == \
        ['/auth/onion_wait?form=login']


def test_a_refused_registration_offers_the_wait_step(app, open_registration, easy):
    client = app.test_client()
    page = challenge(onion_page(client, '/auth/register'))
    response = onion_post(client, '/auth/register', data=register_data(page.csrf))
    assert refused(response)
    assert visible_wait_links(BeautifulSoup(response.get_data(as_text=True), 'html.parser')) == \
        ['/auth/onion_wait?form=register']


def test_the_proof_is_checked_before_the_login_delay(app, alice, easy):
    """The Task 9 delay reservation counts an attempt; a refused proof must not, so it cannot lock anyone out."""
    client = app.test_client()
    page = challenge(onion_page(client, '/auth/login'))
    with patch('app.auth.util.onion_login_gate', side_effect=AssertionError('delay reserved')) as gate:
        assert refused(onion_post(client, '/auth/login', data=login_data(page.csrf)))
    gate.assert_not_called()


def test_wait_token_too_early_then_ok(app, alice, clock):
    client = app.test_client()
    seconds, url = wait_token_from(onion_page(client, '/auth/onion_wait?form=login'))
    assert seconds == 15
    page = onion_page(client, url)
    token = value(page, 'wait_token')
    assert token
    csrf_token = value(page, 'csrf_token')
    clock.t = T0 + 14
    assert refused(onion_post(client, '/auth/login', data=login_data(csrf_token, wait_token=token)))
    assert not logged_in(client)
    clock.t = T0 + 15
    response = onion_post(client, '/auth/login', data=login_data(csrf_token, wait_token=token))
    assert response.status_code == 302
    assert logged_in(client)


def test_an_unready_wait_token_falls_back_to_the_challenge(app, alice, easy, clock):
    """Either proof will do: a wait token not yet good leaves a solved challenge to decide."""
    client = app.test_client()
    page = challenge(onion_page(client, '/auth/login'))
    with app.test_request_context():
        wait = issue_wait_token()
    response = onion_post(client, '/auth/login', data=login_data(page.csrf, wait_token=wait, pow_token=page.token,
                                                                 pow_nonce=solve(page.salt, 8)))
    assert response.status_code == 302
    assert logged_in(client)


# --------------------------------------------------------------------------
# The pages
# --------------------------------------------------------------------------

def test_noscript_link_present(app, onion):
    client = app.test_client()
    page = onion_page(client, '/auth/login?next=%2Fc%2Fnews')
    link = page.find('noscript').find('a')
    assert link['href'] == '/auth/onion_wait?form=login&next=/c/news'
    fallback = page.find(id='onion_pow_fallback')
    assert fallback.has_attr('hidden')
    assert fallback.find('a')['href'] == link['href']

    wait = onion_page(client, link['href'])
    seconds, url = wait_token_from(wait)
    assert seconds == 15
    assert url.startswith('/auth/login?')
    assert 'next=/c/news' in url and 'wait_token=' in url
    assert wait.find('a', href=url) is not None        # and a link, for a browser that ignores the refresh
    assert 'onion' not in url and 'http' not in url and app.config['SERVER_NAME'] not in str(wait)


def test_the_wait_step_for_registration(app, open_registration):
    client = app.test_client()
    page = onion_page(client, '/auth/register')
    link = page.find('noscript').find('a')
    assert link['href'] == '/auth/onion_wait?form=register'
    seconds, url = wait_token_from(onion_page(client, link['href']))
    assert url.startswith('/auth/register?wait_token=')


def test_an_unknown_wait_target_is_the_login_form(app, onion):
    client = app.test_client()
    _, url = wait_token_from(onion_page(client, '/auth/onion_wait?form=https://evil.example'))
    assert url.startswith('/auth/login?wait_token=')


def test_the_wait_step_is_onion_only(app, site):
    assert app.test_client().get('/auth/onion_wait?form=login').status_code == 404


def test_the_proof_fields_are_not_restored_by_the_browser(app, onion):
    """Firefox restores hidden inputs' values on reload but not their data attributes: an old token beside a new
    salt is a refusal. autocomplete=off stops the restore."""
    page = onion_page(app.test_client(), '/auth/login')
    for name in ('pow_token', 'pow_nonce', 'wait_token'):
        assert page.find('input', attrs={'name': name})['autocomplete'] == 'off'


def test_pow_js_self_hosted(app, onion):
    page = onion_page(app.test_client(), '/auth/login')
    sources = [script['src'] for script in page.find_all('script', src=True) if 'onion_pow' in script['src']]
    assert len(sources) == 1
    assert sources[0].split('?')[0] == '/static/js/onion_pow.js'
    script = page.find('script', src=sources[0])
    assert script.get('type') == 'module' and script.get('nonce') is not None


def test_clearnet_has_no_pow(app, site, monkeypatch, redis_double, onion_redis):
    local = make_instance('test.piefed.local', software='piefed')
    make_user(local, 'founder', local=True)
    user = make_user(local, 'alice', local=True)
    user.verified = True
    user.set_password(PASSWORD)
    db.session.commit()
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', True)
    client = app.test_client()
    for path in ('/auth/login', '/auth/register'):
        text = client.get(path).get_data(as_text=True)
        for marker in ('pow_token', 'pow_nonce', 'wait_token', 'onion_pow', 'onion_wait'):
            assert marker not in text
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', False)
    response = client.post('/auth/login', data={'user_name': 'alice', 'password': PASSWORD, 'timezone': 'UTC',
                                                'submit': 'Log In'})
    assert response.status_code == 302
    with client.session_transaction() as stored:
        assert str(stored['_user_id']) == str(user.id)
    assert redis_double.keys('onion_*') == [] and onion_redis.keys('*') == []


# --------------------------------------------------------------------------
# Registration over the onion
# --------------------------------------------------------------------------

@pytest.fixture
def open_registration(onion, site, monkeypatch):
    """Open registration, with no admin around to keep it open: the abandoned-instance safety is not under test."""
    monkeypatch.setattr('app.auth.routes.handle_abandoned_open_instance', lambda: None)
    site.registration_mode = 'Open'
    db.session.commit()


def register_data(csrf_token, **proof):
    return {'user_name': 'newcomer', 'real_email': 'new@example.com', 'password': PASSWORD, 'password2': PASSWORD,
            'timezone': 'UTC', 'submit': 'Register', 'csrf_token': csrf_token, **proof}


def test_registration_needs_a_proof(app, open_registration, easy, monkeypatch):
    called = []
    monkeypatch.setattr('app.auth.routes.process_registration_form', lambda form: called.append(form) or 'registered')
    set_setting('captcha_enabled', False)
    client = app.test_client()
    page = challenge(onion_page(client, '/auth/register'))
    response = onion_post(client, '/auth/register', data=register_data(page.csrf, pow_token=page.token))
    assert refused(response)
    assert called == []
    page = challenge(BeautifulSoup(response.get_data(as_text=True), 'html.parser'))
    response = onion_post(client, '/auth/register',
                          data=register_data(page.csrf, pow_token=page.token, pow_nonce=solve(page.salt, 8)))
    assert response.get_data(as_text=True) == 'registered'
    assert len(called) == 1


def test_a_refused_registration_proof_runs_no_form_validation(app, open_registration, easy, monkeypatch):
    """The form's validators look the email and name up; a refused proof stops before them."""
    monkeypatch.setattr('app.auth.forms.RegistrationForm.validate_real_email',
                        lambda self, field: pytest.fail('email looked up'))
    client = app.test_client()
    page = challenge(onion_page(client, '/auth/register'))
    assert refused(onion_post(client, '/auth/register', data=register_data(page.csrf)))


def test_clearnet_registration_needs_no_proof(app, site, monkeypatch):
    monkeypatch.setattr('app.auth.routes.handle_abandoned_open_instance', lambda: None)
    site.registration_mode = 'Open'
    db.session.commit()
    called = []
    monkeypatch.setattr('app.auth.routes.process_registration_form', lambda form: called.append(form) or 'registered')
    set_setting('captcha_enabled', False)
    response = app.test_client().post('/auth/register', data=register_data(''))
    assert response.get_data(as_text=True) == 'registered'


# --------------------------------------------------------------------------
# The admin setting
# --------------------------------------------------------------------------

def test_the_misc_form_bounds_the_difficulty(app):
    with app.test_request_context():
        field = SiteMiscForm().onion_pow_difficulty
        bounds = [v for v in field.validators if hasattr(v, 'min')][0]
        assert (bounds.min, bounds.max) == (0, MAX_POW_DIFFICULTY)


@pytest.mark.parametrize('posted, stored, errors', [('20', 20, set()), ('0', 0, set()), ('', 16, set()), (None, 16, set()),
                                                    ('25', 16, {'onion_pow_difficulty'}),
                                                    ('-1', 16, {'onion_pow_difficulty'})])
def test_the_admin_sets_the_difficulty(app, db_session, posted, stored, errors):
    instance, _ordinary = _seed()
    admin = _settings_admin(instance)
    language = _language()
    client = app.test_client()
    login(client, admin)
    token = csrf(app, client)
    data = _valid_misc_payload(language)
    data.update({'submit': 'Save', 'csrf_token': token})
    if posted is None:
        del data['onion_pow_difficulty']              # a save that leaves it out keeps the setting
    else:
        data['onion_pow_difficulty'] = posted
    with patch('app.admin.routes.render_template', return_value='rendered') as render:
        client.post('/admin/misc', data=data)
    assert set(render.call_args.kwargs['form'].errors) == errors
    assert get_setting('onion_pow_difficulty', 16) == stored


def test_the_admin_page_shows_the_difficulty(app, db_session):
    instance, _ordinary = _seed()
    admin = _settings_admin(instance)
    client = app.test_client()
    login(client, admin)
    with patch('app.admin.routes.render_template', return_value='rendered') as render:
        client.get('/admin/misc')
    assert render.call_args.kwargs['form'].onion_pow_difficulty.data == 16
    set_setting('onion_pow_difficulty', 11)
    with patch('app.admin.routes.render_template', return_value='rendered') as render:
        client.get('/admin/misc')
    assert render.call_args.kwargs['form'].onion_pow_difficulty.data == 11

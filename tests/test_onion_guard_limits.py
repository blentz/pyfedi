"""Onion abuse limits (onion storefront spec 2026-10-17, sections 3.7 and 3.7a).

Every onion visitor shares the Tor container's address, so onion rate limits are keyed by session, and by account for
the API. A sid only counts once the browser has sent it back: a sid minted during the request, and every cookieless
or unauthenticated onion client, share the one `onion-anon` bucket per endpoint. A cookieless request to a rate-limited
web route is sent round once to pick up a session cookie (session first), and failed onion logins slow that account
down. Clearnet is unchanged throughout.

The `app` fixture is session-scoped, so every config change is a monkeypatch and is undone. The rate limiter is off
for the suite (conftest `disable_rate_limiter`); the tests that need it build their own app with it on, as
tests/test_home_live.py does, and reset its storage before and after.
"""
import ast
import inspect
import pathlib
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import session
from werkzeug.datastructures import Headers

import app as app_pkg
import app.onion_guard as onion_guard
from app import create_app, db, limiter, rate_limit_key
from app.auth.util import find_user
from app.models import SiteDomain
from app.onion import ONION_ANON_KEY, arrived_onion_sid, mint_onion_sid
from app.onion_guard import (COOKIE_CHECK, ONION_SESSION_ENDPOINTS, ONION_SESSION_EXEMPT,
                             clear_onion_login_failures, login_delay_remaining, onion_session_missing,
                             OnionLoginGate, record_onion_login_failure, reserve_onion_login_attempt, resolve_onion_api_rate_key,
                             same_url)
from app.storefront import clear_domain_cache
from tests.conftest import TestConfig
from tests.factories import make_instance, make_user

PROXY = '10.89.0.2'
TOR_CONTAINER = '10.89.0.7'
ONION_URL = 'http://abc.onion'
CLEARNET_URL = 'https://test.piefed.local'
MARKER = {'X-PieFed-Via': 'onion', 'X-Forwarded-For': TOR_CONTAINER}
FROM_CADDY = {'REMOTE_ADDR': PROXY}
LOGIN = {'timezone': 'UTC', 'submit': 'Log In'}
PASSWORD = 'correct horse battery'


@pytest.fixture
def onion(app, site, monkeypatch):
    monkeypatch.setitem(app.config, 'ONION_PROXY_IP', PROXY)
    if not SiteDomain.query.filter_by(domain='test.piefed.local').first():
        db.session.add(SiteDomain(domain='test.piefed.local', scheme='https', is_default=True))
    db.session.add(SiteDomain(domain='abc.onion', scheme='http', federates=False))
    db.session.commit()
    clear_domain_cache()
    yield
    clear_domain_cache()


@pytest.fixture(autouse=True)
def login_form(monkeypatch):
    """The login page, without its template: the suite turns CSRF off, and auth/login.html needs form.csrf_token."""
    monkeypatch.setattr('app.auth.routes.render_login_form', lambda form: 'the login form')


@pytest.fixture
def clock(monkeypatch):
    now = SimpleNamespace(t=1_800_000_000.0)
    monkeypatch.setattr('app.onion_guard._now', lambda: now.t)
    return now


@pytest.fixture
def alice(onion):
    local = make_instance('test.piefed.local', software='piefed')
    make_user(local, 'founder', local=True)   # id 1 is exempt from every ban check
    user = make_user(local, 'alice', local=True)
    user.email = 'alice@example.com'
    user.verified = True
    user.set_password(PASSWORD)
    db.session.commit()
    return user


def onion_get(client, path, headers=None, **kwargs):
    return client.get(path, base_url=ONION_URL, headers={**MARKER, **(headers or {})}, environ_base=FROM_CADDY,
                      **kwargs)


def onion_post(client, path, headers=None, **kwargs):
    return client.post(path, base_url=ONION_URL, headers={**MARKER, **(headers or {})}, environ_base=FROM_CADDY,
                       **kwargs)


def onion_context(app, path='/', headers=None, cookie=None):
    """A request context shaped as ProxyFix leaves an onion request; `cookie` is a session to send in."""
    headers = Headers({**MARKER, 'Host': 'abc.onion', **(headers or {})})
    if cookie is not None:
        value = app.session_interface.get_signing_serializer(app).dumps(cookie)
        headers.add('Cookie', f'{app.config["SESSION_COOKIE_NAME"]}={value}')
    return app.test_request_context(path, headers=headers,
                                    environ_base={'REMOTE_ADDR': TOR_CONTAINER, 'werkzeug.proxy_fix.orig': FROM_CADDY})


def session_cookie(app, data):
    return app.session_interface.get_signing_serializer(app).dumps(data)


def flashes(client):
    with client.session_transaction(base_url=ONION_URL) as stored:
        return ' '.join(message for _, message in stored.get('_flashes', []))


def a_session(client):
    """Session first, as a browser does it: the cookieless 303 sets the cookie and the client keeps it."""
    response = onion_get(client, '/auth/login')
    assert response.status_code == 303
    return response


# --------------------------------------------------------------------------
# Session first (3.7a C)
# --------------------------------------------------------------------------

def test_cookieless_get_redirects(app, onion):
    client = app.test_client(use_cookies=False)
    response = onion_get(client, '/auth/login?next=%2Fc%2Fnews&x=%C3%A9')
    assert response.status_code == 303
    assert response.headers['Location'] == '/auth/login?next=%2Fc%2Fnews&x=%C3%A9&' + COOKIE_CHECK
    assert any(h.startswith(app.config['SESSION_COOKIE_NAME'] + '=') for h in response.headers.getlist('Set-Cookie'))


def test_the_cookie_round_trip_lands_on_the_same_url(app, onion):
    client = app.test_client()
    first = onion_get(client, '/auth/login?next=%2Fc%2Fnews')
    back = onion_get(client, first.headers['Location'])
    assert back.status_code == 303
    assert back.headers['Location'] == '/auth/login?next=%2Fc%2Fnews'
    page = onion_get(client, back.headers['Location'])
    assert page.status_code == 200
    assert page.get_data(as_text=True) == 'the login form'


def test_cookieless_post_refused_softly(app, onion):
    client = app.test_client()
    with patch('app.auth.routes.process_login') as process_login:
        response = onion_post(client, '/auth/login', data={'user_name': 'alice', 'password': PASSWORD, **LOGIN})
        assert response.status_code == 303
        assert response.headers['Location'] == '/auth/login?' + COOKIE_CHECK
        landed = onion_get(client, onion_get(client, response.headers['Location']).headers['Location'])
    process_login.assert_not_called()
    assert landed.status_code == 200
    assert 'Please try again' in flashes(client)


def test_a_client_that_refuses_the_cookie_gets_a_page_not_a_loop(app, onion):
    client = app.test_client(use_cookies=False)
    first = onion_get(client, '/auth/login?next=%2Fx')
    again = onion_get(client, first.headers['Location'])
    assert again.status_code == 400
    assert 'Location' not in again.headers
    body = again.get_data(as_text=True)
    assert 'cookie' in body.lower()
    assert 'href="/auth/login?next=%2Fx"' in body
    posted = onion_post(client, first.headers['Location'], data={'user_name': 'alice', 'password': PASSWORD, **LOGIN})
    assert posted.status_code == 400


def test_the_marker_is_only_stripped_from_a_get(app, onion):
    client = app.test_client()
    a_session(client)
    with patch('app.auth.routes.process_login', return_value='processed') as process_login:
        response = onion_post(client, '/auth/login?' + COOKIE_CHECK,
                              data={'user_name': 'alice', 'password': PASSWORD, **LOGIN})
    assert response.status_code == 200
    process_login.assert_called_once()


@pytest.mark.parametrize('url, path_info, location', [
    ('/', '//evil.example/a', '/evil.example/a'),
    ('/', '/a\\b', '/a%5Cb'),
    ('/caf%C3%A9', None, '/caf%C3%A9'),
])
def test_the_redirect_target_is_a_path_on_this_storefront(app, url, path_info, location):
    """Built from the path, never from Host: a doubled slash or a backslash would make it protocol-relative."""
    overrides = {'PATH_INFO': path_info} if path_info else {}
    with app.test_request_context(url, environ_overrides=overrides, headers={'Host': 'evil.example'}):
        assert same_url([]) == location
        assert same_url([b'a=1', COOKIE_CHECK.encode()]) == f'{location}?a=1&{COOKIE_CHECK}'


def test_a_session_cookie_without_a_sid_is_let_through(app, onion):
    """A client that kept a session cookie (from any page) is not cookieless. It gets a sid now; this request is
    still in the shared bucket because the sid has not come back yet."""
    client = app.test_client(use_cookies=False)
    cookie = session_cookie(app, {'_fresh': False})
    response = onion_get(client, '/auth/login', headers={'Cookie': f'{app.config["SESSION_COOKIE_NAME"]}={cookie}'})
    assert response.status_code == 200
    assert response.headers.getlist('Set-Cookie')


def test_api_requests_are_never_sent_round(app, onion):
    client = app.test_client(use_cookies=False)
    response = onion_post(client, '/api/alpha/user/login', json={'username': 'alice', 'password': PASSWORD})
    assert response.status_code != 303


def test_a_route_without_a_limit_is_not_sent_round(app, onion):
    client = app.test_client(use_cookies=False)
    assert onion_get(client, '/robots.txt').status_code == 200


def test_clearnet_cookieless_unchanged(app, onion):
    client = app.test_client(use_cookies=False)
    assert client.get('/auth/login', base_url=CLEARNET_URL).status_code == 200
    assert client.get('/auth/login?' + COOKIE_CHECK, base_url=CLEARNET_URL).status_code == 200
    with patch('app.auth.routes.process_login', return_value='processed') as process_login:
        response = client.post('/auth/login', base_url=CLEARNET_URL,
                               data={'user_name': 'alice', 'password': PASSWORD, **LOGIN})
    assert response.status_code == 200
    process_login.assert_called_once()
    # The marker from anywhere but Caddy is a clearnet request
    spoofed = client.get('/auth/login', base_url=ONION_URL, headers=MARKER, environ_base={'REMOTE_ADDR': '203.0.113.9'})
    assert spoofed.status_code == 200


def test_onion_session_missing(app, onion):
    with onion_context(app, '/auth/login'):
        assert onion_session_missing()
    with onion_context(app, '/auth/login', cookie={'x': 1}):
        assert not onion_session_missing()
    with onion_context(app, '/robots.txt'):
        assert not onion_session_missing()
    with app.test_request_context('/auth/login', base_url=CLEARNET_URL):
        assert not onion_session_missing()


# --------------------------------------------------------------------------
# Which endpoints are session first
# --------------------------------------------------------------------------

def limiter_decorated_functions():
    """(file, function name) of every function under app/ with a `@limiter.limit(...)` decorator, found by parsing,
    so a decorator split over several lines is found too."""
    root = pathlib.Path(app_pkg.__file__).resolve().parent
    found = set()
    for path in root.rglob('*.py'):
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                target = decorator.func if isinstance(decorator, ast.Call) else decorator
                if isinstance(target, ast.Attribute) and target.attr == 'limit' \
                        and isinstance(target.value, ast.Name) and target.value.id == 'limiter':
                    found.add((str(path), node.name))
    return found


def test_every_limited_endpoint_is_session_first_or_exempt(app):
    functions = limiter_decorated_functions()
    endpoints = {}
    for endpoint, view in app.view_functions.items():
        original = inspect.unwrap(view)
        key = (str(pathlib.Path(original.__code__.co_filename).resolve()), original.__name__)
        if key in functions:
            endpoints.setdefault(key, set()).add(endpoint)
    assert set(endpoints) == functions, f'decorated but not routed: {functions - set(endpoints)}'
    limited = set().union(*endpoints.values())
    assert ONION_SESSION_ENDPOINTS.isdisjoint(ONION_SESSION_EXEMPT)
    assert limited == ONION_SESSION_ENDPOINTS | ONION_SESSION_EXEMPT, \
        f'add to ONION_SESSION_ENDPOINTS or ONION_SESSION_EXEMPT: {limited - ONION_SESSION_ENDPOINTS - ONION_SESSION_EXEMPT}'
    assert {'auth.login', 'auth.register', 'auth.reset_password_request', 'auth.resend_email'} <= ONION_SESSION_ENDPOINTS
    assert isinstance(ONION_SESSION_ENDPOINTS, frozenset)


# --------------------------------------------------------------------------
# The rate-limit key
# --------------------------------------------------------------------------

def test_a_minted_sid_is_the_shared_key(app, onion):
    with onion_context(app, '/auth/login'):
        assert rate_limit_key() == ONION_ANON_KEY
        assert session['onion_sid']                       # minted, so the response sends it
        assert rate_limit_key() == ONION_ANON_KEY         # and it stays shared for the rest of this request


def test_a_round_tripped_sid_is_its_own_key(app, onion):
    with onion_context(app, '/auth/login', cookie={'onion_sid': 'sid-a'}):
        assert rate_limit_key() == 'onion-session:sid-a'
    with onion_context(app, '/auth/login', cookie={'onion_sid': 'sid-b'}):
        assert rate_limit_key() == 'onion-session:sid-b'


def test_the_arrived_sid_is_read_before_any_mint(app, onion):
    with onion_context(app, '/auth/login'):
        mint_onion_sid()
        assert arrived_onion_sid() is None
        assert rate_limit_key() == ONION_ANON_KEY
    with onion_context(app, '/auth/login', cookie={'onion_sid': 'sid-a'}):
        mint_onion_sid()
        assert session['onion_sid'] == 'sid-a'


def test_clearnet_key_is_still_the_ip(app, onion):
    with app.test_request_context('/auth/login', environ_base={'REMOTE_ADDR': '9.9.9.9'}):
        assert rate_limit_key() == '9.9.9.9'
        assert 'onion_sid' not in session


def api_user(onion):
    make_instance('test.piefed.local', software='piefed')
    user = make_user(None, 'apiuser', local=True)
    user.verified = True
    db.session.commit()
    return user


def test_an_onion_api_request_is_keyed_by_its_account(app, onion):
    user = api_user(onion)
    with onion_context(app, '/api/alpha/site', {'Authorization': f'Bearer {user.encode_jwt_token()}'}):
        resolve_onion_api_rate_key()
        assert rate_limit_key() == f'onion-user:{user.id}'
        assert 'onion_sid' not in session   # an API client keeps no cookies, so none is minted


@pytest.mark.parametrize('headers', [{}, {'Authorization': 'Bearer not-a-token'}, {'Authorization': 'Basic x'}])
def test_an_unauthenticated_onion_api_request_is_shared(app, onion, headers):
    with onion_context(app, '/api/alpha/site', headers):
        resolve_onion_api_rate_key()
        assert rate_limit_key() == ONION_ANON_KEY
        assert 'onion_sid' not in session


def test_an_api_session_cookie_does_not_make_a_session_key(app, onion):
    with onion_context(app, '/api/alpha/site', cookie={'onion_sid': 'sid-a'}):
        resolve_onion_api_rate_key()
        assert rate_limit_key() == ONION_ANON_KEY


def test_an_application_error_while_authorising_is_not_swallowed(app, onion):
    with onion_context(app, '/api/alpha/site', {'Authorization': 'Bearer x'}), \
            patch('app.onion_guard.authorise_api_user', side_effect=RuntimeError('storage down')):
        with pytest.raises(RuntimeError):
            resolve_onion_api_rate_key()


def test_the_api_hook_leaves_clearnet_and_web_alone(app, onion):
    with patch('app.onion_guard.authorise_api_user') as authorise:
        with app.test_request_context('/api/alpha/site', headers={'Authorization': 'Bearer x'}):
            resolve_onion_api_rate_key()
        with onion_context(app, '/auth/login', {'Authorization': 'Bearer x'}):
            resolve_onion_api_rate_key()
    authorise.assert_not_called()


# --------------------------------------------------------------------------
# The limits themselves, with the limiter on
# --------------------------------------------------------------------------

class LimitedConfig(TestConfig):
    RATELIMIT_ENABLED = True
    ONION_PROXY_IP = PROXY


@pytest.fixture(scope='module')
def limited_app():
    limited = create_app(LimitedConfig)

    @limited.route('/api/alpha/onion_probe')
    def onion_probe():
        with limiter.limit('2/minute'):
            return 'ok'

    limiter.enabled = False
    yield limited
    limiter.enabled = False


@pytest.fixture
def limits(limited_app, onion):
    limiter.reset()
    limiter.enabled = True
    try:
        yield limited_app
    finally:
        limiter.reset()
        limiter.enabled = False


def test_session_bucket(limits):
    """/auth/login is "300 per day;10 per 5 minutes". Each onion session has its own 10."""
    a, b = limits.test_client(), limits.test_client()
    a_session(a)
    a_session(b)
    statuses = [onion_get(a, '/auth/login').status_code for _ in range(11)]
    assert statuses[:10] == [200] * 10 and statuses[10] == 429
    assert onion_get(b, '/auth/login').status_code == 200


def test_a_cookie_dropping_client_never_reaches_the_view(limits):
    client = limits.test_client(use_cookies=False)
    assert {onion_get(client, '/auth/login').status_code for _ in range(15)} == {303}


def test_minted_sids_share_one_bucket(limits):
    """A client that replays a session cookie without a sid, dropping each new sid it is given, is never in a
    bucket of its own: every request is `onion-anon`, shared with every other such client."""
    stale = f'{limits.config["SESSION_COOKIE_NAME"]}={session_cookie(limits, {"_fresh": False})}'
    statuses = []
    for _ in range(11):
        client = limits.test_client(use_cookies=False)
        response = onion_get(client, '/auth/login', headers={'Cookie': stale})
        statuses.append(response.status_code)
    assert statuses[:10] == [200] * 10 and statuses[10] == 429
    a = limits.test_client()
    a_session(a)
    assert onion_get(a, '/auth/login').status_code == 200   # a round-tripped sid is not in that bucket


def test_cookieless_clients_share_a_limit_where_there_is_no_redirect(limits):
    """/api/is_ip_banned (60 per minute) is not session first. Cookieless onion callers share its one bucket."""
    statuses = [onion_post(limits.test_client(use_cookies=False), '/api/is_ip_banned', data={'ip_addresses': ''})
                .status_code for _ in range(61)]
    assert 429 not in statuses[:60] and statuses[60] == 429


def test_onion_api_buckets_are_per_account(limits):
    with limits.app_context():
        make_instance('test.piefed.local', software='piefed')
        one, two = make_user(None, 'apione', local=True), make_user(None, 'apitwo', local=True)
        one.verified = two.verified = True
        db.session.commit()
        bearer = {u.user_name: {'Authorization': f'Bearer {u.encode_jwt_token()}'} for u in (one, two)}
    client = limits.test_client(use_cookies=False)
    probe = lambda headers: onion_get(client, '/api/alpha/onion_probe', headers=headers).status_code
    assert [probe(bearer['apione']) for _ in range(3)] == [200, 200, 429]
    assert probe(bearer['apitwo']) == 200
    assert [probe({}) for _ in range(3)] == [200, 200, 429]
    assert probe({'Authorization': 'Bearer forged'}) == 429   # shares the unauthenticated bucket
    assert probe(bearer['apitwo']) == 200


# --------------------------------------------------------------------------
# The per-account login delay (3.7)
# --------------------------------------------------------------------------

def test_login_delay_formula(app, redis_double, clock):
    for _ in range(5):
        record_onion_login_failure('alice')
    assert login_delay_remaining('alice') == 0
    record_onion_login_failure('alice')
    assert login_delay_remaining('alice') == 2
    record_onion_login_failure('alice')
    assert login_delay_remaining('alice') == 4
    clock.t += 1.5
    assert login_delay_remaining('alice') == 3
    for _ in range(30):
        record_onion_login_failure('alice')
    assert login_delay_remaining('alice') == 300
    assert redis_double.hget('onion_login_fail:alice', 'count') == '37'
    assert 3500 < redis_double.ttl('onion_login_fail:alice') <= 3600


def test_the_clock_is_the_wall_clock():
    assert abs(onion_guard._now() - time.time()) < 60


def test_the_key_is_the_lower_case_name(app, redis_double, clock):
    for _ in range(6):
        record_onion_login_failure(' Alice ')
    assert login_delay_remaining('ALICE') == 2
    assert redis_double.exists('onion_login_fail:alice')
    clear_onion_login_failures('aLiCe')
    assert not redis_double.exists('onion_login_fail:alice')
    assert login_delay_remaining('alice') == 0


def test_reserving_is_the_failure(app, redis_double, clock):
    """An attempt is counted when it is let through, before its password is checked. A failure needs nothing more;
    a success clears the counter. So the first 5 attempts are free, the 6th still goes ahead, and then the wait is
    2 s from the 6th."""
    assert [reserve_onion_login_attempt('alice').wait for _ in range(6)] == [0] * 6
    assert redis_double.hget('onion_login_fail:alice', 'count') == '6'
    assert reserve_onion_login_attempt('alice').wait == 2
    assert redis_double.hget('onion_login_fail:alice', 'count') == '6'   # a refused attempt is not counted
    assert 3500 < redis_double.ttl('onion_login_fail:alice') <= 3600
    clear_onion_login_failures('alice')
    assert reserve_onion_login_attempt('alice').wait == 0


def test_parallel_attempts_get_one_per_wait_window(app, redis_double, clock):
    """N attempts arriving together, none of them finished yet: only one goes ahead per window."""
    for _ in range(5):
        record_onion_login_failure('alice')
    assert [reserve_onion_login_attempt('alice').wait for _ in range(10)] == [0] + [2] * 9
    clock.t += 2
    assert [reserve_onion_login_attempt('alice').wait for _ in range(10)] == [0] + [4] * 9
    clock.t += 4
    assert [reserve_onion_login_attempt('alice').wait for _ in range(3)] == [0, 8, 8]
    assert redis_double.hget('onion_login_fail:alice', 'count') == '8'


def test_an_account_in_backoff_is_refused_silently(app, redis_double, clock):
    """A typed identifier that is free, naming an account in backoff, is refused without a wait to show (`hidden`),
    and counted on both counters like a wrong password. The typed counter's own backoff is the visible one."""
    for _ in range(6):
        record_onion_login_failure('alice')
    gate = reserve_onion_login_attempt('alice@example.com', 'alice')
    assert gate == OnionLoginGate(wait=0, hidden=True) and not gate.proceed
    assert redis_double.hget('onion_login_fail:alice', 'count') == '7'
    assert redis_double.hget('onion_login_fail:alice@example.com', 'count') == '1'
    assert reserve_onion_login_attempt('alice', 'alice') == OnionLoginGate(wait=4)
    clock.t += 4
    assert reserve_onion_login_attempt('alice@example.com', 'alice').proceed
    assert reserve_onion_login_attempt('nobody').proceed


def test_a_concurrent_change_is_retried(app, redis_double, clock, monkeypatch):
    """Another attempt reserving between the read and the write aborts this transaction (WATCH); it is decided
    again on the new state, so the two are never both let through."""
    for _ in range(6):
        record_onion_login_failure('alice')
    clock.t += 2
    raced = []

    def racing_now():
        if not raced:
            raced.append(1)
            redis_double.hincrby('onion_login_fail:alice', 'count', 1)   # another attempt was let through meanwhile
            redis_double.hset('onion_login_fail:alice', 'last', clock.t)
        return clock.t
    monkeypatch.setattr('app.onion_guard._now', racing_now)
    assert reserve_onion_login_attempt('alice').wait == 4
    assert redis_double.hget('onion_login_fail:alice', 'count') == '7'


def test_endless_contention_is_a_short_wait(app, redis_double, clock, monkeypatch):
    for _ in range(6):
        record_onion_login_failure('alice')
    clock.t += 2

    def always_racing():
        redis_double.hset('onion_login_fail:alice', 'other', clock.t)
        return clock.t
    monkeypatch.setattr('app.onion_guard._now', always_racing)
    assert reserve_onion_login_attempt('alice').wait == 1
    assert redis_double.hget('onion_login_fail:alice', 'count') == '6'


def web_login(client, user_name, password, **kwargs):
    return onion_post(client, '/auth/login', data={'user_name': user_name, 'password': password, **LOGIN}, **kwargs)


def logged_in(response):
    return response.status_code == 302 and not response.headers['Location'].endswith('/auth/login')




@pytest.fixture
def ldap_off():
    with patch('app.auth.util.sync_user_to_ldap'), patch('app.shared.auth.sync_user_to_ldap'):
        yield


@pytest.fixture
def alpha_api(app, monkeypatch):
    monkeypatch.setitem(app.config, 'ENABLE_ALPHA_API', 'true')


def test_login_delay_grows(app, alice, redis_double, clock, ldap_off):
    client = app.test_client()
    a_session(client)
    for _ in range(6):
        assert not logged_in(web_login(client, 'alice', 'wrong password'))
    refused = web_login(client, 'alice', PASSWORD)
    assert not logged_in(refused)
    assert 'try again in 2 seconds' in flashes(client)
    assert redis_double.hget('onion_login_fail:alice', 'count') == '6'   # a refused attempt is not a failure
    clock.t += 2
    assert not logged_in(web_login(client, 'alice', 'wrong password'))
    assert not logged_in(web_login(client, 'alice', PASSWORD))
    assert 'try again in 4 seconds' in flashes(client)


def answer(client, response):
    """What a login attempt shows: its status, where it sends the browser, and the messages it flashed."""
    shown = (response.status_code, response.headers.get('Location'), flashes(client), response.get_data())
    with client.session_transaction(base_url=ONION_URL) as stored:
        stored.pop('_flashes', None)
    return shown


def test_an_account_in_backoff_answers_like_a_wrong_password(app, alice, redis_double, clock, ldap_off):
    """After 6 failures typed as 'alice', alice's email with the CORRECT password gets exactly the answer an unknown
    email with a wrong password gets: no wait message, nothing linking the email to the account. 'alice' itself
    still shows the wait."""
    client = app.test_client()
    a_session(client)
    for _ in range(6):
        web_login(client, ' Alice ', 'wrong password')
    answer(client, onion_get(client, '/robots.txt'))
    unknown = answer(client, web_login(client, 'nobody@example.com', 'wrong password'))
    hidden = answer(client, web_login(client, 'ALICE@example.com', PASSWORD))
    assert hidden == unknown
    assert 'Invalid user name or password' in hidden[2] and 'try again' not in hidden[2].lower()
    assert 'Please try again in 4 seconds' in answer(client, web_login(client, 'alice', PASSWORD))[2]
    assert redis_double.hget('onion_login_fail:alice@example.com', 'count') == '1'   # counted like a failure


def test_rotating_variants_does_not_reset_the_delay(app, alice, redis_double, clock, ldap_off):
    client = app.test_client()
    a_session(client)
    variants = ['alice', 'ALICE@example.com', ' aLiCe ', 'alice@EXAMPLE.com', 'ALICE', 'Alice@example.com ']
    for typed in variants:
        assert not logged_in(web_login(client, typed, 'wrong password'))
    assert redis_double.hget('onion_login_fail:alice', 'count') == '6'
    assert not logged_in(web_login(client, 'aLICE@example.COM', PASSWORD))   # a variant whose own counter is free
    assert redis_double.hget('onion_login_fail:alice', 'count') == '7'
    clock.t += 4
    assert logged_in(web_login(client, 'alice@example.com', PASSWORD))
    assert not redis_double.exists('onion_login_fail:alice')
    assert not redis_double.exists('onion_login_fail:alice@example.com')   # a success clears both counters


def test_unknown_identifiers_behave_like_known_ones(app, alice, redis_double, clock, ldap_off):
    client = app.test_client()
    a_session(client)
    for typed in ('Nobody ', 'alice'):
        for _ in range(6):
            web_login(client, typed, 'wrong password')
    messages = []
    for typed in ('nobody', 'alice'):
        with client.session_transaction(base_url=ONION_URL) as stored:
            stored.pop('_flashes', None)
        web_login(client, typed, PASSWORD)
        messages.append(flashes(client))
    assert messages[0] == messages[1] and 'Please try again in 2 seconds' in messages[0]


def test_api_login_counts_the_account_too(app, alice, redis_double, clock, ldap_off, alpha_api):
    client = app.test_client(use_cookies=False)
    for _ in range(6):
        onion_post(client, '/api/alpha/user/login', json={'username': 'Alice', 'password': 'wrong one'})
    unknown = onion_post(client, '/api/alpha/user/login', json={'username': 'nobody@example.com', 'password': 'x' * 8})
    hidden = onion_post(client, '/api/alpha/user/login', json={'username': 'alice@example.com', 'password': PASSWORD})
    assert (hidden.status_code, hidden.get_data()) == (unknown.status_code, unknown.get_data()) \
        and hidden.status_code == 400
    assert onion_post(client, '/api/alpha/user/login', json={'username': 'alice', 'password': PASSWORD}) \
        .status_code == 429


def test_success_clears(app, alice, redis_double, clock, ldap_off):
    client = app.test_client()
    a_session(client)
    for _ in range(6):
        web_login(client, 'alice', 'wrong password')
    clock.t += 2
    assert logged_in(web_login(client, 'alice', PASSWORD))
    assert not redis_double.exists('onion_login_fail:alice')


def test_delay_onion_only(app, alice, redis_double, clock, ldap_off, alpha_api):
    for _ in range(10):
        record_onion_login_failure('alice')
    client = app.test_client()
    response = client.post('/auth/login', base_url=CLEARNET_URL,
                           data={'user_name': 'alice', 'password': 'wrong password', **LOGIN})
    assert not logged_in(response)
    assert redis_double.hget('onion_login_fail:alice', 'count') == '10'   # a clearnet failure is not counted
    assert logged_in(client.post('/auth/login', base_url=CLEARNET_URL,
                                 data={'user_name': 'alice', 'password': PASSWORD, **LOGIN}))
    assert redis_double.exists('onion_login_fail:alice')                  # nor does a clearnet success clear it
    api = client.post('/api/alpha/user/login', base_url=CLEARNET_URL, json={'username': 'alice', 'password': PASSWORD})
    assert api.status_code == 200


def test_delay_covers_api(app, alice, redis_double, clock, ldap_off, alpha_api):
    client = app.test_client(use_cookies=False)
    for _ in range(6):
        response = onion_post(client, '/api/alpha/user/login', json={'username': 'Alice', 'password': 'wrong one'})
        assert response.status_code == 400
    refused = onion_post(client, '/api/alpha/user/login', json={'username': 'alice', 'password': PASSWORD})
    # The app-wide 429 handler answers every 429 (the limiter's own too) with its page, ahead of the API's JSON one
    assert refused.status_code == 429
    assert 'jwt' not in refused.get_data(as_text=True)
    clock.t += 2
    assert onion_post(client, '/api/alpha/user/login', json={'username': 'nobody', 'password': 'x' * 8}) \
        .status_code == 400
    accepted = onion_post(client, '/api/alpha/user/login', json={'username': 'alice', 'password': PASSWORD})
    assert accepted.status_code == 200 and accepted.get_json()['jwt']
    assert not redis_double.exists('onion_login_fail:alice')
    assert redis_double.hget('onion_login_fail:nobody', 'count') == '1'


# --------------------------------------------------------------------------
# find_user's profile-URL lookup takes the name literally
# --------------------------------------------------------------------------

@pytest.fixture
def profiled(alice):
    """Local accounts carry their actor URL. carol was renamed, so only her profile URL still says 'carol'."""
    alice.ap_profile_id = 'https://test.piefed.local/u/alice'
    carol = make_user(None, 'carol2', local=True)
    carol.ap_profile_id = 'https://test.piefed.local/u/carol'
    db.session.commit()
    return SimpleNamespace(alice=alice, carol=carol)


@pytest.mark.parametrize('typed', ['%', 'alic_', 'a%', '_lice', 'alic\\e', 'alic%', 'carol%', '___'])
def test_wildcards_match_no_profile_url(app, profiled, typed):
    assert find_user(typed) is None


def test_a_profile_url_still_finds_its_account(app, profiled):
    assert find_user('carol').id == profiled.carol.id
    assert find_user(' CAROL ').id == profiled.carol.id


def test_a_wildcard_cannot_log_in(app, profiled, ldap_off):
    client = app.test_client()
    assert not logged_in(client.post('/auth/login', base_url=CLEARNET_URL,
                                     data={'user_name': '%', 'password': PASSWORD, **LOGIN}))
    assert logged_in(client.post('/auth/login', base_url=CLEARNET_URL,
                                 data={'user_name': 'alice', 'password': PASSWORD, **LOGIN}))

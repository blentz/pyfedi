"""Cookies per storefront: host-only everywhere, Secure only on https storefronts (onion storefront spec, task 7)."""
import pytest
from werkzeug.exceptions import NotFound
from flask import Response, g, session

from app import db, login
from app.cookies import cookie_secure, secure_set_cookies
from app.models import SiteDomain
from tests.factories import make_instance, make_user
from app.storefront import clear_domain_cache


@pytest.fixture
def storefronts(db_session, site):
    if not SiteDomain.query.filter_by(domain='test.piefed.local').first():   # the first test finds the seeded row
        db.session.add(SiteDomain(domain='test.piefed.local', scheme='https', is_default=True))
    db.session.add(SiteDomain(domain='abc.onion', scheme='http', federates=False))
    db.session.commit()
    clear_domain_cache()
    yield
    clear_domain_cache()


def _session_cookie(app, base_url):
    with app.test_request_context(base_url=base_url):
        app.preprocess_request()
        session['x'] = 1
        response = app.make_response('')
        app.session_interface.save_session(app, session, response)
    return response.headers.get('Set-Cookie')


def _remember_cookie(app, base_url):
    with app.test_request_context(base_url=base_url):
        app.preprocess_request()
        session['_user_id'] = '1'
        response = Response()
        login._set_cookie(response)
    return response.headers.get('Set-Cookie')


@pytest.fixture
def secure_config(app, monkeypatch):
    monkeypatch.setitem(app.config, 'SESSION_COOKIE_SECURE', True)
    monkeypatch.setitem(app.config, 'REMEMBER_COOKIE_SECURE', True)


@pytest.mark.parametrize('base_url', ['http://abc.onion', 'https://test.piefed.local'])
@pytest.mark.parametrize('cookie', [_session_cookie, _remember_cookie])
def test_session_cookie_host_only(app, storefronts, secure_config, base_url, cookie):
    header = cookie(app, base_url)
    assert header and 'Domain=' not in header


@pytest.mark.parametrize('cookie', [_session_cookie, _remember_cookie])
def test_secure_flag_per_storefront(app, storefronts, secure_config, cookie):
    assert '; Secure' in cookie(app, 'https://test.piefed.local')
    assert 'Secure' not in cookie(app, 'http://abc.onion')


@pytest.mark.parametrize('cookie', [_session_cookie, _remember_cookie])
def test_secure_flag_ignores_request_headers(app, storefronts, secure_config, cookie):
    """The scheme is the storefront's configured one, not the request's port."""
    assert 'Secure' not in cookie(app, 'http://abc.onion:443')


@pytest.mark.parametrize('cookie', [_session_cookie, _remember_cookie])
def test_config_disables_secure_everywhere(app, storefronts, monkeypatch, cookie):
    monkeypatch.setitem(app.config, 'SESSION_COOKIE_SECURE', False)
    monkeypatch.setitem(app.config, 'REMEMBER_COOKIE_SECURE', False)
    assert 'Secure' not in cookie(app, 'https://test.piefed.local')


def test_cookie_secure_outside_request_follows_config(app):
    assert cookie_secure(True) is True
    assert cookie_secure(False) is False


def test_cookie_secure_before_storefront_resolved_follows_config(app):
    with app.test_request_context(base_url='http://abc.onion'):
        assert cookie_secure(True) is True


def test_config_is_not_mutated(app, storefronts, secure_config):
    _session_cookie(app, 'http://abc.onion')
    _remember_cookie(app, 'http://abc.onion')
    assert app.config['SESSION_COOKIE_SECURE'] is True
    assert app.config['REMEMBER_COOKIE_SECURE'] is True


def _hooked(app, base_url, *cookies):
    """`cookies` as `(name, value, kwargs)` set on a response that then passes through the after_request hook."""
    with app.test_request_context(base_url=base_url):
        app.preprocess_request()
        response = Response()
        for name, value, kwargs in cookies:
            response.set_cookie(name, value, **kwargs)
        return secure_set_cookies(response).headers.getlist('Set-Cookie')


def test_hook_adds_secure_on_https_and_keeps_everything_else(app, storefronts, secure_config):
    plain = _hooked(app, 'https://test.piefed.local', ('a', '1', {'path': '/x', 'domain': 'test.piefed.local'}),
                    ('b', '2', {}))
    unhooked = Response()
    unhooked.set_cookie('a', '1', path='/x', domain='test.piefed.local')
    unhooked.set_cookie('b', '2')
    assert [h.replace('; Secure', '') for h in plain] == unhooked.headers.getlist('Set-Cookie')
    assert all(h.endswith('; Secure') or '; Secure;' in h for h in plain)


def test_hook_strips_secure_on_http_and_keeps_deleted_cookies(app, storefronts, secure_config):
    headers = _hooked(app, 'http://abc.onion', ('a', '1', {'secure': True}), ('gone', '', {'expires': 0}))
    assert len(headers) == 2 and not any('Secure' in h for h in headers)
    assert headers[1].startswith('gone=;') and '1970' in headers[1]


def test_hook_does_not_duplicate_secure(app, storefronts, secure_config):
    headers = _hooked(app, 'https://test.piefed.local', ('a', '1', {'secure': True}))
    assert headers[0].count('Secure') == 1


def test_hook_respects_config_off(app, storefronts, monkeypatch):
    monkeypatch.setitem(app.config, 'SESSION_COOKIE_SECURE', False)
    assert 'Secure' not in _hooked(app, 'https://test.piefed.local', ('a', '1', {}))[0]


def test_hook_leaves_response_without_cookies(app, storefronts, secure_config):
    assert _hooked(app, 'https://test.piefed.local') == []


@pytest.fixture
def person(app, storefronts):
    local = make_instance('test.piefed.local', software='piefed')
    make_user(local, 'founder', local=True)
    user = make_user(local, 'person', local=True, with_keys=True)
    user.email = 'person@example.com'
    user.verified = True
    user.set_password('a-good-password')
    db.session.commit()
    return user


def _login(app, base_url, user_name='person'):
    return app.test_client().post('/auth/login', base_url=base_url, follow_redirects=False,
                                  data={'user_name': user_name, 'password': 'a-good-password', 'timezone': 'UTC',
                                        'submit': 'Log In', 'remember': 'y'})


def test_login_cookies_end_to_end(app, person, secure_config):
    onion = _login(app, 'http://abc.onion', 'person@test.piefed.local').headers.getlist('Set-Cookie')
    g.pop('_login_user', None)   # the suite-wide app context keeps the first login's user in g
    https = _login(app, 'https://test.piefed.local').headers.getlist('Set-Cookie')
    names = lambda headers: {h.split('=')[0] for h in headers}
    assert {'remember_token', 'low_bandwidth'} <= names(onion) and len(names(onion)) >= 3
    assert not any('Secure' in h or 'Domain=' in h for h in onion)
    assert names(https) == names(onion)
    assert all('; Secure' in h and 'Domain=' not in h for h in https)


@pytest.mark.parametrize('base_url,secure', [('http://abc.onion', False), ('https://test.piefed.local', True)])
def test_ad_hoc_cookie_route(app, storefronts, secure_config, base_url, secure):
    headers = app.test_client().get('/auth/logout', base_url=base_url).headers.getlist('Set-Cookie')
    assert [h for h in headers if h.startswith('low_bandwidth=')]
    assert all(('; Secure' in h) is secure for h in headers)


def test_hook_runs_on_error_responses(app, storefronts, secure_config, monkeypatch):
    def not_found(_error):   # an error page that sets a cookie of its own, without Secure
        response = Response('gone', status=404)
        response.set_cookie('low_bandwidth', '1')
        return response

    monkeypatch.setitem(app.error_handler_spec[None].setdefault(404, {}), NotFound, not_found)
    response = app.test_client().get('/no-such-page-here', base_url='https://test.piefed.local')
    assert response.status_code == 404
    cookies = response.headers.getlist('Set-Cookie')
    assert [h for h in cookies if h.startswith('low_bandwidth=1')]
    assert all('; Secure' in h for h in cookies)

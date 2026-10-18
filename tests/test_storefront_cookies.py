"""Cookies per storefront: host-only everywhere, Secure only on https storefronts (onion storefront spec, task 7)."""
import pytest
from flask import Response, session

from app import db, login
from app.cookies import cookie_secure
from app.models import SiteDomain
from app.storefront import clear_domain_cache


@pytest.fixture
def storefronts(db_session, site):
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

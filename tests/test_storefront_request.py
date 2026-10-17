"""Each request is served as the storefront its Host names (onion storefront spec §3.3)."""
import pytest
from flask import g, url_for

from app import db
from app.models import SiteDomain
from app.storefront import (DomainInfo, _authority, canonical_url, clear_domain_cache, current_storefront,
                            default_domain, domain_for_host, normalise_host, storefront_url)


@pytest.fixture
def onion(db_session, site):
    db.session.add(SiteDomain(domain='abc.onion', scheme='http', federates=False))
    db.session.commit()
    clear_domain_cache()
    yield 'abc.onion'
    clear_domain_cache()


def test_unknown_host_is_421(app, db_session, site):
    response = app.test_client().get('/robots.txt', headers={'Host': 'evil.example'})
    assert response.status_code == 421
    assert response.mimetype == 'text/plain'
    assert b'<' not in response.data
    assert 'Location' not in response.headers


def test_unknown_host_421_beats_options_preflight(app, db_session):
    response = app.test_client().options('/', headers={'Host': 'evil.example'})
    assert response.status_code == 421


def test_known_alias_resolves(app, onion):
    client = app.test_client()
    response = client.get('/robots.txt', headers={'Host': 'abc.onion'})
    assert response.status_code == 200
    with app.test_request_context(base_url='http://abc.onion'):
        app.preprocess_request()
        assert g.storefront.domain == 'abc.onion'
        assert current_storefront().is_onion
        assert storefront_url() == 'http://abc.onion'


@pytest.mark.parametrize('host', ['HELL.onion', 'hell.onion.', 'hell.onion:80', 'Hell.Onion.:80'])
def test_host_variants(app, db_session, site, host):
    db.session.add(SiteDomain(domain='hell.onion', scheme='http'))
    db.session.commit()
    clear_domain_cache()
    try:
        with app.test_client() as client:
            assert client.get('/robots.txt', headers={'Host': host}).status_code == 200
            assert g.storefront.domain == 'hell.onion'
    finally:
        clear_domain_cache()


def test_url_for_external_follows_host(app, onion):
    with app.test_request_context(base_url='http://abc.onion'):
        app.preprocess_request()
        assert url_for('main.index', _external=True) == 'http://abc.onion/home'
    with app.test_request_context(base_url='https://test.piefed.local'):
        app.preprocess_request()
        assert url_for('main.index', _external=True) == 'https://test.piefed.local/home'
    with app.app_context():
        assert url_for('main.index', _external=True) == 'https://test.piefed.local/home'


def test_outside_request_is_default(app, db_session):
    with app.app_context():
        assert current_storefront() == default_domain()
        assert storefront_url() == 'https://test.piefed.local'
        assert canonical_url() == app.config['SERVER_URL']


def test_canonical_url_ignores_host(app, onion):
    with app.test_request_context(base_url='http://abc.onion'):
        assert canonical_url() == app.config['SERVER_URL']


def test_static_and_inbox_on_alias(app, onion):
    client = app.test_client()
    assert client.get('/static/styles.css', headers={'Host': 'abc.onion'}).status_code != 421
    assert client.post('/inbox', headers={'Host': 'abc.onion'}, data=b'{}').status_code != 421


@pytest.mark.parametrize('path', ['/health', '/health2'])
def test_health_is_exempt_from_421(app, db_session, site, path):
    response = app.test_client().get(path, headers={'Host': 'localhost:5000'})
    assert response.status_code == 200


def test_other_paths_on_localhost_still_421(app, db_session, site):
    assert app.test_client().get('/robots.txt', headers={'Host': 'localhost:5000'}).status_code == 421


@pytest.fixture
def server_name(app, db_session, site, monkeypatch):
    db.session.query(SiteDomain).delete()   # the empty table: SERVER_NAME is the one implicit default
    db.session.commit()

    def set_name(value):
        monkeypatch.setitem(app.config, 'SERVER_NAME', value)
        clear_domain_cache()
    yield set_name
    clear_domain_cache()


@pytest.mark.parametrize('name,host', [('127.0.0.1:5000', '127.0.0.1:5000'), ('127.0.0.1:5000', '127.0.0.1'),
                                       ('[::1]:5000', '[::1]:5000'), ('[::1]:5000', '[::1]')])
def test_server_name_with_port_serves_with_empty_table(app, server_name, name, host):
    server_name(name)
    response = app.test_client().get('/robots.txt', headers={'Host': host})
    assert response.status_code == 200


def test_server_name_with_port_serves_with_seeded_row(app, server_name):
    server_name('127.0.0.1:5000')
    db.session.add(SiteDomain(domain='127.0.0.1:5000', scheme='http', is_default=True))   # as an old migration seeded it
    db.session.commit()
    clear_domain_cache()
    assert app.test_client().get('/robots.txt', headers={'Host': '127.0.0.1:5000'}).status_code == 200


def test_default_row_is_the_bare_host(app, server_name):
    server_name('127.0.0.1:5000')
    with app.app_context():
        assert default_domain().domain == '127.0.0.1'
        assert storefront_url().endswith('://127.0.0.1:5000')
    server_name('[::1]:5000')
    with app.app_context():
        assert default_domain().domain == '::1'
        assert storefront_url().endswith('://[::1]:5000')


def test_urls_use_the_configured_port(app, server_name):
    server_name('127.0.0.1:5000')
    with app.test_request_context(base_url='http://127.0.0.1:5000'):
        app.preprocess_request()
        assert url_for('main.index', _external=True) == 'https://127.0.0.1:5000/home'
        assert storefront_url() == 'https://127.0.0.1:5000'


def test_request_port_never_reaches_urls(app, server_name):
    server_name('test.piefed.local')
    with app.test_request_context(base_url='https://test.piefed.local:6666'):
        app.preprocess_request()
        assert ':6666' not in url_for('main.index', _external=True)
        assert storefront_url() == 'https://test.piefed.local'
    server_name('127.0.0.1:5000')
    with app.test_request_context(base_url='https://127.0.0.1:6666'):
        app.preprocess_request()
        assert storefront_url() == 'https://127.0.0.1:5000'


def test_alias_urls_carry_no_port(app, onion):
    with app.test_request_context(base_url='http://abc.onion:6666'):
        app.preprocess_request()
        assert storefront_url() == 'http://abc.onion'


@pytest.mark.parametrize('host,bare', [('HELL.onion.:80', 'hell.onion'), ('[::1]:5000', '::1'), ('[::1]', '::1'),
                                       ('[::1', ''), ('::1', '::1'), ('', '')])
def test_normalise_host(host, bare):
    assert normalise_host(host) == bare


def test_authority_is_configured_never_requested(app, onion):
    with app.app_context():
        assert _authority(domain_for_host('abc.onion')) == 'abc.onion'
        assert _authority(DomainInfo('::1', 'http', True, 'anyone', False, False)) == '[::1]'
        assert _authority(default_domain()) == app.config['SERVER_NAME']

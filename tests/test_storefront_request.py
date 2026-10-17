"""Each request is served as the storefront its Host names (onion storefront spec §3.3)."""
import pytest
from flask import g, url_for

from app import db
from app.models import SiteDomain
from app.storefront import (canonical_url, clear_domain_cache, current_storefront, default_domain,
                            storefront_url)


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

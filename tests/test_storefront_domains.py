"""site_domain model and the app.storefront lookup (onion storefront spec §3.1)."""
import importlib.util
import logging
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from cachelib import SimpleCache

from app import cache, db
from app.models import Site, SiteDomain
from app.storefront import (check_default_domain, clear_domain_cache, default_domain, domain_for_host,
                            domain_rows, is_registered_host, normalise_host, registration_from_mode)


def add_domain(domain, **kw):
    row = SiteDomain(domain=domain, **kw)
    db.session.add(row)
    db.session.commit()
    clear_domain_cache()
    return row


def test_empty_table_synthesises_default(db_session):
    rows = domain_rows()
    assert len(rows) == 1
    assert rows[0].domain == 'test.piefed.local'
    assert rows[0].is_default and rows[0].scheme == 'https' and rows[0].federates
    assert default_domain() == rows[0]
    assert is_registered_host('TEST.piefed.local:443')


def test_second_default_rejected(db_session):
    add_domain('test.piefed.local', is_default=True)
    db.session.add(SiteDomain(domain='abc.onion', scheme='http', is_default=True))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_domain_for_host_normalises(db_session):
    add_domain('test.piefed.local', is_default=True)
    add_domain('abc.onion', scheme='http')
    for host in ('ABC.onion:80', 'abc.onion.'):
        row = domain_for_host(host)
        assert row.domain == 'abc.onion' and row.is_onion is True
    assert domain_for_host('evil.example') is None
    assert not is_registered_host('evil.example')
    assert default_domain().domain == 'test.piefed.local'
    assert normalise_host('HELL.cloud.:8080') == 'hell.cloud'


def test_no_default_row_falls_back_to_server_name(app, db_session, caplog):
    add_domain('abc.onion', scheme='http')
    assert default_domain().domain == 'test.piefed.local'
    with caplog.at_level(logging.ERROR, logger=app.logger.name):
        check_default_domain()
    assert 'none is the default' in caplog.text


def test_mixed_protocol_default_is_https(app, db_session, monkeypatch):
    url = app.config['SERVER_URL']
    monkeypatch.setitem(app.config, 'HTTP_PROTOCOL', 'mixed')
    assert domain_rows()[0].scheme == 'https'
    assert app.config['SERVER_URL'] == url


def test_http_protocol_default_keeps_http(app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'HTTP_PROTOCOL', 'http')
    assert domain_rows()[0].scheme == 'http'


def test_cache_cleared_on_change(db_session):
    assert len(domain_rows()) == 1
    add_domain('test.piefed.local', is_default=True)
    add_domain('abc.onion', scheme='http')
    assert {r.domain for r in domain_rows()} == {'test.piefed.local', 'abc.onion'}


def test_cache_is_stale_until_cleared(app, db_session, monkeypatch):
    monkeypatch.setitem(app.extensions['cache'], cache, SimpleCache())
    clear_domain_cache()
    assert {r.domain for r in domain_rows()} == {'test.piefed.local'}
    row = SiteDomain(domain='abc.onion', scheme='http')
    db.session.add(row)
    db.session.commit()
    assert {r.domain for r in domain_rows()} == {'test.piefed.local'}
    clear_domain_cache()
    assert {r.domain for r in domain_rows()} == {'abc.onion'}


def test_onion_flag_follows_domain(db_session):
    row = add_domain('x.example')
    assert row.is_onion is False
    row.domain = 'y.onion'
    assert row.is_onion is True


def test_synthesised_onion_server_name(app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'SERVER_NAME', 'abc.onion')
    assert domain_rows()[0].is_onion is True


@pytest.mark.parametrize('mode,expected', [('Open', 'anyone'), ('RequireApplication', 'anyone'),
                                           ('Closed', 'admin'), (None, 'admin')])
def test_registration_from_mode(mode, expected):
    assert registration_from_mode(mode) == expected


def test_migration_mapping_matches_app_mapping():
    module = load_migration()
    for mode, expected in module.REGISTRATION_FROM_MODE.items():
        assert registration_from_mode(mode) == expected


def load_migration(name: str = '*_site_domain.py'):
    path = next(Path(__file__).parent.parent.glob(f'migrations/versions/{name}'))
    spec = importlib.util.spec_from_file_location('site_domain_migration', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def seed_schema(db_session):
    """site_domain as the seed wrote into it: without the instance link plan C2's migration adds after it."""
    per_domain_instance = load_migration('c8f4d2a6b9e1_per_domain_instance.py')
    with Operations.context(MigrationContext.configure(db.session.connection())):
        per_domain_instance.downgrade()
    db.session.commit()
    try:
        yield
    finally:
        db.session.rollback()
        db.session.execute(text('DELETE FROM site_domain'))
        with Operations.context(MigrationContext.configure(db.session.connection())):
            per_domain_instance.upgrade()
        db.session.commit()


def seeded_rows():
    return db.session.execute(text('SELECT domain, scheme, federates, registration, is_default, is_onion '
                                   'FROM site_domain')).all()


@pytest.mark.parametrize('mode,registration', [('Open', 'anyone'), ('RequireApplication', 'anyone'), ('Closed', 'admin')])
def test_migration_seeds_from_env(db_session, seed_schema, mode, registration):
    db.session.add(Site(name='s', registration_mode=mode))
    db.session.commit()
    load_migration().seed_default(db.session.connection(), {'SERVER_NAME': 'Test.Piefed.LOCAL', 'HTTP_PROTOCOL': 'mixed'})
    db.session.commit()
    rows = seeded_rows()
    assert len(rows) == 1
    assert tuple(rows[0]) == ('test.piefed.local', 'https', True, registration, True, False)


@pytest.mark.parametrize('server_name,domain', [('127.0.0.1:5000', '127.0.0.1'), ('[::1]:5000', '::1'),
                                                 ('Hell.Onion.', 'hell.onion'), (None, 'localhost')])
def test_migration_seeds_the_bare_host(db_session, seed_schema, server_name, domain):
    load_migration().seed_default(db.session.connection(), {'SERVER_NAME': server_name, 'HTTP_PROTOCOL': 'http'})
    db.session.commit()
    assert [row.domain for row in seeded_rows()] == [domain]


def test_startup_check_logs_mismatch(app, db_session, caplog):
    add_domain('other.example', is_default=True)
    with caplog.at_level(logging.ERROR, logger=app.logger.name):
        check_default_domain()
    assert 'does not match SERVER_NAME' in caplog.text


def test_startup_check_quiet_when_matching(app, db_session, caplog):
    with caplog.at_level(logging.ERROR, logger=app.logger.name):
        check_default_domain()
    assert caplog.text == ''


def test_startup_check_survives_missing_table(app, db_session, caplog):
    db.session.execute(db.text('ALTER TABLE site_domain RENAME TO site_domain_gone'))
    db.session.commit()
    clear_domain_cache()
    try:
        with caplog.at_level(logging.ERROR, logger=app.logger.name):
            check_default_domain()
        assert caplog.text == ''
    finally:
        db.session.execute(db.text('ALTER TABLE site_domain_gone RENAME TO site_domain'))
        db.session.commit()


@pytest.mark.parametrize('rows', [
    [{'domain': 'abc.onion', 'scheme': 'http'}],                                           # no row names SERVER_NAME
    [{'domain': 'other.example', 'is_default': True}, {'domain': 'abc.onion', 'scheme': 'http'}],   # a stale seed
])
def test_server_name_is_served_when_no_row_names_it(app, db_session, site, rows):
    """final review Minor 5: the canonical site, inbox included, must never answer 421 because the table's rows
    were seeded under another SERVER_NAME. Its host is served as the synthesised default; check_default_domain
    still logs the mismatch."""
    db.session.query(SiteDomain).delete()   # the migration's seed row survives until the first truncate
    for row in rows:
        add_domain(**row)
    served = domain_for_host('TEST.piefed.local:443')
    assert served is not None
    assert (served.domain, served.scheme, served.is_default) == ('test.piefed.local', 'https', True)
    assert domain_for_host('abc.onion').domain == 'abc.onion'
    assert domain_for_host('evil.example') is None
    response = app.test_client().get('/robots.txt', base_url='https://test.piefed.local')
    assert response.status_code == 200

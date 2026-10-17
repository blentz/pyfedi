"""site_domain model and the app.storefront lookup (onion storefront spec §3.1)."""
import importlib.util
import logging
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from app import db
from app.models import SiteDomain
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


def test_no_default_row_falls_back_to_server_name(db_session):
    add_domain('abc.onion', scheme='http')
    assert default_domain().domain == 'test.piefed.local'


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
    path = next(Path(__file__).parent.parent.glob('migrations/versions/*_site_domain.py'))
    spec = importlib.util.spec_from_file_location('site_domain_migration', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for mode, expected in module.REGISTRATION_FROM_MODE.items():
        assert registration_from_mode(mode) == expected


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

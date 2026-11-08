"""Migration a7d3e9b2c4f1: oauth_code.domain and oauth_token.domain, the rows already there given the default domain,
and that it reverses cleanly (owner ruling 2026-11-08: a token works only on the domain it was issued on)."""
import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text

from app import db
from app.models import SiteDomain
from app.oauth_server import tokens
from tests.factories import make_user

MIGRATION = Path(__file__).resolve().parent.parent / 'migrations' / 'versions' / 'a7d3e9b2c4f1_oauth_domain.py'
TABLES = ('oauth_code', 'oauth_token')


def load_migration():
    spec = importlib.util.spec_from_file_location('oauth_domain_migration', MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def column(connection, table):
    return next((c for c in inspect(connection).get_columns(table) if c['name'] == 'domain'), None)


def test_the_revision_chain():
    migration = load_migration()
    assert (migration.revision, migration.down_revision) == ('a7d3e9b2c4f1', 'f6c2a8e4b1d7')


def _rerun(default):
    """Downgrade and upgrade in one rolled-back transaction, with a code and a token in place; their domains after."""
    db.session.close()
    migration = load_migration()
    with db.engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text("SET LOCAL lock_timeout = '5s'"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
                assert [column(connection, table) for table in TABLES] == [None, None]
                if default is not None:
                    connection.execute(text("UPDATE site_domain SET domain = :domain WHERE is_default"),
                                       {'domain': default})
                migration.upgrade()
            assert all(column(connection, table)['nullable'] is False for table in TABLES)
            return [connection.execute(text(f'SELECT domain FROM {table}')).scalars().all() for table in TABLES]
        finally:
            transaction.rollback()


def _a_code_and_a_token():
    registered, _secret = tokens.register_app('Tusky', ['https://app.example/cb'], 'read', None)
    user = make_user(None, 'migratedtoken', local=True)
    tokens.issue_code(registered, user, 'https://app.example/cb', 'read', None, None)
    tokens.issue_token(registered, user, 'read')


def test_existing_rows_get_the_default_domain(app, db_session, api_baseline):
    if db.session.query(SiteDomain).filter_by(is_default=True).first() is None:
        db.session.add(SiteDomain(domain='first.example', scheme='https', is_default=True))
        db.session.commit()
    _a_code_and_a_token()
    assert _rerun(default='first.example') == [['first.example'], ['first.example']]


def test_with_no_domain_row_it_is_server_name(app, db_session, api_baseline, monkeypatch):
    _a_code_and_a_token()
    monkeypatch.setenv('SERVER_NAME', 'Named.Example:8030')
    db.session.query(SiteDomain).delete()
    db.session.commit()
    assert _rerun(default=None) == [['named.example'], ['named.example']]

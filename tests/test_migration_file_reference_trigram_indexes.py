"""Migration f7c2a4e9b1d3: the trigram indexes a file's other records are found by (plan C3), built without locking
writes, rebuilt when an interrupted build left an INVALID one, and reversed cleanly."""
import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app import db

MIGRATION = Path(__file__).resolve().parent.parent / 'migrations' / 'versions' / \
    'f7c2a4e9b1d3_file_reference_trigram_indexes.py'


def load_migration():
    spec = importlib.util.spec_from_file_location('file_reference_trigram_indexes_migration', MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def definition(connection, name):
    return connection.execute(text('SELECT indexdef FROM pg_indexes WHERE indexname = :name'), {'name': name}).scalar()


def valid(connection, name):
    return connection.execute(text('SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid '
                                   'WHERE c.relname = :name'), {'name': name}).scalar()


def run(connection, step):
    connection.commit()    # autocommit_block refuses a transaction it did not begin
    with Operations.context(MigrationContext.configure(connection)):
        step()


def all_built(connection, migration):
    return all(valid(connection, name) is True and f'{column} gin_trgm_ops' in (definition(connection, name) or '')
               for name, _table, column in migration.INDEXES)


def restore(connection, migration):
    connection.rollback()
    if not all_built(connection, migration):
        for name, _table, _column in migration.INDEXES:
            connection.execute(text(f'DROP INDEX IF EXISTS {name}'))
        connection.commit()
        run(connection, migration.upgrade)
    connection.execute(text('DROP TABLE IF EXISTS trgm_interrupted'))
    connection.execute(text('RESET lock_timeout'))
    connection.commit()


def test_the_revision_chain():
    migration = load_migration()
    assert (migration.revision, migration.down_revision) == ('f7c2a4e9b1d3', 'e5b9c3d7a1f4')


def test_downgrade_then_upgrade_restores_every_index(app, db_session):
    db.session.close()
    migration = load_migration()
    with db.engine.connect() as connection:
        connection.execute(text("SET lock_timeout = '5s'"))
        try:
            run(connection, migration.downgrade)
            assert all(definition(connection, name) is None for name, _table, _column in migration.INDEXES)
            run(connection, migration.upgrade)
            assert all_built(connection, migration)
        finally:
            restore(connection, migration)


def test_the_upgrade_rebuilds_an_index_an_interrupted_build_left_invalid(app, db_session):
    """N-M1. `CREATE INDEX CONCURRENTLY IF NOT EXISTS` takes an INVALID leftover of an interrupted build for the
    index, and the planner never uses it: the ~400 ms scans came back silently. The leftover is made the way
    PostgreSQL makes one, a concurrent unique build that fails part-way."""
    db.session.close()
    migration = load_migration()
    with db.engine.connect() as connection:
        connection.execute(text("SET lock_timeout = '5s'"))
        try:
            connection.commit()
            connection.execute(text('DROP INDEX IF EXISTS ix_file_source_url_trgm'))
            connection.execute(text('CREATE TABLE trgm_interrupted (x integer)'))
            connection.execute(text('INSERT INTO trgm_interrupted VALUES (1), (1)'))
            connection.commit()
            with db.engine.connect().execution_options(isolation_level='AUTOCOMMIT') as autocommit:
                with pytest.raises(IntegrityError):
                    autocommit.execute(text('CREATE UNIQUE INDEX CONCURRENTLY ix_file_source_url_trgm '
                                            'ON trgm_interrupted (x)'))
            assert valid(connection, 'ix_file_source_url_trgm') is False

            run(connection, migration.upgrade)

            assert all_built(connection, migration)
        finally:
            restore(connection, migration)

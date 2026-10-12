"""C2 migration a7c3e9f1b5d2: post.quote_ap_id and post_reply.quote_ap_id, and that it reverses cleanly."""
import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text

from app import db

MIGRATION = Path(__file__).resolve().parent.parent / 'migrations' / 'versions' / 'a7c3e9f1b5d2_quote_ap_id.py'
TABLES = ('post', 'post_reply')


def load_migration():
    spec = importlib.util.spec_from_file_location('quote_ap_id_migration', MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def column(connection, table):
    return next((c for c in inspect(connection).get_columns(table) if c['name'] == 'quote_ap_id'), None)


def test_the_revision_chain():
    migration = load_migration()
    assert (migration.revision, migration.down_revision) == ('a7c3e9f1b5d2', 'f4b8d2a6c1e7')


@pytest.mark.parametrize('table', TABLES)
def test_a_nullable_255_character_column(app, db_session, table):
    with db.engine.connect() as connection:
        found = column(connection, table)
    assert found is not None and found['nullable'] is True and found['type'].length == 255


def test_downgrade_then_upgrade_removes_and_restores_both_columns(app, db_session):
    db.session.close()
    migration = load_migration()
    with db.engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text("SET LOCAL lock_timeout = '5s'"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
                assert [column(connection, table) for table in TABLES] == [None, None]
                migration.upgrade()
            assert all(column(connection, table) is not None for table in TABLES)
        finally:
            transaction.rollback()

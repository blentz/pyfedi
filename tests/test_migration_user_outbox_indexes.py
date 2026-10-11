"""Interop C1 item 7 migration f4b8d2a6c1e7: the (user_id, posted_at DESC NULLS LAST) indexes the user outbox pages
with, built without locking writes, and that it reverses cleanly."""
import importlib.util
import io
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text

from app import db

MIGRATION = Path(__file__).resolve().parent.parent / 'migrations' / 'versions' / 'f4b8d2a6c1e7_user_outbox_indexes.py'
INDEXES = {'post': 'ix_post_user_posted', 'post_reply': 'ix_post_reply_user_posted'}


def load_migration():
    spec = importlib.util.spec_from_file_location('user_outbox_indexes_migration', MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def index(connection, table, name):
    return next((found for found in inspect(connection).get_indexes(table) if found['name'] == name), None)


def valid(connection, name):
    """A failed CREATE INDEX CONCURRENTLY leaves an INVALID index behind, which the planner never uses."""
    return connection.execute(text("SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                                   "WHERE c.relname = :name"), {'name': name}).scalar()


def run(connection, step):
    connection.commit()    # autocommit_block refuses a transaction it did not begin (the SET, an inspection)
    with Operations.context(MigrationContext.configure(connection)):
        step()


def test_the_revision_chain():
    migration = load_migration()
    assert (migration.revision, migration.down_revision) == ('f4b8d2a6c1e7', 'e2a7c4f9d1b3')


def test_both_indexes_lead_with_the_author_in_the_page_order(app, db_session):
    """The page sorts posted_at DESC NULLS LAST; a plain DESC index is NULLS FIRST and cannot give that order."""
    with db.engine.connect() as connection:
        for table, name in INDEXES.items():
            found = index(connection, table, name)
            assert found is not None
            assert found['column_names'][:2] == ['user_id', 'posted_at']
            assert found['column_sorting']['posted_at'] == ('desc', 'nulls_last')
            assert valid(connection, name) is True


def test_the_indexes_are_built_and_dropped_concurrently():
    """`post` and `post_reply` are the biggest tables: a plain CREATE INDEX would block every write to them for
    the whole build. Rendered offline, both directions say CONCURRENTLY, outside a transaction."""
    migration = load_migration()
    for step in (migration.upgrade, migration.downgrade):
        buffer = io.StringIO()
        context = MigrationContext.configure(dialect_name='postgresql',
                                             opts={'as_sql': True, 'output_buffer': buffer})
        with Operations.context(context):
            step()
        statements = [line for line in buffer.getvalue().splitlines() if 'INDEX' in line]
        assert len(statements) == 2
        assert all('CONCURRENTLY' in statement for statement in statements)


def test_downgrade_then_upgrade_restores_both_indexes(app, db_session):
    """CONCURRENTLY cannot run in a transaction, so this runs for real rather than inside one rolled back; the
    finally puts back whatever a failure left missing."""
    db.session.close()    # nothing of the session's may still hold a lock on post or post_reply
    migration = load_migration()
    with db.engine.connect() as connection:
        connection.execute(text("SET lock_timeout = '5s'"))
        try:
            run(connection, migration.downgrade)
            for table, name in INDEXES.items():
                assert index(connection, table, name) is None
            run(connection, migration.upgrade)
            for table, name in INDEXES.items():
                assert index(connection, table, name) is not None
                assert valid(connection, name) is True
        finally:
            connection.rollback()
            if any(index(connection, table, name) is None for table, name in INDEXES.items()):
                for name in INDEXES.values():
                    connection.execute(text(f'DROP INDEX IF EXISTS {name}'))
                connection.commit()
                run(connection, migration.upgrade)
            connection.execute(text('RESET lock_timeout'))
            connection.commit()

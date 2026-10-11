"""Interop C1 item 7 migration f4b8d2a6c1e7: the (user_id, posted_at DESC) indexes the user outbox pages with, and
that it reverses cleanly."""
import importlib.util
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


def test_the_revision_chain():
    migration = load_migration()
    assert (migration.revision, migration.down_revision) == ('f4b8d2a6c1e7', 'e2a7c4f9d1b3')


def test_both_indexes_lead_with_the_author(app, db_session):
    with db.engine.connect() as connection:
        for table, name in INDEXES.items():
            found = index(connection, table, name)
            assert found is not None
            assert found['column_names'][0] == 'user_id'


def test_downgrade_then_upgrade_restores_both_indexes(app, db_session):
    db.session.close()    # nothing of the session's may still hold a lock on post or post_reply
    migration = load_migration()
    with db.engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text("SET LOCAL lock_timeout = '5s'"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
                for table, name in INDEXES.items():
                    assert index(connection, table, name) is None
                migration.upgrade()
            for table, name in INDEXES.items():
                assert index(connection, table, name) is not None
        finally:
            transaction.rollback()

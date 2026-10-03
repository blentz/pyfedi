"""The fork's `visibility` migration (interop D6/D17): what it leaves behind, and that it reverses cleanly.

The round trip runs on a connection of its own and is rolled back, so it never touches the worker's database for
the next test (Postgres DDL is transactional). `lock_timeout` turns a lock the session left behind into a failure
rather than a hang.
"""
import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text

from app import db
from tests.factories import make_community, make_instance, make_post, make_post_reply, make_user

MIGRATION = Path(__file__).resolve().parent.parent / 'migrations' / 'versions' / 'a7e3f1c2b9d4_visibility.py'
TABLES = ('post', 'post_reply')


def load_migration():
    spec = importlib.util.spec_from_file_location('visibility_migration', MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def visibility_state(connection, table):
    inspector = inspect(connection)
    column = next((c for c in inspector.get_columns(table) if c['name'] == 'visibility'), None)
    indexed = any(i['column_names'] == ['visibility'] for i in inspector.get_indexes(table))
    return column, indexed


@pytest.mark.parametrize('table', TABLES)
def test_visibility_is_not_null_defaults_to_public_and_is_indexed(app, table):
    with db.engine.connect() as connection:
        column, indexed = visibility_state(connection, table)
    assert column is not None
    assert column['nullable'] is False
    assert "'public'" in column['default']
    assert indexed


def test_downgrade_then_upgrade_restores_the_columns_and_backfills_public(app, db_session):
    author = make_user(make_instance('m.example'), 'alice')
    post = make_post(make_community(), author, 'https://m.example/p/1')
    make_post_reply(post, author)
    post_id = post.id
    db.session.commit()
    db.session.close()  # nothing of the session's may still hold a lock on post or post_reply
    migration = load_migration()
    assert migration.branch_labels == ('fork',)

    with db.engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text("SET LOCAL lock_timeout = '5s'"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
                for table in TABLES:
                    assert visibility_state(connection, table) == (None, False), table
                migration.upgrade()
            for table in TABLES:
                column, indexed = visibility_state(connection, table)
                assert column is not None and column['nullable'] is False and indexed, table
            assert connection.execute(text('SELECT visibility FROM post WHERE id = :id'),
                                      {'id': post_id}).scalar() == 'public'
            assert connection.execute(text('SELECT DISTINCT visibility FROM post_reply')).scalars().all() == ['public']
        finally:
            transaction.rollback()

"""Interop C1 item 7 migration f4b8d2a6c1e7: the (user_id, posted_at DESC NULLS LAST) indexes the user outbox pages
with, built without locking writes, and that it reverses cleanly."""
import importlib.util
import io
from pathlib import Path

import alembic
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from packaging.version import Version
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

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


def _rebuild_on_failure(connection, migration):
    """The finally of the rerun tests: put back whatever a failure left missing or broken."""
    connection.rollback()
    if any(index(connection, table, name) is None or valid(connection, name) is not True
           for table, name in INDEXES.items()):
        for name in INDEXES.values():
            connection.execute(text(f'DROP INDEX IF EXISTS {name}'))
        connection.commit()
        run(connection, migration.upgrade)
    connection.execute(text('DROP TABLE IF EXISTS m5_interrupted'))
    connection.execute(text('RESET lock_timeout'))
    connection.commit()


def test_the_upgrade_reruns_after_an_interrupted_build(app, db_session):
    """Final review M5. An interrupted CREATE INDEX CONCURRENTLY (a deploy timeout, a cancelled lock wait) leaves an
    INVALID index of that name behind, and the rerun failed with 'already exists', blocking the auto-deploy. The
    leftover is made here the way PostgreSQL makes one: a concurrent unique build that fails part-way."""
    db.session.close()
    migration = load_migration()
    with db.engine.connect() as connection:
        connection.execute(text("SET lock_timeout = '5s'"))
        try:
            connection.commit()
            connection.execute(text('DROP INDEX IF EXISTS ix_post_user_posted'))
            connection.execute(text('CREATE TABLE m5_interrupted (x integer)'))
            connection.execute(text('INSERT INTO m5_interrupted VALUES (1), (1)'))
            connection.commit()
            with db.engine.connect().execution_options(isolation_level='AUTOCOMMIT') as autocommit:
                with pytest.raises(IntegrityError):
                    autocommit.execute(text('CREATE UNIQUE INDEX CONCURRENTLY ix_post_user_posted ON m5_interrupted (x)'))
            assert valid(connection, 'ix_post_user_posted') is False

            run(connection, migration.upgrade)

            for table, name in INDEXES.items():
                assert index(connection, table, name) is not None
                assert valid(connection, name) is True
        finally:
            _rebuild_on_failure(connection, migration)


def test_the_upgrade_reruns_when_only_the_first_index_was_built(app, db_session):
    """Interrupted between the two builds: the first index is there and valid, the second is not."""
    db.session.close()
    migration = load_migration()
    with db.engine.connect() as connection:
        connection.execute(text("SET lock_timeout = '5s'"))
        try:
            connection.commit()
            connection.execute(text('DROP INDEX IF EXISTS ix_post_reply_user_posted'))
            connection.commit()

            run(connection, migration.upgrade)

            for table, name in INDEXES.items():
                assert index(connection, table, name) is not None
                assert valid(connection, name) is True
        finally:
            _rebuild_on_failure(connection, migration)


def test_the_alembic_the_upgrade_needs_is_required():
    """create_index(if_not_exists=...) arrived in Alembic 1.12. flask-migrate~=4.1 asks only for 1.9, so without a
    pin of its own a fresh install could pick an Alembic the upgrade's rerun fails on."""
    requirements = (Path(__file__).resolve().parent.parent / 'requirements.txt').read_text().splitlines()
    assert 'alembic>=1.12' in [line.split('#', 1)[0].strip() for line in requirements]
    assert Version(alembic.__version__) >= Version('1.12')

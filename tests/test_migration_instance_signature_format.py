"""S5 migration c4a8e2f61d3b: Instance.signature_format, and that it reverses cleanly."""
import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text

from app import db
from tests.factories import make_instance

MIGRATION = Path(__file__).resolve().parent.parent / 'migrations' / 'versions' / 'c4a8e2f61d3b_instance_signature_format.py'


def load_migration():
    spec = importlib.util.spec_from_file_location('signature_format_migration', MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def column(connection):
    return next((c for c in inspect(connection).get_columns('instance') if c['name'] == 'signature_format'), None)


def test_the_revision_chain():
    migration = load_migration()
    assert (migration.revision, migration.down_revision) == ('c4a8e2f61d3b', 'b7e1c2d9f4a6')


def test_not_null_with_a_cavage_default(app, db_session):
    with db.engine.connect() as connection:
        found = column(connection)
    assert found is not None and found['nullable'] is False and "'cavage'" in found['default']
    instance = make_instance('fresh.example')
    db.session.refresh(instance)
    assert instance.signature_format == 'cavage'


def test_downgrade_then_upgrade_restores_the_column(app, db_session):
    instance_id = make_instance('peer.example').id
    db.session.close()
    migration = load_migration()
    with db.engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text("SET LOCAL lock_timeout = '5s'"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
                assert column(connection) is None
                migration.upgrade()
            assert connection.execute(text('SELECT signature_format FROM instance WHERE id = :id'),
                                      {'id': instance_id}).scalar() == 'cavage'
        finally:
            transaction.rollback()

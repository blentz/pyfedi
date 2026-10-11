"""Interop C1 item 3 migration e2a7c4f9d1b3: Report.ap_id, its index, and that it reverses cleanly."""
import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text

from app import db
from app.models import Report

MIGRATION = Path(__file__).resolve().parent.parent / 'migrations' / 'versions' / 'e2a7c4f9d1b3_report_ap_id.py'


def load_migration():
    spec = importlib.util.spec_from_file_location('report_ap_id_migration', MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def column(connection):
    return next((c for c in inspect(connection).get_columns('report') if c['name'] == 'ap_id'), None)


def index(connection):
    return next((i for i in inspect(connection).get_indexes('report') if i['name'] == 'ix_report_ap_id'), None)


def test_the_revision_chain():
    migration = load_migration()
    assert (migration.revision, migration.down_revision) == ('e2a7c4f9d1b3', 'd9f3b7a15c2e')


def test_a_nullable_indexed_column(app, db_session):
    with db.engine.connect() as connection:
        found = column(connection)
        found_index = index(connection)
    assert found is not None and found['nullable'] is True
    assert found['type'].length == 255
    assert found_index is not None and found_index['column_names'] == ['ap_id']


def test_downgrade_then_upgrade_restores_the_column_and_index(app, db_session):
    report = Report(reasons='spam', description='', type=0)
    db.session.add(report)
    db.session.commit()
    report_id = report.id
    db.session.close()
    migration = load_migration()
    with db.engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text("SET LOCAL lock_timeout = '5s'"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
                assert column(connection) is None
                assert index(connection) is None
                migration.upgrade()
            assert column(connection) is not None
            assert index(connection) is not None
            assert connection.execute(text('SELECT ap_id FROM report WHERE id = :id'),
                                      {'id': report_id}).scalar() is None
        finally:
            transaction.rollback()

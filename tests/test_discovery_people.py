"""Pixelfed seeding (spec 2026-10-29): the instance actor follows the top N opted-in Pixelfed people per server, and
their posts land in microblogs. One discovery_person_sync row per followed account, and the admin's N."""
import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from app import db
from app.discovery import SYNC_NONE
from app.discovery.people import people_per_host
from app.discovery.sync import MAX_PER_HOST
from app.models import DiscoveryPersonSync, User
from app.utils import set_setting
from tests.discovery_fixtures import add_entry
from tests.factories import make_instance, make_user

pytestmark = pytest.mark.usefixtures('site')

MIGRATION = Path(__file__).resolve().parent.parent / 'migrations' / 'versions' / 'f3c7a1e9b5d2_discovery_person_sync.py'


def remote_person(name='erin', host='pixelfed.example'):
    user = make_user(make_instance(host, software='pixelfed'), name)
    db.session.commit()
    return user


# --- the row ---

def test_a_row_defaults_to_no_follow_sent(db_session):
    erin = remote_person()
    row = DiscoveryPersonSync(user_id=erin.id, follow_target=erin.ap_profile_id)
    db.session.add(row)
    db.session.commit()

    assert row.follow_state == SYNC_NONE
    assert row.follow_uuid is None and row.followed_at is None and row.last_polled_at is None
    assert row.created_at is not None


def test_an_account_has_at_most_one_row(db_session):
    erin = remote_person()
    db.session.add(DiscoveryPersonSync(user_id=erin.id, follow_target='https://pixelfed.example/users/a'))
    db.session.commit()
    db.session.add(DiscoveryPersonSync(user_id=erin.id, follow_target='https://pixelfed.example/users/b'))

    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_the_row_goes_with_its_account_and_outlives_its_entry(db_session):
    erin = remote_person()
    erin_id = erin.id
    entry = add_entry('Erin', host='pixelfed.example', kind='person', platform='pixelfed')
    db.session.add(DiscoveryPersonSync(user_id=erin_id, entry_id=entry.id, follow_target=erin.ap_profile_id))
    db.session.commit()

    db.session.delete(entry)
    db.session.commit()
    db.session.expire_all()
    assert db.session.get(DiscoveryPersonSync, erin_id).entry_id is None

    db.session.execute(User.__table__.delete().where(User.id == erin_id))
    db.session.commit()
    db.session.expire_all()
    assert db.session.get(DiscoveryPersonSync, erin_id) is None


# --- the setting (owner ruling S3: its own, default 0) ---

def test_the_default_is_zero(db_session):
    assert people_per_host() == 0


@pytest.mark.parametrize('stored,expected', [(5, 5), (0, 0), (MAX_PER_HOST + 1, MAX_PER_HOST), (-1, 0), (True, 0),
                                             ('5', 0), (None, 0)])
def test_a_stored_value_is_held_to_the_range(db_session, stored, expected):
    set_setting('discovery_people_per_host', stored)
    assert people_per_host() == expected


# --- the migration ---

def load_migration():
    spec = importlib.util.spec_from_file_location('discovery_person_sync_migration', MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_revision_chain():
    migration = load_migration()
    assert (migration.revision, migration.down_revision) == ('f3c7a1e9b5d2', 'e8b4d2a6c1f3')


def test_downgrade_then_upgrade_restores_the_table(app, db_session):
    db.session.close()
    migration = load_migration()
    with db.engine.connect() as connection:
        transaction = connection.begin()
        try:
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
                assert 'discovery_person_sync' not in inspect(connection).get_table_names()
                migration.upgrade()
            assert 'discovery_person_sync' in inspect(connection).get_table_names()
            assert {i['name'] for i in inspect(connection).get_indexes('discovery_person_sync')} == \
                {'ix_discovery_person_sync_follow_uuid'}
        finally:
            transaction.rollback()

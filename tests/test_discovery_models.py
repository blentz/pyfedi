"""Interop D24: the fork migration adding discovery_entry and post.extensions, and the two models."""
import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app import db
from app.models import DiscoveryEntry, Post
from tests.factories import make_community, make_instance, make_post, make_user

MIGRATION = (Path(__file__).resolve().parent.parent / 'migrations' / 'versions'
             / 'c6e1d9a4b7f2_discovery_entry_and_post_extensions.py')


def load_migration():
    spec = importlib.util.spec_from_file_location('discovery_migration', MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def an_entry(**changes):
    values = dict(kind='community', platform='peertube', actor_url='https://tube.example/video-channels/linux',
                  name='Linux', host='tube.example', followers=5, source='sepiasearch')
    values.update(changes)
    return DiscoveryEntry(**values)


def test_the_migration_revises_the_previous_fork_head():
    migration = load_migration()
    assert migration.revision == 'c6e1d9a4b7f2'
    assert migration.down_revision == 'b5d2c8e1f7a3'
    assert migration.branch_labels is None


def test_an_entry_round_trips_with_its_defaults(app, db_session):
    entry = an_entry()
    db.session.add(entry)
    db.session.commit()
    db.session.expire_all()

    stored = db.session.get(DiscoveryEntry, entry.id)

    assert stored.nsfw is False
    assert stored.avatar_url is None
    assert stored.first_seen is not None and stored.last_seen is not None


def test_followers_defaults_to_zero_in_the_model_and_the_table(app, db_session):
    db.session.add(DiscoveryEntry(kind='community', platform='peertube', actor_url='https://t.example/video-channels/a',
                                  name='Model default', host='t.example', source='sepiasearch'))
    db.session.execute(text("INSERT INTO discovery_entry (kind, platform, actor_url, name, host, source, first_seen, "
                            "last_seen) VALUES ('community', 'peertube', 'https://t.example/video-channels/b', "
                            "'Server default', 't.example', 'sepiasearch', now(), now())"))
    db.session.commit()
    db.session.expire_all()

    assert {row.name: row.followers for row in DiscoveryEntry.query.all()} == {'Model default': 0, 'Server default': 0}


def test_actor_url_is_unique(app, db_session):
    db.session.add(an_entry())
    db.session.commit()
    db.session.add(an_entry(name='Again'))

    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_post_extensions_stores_json(app, db_session):
    author = make_user(make_instance('m.example'), 'alice')
    post = make_post(make_community(), author, 'https://m.example/p/1')
    assert post.extensions is None

    post.extensions = {'podcast': {'credits': [{'name': 'Ann', 'role': 'host'}]}}
    db.session.commit()
    db.session.expire_all()

    assert db.session.get(Post, post.id).extensions['podcast']['credits'][0]['name'] == 'Ann'


def test_downgrade_then_upgrade_restores_both(app, db_session):
    db.session.close()  # nothing of the session's may still hold a lock on post
    migration = load_migration()

    with db.engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text("SET LOCAL lock_timeout = '5s'"))
            connection.execute(text('DROP TABLE IF EXISTS discovery_sync'))  # its FK holds discovery_entry
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
                inspector = inspect(connection)
                assert 'discovery_entry' not in inspector.get_table_names()
                assert 'extensions' not in [c['name'] for c in inspector.get_columns('post')]
                migration.upgrade()
            inspector = inspect(connection)
            assert 'discovery_entry' in inspector.get_table_names()
            unique = [i for i in inspector.get_indexes('discovery_entry') if i['column_names'] == ['actor_url']]
            assert unique and unique[0]['unique']
            column = next(c for c in inspector.get_columns('post') if c['name'] == 'extensions')
            assert column['nullable'] is True
        finally:
            transaction.rollback()

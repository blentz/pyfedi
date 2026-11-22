"""The microblog feed setting's rules (app.microblog_feed, spec 2026-11-21-microblog-feed-setting) and its migration.
What each surface does with them is tested beside that surface: the outbox (test_ap_interop_person_outbox.py), the
Mastodon client API (test_mastodon_timelines.py), the pushes (test_shared_tasks_send_post.py,
test_shared_tasks_send_reply.py, test_send_peertube_video.py) and the settings page (test_user_settings.py)."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import inspect

from app import db
from app.constants import COMMUNITY_KIND_MICROBLOGS
from tests.factories import make_instance
from app.microblog_feed import feed_of, in_microblog_community, post_in_feed, shows_community_posts, shows_replies

MIGRATION = Path(__file__).resolve().parent.parent / 'migrations' / 'versions' / '9c1e42dfcd53_user_microblog_feed.py'


def _user(feed):
    return SimpleNamespace(microblog_feed=feed)


@pytest.mark.parametrize('feed, community_posts, replies', [('microblog', False, False), ('community', True, False),
                                                             ('all', True, True)])
def test_each_feed_carries_what_its_label_says(feed, community_posts, replies):
    assert feed_of(_user(feed)) == feed
    assert shows_community_posts(_user(feed)) is community_posts
    assert shows_replies(_user(feed)) is replies


@pytest.mark.parametrize('stored', [None, '', 'everything'])
def test_a_value_outside_the_choices_reads_as_the_default(stored):
    assert feed_of(_user(stored)) == 'microblog'
    assert not shows_community_posts(_user(stored)) and not shows_replies(_user(stored))


@pytest.mark.parametrize('kind, ap_id, expected', [(COMMUNITY_KIND_MICROBLOGS, None, True),
                                                   (COMMUNITY_KIND_MICROBLOGS, 'microblogs@remote.example', False),
                                                   ('community', None, False)])
def test_only_the_local_microblogs_community_is_the_microblog_feed(kind, ap_id, expected):
    community = SimpleNamespace(kind=kind, ap_id=ap_id)
    assert in_microblog_community(community) is expected
    assert post_in_feed(_user('microblog'), SimpleNamespace(community=community)) is expected
    assert post_in_feed(_user('community'), SimpleNamespace(community=community)) is True


def _migration():
    spec = importlib.util.spec_from_file_location('user_microblog_feed_migration', MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_revision_chain():
    migration = _migration()
    assert (migration.revision, migration.down_revision) == ('9c1e42dfcd53', 'f5b1d8e3a7c9')


def test_the_column_is_required_and_defaults_to_microblog_posts_only(app, db_session):
    """Rows from before the column existed read 'microblog': the server default fills them."""
    with db.engine.connect() as connection:
        column = next(found for found in inspect(connection).get_columns('user') if found['name'] == 'microblog_feed')
    assert column['nullable'] is False
    assert "'microblog'" in str(column['default'])


@pytest.mark.serial   # it alters the user table every worker reads (run_tests.sh runs these alone)
def test_the_migration_reverses_and_reapplies(app, db_session):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text

    migration = _migration()
    instance_id = make_instance('mfmig.example').id   # committed: the migration's own connection sees it

    def columns(connection):
        return {found['name'] for found in inspect(connection).get_columns('user')}

    with db.engine.connect() as connection:
        connection.execute(text("INSERT INTO \"user\" (user_name, email, instance_id) VALUES ('mfmig', 'mfmig@example.com', :instance)"),
                           {'instance': instance_id})
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
            assert 'microblog_feed' not in columns(connection)
            migration.upgrade()
        assert 'microblog_feed' in columns(connection)
        assert connection.execute(text("SELECT microblog_feed FROM \"user\" WHERE user_name = 'mfmig'")).scalar() == \
            'microblog'   # a row from before the column reads the default
        connection.rollback()

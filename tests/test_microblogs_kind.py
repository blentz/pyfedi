"""The local microblogs community is marked by kind (spec 2026-10-14 §3.1, §10 A15.3). Name tests that also match
a remote PieFed's microblogs community keep the name."""
import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from app import db
from app.activitypub.util import find_microblogging_community
from app.community.live import is_local_microblogs as live_is_local_microblogs
from app.constants import COMMUNITY_KIND_DISCUSSION, COMMUNITY_KIND_MICROBLOGS
from app.utils import is_local_microblogs
from tests.factories import make_community, make_instance, make_site, make_user

MIGRATION = Path(__file__).resolve().parent.parent / 'migrations' / 'versions' / 'd4a7c1e9b3f6_mark_microblogs_kind.py'


def _local():
    make_site()
    make_instance('test.piefed.local')
    make_user(None, 'admin', local=True)   # id 1: find_microblogging_community's owner


def test_a_new_microblogs_community_has_the_kind(app, db_session):
    _local()
    assert find_microblogging_community().kind == COMMUNITY_KIND_MICROBLOGS


def test_is_local_microblogs_reads_the_kind(app, db_session):
    _local()
    named = make_community('microblogs')          # the name alone no longer makes it the microblogs community
    assert not is_local_microblogs(named)
    assert not live_is_local_microblogs(named)
    named.kind = COMMUNITY_KIND_MICROBLOGS
    db.session.commit()
    assert is_local_microblogs(named)
    assert live_is_local_microblogs(named)


def test_a_remote_community_of_the_kind_is_not_the_local_one(app, db_session):
    _local()
    remote_instance = make_instance('other.example')
    remote = make_community('microblogs')
    remote.ap_id = 'microblogs@other.example'
    remote.ap_profile_id = 'https://other.example/c/microblogs'
    remote.instance_id = remote_instance.id
    remote.kind = COMMUNITY_KIND_MICROBLOGS
    db.session.commit()
    assert not is_local_microblogs(remote)
    assert not live_is_local_microblogs(remote)


def test_the_migration_marks_the_row_and_reverses(app, db_session):
    _local()
    row = make_community('microblogs')
    row.user_id, row.instance_id, row.kind = 1, 1, COMMUNITY_KIND_DISCUSSION
    other = make_community('books')
    db.session.commit()
    row_id, other_id = row.id, other.id   # the session closes below
    spec = importlib.util.spec_from_file_location('mark_microblogs_kind', MIGRATION)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    db.session.close()
    with db.engine.connect() as connection:
        def kind(community_id):
            return connection.execute(text('SELECT kind FROM community WHERE id = :id'), {'id': community_id}).scalar()
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        assert kind(row_id) == COMMUNITY_KIND_MICROBLOGS
        assert kind(other_id) == COMMUNITY_KIND_DISCUSSION
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
        assert kind(row_id) == COMMUNITY_KIND_DISCUSSION
        connection.rollback()

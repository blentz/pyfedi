"""A video_channel community's Group names its owner Person in `attributedTo` (spec 2026-10-14 §6.2, §10 A11).
PeerTube refuses to subscribe to a Group without one: `Cannot find account attributed to video channel`. Every other
community keeps the moderators collection URL there, which Lemmy and mbin read; `moderators` keeps it for all."""
import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect

from app import db
from app.activitypub.util import community_actor_data
from app.constants import COMMUNITY_KIND_DISCUSSION, COMMUNITY_KIND_VIDEO_CHANNEL
from app.models import Community
from tests.factories import make_community, make_instance, make_site, make_user

MIGRATION = Path(__file__).resolve().parent.parent / 'migrations' / 'versions' / 'c2f8d4b6a9e1_community_kind.py'


def _community(kind=None, owned=True):
    make_site()
    make_instance('test.piefed.local')   # id 1, the local instance
    owner = make_user(None, 'alice', local=True)
    community = make_community('vc1')
    community.user_id = owner.id if owned else None
    if kind is not None:
        community.kind = kind
    db.session.commit()
    return owner, community


def test_a_new_community_is_a_discussion(app, db_session):
    _, community = _community()
    db.session.expire_all()
    assert db.session.get(Community, community.id).kind == COMMUNITY_KIND_DISCUSSION


def test_a_video_channel_names_its_owner_person(app, db_session):
    owner, community = _community(COMMUNITY_KIND_VIDEO_CHANNEL)

    actor = community_actor_data(community)

    assert actor['attributedTo'] == [{'type': 'Person', 'id': owner.public_url()}]
    assert actor['moderators'] == f"{actor['id']}/moderators"


def test_a_discussion_community_keeps_the_moderators_url(app, db_session):
    _, community = _community(COMMUNITY_KIND_DISCUSSION)

    actor = community_actor_data(community)

    assert actor['attributedTo'] == f"{actor['id']}/moderators"


def test_a_video_channel_without_an_owner_keeps_the_moderators_url(app, db_session):
    _, community = _community(COMMUNITY_KIND_VIDEO_CHANNEL, owned=False)

    actor = community_actor_data(community)

    assert actor['attributedTo'] == f"{actor['id']}/moderators"


def test_the_migration_adds_the_column_and_reverses(app, db_session):
    spec = importlib.util.spec_from_file_location('community_kind_migration', MIGRATION)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    db.session.close()
    with db.engine.connect() as connection:
        def columns():
            return {column['name']: column for column in inspect(connection).get_columns('community')}
        assert columns()['kind']['nullable'] is False
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
        assert 'kind' not in columns()
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        assert "'discussion'" in columns()['kind']['default']
        connection.rollback()

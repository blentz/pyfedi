"""Interop D24 proactive sync: one discovery_sync row per synced community."""
import pytest
from sqlalchemy.exc import IntegrityError

from app import db
from app.discovery import SYNC_NONE
from app.models import Community, DiscoverySync
from tests.discovery_fixtures import add_entry
from tests.factories import make_community, make_instance, make_user

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture(autouse=True)
def founder(db_session):
    # make_community points at user 1 and instance 1
    return make_user(make_instance('world.example', software='piefed'), 'zqfounder', local=True)


def test_a_row_defaults_to_no_follow_sent(db_session):
    community = make_community('zqsync', host='tube.example')
    entry = add_entry('Zqsync', host='tube.example')
    row = DiscoverySync(community_id=community.id, entry_id=entry.id, follow_target=entry.actor_url)
    db.session.add(row)
    db.session.commit()

    assert row.follow_state == SYNC_NONE
    assert row.follow_uuid is None and row.followed_at is None and row.last_polled_at is None
    assert row.created_at is not None


def test_a_community_has_at_most_one_row(db_session):
    community = make_community('zqsync', host='tube.example')
    db.session.add(DiscoverySync(community_id=community.id, follow_target='https://tube.example/a'))
    db.session.commit()
    db.session.add(DiscoverySync(community_id=community.id, follow_target='https://tube.example/b'))

    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_deleting_the_entry_keeps_the_row(db_session):
    community = make_community('zqsync', host='tube.example')
    entry = add_entry('Zqsync', host='tube.example')
    db.session.add(DiscoverySync(community_id=community.id, entry_id=entry.id, follow_target=entry.actor_url))
    db.session.commit()

    db.session.delete(entry)
    db.session.commit()

    assert db.session.get(DiscoverySync, community.id).entry_id is None


def test_deleting_the_community_deletes_the_row(db_session):
    community = make_community('zqsync', host='tube.example')
    db.session.add(DiscoverySync(community_id=community.id, follow_target='https://tube.example/a'))
    db.session.commit()

    db.session.execute(db.delete(Community).where(Community.id == community.id))
    db.session.commit()

    assert db.session.get(DiscoverySync, community.id) is None

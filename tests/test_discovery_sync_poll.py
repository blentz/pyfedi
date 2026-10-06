"""Interop D24 proactive sync: the daily poll of one synced community."""
import pytest

from app import db
from app.discovery import sync
from app.discovery.sync import poll_synced_community
from app.models import DiscoverySync, utcnow
from tests.factories import make_community, make_instance, make_user

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture(autouse=True)
def founder(db_session):
    make_user(make_instance('test.piefed.local', software='piefed'), 'founder', local=True)


@pytest.fixture
def row(db_session):
    community = make_community('zqchan', host='tube.example')
    row = DiscoverySync(community_id=community.id, follow_target=community.ap_profile_id, last_error='old')
    db.session.add(row)
    db.session.commit()
    return row


def stub(monkeypatch, outcome=None, raises=None):
    calls = []

    def run(community_id, stop_at_known=False):
        calls.append((community_id, stop_at_known))
        if raises:
            raise raises
        return outcome
    monkeypatch.setattr(sync, 'run_backfill', run)
    return calls


def test_a_good_poll_stops_at_known_posts_records_the_time_and_clears_the_error(row, monkeypatch):
    calls = stub(monkeypatch)

    poll_synced_community(row.community_id)

    fresh = db.session.get(DiscoverySync, row.community_id)
    assert calls == [(row.community_id, True)]
    assert fresh.last_polled_at is not None and fresh.last_error is None


def test_an_unreadable_outbox_is_recorded(row, monkeypatch):
    stub(monkeypatch, outcome='outbox unreadable')
    poll_synced_community(row.community_id)
    assert db.session.get(DiscoverySync, row.community_id).last_error == 'outbox unreadable'


def test_an_exception_is_recorded_truncated_and_not_raised(row, monkeypatch):
    stub(monkeypatch, raises=RuntimeError('x' * 400))
    poll_synced_community(row.community_id)
    error = db.session.get(DiscoverySync, row.community_id).last_error
    assert error.startswith('RuntimeError: x') and len(error) == 255


def test_a_missing_row_does_nothing(db_session, monkeypatch):
    calls = stub(monkeypatch)
    poll_synced_community(999999)
    assert calls == []


@pytest.mark.parametrize('change', ['banned', 'deleted'])
def test_a_banned_or_deleted_community_loses_its_row_unpolled(row, monkeypatch, change):
    calls = stub(monkeypatch)
    community = db.session.get(sync.Community, row.community_id)
    if change == 'banned':
        community.banned = True
    else:
        community.ap_deleted_at = utcnow()
    db.session.commit()

    poll_synced_community(row.community_id)

    assert calls == [] and db.session.get(DiscoverySync, row.community_id) is None

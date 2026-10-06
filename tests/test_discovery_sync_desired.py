"""Interop D24 proactive sync: which directory entries the instance keeps synced."""
import pytest

from app import db
from app.discovery import sync
from app.discovery.sync import desired_entries, sync_per_host, sync_platforms
from app.utils import set_setting
from tests.discovery_fixtures import add_entry, fresh_cache  # noqa: F401
from tests.factories import make_banned_instance, make_community, make_instance, make_user

pytestmark = pytest.mark.usefixtures('site', 'fresh_cache')


@pytest.fixture(autouse=True)
def founder(db_session):
    # make_community points at user 1 and instance 1
    return make_user(make_instance('world.example', software='piefed'), 'zqfounder', local=True)


def names(desired):
    return {host: [e.name for e in entries] for host, entries in desired.items()}


@pytest.mark.parametrize('stored, expected', [
    (None, 0), (5, 5), (0, 0), (50, 50), (51, 50), (-1, 0), ('7', 0), (True, 0), (2.5, 0)])
def test_per_host_is_an_int_from_0_to_50(db_session, stored, expected):
    if stored is not None:
        set_setting('discovery_sync_per_host', stored)
    assert sync_per_host() == expected


@pytest.mark.parametrize('stored, expected', [
    (None, ['peertube', 'castopod']), (['castopod'], ['castopod']),
    (['mastodon', 'peertube'], ['peertube']), ('peertube', []), ([], [])])
def test_platforms_keep_only_syncable_ones(db_session, stored, expected):
    if stored is not None:
        set_setting('discovery_sync_platforms', stored)
    assert sync_platforms() == expected


def test_nothing_is_desired_while_per_host_is_zero(db_session):
    add_entry('Bigchan', host='tube.example', followers=900)
    assert desired_entries() == {}


def test_nothing_is_desired_with_no_platform(db_session):
    set_setting('discovery_sync_per_host', 5)
    set_setting('discovery_sync_platforms', [])
    add_entry('Bigchan', host='tube.example', followers=900)
    assert desired_entries() == {}


def test_top_n_per_host_ranked_by_followers_then_name(db_session):
    set_setting('discovery_sync_per_host', 2)
    add_entry('Bigchan', host='tube.example', followers=900)
    add_entry('Bbtie', host='tube.example', followers=500)
    add_entry('Aatie', host='tube.example', followers=500)
    add_entry('Tiny', host='tube.example', followers=1)
    add_entry('Pod', host='pod.example', platform='castopod', url='https://pod.example/@pod', followers=3)

    assert names(desired_entries()) == {'tube.example': ['Bigchan', 'Aatie'], 'pod.example': ['Pod']}


def test_people_nsfw_disabled_platforms_and_excluded_hosts_are_never_desired(db_session):
    set_setting('discovery_sync_per_host', 5)
    set_setting('discovery_sync_platforms', ['peertube'])
    add_entry('Ok', host='tube.example')
    add_entry('Spicy', host='tube.example', nsfw=True)
    add_entry('Ann', host='m.example', platform='mastodon', kind='person', url='https://m.example/users/ann')
    add_entry('Pod', host='pod.example', platform='castopod', url='https://pod.example/@pod')
    add_entry('Gone', host='banned.example')
    make_banned_instance('banned.example')

    assert names(desired_entries()) == {'tube.example': ['Ok']}


def test_an_entry_whose_community_is_banned_or_deleted_is_skipped_and_does_not_use_a_slot(db_session):
    from app.models import utcnow
    set_setting('discovery_sync_per_host', 1)
    banned = add_entry('Banned', host='tube.example', followers=900)
    deleted = add_entry('Deleted', host='tube.example', followers=800)
    add_entry('Next', host='tube.example', followers=1)
    for entry, name in ((banned, 'banned'), (deleted, 'deleted')):
        community = make_community(name, host='tube.example')
        community.ap_profile_id = entry.actor_url.lower()
    db.session.query(sync.Community).filter_by(name='banned').one().banned = True
    db.session.query(sync.Community).filter_by(name='deleted').one().ap_deleted_at = utcnow()
    db.session.commit()

    assert names(desired_entries()) == {'tube.example': ['Next']}

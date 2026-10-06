"""Interop D24 proactive sync: the daily reconcile keeps exactly each host's top N synced."""
from datetime import timedelta
from fnmatch import fnmatch

import pytest

from app import celery, db
from app.discovery import SYNC_ACCEPTED, SYNC_NONE, SYNC_PENDING, sync
from app.discovery.sync import enqueue_polls, reconcile_sync, reconcile_sync_task
from app.models import Community, CommunityMember, DiscoverySync, Instance, utcnow
from app.utils import set_setting
from tests.discovery_fixtures import add_entry, fresh_cache  # noqa: F401
from tests.factories import make_community, make_community_member, make_instance, make_user

pytestmark = pytest.mark.usefixtures('site', 'fresh_cache')


@pytest.fixture(autouse=True)
def founder(db_session):
    make_user(make_instance('test.piefed.local', software='piefed'), 'founder', local=True)


@pytest.fixture
def fed(monkeypatch):
    """Doubles for everything that leaves this process; records what was asked."""
    log = {'follow': [], 'undo': [], 'backfill': [], 'resolve': []}

    def resolve(actor_url, community_only=False):
        log['resolve'].append(actor_url)
        name = actor_url.rstrip('/').rsplit('/', 1)[-1].lower()
        host = actor_url.split('/')[2]
        community = make_community(name, host=host)
        community.ap_profile_id = actor_url.lower()
        db.session.commit()
        return community

    def follow(row, community):
        log['follow'].append(row.follow_target)
        row.follow_state = SYNC_PENDING
        row.followed_at = utcnow()
        row.follow_uuid = row.follow_uuid or 'u-' + str(row.community_id)
        db.session.commit()
        return True

    monkeypatch.setattr(sync, 'find_actor_or_create', resolve)
    monkeypatch.setattr(sync, 'send_instance_follow', follow)
    monkeypatch.setattr(sync, 'send_instance_undo', lambda row, community: log['undo'].append(row.follow_target))
    monkeypatch.setattr(sync, 'queue_backfill', log['backfill'].append)
    return log


def chans(host, count, start=1000):
    return [add_entry(f'C{i:02d}{host[:2]}', host=host, followers=start - i) for i in range(count)]


def test_off_by_default_adds_nothing(db_session, fed):
    chans('tube.example', 3)
    assert reconcile_sync() == {'added': 0, 'dropped': 0, 'refollowed': 0, 'failed_hosts': []}
    assert fed['follow'] == []


def test_adds_top_n_creates_backfills_and_follows(db_session, fed):
    set_setting('discovery_sync_per_host', 2)
    entries = chans('tube.example', 3)

    summary = reconcile_sync()

    assert summary['added'] == 2
    assert fed['follow'] == [entries[0].actor_url, entries[1].actor_url]
    assert len(fed['backfill']) == 2
    assert {r.entry_id for r in DiscoverySync.query} == {entries[0].id, entries[1].id}


def test_an_existing_community_with_posts_is_not_backfilled_again(db_session, fed, monkeypatch):
    set_setting('discovery_sync_per_host', 1)
    entry = chans('tube.example', 1)[0]
    community = make_community('c00tu', host='tube.example')
    community.ap_profile_id = entry.actor_url.lower()
    community.post_count = 3
    db.session.commit()

    reconcile_sync()

    assert fed['resolve'] == [] and fed['backfill'] == []
    assert fed['follow'] == [entry.actor_url]


def test_a_resolve_that_gives_no_community_is_skipped(db_session, fed, monkeypatch):
    set_setting('discovery_sync_per_host', 1)
    chans('tube.example', 1)
    monkeypatch.setattr(sync, 'find_actor_or_create', lambda url, community_only=False: None)

    assert reconcile_sync()['added'] == 0
    assert DiscoverySync.query.count() == 0


def test_adds_ramp_at_ten_per_host_per_run(db_session, fed):
    set_setting('discovery_sync_per_host', 25)
    chans('tube.example', 25)

    assert reconcile_sync()['added'] == 10
    assert reconcile_sync()['added'] == 10
    assert reconcile_sync()['added'] == 5


def test_lowering_n_drops_the_lowest_ranked_and_keeps_the_rest_untouched(db_session, fed):
    """Review Focus 3."""
    set_setting('discovery_sync_per_host', 4)
    entries = chans('tube.example', 4)
    reconcile_sync()
    fed['follow'].clear()
    set_setting('discovery_sync_per_host', 1)

    summary = reconcile_sync()

    assert summary['dropped'] == 3
    assert sorted(fed['undo']) == sorted(e.actor_url for e in entries[1:])
    assert fed['follow'] == []
    assert [r.entry_id for r in DiscoverySync.query] == [entries[0].id]
    assert Community.query.count() == 4   # dropped communities and their posts stay


def test_per_host_zero_drops_everything(db_session, fed):
    set_setting('discovery_sync_per_host', 2)
    chans('tube.example', 2)
    reconcile_sync()
    set_setting('discovery_sync_per_host', 0)

    assert reconcile_sync()['dropped'] == 2
    assert DiscoverySync.query.count() == 0


def test_dropping_a_channel_a_local_user_joined_keeps_their_membership(db_session, fed):
    """Review Focus 4."""
    set_setting('discovery_sync_per_host', 1)
    entry = chans('tube.example', 1)[0]
    reconcile_sync()
    community = Community.query.one()
    local = make_user(db.session.get(Instance, 1), 'zqlocal', local=True)
    make_community_member(local, community)
    set_setting('discovery_sync_per_host', 0)

    reconcile_sync()

    assert fed['undo'] == [entry.actor_url]
    assert CommunityMember.query.filter_by(user_id=local.id, community_id=community.id).count() == 1


def test_a_row_whose_community_is_gone_is_dropped(db_session, fed):
    set_setting('discovery_sync_per_host', 1)
    chans('tube.example', 1)
    reconcile_sync()
    community = Community.query.one()
    community.banned = True
    db.session.commit()

    assert reconcile_sync()['dropped'] == 1


def test_drop_row_with_no_community_still_deletes_it(db_session, fed, monkeypatch):
    community = make_community('orphan', host='tube.example')
    db.session.add(DiscoverySync(community_id=community.id, follow_target='https://tube.example/x'))
    db.session.commit()
    row = db.session.get(DiscoverySync, community.id)
    real_get = db.session.get
    # the FK cascades a deleted community's row away, so fake the lookup finding no community instead
    monkeypatch.setattr(db.session, 'get', lambda model, ident: None if model is Community else real_get(model, ident))

    sync.drop_row(row)

    assert DiscoverySync.query.count() == 0 and fed['undo'] == []


@pytest.mark.parametrize('state, age_days, resent', [
    (SYNC_NONE, 0, True), (SYNC_PENDING, 8, True), (SYNC_PENDING, 6, False), (SYNC_ACCEPTED, 30, False),
    (SYNC_PENDING, None, True)])
def test_follow_retry_rules(db_session, fed, state, age_days, resent):
    set_setting('discovery_sync_per_host', 1)
    chans('tube.example', 1)
    reconcile_sync()
    row = DiscoverySync.query.one()
    row.follow_state = state
    row.followed_at = None if age_days is None else utcnow() - timedelta(days=age_days)
    db.session.commit()
    fed['follow'].clear()

    assert reconcile_sync()['refollowed'] == (1 if resent else 0)
    assert len(fed['follow']) == (1 if resent else 0)


def test_one_host_failing_does_not_stop_the_others(db_session, fed, monkeypatch):
    set_setting('discovery_sync_per_host', 1)
    chans('aaa.example', 1)
    good = chans('bbb.example', 1)[0]
    real_resolve = sync.find_actor_or_create

    def flaky(url, community_only=False):
        if 'aaa.example' in url:
            raise RuntimeError('peer exploded')
        return real_resolve(url, community_only=community_only)
    monkeypatch.setattr(sync, 'find_actor_or_create', flaky)

    summary = reconcile_sync()

    assert summary['failed_hosts'] == ['aaa.example']
    assert [r.follow_target for r in DiscoverySync.query] == [good.actor_url]


def test_polls_are_spaced_per_host(db_session, fed, monkeypatch, app):
    set_setting('discovery_sync_per_host', 2)
    chans('tube.example', 2)
    chans('pod.example', 1)
    reconcile_sync()
    scheduled = []
    monkeypatch.setattr(app, 'debug', False)
    monkeypatch.setattr(sync.poll_synced_community, 'apply_async',
                        lambda args, countdown: scheduled.append((args[0], countdown)))

    assert enqueue_polls() == 3

    by_host = {}
    for community_id, countdown in scheduled:
        host = db.session.get(DiscoverySync, community_id).follow_target.split('/')[2]
        by_host.setdefault(host, []).append(countdown)
    assert sorted(by_host['tube.example']) == [0, 2] and by_host['pod.example'] == [0]


def test_in_debug_polls_run_inline(db_session, fed, monkeypatch, app):
    set_setting('discovery_sync_per_host', 1)
    chans('tube.example', 1)
    reconcile_sync()
    ran = []
    monkeypatch.setattr(app, 'debug', True)
    monkeypatch.setattr(sync, 'poll_synced_community', ran.append)

    enqueue_polls()

    assert ran == [DiscoverySync.query.one().community_id]


def test_the_task_reconciles_then_polls(db_session, monkeypatch):
    order = []
    monkeypatch.setattr(sync, 'reconcile_sync', lambda: order.append('reconcile') or {'added': 0})
    monkeypatch.setattr(sync, 'enqueue_polls', lambda: order.append('poll') or 0)

    assert reconcile_sync_task() == {'added': 0}
    assert order == ['reconcile', 'poll']


def test_host_of_a_malformed_target_is_empty():
    assert sync._host('nohost') == '' and sync._host('https://Tube.Example/x') == 'tube.example'


@pytest.mark.parametrize('task', ['poll_synced_community', 'reconcile_sync_task'])
def test_sync_tasks_run_on_the_background_queue(app, task):
    name = f'app.discovery.sync.{task}'
    assert getattr(sync, task).name == name
    routes = celery.conf.CELERY_ROUTES
    assert any(fnmatch(name, pattern) and route == {'queue': 'background'} for pattern, route in routes.items())


def test_an_entry_whose_url_differs_from_the_canonical_id_is_not_cycled(db_session, fed, monkeypatch):
    set_setting('discovery_sync_per_host', 1)
    entry = add_entry('Alias', host='tube.example', followers=5, url='https://tube.example/c/Alias')

    def resolve(actor_url, community_only=False):
        community = make_community('alias', host='tube.example')
        community.ap_profile_id = 'https://tube.example/video-channels/alias'
        db.session.commit()
        return community
    monkeypatch.setattr(sync, 'find_actor_or_create', resolve)

    assert reconcile_sync()['added'] == 1
    fed['follow'].clear()
    summary = reconcile_sync()

    assert summary == {'added': 0, 'dropped': 0, 'refollowed': 0, 'failed_hosts': []}
    assert fed['undo'] == [] and fed['follow'] == []
    assert [r.entry_id for r in DiscoverySync.query] == [entry.id]


def test_two_entries_resolving_to_one_community_give_one_row_and_no_failed_host(db_session, fed, monkeypatch):
    set_setting('discovery_sync_per_host', 3)
    add_entry('A', host='tube.example', followers=30, url='https://tube.example/c/a')
    add_entry('B', host='tube.example', followers=20, url='https://tube.example/c/b')
    other = add_entry('C', host='tube.example', followers=10, url='https://tube.example/c/c')
    shared = make_community('shared', host='tube.example')
    real_resolve = sync.find_actor_or_create
    monkeypatch.setattr(sync, 'find_actor_or_create',
                        lambda url, community_only=False: shared if url.endswith(('/a', '/b')) else
                        real_resolve(url, community_only=community_only))

    summary = reconcile_sync()

    assert summary['failed_hosts'] == [] and summary['added'] == 2
    assert {r.community_id for r in DiscoverySync.query} == {shared.id,
                                                             Community.query.filter_by(name='c').one().id}
    assert len(fed['follow']) == 2 and other.actor_url in fed['follow']


def test_one_row_failing_to_drop_does_not_abort_the_reconcile(db_session, fed, monkeypatch):
    set_setting('discovery_sync_per_host', 3)
    entries = chans('tube.example', 3)
    reconcile_sync()
    set_setting('discovery_sync_per_host', 1)

    def undo(row, community):
        if row.follow_target == entries[1].actor_url:
            raise RuntimeError('signing blew up')
        fed['undo'].append(row.follow_target)
    monkeypatch.setattr(sync, 'send_instance_undo', undo)

    summary = reconcile_sync()

    assert summary['dropped'] == 1 and fed['undo'] == [entries[2].actor_url]
    assert {r.entry_id for r in DiscoverySync.query} == {entries[0].id, entries[1].id}
    assert summary['failed_hosts'] == []


def test_a_community_mid_backfill_gets_no_poll(db_session, fed, monkeypatch, app):
    set_setting('discovery_sync_per_host', 2)
    chans('tube.example', 2)
    reconcile_sync()
    busy, idle = [r.community_id for r in DiscoverySync.query.order_by(DiscoverySync.community_id)]
    ran = []
    monkeypatch.setattr(app, 'debug', True)
    monkeypatch.setattr(sync, 'poll_synced_community', ran.append)
    monkeypatch.setattr(sync, 'backfill_in_progress', lambda community_id: community_id == busy)

    assert enqueue_polls() == 1
    assert ran == [idle]


def test_refollowed_counts_only_follows_that_were_sent(db_session, fed, monkeypatch):
    set_setting('discovery_sync_per_host', 1)
    chans('tube.example', 1)
    reconcile_sync()
    row = DiscoverySync.query.one()
    row.follow_state = SYNC_NONE
    db.session.commit()
    monkeypatch.setattr(sync, 'send_instance_follow', lambda row, community: False)

    assert reconcile_sync()['refollowed'] == 0

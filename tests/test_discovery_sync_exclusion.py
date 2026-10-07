"""Interop D24: a synced community an admin deletes is unfollowed and never re-added."""
import inspect
from unittest.mock import patch

import pytest

from app import db
from app.discovery import SYNC_ACCEPTED, sync
from app.discovery.sync import desired_entries, forget_synced_community, reconcile_sync
from app.admin.routes import unsubscribe_everyone_then_delete_task
from app.models import Community, DiscoveryExclusion, DiscoverySync, User
from app.utils import set_setting
from tests.discovery_fixtures import add_entry, fresh_cache  # noqa: F401
from tests.factories import make_community, make_instance, make_user
from tests.test_community_lifecycle import csrf, login, url

pytestmark = pytest.mark.usefixtures('site', 'fresh_cache')
TARGET = 'https://Tube.example/video-channels/Chan'


@pytest.fixture(autouse=True)
def founder(db_session):
    make_user(make_instance('test.piefed.local', software='piefed'), 'founder', local=True)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(sync, 'remote_object_to_json', lambda url: None)


@pytest.fixture
def undone(monkeypatch):
    sent = []
    monkeypatch.setattr(sync, 'send_instance_undo', lambda row, community: sent.append(row.follow_target))
    return sent


def remote_community(name='chan', host='tube.example'):
    community = make_community(name, host=host)
    community.ap_id = f'{name}@{host}'
    db.session.commit()
    return community


def synced(community, target=TARGET):
    db.session.add(DiscoverySync(community_id=community.id, follow_target=target, follow_state=SYNC_ACCEPTED))
    db.session.commit()


def excluded():
    return {row.actor_url for row in DiscoveryExclusion.query}


def test_a_synced_community_is_unfollowed_and_both_urls_are_excluded(undone):
    community = remote_community()
    synced(community)

    forget_synced_community(community)

    assert undone == [TARGET]
    assert excluded() == {community.ap_profile_id.lower(), TARGET.lower()}
    assert DiscoverySync.query.count() == 1   # the community delete cascades the row, not this helper


def test_forgetting_twice_records_each_url_once(undone):
    community = remote_community()
    synced(community)

    forget_synced_community(community)
    forget_synced_community(community)

    assert DiscoveryExclusion.query.count() == 2


def test_an_unsynced_remote_community_is_excluded_without_an_undo(undone):
    community = remote_community()

    forget_synced_community(community)

    assert undone == [] and excluded() == {community.ap_profile_id.lower()}


def test_a_local_community_records_nothing(undone):
    community = make_community('mine')
    community.ap_profile_id = None
    db.session.commit()

    forget_synced_community(community)

    assert undone == [] and DiscoveryExclusion.query.count() == 0


def test_an_excluded_entry_is_never_desired_so_never_followed(db_session, monkeypatch):
    set_setting('discovery_sync_per_host', 5)
    kept = add_entry('Keep', host='tube.example')
    gone = add_entry('Chan', host='tube.example')
    db.session.add(DiscoveryExclusion(actor_url=gone.actor_url.lower()))
    db.session.commit()
    follows = []
    monkeypatch.setattr(sync, 'send_instance_follow', lambda row, community: follows.append(row.follow_target))
    monkeypatch.setattr(sync, 'queue_backfill', lambda community_id: None)

    assert [e.id for e in desired_entries()['tube.example']] == [kept.id]
    monkeypatch.setattr(sync, 'find_actor_or_create', lambda url, community_only=False: make_resolved(url))
    reconcile_sync()
    assert follows == [kept.actor_url]


def make_resolved(url):
    community = make_community(url.rsplit('/', 1)[-1], host=url.split('/')[2])
    community.ap_profile_id = url.lower()
    db.session.commit()
    return community


def test_an_alias_entry_resolving_to_an_excluded_community_is_not_added(db_session, monkeypatch):
    set_setting('discovery_sync_per_host', 5)
    add_entry('Alias', host='tube.example')
    community = remote_community('real')
    db.session.add(DiscoveryExclusion(actor_url=community.ap_profile_id.lower()))
    db.session.commit()
    follows = []
    monkeypatch.setattr(sync, 'find_actor_or_create', lambda url, community_only=False: community)
    monkeypatch.setattr(sync, 'send_instance_follow', lambda row, c: follows.append(row))

    assert reconcile_sync()['added'] == 0
    assert follows == [] and DiscoverySync.query.count() == 0
    assert 'https://tube.example/video-channels/alias' in excluded()


def test_a_community_found_before_the_lookup_is_kept_when_its_url_is_excluded(db_session, monkeypatch):
    entry = add_entry('Real', host='tube.example')
    community = remote_community('real')
    community.ap_profile_id = entry.actor_url.lower()
    db.session.add(DiscoveryExclusion(actor_url=community.ap_profile_id))
    db.session.commit()

    assert sync._add(entry, {}) is False
    assert db.session.get(Community, community.id) is not None


def test_an_alias_whose_actor_id_is_excluded_is_refused_before_any_lookup(db_session, monkeypatch):
    set_setting('discovery_sync_per_host', 5)
    entry = add_entry('Alias', host='tube.example')
    db.session.add(DiscoveryExclusion(actor_url='https://tube.example/c/real'))
    db.session.commit()
    looked_up = []
    monkeypatch.setattr(sync, 'remote_object_to_json', lambda url: {'id': 'https://Tube.example/c/Real'})
    monkeypatch.setattr(sync, 'find_actor_or_create', lambda url, community_only=False: looked_up.append(url))

    assert reconcile_sync()['added'] == 0
    assert looked_up == [] and Community.query.filter_by(name='real').count() == 0
    assert entry.actor_url.lower() in excluded()
    reconcile_sync()   # the alias is excluded itself now: not even fetched
    assert looked_up == []


def test_an_existing_community_reached_through_an_alias_is_never_deleted(db_session, monkeypatch):
    set_setting('discovery_sync_per_host', 5)
    entry = add_entry('Alias', host='tube.example')
    community = remote_community('real')
    community.post_count = 3
    db.session.add(DiscoveryExclusion(actor_url=community.ap_profile_id.lower()))
    db.session.commit()
    community_id = community.id
    monkeypatch.setattr(sync, 'find_actor_or_create', lambda url, community_only=False: community)

    assert reconcile_sync()['added'] == 0
    kept = db.session.get(Community, community_id)
    assert kept is not None and kept.post_count == 3
    assert entry.actor_url.lower() in excluded() and DiscoverySync.query.count() == 0


def test_an_actor_document_without_an_id_falls_through_to_the_lookup(db_session, monkeypatch):
    entry = add_entry('Alias', host='tube.example')
    monkeypatch.setattr(sync, 'remote_object_to_json', lambda url: {'name': 'no id'})
    monkeypatch.setattr(sync, 'find_actor_or_create', lambda url, community_only=False: None)

    assert sync._add(entry, {}) is False
    assert DiscoveryExclusion.query.count() == 0


def test_a_local_community_with_a_server_url_profile_records_nothing(app, undone):
    community = make_community('mine')
    community.ap_profile_id = app.config['SERVER_URL'] + '/c/mine'
    db.session.commit()
    assert community.is_local() and community.ap_profile_id

    forget_synced_community(community)

    assert DiscoveryExclusion.query.count() == 0


def test_the_admin_click_excludes_both_urls_and_unfollows_before_the_task_runs(app, db_session, undone):
    from app.admin.routes import admin_community_delete
    community = remote_community()
    synced(community)

    with app.test_request_context(), patch('app.admin.routes.unsubscribe_everyone_then_delete') as queued, \
            patch('app.admin.routes.current_user'), patch('app.admin.routes.flash'):
        inspect.unwrap(admin_community_delete)(community.id)

    assert queued.called and undone == [TARGET]
    assert excluded() == {community.ap_profile_id.lower(), TARGET.lower()}


def spy(seen):
    """A forget_synced_community double that records what still exists when it is called."""
    def record(community):
        seen.append((db.session.get(Community, community.id) is not None,
                     db.session.get(DiscoverySync, community.id) is not None))
    return record


def test_the_admin_delete_task_forgets_the_community_before_the_row_is_gone(app, db_session):
    community = remote_community()
    synced(community)
    community_id, seen = community.id, []

    with patch('app.discovery.sync.forget_synced_community', side_effect=spy(seen)), \
            patch('app.admin.routes.unsubscribe_from_community'), patch('app.admin.routes.sleep'):
        unsubscribe_everyone_then_delete_task(community_id)

    assert seen == [(True, True)]
    db.session.expire_all()
    assert db.session.get(Community, community_id) is None


def test_the_admin_delete_task_leaves_the_exclusion_and_sends_the_undo(app, db_session, undone):
    community = remote_community()
    synced(community)
    community_id, profile = community.id, community.ap_profile_id.lower()

    with patch('app.admin.routes.unsubscribe_from_community'), patch('app.admin.routes.sleep'):
        unsubscribe_everyone_then_delete_task(community_id)

    db.session.expire_all()
    assert undone == [TARGET] and profile in excluded()
    assert DiscoverySync.query.count() == 0


def test_the_community_delete_route_forgets_the_community_before_the_row_is_gone(app, db_session):
    community = remote_community()
    synced(community)
    community_id, seen = community.id, []
    client = app.test_client()
    login(client, db.session.get(User, 1))

    with patch('app.discovery.sync.forget_synced_community', side_effect=spy(seen)):
        response = client.post(url(app, 'community.community_delete', community_id=community_id),
                               data={'submit': 'Delete', 'csrf_token': csrf(app, client)})

    assert response.status_code == 302 and seen == [(True, True)]
    assert db.session.get(Community, community_id) is None

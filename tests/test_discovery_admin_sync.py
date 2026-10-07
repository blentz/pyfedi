"""Interop D24 proactive sync: the admin Discovery tab sets the sync up and shows what is synced."""
import pytest

from app import db
from app.discovery import SYNC_ACCEPTED, SYNC_NONE, SYNC_PENDING, SYNC_REJECTED, admin_views, SYNC_FAILED
from app.models import DiscoverySync, utcnow
from app.utils import get_setting, set_setting
from tests.discovery_fixtures import admin, fresh_cache  # noqa: F401
from tests.factories import make_community, make_instance

pytestmark = pytest.mark.usefixtures('site', 'fresh_cache')
PAGE = '/admin/federation/discovery'


def test_the_form_shows_the_defaults(admin):
    client, _ = admin
    html = client.get(PAGE).get_data(as_text=True)
    assert 'name="sync_per_host"' in html and 'value="0"' in html
    assert 'name="sync_external_search"' in html
    assert 'preload_count' not in html and 'preload_subscribe' not in html


def test_saving_stores_the_settings(admin):
    client, token = admin
    response = client.post(PAGE, data={'csrf_token': token, 'sync_per_host': '5', 'sync_platforms': ['castopod'],
                                       'sync_save': 'Save'})
    assert response.status_code == 302
    assert get_setting('discovery_sync_per_host') == 5
    assert get_setting('discovery_sync_platforms') == ['castopod']
    assert get_setting('discovery_external_search') is False   # unchecked box


@pytest.mark.parametrize('value', ['-1', '51', 'many'])
def test_an_out_of_range_count_is_refused_and_nothing_stored(admin, value):
    client, token = admin
    response = client.post(PAGE, data={'csrf_token': token, 'sync_per_host': value, 'sync_save': 'Save'})
    assert response.status_code == 200
    assert get_setting('discovery_sync_per_host') is None


def test_sync_now_queues_the_reconcile(admin, monkeypatch, app):
    client, token = admin
    queued = []
    monkeypatch.setattr(app, 'debug', False)
    monkeypatch.setattr(admin_views.reconcile_sync_task, 'delay', lambda: queued.append(True))

    response = client.post(PAGE, data={'csrf_token': token, 'sync_now': 'Sync now'})

    assert response.status_code == 302 and queued == [True]


def test_sync_now_in_debug_runs_inline(admin, monkeypatch, app):
    client, token = admin
    ran = []
    monkeypatch.setattr(app, 'debug', True)
    monkeypatch.setattr(admin_views, 'reconcile_sync_task', lambda: ran.append(True))

    client.post(PAGE, data={'csrf_token': token, 'sync_now': 'Sync now'})

    assert ran == [True]


def test_sync_now_without_csrf_is_refused(admin, monkeypatch):
    client, _ = admin
    queued = []
    monkeypatch.setattr(admin_views.reconcile_sync_task, 'delay', lambda: queued.append(True))
    client.post(PAGE, data={'sync_now': 'Sync now'})
    assert queued == []


def test_the_status_table_lists_each_synced_community(admin):
    client, _ = admin
    instance = make_instance('tube.example', software='peertube')
    community = make_community('zqchan', host='tube.example')
    community.instance_id = instance.id
    db.session.add(DiscoverySync(community_id=community.id, follow_target='https://tube.example/video-channels/zqchan',
                                 follow_state=SYNC_ACCEPTED, last_polled_at=utcnow(), last_error='outbox unreadable'))
    db.session.commit()

    html = client.get(PAGE).get_data(as_text=True)

    assert 'tube.example' in html and 'Following' in html and 'outbox unreadable' in html


def test_an_empty_status_table_says_so(admin):
    client, _ = admin
    assert 'Nothing is synced yet.' in client.get(PAGE).get_data(as_text=True)


STATE_LABELS = {SYNC_NONE: 'Not followed yet', SYNC_PENDING: 'Waiting for an answer', SYNC_ACCEPTED: 'Following',
                SYNC_REJECTED: 'Refused', SYNC_FAILED: 'Could not deliver'}


@pytest.mark.parametrize('state', STATE_LABELS)
def test_each_follow_state_reads_as_its_label_not_its_raw_value(admin, state):
    client, _ = admin
    community = make_community('statechan', host='tube.example')
    db.session.add(DiscoverySync(community_id=community.id, follow_target='https://tube.example/video-channels/s',
                                 follow_state=state))
    db.session.commit()

    html = client.get(PAGE).get_data(as_text=True)

    assert f'<td>{STATE_LABELS[state]}</td>' in html
    assert f'<td>{state}</td>' not in html


def test_the_community_title_links_to_its_page(admin):
    client, _ = admin
    community = make_community('linkchan', host='tube.example')
    community.ap_id = 'linkchan@tube.example'
    db.session.add(DiscoverySync(community_id=community.id, follow_target='https://tube.example/video-channels/l'))
    db.session.commit()

    html = client.get(PAGE).get_data(as_text=True)

    assert '<a href="/c/linkchan@tube.example">linkchan</a>' in html

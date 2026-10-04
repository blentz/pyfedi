"""Interop D24, goal (a): an admin pre-loads PeerTube channels and Castopod podcasts the way the
lemmyverse pre-load does for Lemmy communities: preview, then subscribe through the join path."""
from types import SimpleNamespace

import pytest

from app import db
from app.discovery import preload
from app.discovery.preload import preload_candidates, preload_discovered_communities
from app.models import Community, DiscoveryEntry
from tests.discovery_fixtures import add_entry, admin, fresh_cache  # noqa: F401
from tests.factories import make_banned_instance, make_community, make_instance, make_user

pytestmark = pytest.mark.usefixtures('site', 'fresh_cache')

PAGE = '/admin/federation/discovery'


@pytest.fixture
def world(app, db_session):
    instance = make_instance('world.example', software='piefed')
    founder = make_user(instance, 'worldfounder', local=True)
    add_entry('Bigchan', followers=900)
    add_entry('Midchan', followers=500)
    add_entry('Tinychan', followers=10)
    add_entry('Zqpodshow', followers=700, platform='castopod', url='https://pod.example/@pod', host='pod.example')
    add_entry('Ann', followers=999, platform='mastodon', kind='person', url='https://m.example/users/ann',
              host='m.example')
    add_entry('Spicy', followers=950, nsfw=True)
    known = add_entry('Known', followers=800, url='https://known.example/video-channels/Known')
    make_community('known', host='known.example')
    community = Community.query.filter_by(name='known').one()
    community.ap_profile_id = 'https://known.example/video-channels/known'   # stored lower-case, as ingest stores it
    db.session.commit()
    return SimpleNamespace(founder=founder, known=known)


def test_candidates_honour_n_and_the_platform_filter(world):
    assert [e.name for e in preload_candidates(2, ['peertube'])] == ['Bigchan', 'Midchan']
    assert [e.name for e in preload_candidates(10, ['castopod'])] == ['Zqpodshow']
    assert [e.name for e in preload_candidates(10, ['peertube', 'castopod'])] == ['Bigchan', 'Zqpodshow', 'Midchan', 'Tinychan']


def test_candidates_never_include_people_nsfw_known_or_unknown_platforms(world):
    names = [e.name for e in preload_candidates(50, ['peertube', 'castopod', 'mastodon'])]

    assert 'Ann' not in names and 'Spicy' not in names
    assert 'Known' not in names   # known case-insensitively: the entry says /Known, the community /known


def test_candidates_skip_a_host_banned_after_the_refresh(world):
    make_banned_instance('bigchan.example')

    assert 'Bigchan' not in [e.name for e in preload_candidates(10, ['peertube'])]


def test_subscribe_joins_each_new_community_once_and_skips_known_ones(world, monkeypatch):
    resolved, joined = [], []

    def resolve(actor_url, community_only=False):
        resolved.append((actor_url, community_only))
        name = actor_url.rstrip('/').rsplit('/', 1)[-1].lower()
        community = make_community(name, host=f'{name}.example')
        community.ap_id = f'{name}@{name}.example'
        db.session.commit()
        return community

    monkeypatch.setattr(preload, 'find_actor_or_create', resolve)
    monkeypatch.setattr(preload, 'do_subscribe', lambda actor, user_id, admin_preload=False:
                        joined.append((actor, user_id, admin_preload)) or {'community': actor, 'status': 'joined'})
    big = DiscoveryEntry.query.filter_by(name='Bigchan').one()

    results = preload_discovered_communities([big.id, world.known.id, 999999], world.founder.id)

    assert resolved == [(big.actor_url, True)]
    assert joined == [('bigchan@bigchan.example', world.founder.id, True)]
    assert [r['status'] for r in results] == ['joined', 'already known', 'gone']


def test_an_actor_that_does_not_resolve_to_a_community_is_reported(world, monkeypatch):
    monkeypatch.setattr(preload, 'find_actor_or_create', lambda actor_url, community_only=False: None)
    monkeypatch.setattr(preload, 'do_subscribe', lambda *a, **k: pytest.fail('nothing to join'))
    small = DiscoveryEntry.query.filter_by(name='Tinychan').one()

    assert preload_discovered_communities([small.id], world.founder.id) == [{'entry': small.id, 'status': 'not found'}]


def test_the_preview_lists_the_candidates_and_subscribes_nobody(world, admin, monkeypatch):
    client, token = admin
    monkeypatch.setattr('app.discovery.admin_views.preload_discovered_communities',
                        SimpleNamespace(delay=lambda *a: pytest.fail('a preview must not subscribe')))

    page = client.post(PAGE, data={'preload_count': '2', 'preload_platforms': ['peertube'],
                                   'preload_preview': 'go', 'csrf_token': token}).get_data(as_text=True)

    assert '<td>Bigchan</td>' in page and '<td>Midchan</td>' in page
    assert 'Tinychan' not in page and 'Zqpodshow' not in page


def test_subscribe_hands_the_candidate_ids_to_the_celery_task(world, admin, monkeypatch):
    client, token = admin
    queued = []
    monkeypatch.setattr('app.discovery.admin_views.preload_discovered_communities',
                        SimpleNamespace(delay=lambda entry_ids, user_id: queued.append((entry_ids, user_id))))

    response = client.post(PAGE, data={'preload_count': '2', 'preload_platforms': ['peertube', 'castopod'],
                                       'preload_subscribe': 'go', 'csrf_token': token})

    big = DiscoveryEntry.query.filter_by(name='Bigchan').one()
    pod = DiscoveryEntry.query.filter_by(name='Zqpodshow').one()
    assert response.status_code == 302
    assert queued == [([big.id, pod.id], preload.PRELOAD_USER_ID)]


@pytest.mark.parametrize('count', ['0', '201'])
def test_a_count_outside_one_to_two_hundred_is_refused(world, admin, monkeypatch, count):
    client, token = admin
    queued = []
    monkeypatch.setattr('app.discovery.admin_views.preload_discovered_communities',
                        SimpleNamespace(delay=lambda *a: queued.append(a)))

    response = client.post(PAGE, data={'preload_count': count, 'preload_platforms': ['peertube'],
                                       'preload_subscribe': 'go', 'csrf_token': token})

    assert response.status_code == 200          # re-rendered with the form error, not a redirect
    assert 'Number must be between 1 and 200' in response.get_data(as_text=True)
    assert queued == []


def test_the_task_refuses_a_person_entry_passed_directly(world, monkeypatch):
    monkeypatch.setattr(preload, 'find_actor_or_create', lambda *a, **k: pytest.fail('must not resolve a person'))
    monkeypatch.setattr(preload, 'do_subscribe', lambda *a, **k: pytest.fail('must not subscribe'))
    ann = DiscoveryEntry.query.filter_by(name='Ann').one()

    assert preload_discovered_communities([ann.id], world.founder.id) == [{'entry': ann.id, 'status': 'gone'}]


def test_the_task_skips_a_host_banned_after_enqueue(world, monkeypatch):
    monkeypatch.setattr(preload, 'find_actor_or_create', lambda *a, **k: pytest.fail('banned host'))
    monkeypatch.setattr(preload, 'do_subscribe', lambda *a, **k: pytest.fail('banned host'))
    big = DiscoveryEntry.query.filter_by(name='Bigchan').one()
    make_banned_instance('bigchan.example')

    assert preload_discovered_communities([big.id], world.founder.id) == [{'entry': big.id, 'status': 'skipped'}]


def test_the_task_skips_an_entry_flagged_nsfw_after_enqueue(world, monkeypatch):
    monkeypatch.setattr(preload, 'find_actor_or_create', lambda *a, **k: pytest.fail('nsfw'))
    monkeypatch.setattr(preload, 'do_subscribe', lambda *a, **k: pytest.fail('nsfw'))
    mid = DiscoveryEntry.query.filter_by(name='Midchan').one()
    mid.nsfw = True
    db.session.commit()

    assert preload_discovered_communities([mid.id], world.founder.id) == [{'entry': mid.id, 'status': 'skipped'}]


def test_one_entry_that_blows_up_is_reported_and_the_rest_still_run(world, monkeypatch):
    big = DiscoveryEntry.query.filter_by(name='Bigchan').one()
    mid = DiscoveryEntry.query.filter_by(name='Midchan').one()

    def resolve(actor_url, community_only=False):
        if actor_url == big.actor_url:
            raise RuntimeError('peer answered garbage')
        community = make_community('midchan', host='midchan.example')
        community.ap_id = 'midchan@midchan.example'
        db.session.commit()
        return community

    monkeypatch.setattr(preload, 'find_actor_or_create', resolve)
    monkeypatch.setattr(preload, 'do_subscribe', lambda actor, user_id, admin_preload=False:
                        {'community': actor, 'status': 'joined'})

    results = preload_discovered_communities([big.id, mid.id], world.founder.id)

    assert results == [{'entry': big.id, 'status': 'error'}, {'community': 'midchan@midchan.example', 'status': 'joined'}]


def test_subscribing_with_nothing_new_enqueues_nothing_and_says_so(world, admin, monkeypatch):
    client, token = admin
    queued = []
    monkeypatch.setattr('app.discovery.admin_views.preload_discovered_communities',
                        SimpleNamespace(delay=lambda *a: queued.append(a)))
    pod = DiscoveryEntry.query.filter_by(name='Zqpodshow').one()
    pod.nsfw = True
    db.session.commit()

    page = client.post(PAGE, data={'preload_count': '5', 'preload_platforms': ['castopod'],
                                   'preload_subscribe': 'go', 'csrf_token': token},
                       follow_redirects=True).get_data(as_text=True)

    assert queued == []
    assert 'Nothing new to subscribe to' in page


@pytest.mark.parametrize('flag', ['deleted', 'banned'])
def test_subscribe_is_refused_when_the_preload_user_cannot_subscribe(world, admin, monkeypatch, flag):
    client, token = admin
    queued = []
    monkeypatch.setattr('app.discovery.admin_views.preload_discovered_communities',
                        SimpleNamespace(delay=lambda *a: queued.append(a)))
    assert world.founder.id == preload.PRELOAD_USER_ID
    setattr(world.founder, flag, True)
    db.session.commit()

    page = client.post(PAGE, data={'preload_count': '2', 'preload_platforms': ['peertube'],
                                   'preload_subscribe': 'go', 'csrf_token': token},
                       follow_redirects=True).get_data(as_text=True)

    assert queued == []
    assert 'cannot subscribe' in page


@pytest.mark.parametrize('flag', ['deleted', 'banned', 'missing'])
def test_the_task_subscribes_nothing_for_a_user_that_cannot_subscribe(world, monkeypatch, caplog, flag):
    monkeypatch.setattr(preload, 'find_actor_or_create', lambda *a, **k: pytest.fail('must not resolve'))
    monkeypatch.setattr(preload, 'do_subscribe', lambda *a, **k: pytest.fail('must not subscribe'))
    big = DiscoveryEntry.query.filter_by(name='Bigchan').one()
    user_id = world.founder.id
    if flag == 'missing':
        user_id = 999999
    else:
        setattr(world.founder, flag, True)
        db.session.commit()

    assert preload_discovered_communities([big.id], user_id) == []
    assert 'cannot subscribe' in caplog.text

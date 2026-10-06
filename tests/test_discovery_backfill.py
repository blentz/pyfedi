"""Interop D24: a community that discovery creates ("Open to join", the admin pre-load) is backfilled like one
added by name, so it does not open empty."""
from types import SimpleNamespace

import pytest
from flask import g

from app import db
from app.discovery import backfill, preload, views
from app.discovery.preload import preload_discovered_communities
from app.models import Site
from tests.discovery_fixtures import add_entry, fresh_cache  # noqa: F401
from tests.factories import make_community, make_instance, make_user
from tests.test_admin_federation import csrf, login

pytestmark = pytest.mark.usefixtures('fresh_cache')


@pytest.fixture
def queued(monkeypatch):
    calls = []
    monkeypatch.setattr(views, 'queue_backfill', calls.append)
    monkeypatch.setattr(preload, 'queue_backfill', calls.append)
    return calls


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    user = api_baseline.user1
    user.verified = True
    db.session.commit()
    client = app.test_client()
    login(client, user)
    return SimpleNamespace(client=client, user=user, token=csrf(app, client))


def channel_entry(name='zqbackfill'):
    return add_entry(name.title(), platform='peertube', kind='community', host='tube.example',
                     url=f'https://tube.example/video-channels/{name}')


def creating(name='zqbackfill'):
    """A find_actor_or_create double that creates the channel's community, as a first fetch does."""
    def resolve(actor_url, community_only=False):
        community = make_community(name, host='tube.example')
        community.ap_profile_id = actor_url.lower()
        community.ap_id = f'{name}@tube.example'
        db.session.commit()
        resolve.community = community
        return community
    return resolve


def test_opening_an_entry_that_creates_its_community_queues_one_backfill(env, queued, monkeypatch):
    entry = channel_entry()
    resolve = creating()
    monkeypatch.setattr(views, 'find_actor_or_create', resolve)

    env.client.post(f'/discovery/{entry.id}/resolve', data={'csrf_token': env.token})

    assert queued == [resolve.community.id]


def test_opening_an_entry_whose_community_is_known_queues_nothing(env, queued, monkeypatch):
    entry = channel_entry()
    known = make_community('zqbackfill', host='tube.example')
    known.ap_profile_id = entry.actor_url.lower()
    known.ap_id = 'zqbackfill@tube.example'
    db.session.commit()
    monkeypatch.setattr(views, 'find_actor_or_create', lambda actor_url, community_only=False: known)

    env.client.post(f'/discovery/{entry.id}/resolve', data={'csrf_token': env.token})

    assert queued == []


def test_opening_a_person_queues_nothing(env, queued, monkeypatch):
    entry = add_entry('Zqperson', platform='mastodon', kind='person', host='m.example',
                      url='https://m.example/users/zqperson')
    person = make_user(make_instance('m.example', software='mastodon'), 'zqperson')
    monkeypatch.setattr(views, 'find_actor_or_create', lambda actor_url, community_only=False: person)

    env.client.post(f'/discovery/{entry.id}/resolve', data={'csrf_token': env.token})

    assert queued == []


def test_the_preload_backfills_each_community_it_creates(app, db_session, queued, monkeypatch):
    founder = make_user(make_instance('world.example', software='piefed'), 'zqfounder', local=True)
    entry = channel_entry()
    resolve = creating()
    monkeypatch.setattr(preload, 'find_actor_or_create', resolve)
    monkeypatch.setattr(preload, 'do_subscribe', lambda actor, user_id, admin_preload=False:
                        {'community': actor, 'status': 'joined'})
    monkeypatch.setattr(preload, 'preload_user_can_subscribe', lambda user_id: True)

    preload_discovered_communities([entry.id], founder.id)

    assert queued == [resolve.community.id]


def test_the_backfill_task_reads_the_actor_and_runs_the_community_backfill(app, db_session, site, monkeypatch):
    make_user(make_instance('world.example', software='piefed'), 'zqfounder', local=True)   # community.user_id 1
    community = make_community('zqbackfill', host='tube.example')
    community.ap_profile_id = 'https://tube.example/video-channels/zqbackfill'
    db.session.commit()
    actor = {'id': community.ap_profile_id, 'type': 'Group', 'attributedTo': [{'type': 'Person', 'id': 'x'}]}
    fetched, ran = [], []
    monkeypatch.setattr(backfill, 'remote_object_to_json', lambda uri: fetched.append(uri) or actor)
    monkeypatch.setattr(backfill, 'retrieve_mods_and_backfill',
                        lambda community_id, server, name, community_json=None, stop_at_known=False:
                        ran.append((community_id, server, name, community_json)))

    backfill.backfill_discovered_community(community.id)

    assert fetched == [community.ap_profile_id]
    assert ran == [(community.id, 'tube.example', 'zqbackfill', actor)]


def test_the_backfill_task_does_nothing_for_a_community_that_is_gone(app, db_session, monkeypatch):
    monkeypatch.setattr(backfill, 'remote_object_to_json', lambda uri: pytest.fail('nothing to fetch'))
    monkeypatch.setattr(backfill, 'retrieve_mods_and_backfill', lambda *a, **k: pytest.fail('nothing to backfill'))

    backfill.backfill_discovered_community(987654)


@pytest.fixture
def real_cache(app, monkeypatch):
    """The tests run on NullCache; the in-progress flag needs a cache that keeps what it is given."""
    from cachelib import SimpleCache
    from app import cache
    monkeypatch.setitem(app.extensions['cache'], cache, SimpleCache())


def test_a_queued_backfill_is_in_progress_until_the_task_ends(app, db_session, site, real_cache, monkeypatch):
    make_user(make_instance('world.example', software='piefed'), 'zqfounder', local=True)
    community = make_community('zqbackfill', host='tube.example')
    db.session.commit()
    monkeypatch.setattr(backfill.backfill_discovered_community, 'delay', lambda community_id: None)
    monkeypatch.setattr(backfill, 'remote_object_to_json', lambda uri: None)
    monkeypatch.setattr(backfill, 'retrieve_mods_and_backfill', lambda *a, **k: None)

    backfill.queue_backfill(community.id)
    assert backfill.backfill_in_progress(community.id)

    backfill.backfill_discovered_community(community.id)
    assert not backfill.backfill_in_progress(community.id)


def test_a_failing_backfill_still_ends_the_in_progress_state(app, db_session, site, real_cache, monkeypatch):
    make_user(make_instance('world.example', software='piefed'), 'zqfounder', local=True)
    community = make_community('zqbackfill', host='tube.example')
    db.session.commit()
    monkeypatch.setattr(backfill.backfill_discovered_community, 'delay', lambda community_id: None)
    monkeypatch.setattr(backfill, 'remote_object_to_json', lambda uri: None)

    def boom(*a, **k):
        raise RuntimeError('peer answered garbage')
    monkeypatch.setattr(backfill, 'retrieve_mods_and_backfill', boom)

    backfill.queue_backfill(community.id)
    with pytest.raises(RuntimeError):
        backfill.backfill_discovered_community(community.id)
    assert not backfill.backfill_in_progress(community.id)


def test_an_empty_community_being_backfilled_says_its_posts_are_on_the_way(env, real_cache, monkeypatch):
    community = make_community('zqfilling', host='tube.example')
    community.ap_id = 'zqfilling@tube.example'
    db.session.commit()
    monkeypatch.setattr(backfill.backfill_discovered_community, 'delay', lambda community_id: None)
    backfill.queue_backfill(community.id)

    html = env.client.get(f'/c/{community.link()}').get_data(as_text=True)

    assert 'Fetching recent posts from tube.example' in html


def test_an_empty_community_not_being_backfilled_shows_no_hint(env, real_cache):
    community = make_community('zqquiet', host='tube.example')
    community.ap_id = 'zqquiet@tube.example'
    db.session.commit()

    html = env.client.get(f'/c/{community.link()}').get_data(as_text=True)

    assert 'Fetching recent posts' not in html


def test_the_hint_goes_once_the_community_has_posts(env, real_cache, monkeypatch):
    from tests.factories import make_post
    community = make_community('zqfilled', host='tube.example')
    community.ap_id = 'zqfilled@tube.example'
    db.session.commit()
    make_post(community, make_user(make_instance('tube.example', software='peertube'), 'zqauthor'), 'https://tube.example/videos/watch/zq1', title='Zqvideo')
    monkeypatch.setattr(backfill.backfill_discovered_community, 'delay', lambda community_id: None)
    backfill.queue_backfill(community.id)

    html = env.client.get(f'/c/{community.link()}').get_data(as_text=True)

    assert 'Zqvideo' in html
    assert 'Fetching recent posts' not in html

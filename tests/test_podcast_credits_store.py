"""Interop D24, decisions 6 and 7: credits are fetched from the podcast's feed after an episode arrives,
fediverse hrefs are linked to their PieFed User, and the result lives in post.extensions['podcast']."""
import gzip
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import cache, db
from app.utils import get_request_capped
from app.activitypub.util import actor_json_to_model, create_post
from app.discovery import credits
from app.discovery.credits import MAX_FEED_BYTES, fetch_episode_credits_task, resolve_credit_user, store_credits
from app.discovery.podcast import podcast_community_for
from app.models import Post, User
from tests.factories import make_banned_instance, make_post, make_site, peer_actor_json, peer_instance

PEER = 'pod.example'
ACTOR = f'https://{PEER}/@mypodcast'
FEED_URL = f'https://{PEER}/@mypodcast/feed.xml'
EP1 = f'https://{PEER}/@mypodcast/episodes/ep-1'
FIXTURE = Path(__file__).resolve().parent.parent / 'app' / 'discovery' / 'fixtures' / 'castopod_feed.xml'
PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'

pytestmark = pytest.mark.usefixtures('no_real_sleeping')


@pytest.fixture(autouse=True)
def fresh_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def world(app, db_session):
    make_site()
    peer_instance(PEER)
    podcast = actor_json_to_model(peer_actor_json('Person', name='mypodcast', server=PEER,
                                                  fields={'id': ACTOR, 'type': 'Podcast', 'name': 'My Podcast',
                                                          'rssFeed': FEED_URL}), 'mypodcast', PEER)
    community = podcast_community_for(podcast)
    post = make_post(community, podcast, f'{ACTOR}/posts/1', microblog=True)
    return SimpleNamespace(podcast=podcast, community=community, post=post)


def stored(post_id):
    db.session.expire_all()
    return db.session.get(Post, post_id).extensions


def test_a_fediverse_href_resolves_to_its_user(app, db_session, http_mock):
    peer_instance('social.example')
    http_mock.get('https://social.example/@ann').respond(json=peer_actor_json('Person', name='ann',
                                                                               server='social.example'))

    user_id = resolve_credit_user('https://social.example/@ann')

    assert db.session.get(User, user_id).ap_profile_id == 'https://social.example/u/ann'


def test_an_ordinary_web_page_resolves_to_nobody(app, db_session, http_mock):
    http_mock.get('https://ben.example/about').respond(200, text='<html>Ben</html>')

    assert resolve_credit_user('https://ben.example/about') is None


def test_plain_http_and_banned_instances_are_never_fetched(app, db_session, http_mock):
    make_banned_instance('banned.example')

    assert resolve_credit_user('http://social.example/@ann') is None
    assert resolve_credit_user('https://banned.example/@ann') is None
    assert resolve_credit_user(None) is None


def test_the_task_stores_hosts_and_guests_with_their_users(world, http_mock, monkeypatch):
    http_mock.get(FEED_URL).respond(200, content=FIXTURE.read_bytes(), headers={'Content-Type': 'application/rss+xml'})
    monkeypatch.setattr(credits, 'resolve_credit_user', {'https://social.example/@ann': 77}.get)

    fetch_episode_credits_task(world.post.id, EP1)

    saved = stored(world.post.id)['podcast']['credits']
    assert [(c['name'], c['role'], c['user_id']) for c in saved] == [
        ('Ann Host', 'host', 77), ('Ben Cohost', 'host', None), ('Cara Guest', 'guest', None)]


@pytest.mark.parametrize('answer', [dict(status_code=500), dict(status_code=200, text='<html>not a feed</html>'),
                                    dict(status_code=200, content=b'<rss>' + b' ' * MAX_FEED_BYTES + b'</rss>')])
def test_a_broken_or_oversized_feed_leaves_no_credits(world, http_mock, answer):
    http_mock.get(FEED_URL).respond(**answer)

    fetch_episode_credits_task(world.post.id, EP1)

    assert stored(world.post.id) is None


def test_a_podcast_without_a_feed_fetches_nothing(world, http_mock):
    world.community.rss_url = None
    db.session.commit()

    fetch_episode_credits_task(world.post.id, EP1)

    assert stored(world.post.id) is None


def test_storing_keeps_other_extensions(world):
    world.post.extensions = {'other': 1}
    db.session.commit()

    store_credits(world.post, [{'name': 'Ann Host', 'role': 'host', 'image': None, 'profile_url': None,
                                'user_id': None}])

    assert stored(world.post.id) == {'other': 1, 'podcast': {'credits': [
        {'name': 'Ann Host', 'role': 'host', 'image': None, 'profile_url': None, 'user_id': None}]}}


def test_an_ingested_episode_announcement_gets_its_credits(world, http_mock, monkeypatch):
    episode = http_mock.get(EP1).respond(404)   # no audio this time; the credits do not depend on it
    feed = http_mock.get(FEED_URL).respond(200, content=FIXTURE.read_bytes())
    monkeypatch.setattr(credits, 'resolve_credit_user', lambda profile_url: None)
    note_id = f'{ACTOR}/posts/2'
    activity = {'id': f'{note_id}/activity', 'type': 'Create', 'to': [PUBLIC], 'cc': [],
                'object': {'id': note_id, 'type': 'Note', 'attributedTo': ACTOR, 'to': [PUBLIC], 'cc': [],
                           'content': f'<a href="{EP1}">Episode 1: Hello</a><br/><p>New episode is out!</p>'}}

    post = create_post(False, world.community, activity, world.podcast)

    assert episode.called and feed.called
    assert [c['name'] for c in stored(post.id)['podcast']['credits']] == ['Ann Host', 'Ben Cohost', 'Cara Guest']


def test_a_lookup_that_blows_up_costs_only_that_credit_its_link(world, http_mock, monkeypatch):
    http_mock.get(FEED_URL).respond(200, content=FIXTURE.read_bytes())

    def lookup(profile_url, *args, **kwargs):
        if profile_url == 'https://social.example/@ann':
            raise KeyError('malformed actor')
        return SimpleNamespace(id=5, banned=False)

    monkeypatch.setattr('app.activitypub.util.find_actor_or_create', lookup)
    monkeypatch.setattr(credits, 'User', SimpleNamespace)   # the stub actor stands in for a User

    fetch_episode_credits_task(world.post.id, EP1)

    saved = stored(world.post.id)['podcast']['credits']
    assert [(c['name'], c['user_id']) for c in saved] == [('Ann Host', None), ('Ben Cohost', 5), ('Cara Guest', 5)]


def test_a_feed_without_a_content_length_is_not_read_past_the_cap(world, http_mock):
    chunk = b' ' * 65536
    pulled = []

    def body():
        yield b'<rss>'
        for _ in range(200):   # 12.5 MB on offer
            pulled.append(1)
            yield chunk

    http_mock.get(FEED_URL).respond(200, content=body())

    fetch_episode_credits_task(world.post.id, EP1)

    assert stored(world.post.id) is None
    assert len(pulled) * len(chunk) <= MAX_FEED_BYTES + 2 * len(chunk)


def test_a_feed_trickled_past_the_deadline_is_abandoned(app, http_mock, monkeypatch):
    now = [1000.0]
    real_time = __import__('time')
    monkeypatch.setattr('app.utils.time', SimpleNamespace(monotonic=lambda: now[0],
                                                          **{n: getattr(real_time, n) for n in dir(real_time)
                                                             if n != 'monotonic'}))
    pulled = []

    def trickle():
        for _ in range(1000):
            pulled.append(1)
            now[0] += 5   # five fake seconds per byte
            yield b'x'

    http_mock.get(FEED_URL).respond(200, content=trickle())

    assert get_request_capped(FEED_URL, MAX_FEED_BYTES, max_seconds=15) == (200, None)
    assert len(pulled) <= 5


def test_a_compressed_feed_is_refused_undecoded(app, http_mock):
    route = http_mock.get(FEED_URL).respond(200, content=gzip.compress(b'<rss></rss>'),
                                            headers={'Content-Encoding': 'gzip'})

    assert get_request_capped(FEED_URL, MAX_FEED_BYTES) == (200, None)
    assert route.calls.last.request.headers['Accept-Encoding'] == 'identity'

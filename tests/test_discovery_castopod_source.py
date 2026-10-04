"""Interop D24: Castopod podcasts found through the Podcast Index API, signed with the admin's key."""
import json
import logging
from pathlib import Path

import httpx
import pytest

from app import cache
from app.discovery import castopod, sources
from app.discovery.castopod import PODCASTINDEX_API, fetch_castopod_podcasts, podcastindex_headers
from app.utils import set_setting
from tests.discovery_fixtures import nobody_excluded

FIXTURE = Path(__file__).resolve().parent.parent / 'app' / 'discovery' / 'fixtures' / 'castopod_podcastindex.json'
VALUE = f'{PODCASTINDEX_API}/podcasts/bytag'
NEW = f'{PODCASTINDEX_API}/recent/newfeeds'
SEARCH = f'{PODCASTINDEX_API}/search/byterm'
EPISODES = f'{PODCASTINDEX_API}/episodes/byfeedid'
NOW = 1700000000
# sha1('KEYabcSECRETxyz1700000000'), computed once outside the implementation and pasted in.
EXPECTED_AUTHORIZATION = '3e78f375af4c08655c786763a7e4b65d8ba42893'

pytestmark = pytest.mark.usefixtures('no_real_sleeping')


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(castopod, 'now_unix', lambda: NOW)


@pytest.fixture
def credentials(app, db_session):
    cache.clear()  # get_setting is memoized
    set_setting(castopod.SETTING_KEY, 'KEYabc')
    set_setting(castopod.SETTING_SECRET, 'SECRETxyz')
    yield
    cache.clear()


def serve_lists(http_mock, value=None, new=None, search=None):
    """Each list answers with the given feeds; an omitted list answers empty."""
    return tuple(http_mock.get(url).respond(json={'status': 'true', 'feeds': feeds or []})
                 for url, feeds in ((VALUE, value), (NEW, new), (SEARCH, search)))


def serve_fixture(http_mock):
    fixture = json.loads(FIXTURE.read_text())
    lists = tuple(http_mock.get(url).respond(json=fixture['lists'][name])
                  for url, name in ((VALUE, 'value'), (NEW, 'new'), (SEARCH, 'search')))
    episodes = {feed_id: http_mock.get(EPISODES, params={'id': feed_id}).respond(json=answer)
                for feed_id, answer in fixture['episodes'].items()}
    return lists, episodes


def test_without_credentials_nothing_is_fetched(app, db_session, http_mock):
    cache.clear()
    assert fetch_castopod_podcasts(nobody_excluded) == []


def test_the_signature_is_sha1_of_key_secret_and_time():
    headers = podcastindex_headers('KEYabc', 'SECRETxyz', NOW)

    assert headers == {'X-Auth-Key': 'KEYabc', 'X-Auth-Date': '1700000000',
                       'Authorization': EXPECTED_AUTHORIZATION}


def test_every_request_is_signed(credentials, http_mock):
    (value, new, search), episodes = serve_fixture(http_mock)

    fetch_castopod_podcasts(nobody_excluded)

    for route in (value, new, search, episodes['101']):
        request = route.calls.last.request
        assert request.headers['X-Auth-Key'] == 'KEYabc'
        assert request.headers['X-Auth-Date'] == str(NOW)
        assert request.headers['Authorization'] == EXPECTED_AUTHORIZATION
        assert request.headers['User-Agent'].startswith('PieFed/')


def test_the_three_lists_that_surface_castopod_are_read(credentials, http_mock):
    (value, new, search), _episodes = serve_fixture(http_mock)

    fetch_castopod_podcasts(nobody_excluded)

    assert dict(value.calls.last.request.url.params) == {'podcast-value': '', 'max': '1000'}
    assert dict(new.calls.last.request.url.params) == {'max': '1000'}
    assert dict(search.calls.last.request.url.params) == {'q': 'castopod', 'max': '1000'}


def test_only_podcasts_with_an_activitypub_social_interact_are_kept(credentials, http_mock):
    serve_fixture(http_mock)

    entries = fetch_castopod_podcasts(nobody_excluded)

    assert entries == [{'kind': 'community', 'platform': 'castopod', 'actor_url': 'https://pod.example/@mypodcast',
                        'name': 'My Podcast', 'host': 'pod.example', 'avatar': 'https://pod.example/media/cover.jpg',
                        'followers': 9, 'nsfw': False, 'source': 'podcastindex'},
                       {'kind': 'community', 'platform': 'castopod', 'actor_url': 'https://radio.example/@morning',
                        'name': 'Morning Show', 'host': 'radio.example',
                        'avatar': 'https://radio.example/media/morning.jpg',
                        'followers': 0, 'nsfw': False, 'source': 'podcastindex'}]


def test_a_feed_on_two_lists_is_looked_up_once(credentials, http_mock):
    _lists, episodes = serve_fixture(http_mock)

    fetch_castopod_podcasts(nobody_excluded)

    assert episodes['101'].call_count == 1


def test_one_failing_list_does_not_abort_the_source(credentials, http_mock):
    fixture = json.loads(FIXTURE.read_text())
    http_mock.get(VALUE).mock(side_effect=httpx.ConnectError('refused'))
    http_mock.get(NEW).respond(json=fixture['lists']['new'])
    http_mock.get(SEARCH).respond(500)
    for feed_id in ('103', '104'):   # 101 is listed only by the failing lists
        http_mock.get(EPISODES, params={'id': feed_id}).respond(json=fixture['episodes'][feed_id])

    entries = fetch_castopod_podcasts(nobody_excluded)

    assert [entry['actor_url'] for entry in entries] == ['https://radio.example/@morning']


def test_an_excluded_podcast_host_is_dropped(credentials, http_mock):
    serve_fixture(http_mock)

    entries = fetch_castopod_podcasts(lambda host: host == 'pod.example')

    assert [entry['actor_url'] for entry in entries] == ['https://radio.example/@morning']


def test_episode_lookups_are_capped(credentials, http_mock):
    feeds = [{'id': i, 'url': f'https://c{i}.example/@show/feed.xml', 'title': f'Show {i}'} for i in range(50)]
    serve_lists(http_mock, value=feeds)
    route = http_mock.get(EPISODES).respond(json={'items': []})

    fetch_castopod_podcasts(nobody_excluded)

    assert route.call_count == castopod.MAX_EPISODE_LOOKUPS == 40


def test_malformed_rows_are_skipped_not_fatal(credentials, http_mock):
    good = json.loads(FIXTURE.read_text())['episodes']['101']
    feeds = [None, 'text', {'id': 'x', 'url': 'https://a.example/@a/feed.xml'},
             {'id': 7, 'url': 5}, {'id': 8, 'url': 'https://b.example/@b/feed.xml', 'title': 'B'},
             {'id': 101, 'url': 'https://pod.example/@mypodcast/feed.xml', 'title': 'My Podcast',
              'trendScore': 'many', 'artwork': 12}]
    serve_lists(http_mock, value=feeds)
    http_mock.get(EPISODES, params={'id': '8'}).respond(json={'items': [None, 'x', {'socialInteract': 'no'},
                                                                         {'socialInteract': [None, 3, {
                                                                             'protocol': 'activitypub',
                                                                             'accountUrl': 7}]}]})
    http_mock.get(EPISODES, params={'id': '101'}).respond(json=good)

    entries = fetch_castopod_podcasts(nobody_excluded)

    assert [(e['actor_url'], e['followers'], e['avatar']) for e in entries] == [
        ('https://pod.example/@mypodcast', 0, None)]


def test_a_failing_episode_lookup_does_not_abort_the_source(credentials, http_mock):
    fixture = json.loads(FIXTURE.read_text())
    serve_lists(http_mock, value=fixture['lists']['value']['feeds'], new=fixture['lists']['new']['feeds'])
    http_mock.get(EPISODES, params={'id': '101'}).respond(500)
    http_mock.get(EPISODES, params={'id': '103'}).respond(json=fixture['episodes']['103'])
    http_mock.get(EPISODES, params={'id': '104'}).respond(json=fixture['episodes']['104'])

    entries = fetch_castopod_podcasts(nobody_excluded)

    assert [entry['actor_url'] for entry in entries] == ['https://radio.example/@morning']


def test_every_list_failing_is_a_source_error_and_the_secret_is_never_logged(credentials, http_mock, caplog):
    caplog.set_level(logging.DEBUG)
    for url in (VALUE, NEW, SEARCH):
        http_mock.get(url).mock(side_effect=httpx.ConnectError('refused'))

    with pytest.raises(sources.DiscoverySourceError) as raised:
        fetch_castopod_podcasts(nobody_excluded)

    assert 'SECRETxyz' not in caplog.text
    assert 'KEYabc' not in caplog.text
    assert 'SECRETxyz' not in str(raised.value)
    assert 'KEYabc' not in str(raised.value)


@pytest.mark.parametrize('feed, expected', [
    ({'url': 'https://pod.example/@show/feed.xml'}, True),
    ({'url': 'https://pod.example/@show/feed'}, True),
    ({'url': 'https://pod.example/feed.xml', 'generator': 'Castopod 1.12'}, True),
    ({'url': 'https://pod.example/@show/feed.xml\n'}, False),
    ({'url': 'https://feeds.example/other.rss'}, False),
])
def test_is_castopod_feed(feed, expected):
    assert castopod.is_castopod_feed(feed) is expected


def social_interact(**tag):
    return [{'socialInteract': [dict({'protocol': 'activitypub'}, **tag)]}]


FEED = 'https://radio.example/@morning/feed.xml'


@pytest.mark.parametrize('items, expected', [
    # Castopod leaves accountUrl empty and names the account by accountId: the actor is https://host/@handle
    (social_interact(accountId='@morning@radio.example', accountUrl=''), 'https://radio.example/@morning'),
    (social_interact(accountId='@morning@radio.example'), 'https://radio.example/@morning'),
    # an accountUrl, when present, wins
    (social_interact(accountId='@morning@radio.example', accountUrl='https://radio.example/@other'),
     'https://radio.example/@other'),
    # an accountId on another host than the feed's names someone else's account
    (social_interact(accountId='@morning@elsewhere.example', accountUrl=''), None),
    (social_interact(accountId='morning@radio.example'), None),
    (social_interact(accountId='@mor/ning@radio.example'), None),
    (social_interact(accountId='@morning@radio.example\n'), None),
    (social_interact(accountId=7), None),
    (social_interact(protocol='twitter', accountId='@morning@radio.example'), None),
])
def test_actor_url_from_social_interact(items, expected):
    assert castopod.actor_url_from_social_interact(items, FEED) == expected

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
TRENDING = f'{PODCASTINDEX_API}/podcasts/trending'
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


def serve_fixture(http_mock):
    fixture = json.loads(FIXTURE.read_text())
    trending = http_mock.get(TRENDING).respond(json=fixture['trending'])
    episodes_101 = http_mock.get(EPISODES, params={'id': '101'}).respond(json=fixture['episodes']['101'])
    episodes_103 = http_mock.get(EPISODES, params={'id': '103'}).respond(json=fixture['episodes']['103'])
    return trending, episodes_101, episodes_103


def test_without_credentials_nothing_is_fetched(app, db_session, http_mock):
    cache.clear()
    assert fetch_castopod_podcasts(nobody_excluded) == []


def test_the_signature_is_sha1_of_key_secret_and_time():
    headers = podcastindex_headers('KEYabc', 'SECRETxyz', NOW)

    assert headers == {'X-Auth-Key': 'KEYabc', 'X-Auth-Date': '1700000000',
                       'Authorization': EXPECTED_AUTHORIZATION}


def test_every_request_is_signed(credentials, http_mock):
    trending, episodes_101, _episodes_103 = serve_fixture(http_mock)

    fetch_castopod_podcasts(nobody_excluded)

    for request in (trending.calls.last.request, episodes_101.calls.last.request):
        assert request.headers['X-Auth-Key'] == 'KEYabc'
        assert request.headers['X-Auth-Date'] == str(NOW)
        assert request.headers['Authorization'] == EXPECTED_AUTHORIZATION
        assert request.headers['User-Agent'].startswith('PieFed/')
    assert trending.calls.last.request.url.params['max'] == '200'


def test_only_podcasts_with_an_activitypub_social_interact_are_kept(credentials, http_mock):
    serve_fixture(http_mock)

    entries = fetch_castopod_podcasts(nobody_excluded)

    assert entries == [{'kind': 'community', 'platform': 'castopod', 'actor_url': 'https://pod.example/@mypodcast',
                        'name': 'My Podcast', 'host': 'pod.example', 'avatar': 'https://pod.example/media/cover.jpg',
                        'followers': 9, 'nsfw': False, 'source': 'podcastindex'}]


def test_an_excluded_podcast_host_is_dropped(credentials, http_mock):
    serve_fixture(http_mock)

    assert fetch_castopod_podcasts(lambda host: host == 'pod.example') == []


def test_episode_lookups_are_capped(credentials, http_mock):
    feeds = [{'id': i, 'url': f'https://c{i}.example/@show/feed.xml', 'title': f'Show {i}'} for i in range(50)]
    http_mock.get(TRENDING).respond(json={'feeds': feeds})
    route = http_mock.get(EPISODES).respond(json={'items': []})

    fetch_castopod_podcasts(nobody_excluded)

    assert route.call_count == castopod.MAX_EPISODE_LOOKUPS == 40


def test_malformed_rows_are_skipped_not_fatal(credentials, http_mock):
    good = json.loads(FIXTURE.read_text())['episodes']['101']
    feeds = [None, 'text', {'id': 'x', 'url': 'https://a.example/@a/feed.xml'},
             {'id': 7, 'url': 5}, {'id': 8, 'url': 'https://b.example/@b/feed.xml', 'title': 'B'},
             {'id': 101, 'url': 'https://pod.example/@mypodcast/feed.xml', 'title': 'My Podcast',
              'trendScore': 'many', 'artwork': 12}]
    http_mock.get(TRENDING).respond(json={'feeds': feeds})
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
    http_mock.get(TRENDING).respond(json=fixture['trending'])
    http_mock.get(EPISODES, params={'id': '101'}).respond(500)
    http_mock.get(EPISODES, params={'id': '103'}).respond(json=fixture['episodes']['103'])

    assert fetch_castopod_podcasts(nobody_excluded) == []


def test_a_failing_index_is_a_source_error_and_the_secret_is_never_logged(credentials, http_mock, caplog):
    caplog.set_level(logging.DEBUG)
    http_mock.get(TRENDING).mock(side_effect=httpx.ConnectError('refused'))

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

"""Interop D24: Castopod podcasts from index.castopod.org's public export. Publishing on Castopod is the opt-in,
so every live podcast in the export whose page is a Castopod actor (https://host/@handle) is listed."""
import json
from pathlib import Path

import httpx
import pytest

from app.discovery import castopod, refresh, sources
from app.discovery.castopod import CASTOPOD_INDEX_URL, fetch_castopod_podcasts
from tests.discovery_fixtures import fresh_cache, nobody_excluded  # noqa: F401

pytestmark = pytest.mark.usefixtures('fresh_cache')

FIXTURE = Path(__file__).resolve().parent.parent / 'app' / 'discovery' / 'fixtures' / 'castopod_index.json'


def podcast(handle='show', host='pod.example', **changes):
    values = {'id': 1, 'title': f'Show {handle}', 'url': f'https://{host}/@{handle}/feed.xml',
              'link': f'https://{host}/@{handle}', 'host': host, 'imageUrl': f'https://{host}/{handle}.jpg',
              'explicit': 0, 'dead': 0, 'popularityScore': 1, 'generator': 'Castopod - https://castopod.org/'}
    values.update(changes)
    return values


def test_the_index_is_read_without_credentials_and_normalised(app, http_mock):
    route = http_mock.get(CASTOPOD_INDEX_URL).respond(json=json.loads(FIXTURE.read_text()))

    entries = fetch_castopod_podcasts(nobody_excluded)

    assert route.call_count == 1
    assert 'Authorization' not in route.calls.last.request.headers
    assert entries == [{'kind': 'community', 'platform': 'castopod', 'actor_url': 'https://pod.example/@mypodcast',
                        'name': 'My Podcast', 'host': 'pod.example', 'avatar': 'https://pod.example/media/cover.jpg',
                        'followers': 9, 'nsfw': False, 'source': 'castopod-index'},
                       {'kind': 'community', 'platform': 'castopod', 'actor_url': 'https://radio.example/@morning',
                        'name': 'Morning Show', 'host': 'radio.example',
                        'avatar': 'https://radio.example/media/morning.jpg',
                        'followers': 3, 'nsfw': False, 'source': 'castopod-index'}]


def test_a_dead_podcast_is_skipped(app, http_mock):
    http_mock.get(CASTOPOD_INDEX_URL).respond(json=[podcast('alive'), podcast('gone', dead=1),
                                                    podcast('unknown', dead=None)])

    entries = fetch_castopod_podcasts(nobody_excluded)

    assert [entry['actor_url'] for entry in entries] == ['https://pod.example/@alive', 'https://pod.example/@unknown']


@pytest.mark.parametrize('link', [
    'http://pod.example/@show',
    'https://pod.example/shows/show',
    'https://pod.example/@show/feed.xml',
    'https://pod.example/@',
    'https://pod.example/@sh ow',
    'https://pod.example/@show\n',
    'https://pod.example/@show?x=1',
    'https://user@pod.example/@show',
    'https://pod.example:8443/@show',
    'https://localhost/@show',
    None,
    7,
])
def test_a_link_that_is_not_a_clean_https_castopod_actor_is_skipped(app, http_mock, link):
    http_mock.get(CASTOPOD_INDEX_URL).respond(json=[podcast('kept'), podcast('bad', link=link)])

    entries = fetch_castopod_podcasts(nobody_excluded)

    assert [entry['actor_url'] for entry in entries] == ['https://pod.example/@kept']


def test_explicit_maps_to_nsfw(app, http_mock):
    http_mock.get(CASTOPOD_INDEX_URL).respond(json=[podcast('clean', explicit=0), podcast('adult', explicit=1)])

    entries = fetch_castopod_podcasts(nobody_excluded)

    assert {entry['actor_url']: entry['nsfw'] for entry in entries} == {'https://pod.example/@clean': False,
                                                                       'https://pod.example/@adult': True}


def test_entries_are_ordered_by_popularity_descending(app, http_mock):
    http_mock.get(CASTOPOD_INDEX_URL).respond(json=[podcast('low', popularityScore=1),
                                                    podcast('high', popularityScore=50),
                                                    podcast('junk', popularityScore='lots'),
                                                    podcast('mid', popularityScore=7)])

    entries = fetch_castopod_podcasts(nobody_excluded)

    assert [(entry['name'], entry['followers']) for entry in entries] == [
        ('Show high', 50), ('Show mid', 7), ('Show low', 1), ('Show junk', 0)]


def test_the_host_is_the_links_host_not_the_rows(app, http_mock):
    http_mock.get(CASTOPOD_INDEX_URL).respond(json=[podcast('show', host='elsewhere.example',
                                                            link='https://pod.example/@show')])

    (entry,) = fetch_castopod_podcasts(nobody_excluded)

    assert entry['host'] == 'pod.example'


def test_an_excluded_host_is_dropped(app, http_mock):
    http_mock.get(CASTOPOD_INDEX_URL).respond(json=[podcast('a', host='a.example'), podcast('b', host='b.example')])

    entries = fetch_castopod_podcasts(lambda host: host == 'a.example')

    assert [entry['host'] for entry in entries] == ['b.example']


def test_malformed_rows_are_skipped_not_fatal(app, http_mock):
    http_mock.get(CASTOPOD_INDEX_URL).respond(json=[None, 'text', 3, [], {}, podcast('kept', title=5, imageUrl=9)])

    entries = fetch_castopod_podcasts(nobody_excluded)

    assert [(e['actor_url'], e['name'], e['avatar']) for e in entries] == [('https://pod.example/@kept', '', None)]


@pytest.mark.parametrize('answer', [
    {'json': {'podcasts': []}},
    {'json': 'text'},
    {'content': b'not json'},
    {'status_code': 500},
])
def test_a_malformed_or_non_list_body_is_a_source_error(app, http_mock, answer):
    http_mock.get(CASTOPOD_INDEX_URL).respond(**answer)

    with pytest.raises(sources.DiscoverySourceError):
        fetch_castopod_podcasts(nobody_excluded)


def test_an_unreachable_index_is_a_source_error(app, http_mock):
    http_mock.get(CASTOPOD_INDEX_URL).mock(side_effect=httpx.ConnectError('refused'))

    with pytest.raises(sources.DiscoverySourceError):
        fetch_castopod_podcasts(nobody_excluded)


def padded(size: int) -> bytes:
    """A JSON list holding one podcast, padded with whitespace to `size` bytes."""
    body = json.dumps([podcast('big')]).encode()
    return body[:-1] + b' ' * (size - len(body)) + b']'


def test_the_index_may_be_up_to_sixteen_megabytes(app, http_mock):
    http_mock.get(CASTOPOD_INDEX_URL).respond(200, content=padded(15 * 1024 * 1024))

    entries = fetch_castopod_podcasts(nobody_excluded)

    assert castopod.MAX_INDEX_BYTES == 16 * 1024 * 1024
    assert [entry['actor_url'] for entry in entries] == ['https://pod.example/@big']


def test_an_index_over_sixteen_megabytes_is_a_source_error(app, http_mock):
    http_mock.get(CASTOPOD_INDEX_URL).respond(200, content=padded(castopod.MAX_INDEX_BYTES + 1))

    with pytest.raises(sources.DiscoverySourceError):
        fetch_castopod_podcasts(nobody_excluded)


def test_the_larger_cap_applies_to_this_fetch_only(app, http_mock):
    http_mock.get('https://other.example/x').respond(200, content=padded(sources.MAX_DIRECTORY_BYTES + 1))

    assert sources.MAX_DIRECTORY_BYTES == 5 * 1024 * 1024
    assert sources.fetch_json('https://other.example/x') is None


def test_the_whole_index_is_kept_past_the_usual_per_source_cap(app, db_session, monkeypatch, http_mock):
    monkeypatch.setattr(refresh, 'peertube_isolated_hosts', lambda: frozenset())
    for source in list(refresh.FETCHERS):
        monkeypatch.setitem(refresh.FETCHERS, source, lambda exclude: [])
    monkeypatch.setitem(refresh.FETCHERS, castopod.SOURCE, fetch_castopod_podcasts)
    http_mock.get(CASTOPOD_INDEX_URL).respond(json=[podcast('show', host=f'h{i}.example') for i in range(600)])

    results = refresh.refresh_discovery()

    assert refresh.MAX_PER_SOURCE == 500
    assert refresh.SOURCE_LIMITS == {castopod.SOURCE: 2500}
    assert results[castopod.SOURCE] == 600

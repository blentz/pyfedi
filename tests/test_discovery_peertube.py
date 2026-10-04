"""Interop D24: PeerTube channels from SepiaSearch, and the helpers every fetcher shares."""
import json
import logging
from pathlib import Path

import httpx
import pytest

from app.discovery import peertube, sources
from app.discovery.peertube import SEPIASEARCH_URL, channel_to_entry, fetch_peertube_channels
from tests.discovery_fixtures import nobody_excluded

FIXTURE = Path(__file__).resolve().parent.parent / 'app' / 'discovery' / 'fixtures' / 'sepiasearch.json'

pytestmark = pytest.mark.usefixtures('no_real_sleeping')


def a_page(size):
    return {'total': size, 'data': [{'url': f'https://t{i}.example/video-channels/c{i}', 'host': f't{i}.example',
                                     'name': f'c{i}', 'followersCount': i} for i in range(size)]}


def test_the_fixture_is_normalised_and_ranked_by_followers(app, http_mock):
    http_mock.get(SEPIASEARCH_URL).respond(json=json.loads(FIXTURE.read_text()))

    entries = fetch_peertube_channels(nobody_excluded)

    assert [e['name'] for e in entries] == ['Linux Videos', 'Cooking Together']
    assert entries[0] == {'kind': 'community', 'platform': 'peertube',
                          'actor_url': 'https://tube.example/video-channels/linux_videos',
                          'name': 'Linux Videos', 'host': 'tube.example',
                          'avatar': 'https://tube.example/lazy-static/avatars/big.png',
                          'followers': 4581, 'nsfw': False, 'source': 'sepiasearch'}


def test_a_channel_whose_url_is_not_on_its_host_is_dropped(app):
    assert channel_to_entry({'url': 'https://other.example/video-channels/x', 'host': 'tube.example',
                             'name': 'x'}) is None


def test_malformed_rows_are_skipped_not_fatal(app, http_mock):
    """Review focus 1."""
    rows = [None, 'a string', {'url': 42, 'host': 'tube.example'},
            {'url': 'https://[::1/x', 'host': 'tube.example'},
            {'url': 'http://tube.example/video-channels/plain', 'host': 'tube.example', 'name': 'plain'},
            {'url': 'https://tube.example/video-channels/ok', 'host': 'tube.example', 'name': 'ok',
             'followersCount': 'many', 'avatars': 'nope'}]
    http_mock.get(SEPIASEARCH_URL).respond(json={'total': 6, 'data': rows})

    entries = fetch_peertube_channels(nobody_excluded)

    assert [(e['name'], e['followers'], e['avatar']) for e in entries] == [('ok', 0, None)]


def test_paging_stops_at_five_pages_and_pauses_between_them(app, http_mock, monkeypatch):
    pauses = []
    monkeypatch.setattr(sources, 'polite_pause', lambda: pauses.append(1))
    route = http_mock.get(SEPIASEARCH_URL).respond(json=a_page(peertube.PAGE_SIZE))

    fetch_peertube_channels(nobody_excluded)

    assert route.call_count == peertube.MAX_PAGES == 5
    assert [call.request.url.params['start'] for call in route.calls] == ['0', '100', '200', '300', '400']
    assert {call.request.url.params['count'] for call in route.calls} == {'100'}
    assert len(pauses) == 4


def test_a_short_page_ends_paging(app, http_mock):
    route = http_mock.get(SEPIASEARCH_URL).respond(json=a_page(3))

    assert len(fetch_peertube_channels(nobody_excluded)) == 3
    assert route.call_count == 1


def test_excluded_hosts_are_left_out(app, http_mock):
    http_mock.get(SEPIASEARCH_URL).respond(json=a_page(3))

    entries = fetch_peertube_channels(lambda host: host == 't1.example')

    assert sorted(e['host'] for e in entries) == ['t0.example', 't2.example']


def test_an_unreadable_index_is_a_source_error(app, http_mock):
    http_mock.get(SEPIASEARCH_URL).respond(503)

    with pytest.raises(sources.DiscoverySourceError):
        fetch_peertube_channels(nobody_excluded)


def test_a_page_that_fails_mid_run_is_logged_and_ends_paging(app, http_mock, caplog):
    caplog.set_level(logging.INFO)
    route = http_mock.get(SEPIASEARCH_URL)
    route.side_effect = [httpx.Response(200, json=a_page(peertube.PAGE_SIZE)), httpx.Response(502)]

    entries = fetch_peertube_channels(nobody_excluded)

    assert len(entries) == peertube.PAGE_SIZE
    assert route.call_count == 2
    assert f'discovery: {SEPIASEARCH_URL} answered 502' in caplog.text
    assert 'discovery: sepiasearch page 2 unreadable, paging stopped' in caplog.text


def test_fetch_json_logs_a_failed_answer_with_the_url_only(app, http_mock, caplog):
    caplog.set_level(logging.DEBUG)
    http_mock.get('https://dir.example/x').respond(503)
    http_mock.get('https://html.example/x').respond(200, text='<html></html>')

    assert sources.fetch_json('https://dir.example/x', params={'limit': 80}, headers={'X-Auth-Key': 'KEYabc'}) is None
    assert sources.fetch_json('https://html.example/x') is None

    assert 'discovery: https://dir.example/x answered 503' in caplog.text
    assert 'discovery: https://html.example/x answered 200 with no readable JSON' in caplog.text
    assert 'KEYabc' not in caplog.text
    ours = ' '.join(record.getMessage() for record in caplog.records if record.name != 'httpx')
    assert 'limit' not in ours   # the url as asked for, without its query


def test_fetch_json_answers_none_for_a_transport_error(app, http_mock):
    http_mock.get('https://down.example/x').mock(side_effect=httpx.ConnectError('refused'))

    assert sources.fetch_json('https://down.example/x') is None


def test_fetch_json_answers_none_for_a_body_that_is_not_json(app, http_mock):
    http_mock.get('https://html.example/x').respond(200, text='<html></html>')

    assert sources.fetch_json('https://html.example/x') is None


def test_fetch_json_refuses_a_body_over_the_cap(app, http_mock):
    http_mock.get('https://big.example/x').respond(200, content=b'[' + b' ' * sources.MAX_DIRECTORY_BYTES + b']')

    assert sources.fetch_json('https://big.example/x') is None


def test_fetch_json_sends_its_params_and_headers(app, http_mock):
    route = http_mock.get('https://dir.example/x', params={'limit': '80'}).respond(json={'ok': True})

    assert sources.fetch_json('https://dir.example/x', params={'limit': 80}, headers={'X-Auth-Key': 'k'}) == {'ok': True}
    assert route.calls.last.request.headers['X-Auth-Key'] == 'k'


@pytest.mark.parametrize('value, expected', [(5, 5), (0, 0), (-1, 0), ('5', 0), (True, 0), (None, 0), (2.5, 0)])
def test_as_count(value, expected):
    assert sources.as_count(value) == expected


@pytest.mark.parametrize('value, expected', [('tube.example', True), ('Tube.Example', False), ('a/b.example', False),
                                             ('localhost', False), ('tube.example\n', False), (None, False)])
def test_is_hostname(value, expected):
    assert sources.is_hostname(value) is expected


@pytest.mark.parametrize('value, expected', [('ann', True), ('Ann_B.c-1', True), ('a' * 64, True), ('a' * 65, False),
                                             ('', False), ('ann@m.example', False), ('a/b', False), ('ann\n', False),
                                             ('än', False), (None, False), (42, False)])
def test_is_username(value, expected):
    assert sources.is_username(value) is expected

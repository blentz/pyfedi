"""Interop D24: Mastodon people from the top joinmastodon servers' opt-in profile directories."""
import json
from pathlib import Path

import pytest

from app.discovery import mastodon, sources
from app.discovery.mastodon import SERVERS_URL, account_to_entry, fetch_mastodon_people, top_mastodon_servers
from tests.discovery_fixtures import nobody_excluded

FIXTURE = Path(__file__).resolve().parent.parent / 'app' / 'discovery' / 'fixtures' / 'mastodon_directory.json'
DIRECTORY = 'https://mastodon.example/api/v1/directory'

pytestmark = pytest.mark.usefixtures('no_real_sleeping')


def servers(count):
    return [{'domain': f'm{i}.example', 'last_week_users': i} for i in range(count)]


def test_the_fixture_keeps_only_discoverable_people(app, http_mock):
    http_mock.get(SERVERS_URL).respond(json=[{'domain': 'mastodon.example', 'last_week_users': 100}])
    route = http_mock.get(DIRECTORY).respond(json=json.loads(FIXTURE.read_text()))

    entries = fetch_mastodon_people(nobody_excluded)

    assert [(e['name'], e['actor_url']) for e in entries] == [
        ('Ann Example', 'https://mastodon.example/users/ann'),
        ('ben', 'https://mastodon.example/users/ben')]
    assert entries[0] == {'kind': 'person', 'platform': 'mastodon', 'actor_url': 'https://mastodon.example/users/ann',
                          'name': 'Ann Example', 'host': 'mastodon.example',
                          'avatar': 'https://mastodon.example/avatars/ann.png', 'followers': 340, 'nsfw': False,
                          'source': 'joinmastodon'}
    params = route.calls.last.request.url.params
    assert (params['local'], params['order'], params['limit']) == ('true', 'active', '80')


def test_an_account_uri_on_another_host_is_not_trusted(app):
    entry = account_to_entry({'username': 'ann', 'discoverable': True, 'uri': 'https://evil.example/users/ann'},
                             'mastodon.example')

    assert entry['actor_url'] == 'https://mastodon.example/users/ann'


def test_malformed_account_rows_are_skipped_and_good_rows_kept(app, http_mock):
    """Review focus 1: no single malformed row aborts the source."""
    good = {'username': 'ann', 'discoverable': True, 'uri': 'https://mastodon.example/users/ann'}
    rows = [None, 'ann', 7, {'username': 'x', 'discoverable': True, 'uri': 42},
            {'username': 'x', 'discoverable': True, 'uri': 'https://[::1/x'},
            {'username': 'x', 'discoverable': True, 'uri': 'http://mastodon.example/users/x'},
            {'username': 'bad name', 'discoverable': True}, {'username': None, 'discoverable': True},
            {'username': 'y', 'discoverable': 'true'}, good]
    http_mock.get(SERVERS_URL).respond(json=[{'domain': 'mastodon.example', 'last_week_users': 1}])
    http_mock.get(DIRECTORY).respond(json=rows)

    entries = fetch_mastodon_people(nobody_excluded)

    actor_urls = [e['actor_url'] for e in entries]
    assert 'https://mastodon.example/users/ann' in actor_urls
    # unusable uris fall back to the canonical one on the directory's own host
    assert all(url.startswith('https://mastodon.example/users/') for url in actor_urls)
    assert 'https://[::1/x' not in actor_urls


def test_only_the_twenty_busiest_servers_are_asked(app, http_mock, monkeypatch):
    pauses = []
    monkeypatch.setattr(sources, 'polite_pause', lambda: pauses.append(1))
    http_mock.get(SERVERS_URL).respond(json=servers(25))
    route = http_mock.get(url__regex=r'https://m\d+\.example/api/v1/directory').respond(json=[])

    fetch_mastodon_people(nobody_excluded)

    assert route.call_count == mastodon.MAX_SERVERS == 20
    assert [call.request.url.host for call in route.calls][:2] == ['m24.example', 'm23.example']
    assert len(pauses) == 19


def test_an_excluded_server_is_never_asked_and_the_next_one_is(app, http_mock):
    http_mock.get(SERVERS_URL).respond(json=servers(25))
    route = http_mock.get(url__regex=r'https://m\d+\.example/api/v1/directory').respond(json=[])

    fetch_mastodon_people(lambda host: host == 'm24.example')

    hosts = [call.request.url.host for call in route.calls]
    assert 'm24.example' not in hosts
    assert hosts[0] == 'm23.example' and len(hosts) == 20


def test_a_server_whose_directory_fails_is_skipped(app, http_mock):
    http_mock.get(SERVERS_URL).respond(json=[{'domain': 'a.example', 'last_week_users': 2},
                                             {'domain': 'mastodon.example', 'last_week_users': 1}])
    http_mock.get('https://a.example/api/v1/directory').respond(500)
    http_mock.get(DIRECTORY).respond(json=json.loads(FIXTURE.read_text()))

    assert len(fetch_mastodon_people(nobody_excluded)) == 2


def test_malformed_server_rows_are_ignored(app):
    """Review focus 1: a directory of servers is third-party data like any other."""
    rows = [None, {'domain': 'evil.example/path'}, {'domain': 42}, {'domain': 'ok.example', 'last_week_users': 'x'},
            {'domain': 'Big.Example ', 'last_week_users': 9}]

    assert top_mastodon_servers(rows) == ['big.example', 'ok.example']


def test_an_unreadable_server_list_is_a_source_error(app, http_mock):
    http_mock.get(SERVERS_URL).respond(502)

    with pytest.raises(sources.DiscoverySourceError):
        fetch_mastodon_people(nobody_excluded)

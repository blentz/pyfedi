"""Interop D24: Pixelfed people from FediDB's Pixelfed hosts and each host's opt-in landing directory."""
import json
from pathlib import Path

import pytest

from app.discovery import pixelfed, sources
from app.discovery.pixelfed import FEDIDB_URL, fetch_pixelfed_people, pixelfed_hosts
from tests.discovery_fixtures import nobody_excluded

FIXTURE = Path(__file__).resolve().parent.parent / 'app' / 'discovery' / 'fixtures' / 'pixelfed_directory.json'
DIRECTORY = 'https://pixelfed.example/api/landing/v1/directory'

pytestmark = pytest.mark.usefixtures('no_real_sleeping')


def fedidb(*domains):
    return {'data': [{'domain': domain} for domain in domains], 'meta': {'next_cursor': None}}


def test_the_fixture_is_normalised(app, http_mock):
    route = http_mock.get(FEDIDB_URL).respond(json=fedidb('pixelfed.example'))
    http_mock.get(DIRECTORY).respond(json=json.loads(FIXTURE.read_text()))

    entries = fetch_pixelfed_people(nobody_excluded)

    assert [(e['name'], e['actor_url']) for e in entries] == [
        ('Cara Photos', 'https://pixelfed.example/users/cara'), ('dev', 'https://pixelfed.example/users/dev')]
    assert entries[0] == {'kind': 'person', 'platform': 'pixelfed', 'actor_url': 'https://pixelfed.example/users/cara',
                          'name': 'Cara Photos', 'host': 'pixelfed.example',
                          'avatar': 'https://pixelfed.example/storage/avatars/cara.jpg', 'followers': 210,
                          'nsfw': False, 'source': 'fedidb'}
    assert route.calls.last.request.url.params['software'] == 'pixelfed'


def test_a_host_whose_directory_is_off_is_skipped(app, http_mock):
    http_mock.get(FEDIDB_URL).respond(json=fedidb('off.example', 'pixelfed.example'))
    http_mock.get('https://off.example/api/landing/v1/directory').respond(404)
    http_mock.get(DIRECTORY).respond(json=json.loads(FIXTURE.read_text()))

    assert {e['host'] for e in fetch_pixelfed_people(nobody_excluded)} == {'pixelfed.example'}


def test_at_most_two_pages_per_host(app, http_mock):
    http_mock.get(FEDIDB_URL).respond(json=fedidb('pixelfed.example'))
    second = http_mock.get(DIRECTORY, params={'cursor': 'abc'}).respond(
        json={'data': [{'username': 'two', 'name': 'Two'}], 'meta': {'next_cursor': 'def'}})
    first = http_mock.get(DIRECTORY).respond(
        json={'data': [{'username': 'one', 'name': 'One'}], 'meta': {'next_cursor': 'abc'}})

    entries = fetch_pixelfed_people(nobody_excluded)

    assert [e['name'] for e in entries] == ['One', 'Two']
    assert first.call_count == 1 and second.call_count == 1


def test_only_twenty_hosts_are_asked_and_excluded_ones_never(app, http_mock):
    http_mock.get(FEDIDB_URL).respond(json=fedidb(*[f'p{i}.example' for i in range(25)]))
    route = http_mock.get(url__regex=r'https://p\d+\.example/api/landing/v1/directory').respond(
        json={'data': [], 'meta': {'next_cursor': None}})

    fetch_pixelfed_people(lambda host: host == 'p0.example')

    hosts = [call.request.url.host for call in route.calls]
    assert len(hosts) == pixelfed.MAX_SERVERS == 20
    assert 'p0.example' not in hosts


def test_junk_host_rows_are_ignored(app):
    assert pixelfed_hosts({'data': [None, {'domain': 'a/b'}, {'domain': ' Pix.Example '}]}) == ['pix.example']
    assert pixelfed_hosts(['not', 'a', 'dict']) == []


def test_fedidb_hosts_are_de_duplicated_case_insensitively_in_first_order(app, http_mock):
    assert pixelfed_hosts(fedidb('b.example', 'A.example', 'B.Example ', 'a.example', 'c.example')) == [
        'b.example', 'a.example', 'c.example']

    http_mock.get(FEDIDB_URL).respond(json=fedidb('pixelfed.example', 'Pixelfed.Example'))
    route = http_mock.get(DIRECTORY).respond(json={'data': [{'username': 'one'}], 'meta': {'next_cursor': None}})

    assert [e['name'] for e in fetch_pixelfed_people(nobody_excluded)] == ['one']
    assert route.call_count == 1


def test_calls_are_paced_across_pages_and_hosts(app, http_mock, monkeypatch):
    pauses = []
    monkeypatch.setattr(sources, 'polite_pause', lambda: pauses.append(len(http_mock.calls)))
    http_mock.get(FEDIDB_URL).respond(json=fedidb('pixelfed.example', 'off.example', 'third.example'))
    http_mock.get(DIRECTORY, params={'cursor': 'abc'}).respond(json={'data': [], 'meta': {'next_cursor': None}})
    http_mock.get(DIRECTORY).respond(json={'data': [], 'meta': {'next_cursor': 'abc'}})
    http_mock.get('https://off.example/api/landing/v1/directory').respond(404)
    http_mock.get('https://third.example/api/landing/v1/directory').respond(json={'data': [], 'meta': {}})

    fetch_pixelfed_people(nobody_excluded)

    # FediDB, then four directory calls: a pause before every directory call but the first
    assert len(http_mock.calls) == 5
    assert pauses == [2, 3, 4]


@pytest.mark.parametrize('meta', [None, 'abc', ['abc'], {}, {'next_cursor': 42}, {'next_cursor': ''},
                                  {'next_cursor': ['abc']}])
def test_a_malformed_meta_ends_the_host_and_keeps_its_rows(app, http_mock, meta):
    http_mock.get(FEDIDB_URL).respond(json=fedidb('pixelfed.example'))
    route = http_mock.get(DIRECTORY).respond(json={'data': [{'username': 'one'}], 'meta': meta})

    assert [e['name'] for e in fetch_pixelfed_people(nobody_excluded)] == ['one']
    assert route.call_count == 1


def test_malformed_profile_rows_are_skipped_and_good_rows_kept(app, http_mock):
    """Review focus 1: no single malformed row aborts the source. The actor url is built from the host and
    username, so a bad `url` field cannot leak into it."""
    good = {'username': 'ann', 'name': 'Ann', 'url': 'https://pixelfed.example/ann'}
    rows = [None, 'ann', 7, {'username': 'x', 'url': 42}, {'username': 'x', 'url': 'https://[::1/x'},
            {'username': 'x', 'url': 'http://pixelfed.example/x'}, {'username': 'bad name'}, {'username': None},
            {'username': 'y', 'followers_count': 'many', 'avatar': ['nope']}, good]
    http_mock.get(FEDIDB_URL).respond(json=fedidb('pixelfed.example'))
    http_mock.get(DIRECTORY).respond(json={'data': rows, 'meta': {'next_cursor': None}})

    entries = fetch_pixelfed_people(nobody_excluded)

    assert [e['actor_url'] for e in entries] == ['https://pixelfed.example/users/x'] * 3 + [
        'https://pixelfed.example/users/y', 'https://pixelfed.example/users/ann']
    assert entries[3]['followers'] == 0 and entries[3]['avatar'] is None


def test_an_unreadable_fedidb_is_a_source_error(app, http_mock):
    http_mock.get(FEDIDB_URL).respond(500)

    with pytest.raises(sources.DiscoverySourceError):
        fetch_pixelfed_people(nobody_excluded)

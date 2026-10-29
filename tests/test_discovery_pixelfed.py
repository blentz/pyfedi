"""Interop D24: Pixelfed people from each host's opt-in landing directory, the hosts taken from pixelfed.org's curated
server list (source `pixelfed.org`) and from FediDB (source `fedidb`), which leaves out the hosts the curated list
already covers so no directory is read twice."""
import json
from pathlib import Path

import pytest

from app.discovery import pixelfed, sources
from app.discovery.pixelfed import FEDIDB_URL, PIXELFED_ORG_URL, curated_hosts, curated_servers, \
    fetch_pixelfed_org_people, fetch_pixelfed_people, pixelfed_hosts
from app.discovery.refresh import FETCHERS
from tests.discovery_fixtures import nobody_excluded

FIXTURE = Path(__file__).resolve().parent.parent / 'app' / 'discovery' / 'fixtures' / 'pixelfed_directory.json'
CURATED = Path(__file__).resolve().parent.parent / 'app' / 'discovery' / 'fixtures' / 'pixelfed_org_servers.json'
DIRECTORY = 'https://pixelfed.example/api/landing/v1/directory'

pytestmark = pytest.mark.usefixtures('no_real_sleeping')


@pytest.fixture(autouse=True)
def no_curated_list(monkeypatch):
    """The FediDB tests below read no curated list (as when pixelfed.org cannot be reached); the curated tests ask
    for the real one by name."""
    monkeypatch.setattr(pixelfed, 'curated_servers', lambda: None)


def empty_directory(http_mock, *hosts):
    for host in hosts:
        http_mock.get(f'https://{host}/api/landing/v1/directory').respond(json={'data': [], 'meta': {'next_cursor': None}})


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


# --- pixelfed.org's curated server list (2026-10-29) ---

def curated_payload():
    return json.loads(CURATED.read_text())


def test_curated_hosts_are_ordered_by_users_and_flag_adult_servers(app):
    """The recorded list holds pixelfed.social (General), spicy.social (Adult) and pixelfed.uno (Regional)."""
    assert curated_hosts(curated_payload()) == [('pixelfed.social', False), ('pixelfed.uno', False),
                                                ('spicy.social', True)]


@pytest.mark.parametrize('payload', [None, [], {'data': 'x'}, {}], ids=['none', 'list', 'data-not-a-list', 'no-data'])
def test_an_unreadable_curated_list_has_no_hosts(app, payload):
    assert curated_hosts(payload) == []


def test_junk_inactive_and_repeated_rows_are_dropped(app):
    rows = [None, 'x', {'domain': 7}, {'domain': 'bad host!'}, {'domain': 'Off.Example', 'active': False},
            {'domain': 'Ok.Example', 'active': True, 'stats': {'user_count': 'many'}},
            {'domain': 'ok.example', 'active': True, 'stats': {'user_count': 9}},
            {'domain': 'big.example', 'active': True, 'category': None, 'stats': None}]
    assert curated_hosts({'data': rows}) == [('ok.example', False), ('big.example', False)]


def test_curated_servers_reads_pixelfed_org(app, http_mock):
    """`curated_servers` here is the function this module imported, not the autouse stand-in on the module."""
    http_mock.get(PIXELFED_ORG_URL).respond(json=curated_payload())
    assert [host for host, _ in curated_servers()] == ['pixelfed.social', 'pixelfed.uno', 'spicy.social']


def test_curated_servers_is_none_when_pixelfed_org_cannot_be_read(app, http_mock):
    http_mock.get(PIXELFED_ORG_URL).respond(503)
    assert curated_servers() is None


def test_curated_people_come_from_curated_hosts_in_user_order(app, http_mock, monkeypatch):
    monkeypatch.setattr(pixelfed, 'curated_servers', lambda: [('big.example', False), ('adult.example', True)])
    for host, name in (('big.example', 'Big'), ('adult.example', 'Spicy')):
        http_mock.get(f'https://{host}/api/landing/v1/directory').respond(
            json={'data': [{'username': name.lower(), 'name': name}], 'meta': {'next_cursor': None}})

    entries = fetch_pixelfed_org_people(nobody_excluded)

    assert [(e['host'], e['nsfw'], e['source']) for e in entries] == [('big.example', False, 'pixelfed.org'),
                                                                       ('adult.example', True, 'pixelfed.org')]


def test_curated_people_honour_the_exclusion_and_the_host_cap(app, http_mock, monkeypatch):
    monkeypatch.setattr(pixelfed, 'curated_servers', lambda: [(f'p{i}.example', False) for i in range(25)])
    route = http_mock.get(url__regex=r'https://p\d+\.example/api/landing/v1/directory').respond(
        json={'data': [], 'meta': {'next_cursor': None}})

    fetch_pixelfed_org_people(lambda host: host == 'p0.example')

    asked = [call.request.url.host for call in route.calls]
    assert 'p0.example' not in asked and len(asked) == 20


def test_an_unreadable_curated_list_is_a_source_error(app, monkeypatch):
    monkeypatch.setattr(pixelfed, 'curated_servers', lambda: None)
    with pytest.raises(sources.DiscoverySourceError):
        fetch_pixelfed_org_people(nobody_excluded)


def test_fedidb_leaves_out_the_hosts_the_curated_list_covers(app, http_mock, monkeypatch):
    monkeypatch.setattr(pixelfed, 'curated_servers', lambda: [('pixelfed.example', False)])
    http_mock.get(FEDIDB_URL).respond(json=fedidb('pixelfed.example', 'other.example'))
    empty_directory(http_mock, 'other.example')

    fetch_pixelfed_people(nobody_excluded)   # pixelfed.example's directory is never asked: not mocked, not read


def test_both_sources_are_refreshed(app):
    assert FETCHERS[pixelfed.CURATED_SOURCE] is fetch_pixelfed_org_people
    assert FETCHERS[pixelfed.SOURCE] is fetch_pixelfed_people

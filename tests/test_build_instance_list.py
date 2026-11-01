"""build_instance_list (v1.8.10): the known_instances.json this instance serves other instances' choosers.

It lists this instance, the PieFed instances it knows (1.6.0 or later, not dormant or gone), and the PieFed
instances each of those links to. Two defects in v1.8.10's version, fixed here:

* versions were compared as strings, so 1.10.0 sorted below 1.6.0 and every PieFed past 1.9 was left out, in the
  SQL filter and in the linked list alike;
* one malformed entry in a peer's federated_instances (no domain, not an object, `linked` not a list) raised out
  of the whole task, so one peer could stop the list being written at all.
"""
import json

import pytest

from app import db
from app.shared.tasks.maintenance import build_instance_list, version_at_least
from tests.factories import make_instance

pytestmark = pytest.mark.usefixtures('private_static_tree')

FEDERATED = 'https://{}/api/v3/federated_instances'
LISTED = 'app/static/media/known_instances.json'


def piefed(domain, version, **columns):
    instance = make_instance(domain, software='piefed')
    instance.version = version
    for column, value in columns.items():
        setattr(instance, column, value)
    db.session.commit()
    return instance


def linked(*entries):
    return {'federated_instances': {'linked': list(entries)}}


def listed():
    with open(LISTED) as handle:
        return [entry['domain'] for entry in json.load(handle)]


@pytest.mark.parametrize('version, minimum, expected', [
    ('1.6.0', '1.6.0', True), ('1.10.0', '1.6.0', True), ('1.8.10', '1.6.0', True), ('2.0', '1.6.0', True),
    ('1.5.9', '1.6.0', False), ('1.6.0-dev', '1.6.0', True), ('1.6', '1.6.0', True), ('', '1.6.0', False),
    (None, '1.6.0', False), ('unknown', '1.6.0', False),
], ids=lambda value: repr(value))
def test_versions_are_compared_as_numbers(version, minimum, expected):
    assert version_at_least(version, minimum) is expected


def test_known_and_linked_piefed_instances_are_listed(app, db_session, http_mock):
    piefed('new.example', '1.10.2')
    piefed('old.example', '1.5.9')
    piefed('asleep.example', '1.8.0', dormant=True)
    make_instance('lemmy.example', software='lemmy')
    http_mock.get(FEDERATED.format('new.example')).respond(200, json=linked(
        {'domain': 'far.example', 'software': 'piefed', 'version': '1.11.0'},
        {'domain': 'ancient.example', 'software': 'piefed', 'version': '1.2.0'},
        {'domain': 'mastodon.example', 'software': 'mastodon', 'version': '4.3.0'},
        {'domain': 'new.example', 'software': 'piefed', 'version': '1.10.2'},
        {'domain': 'bare.example'}))

    build_instance_list()

    assert listed() == [app.config['SERVER_NAME'], 'new.example', 'far.example']


@pytest.mark.parametrize('answer', [
    {'status_code': 200, 'json': linked('not an object', {'software': 'piefed', 'version': '1.8.0'},
                                       {'domain': 'far.example', 'software': 'piefed', 'version': '1.8.0'})},
    {'status_code': 200, 'json': {'federated_instances': {'linked': 'not a list'}}},
    {'status_code': 200, 'json': {'federated_instances': None}},
    {'status_code': 200, 'content': b'not json'},
    {'status_code': 503},
], ids=['bad-entries', 'linked-not-a-list', 'no-linked', 'not-json', 'unavailable'])
def test_a_peers_bad_answer_costs_only_what_it_says(app, db_session, http_mock, answer):
    """Whatever one peer answers, the list is still written with everything else in it."""
    piefed('peer.example', '1.8.0')
    piefed('other.example', '1.8.0')
    http_mock.get(FEDERATED.format('peer.example')).respond(**answer)
    http_mock.get(FEDERATED.format('other.example')).respond(200, json=linked())

    build_instance_list()

    expected = {app.config['SERVER_NAME'], 'peer.example', 'other.example'}
    if answer.get('json') and answer['json']['federated_instances'] and \
            isinstance(answer['json']['federated_instances']['linked'], list):
        expected.add('far.example')
    assert set(listed()) == expected


def test_a_peer_that_cannot_be_reached_is_still_listed(app, db_session, monkeypatch):
    piefed('peer.example', '1.8.0')

    def unreachable(url, *args, **kwargs):
        raise ConnectionError('no route to host')
    monkeypatch.setattr('app.shared.tasks.maintenance.get_request', unreachable)

    build_instance_list()

    assert listed() == [app.config['SERVER_NAME'], 'peer.example']


def test_a_failure_writing_the_list_is_raised(app, db_session, monkeypatch):
    def full(*args, **kwargs):
        raise OSError('no space left on device')
    monkeypatch.setattr('app.shared.tasks.maintenance.ensure_directory_exists', full)

    with pytest.raises(OSError, match='no space left'):
        build_instance_list()

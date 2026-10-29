"""Pixelfed seeding (spec 2026-10-29 §3.6): a seeded account's Atom feed fills in what its Follow cannot, posts made
before the Follow and any a delivery lost. Pixelfed's outbox has no items, so the channel sync's outbox poll cannot."""
import httpx
import pytest

from app import db
from app.discovery import people
from app.discovery.people import ATOM_MAX_ENTRIES, enqueue_person_polls, poll_synced_person
from app.models import DiscoveryPersonSync
from tests.factories import make_instance, make_user

pytestmark = pytest.mark.usefixtures('site')

HOST = 'pixelfed.example'
TARGET = f'https://{HOST}/users/erin'


def atom(*ids):
    entries = ''.join(f'<entry><id>{i}</id><title>t</title></entry>' for i in ids)
    return f'<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom">{entries}</feed>'.encode()


@pytest.fixture
def row(db_session):
    erin = make_user(make_instance(HOST, software='pixelfed'), 'erin')
    row = DiscoveryPersonSync(user_id=erin.id, follow_target=TARGET, last_error='an old error')
    db.session.add(row)
    db.session.commit()
    return row


@pytest.fixture
def net(monkeypatch):
    """The feed each URL answers (status, body), and the post URLs resolved."""
    log = {'feeds': {}, 'asked': [], 'resolved': [], 'known': set()}

    def fetch(url, max_bytes, headers=None, max_seconds=None):
        log['asked'].append(url)
        return log['feeds'].get(url, (404, b''))

    monkeypatch.setattr(people, 'get_request_capped', fetch)
    monkeypatch.setattr(people, 'resolve_remote_post_from_search', log['resolved'].append)
    monkeypatch.setattr(people, '_known_post', lambda url: url in log['known'])
    return log


def test_each_post_the_feed_lists_is_fetched(row, net):
    posts = [f'https://{HOST}/p/erin/{n}' for n in (1, 2)]
    net['feeds'][f'{TARGET}.atom'] = (200, atom(*posts))

    poll_synced_person(row.user_id)

    assert net['asked'] == [f'{TARGET}.atom']
    assert net['resolved'] == posts
    db.session.expire_all()
    row = db.session.get(DiscoveryPersonSync, row.user_id)
    assert row.last_polled_at is not None and row.last_error is None


def test_known_off_host_and_unsafe_ids_are_not_fetched(row, net):
    net['known'].add(f'https://{HOST}/p/erin/known')
    net['feeds'][f'{TARGET}.atom'] = (200, atom(f'https://{HOST}/p/erin/known', 'https://elsewhere.example/p/x/1',
                                                f'http://{HOST}/p/erin/2', 'not a url', f'https://{HOST}/p/erin/3'))

    poll_synced_person(row.user_id)

    assert net['resolved'] == [f'https://{HOST}/p/erin/3']


def test_at_most_what_one_feed_holds_is_fetched(row, net):
    net['feeds'][f'{TARGET}.atom'] = (200, atom(*[f'https://{HOST}/p/erin/{n}' for n in range(ATOM_MAX_ENTRIES + 5)]))

    poll_synced_person(row.user_id)

    assert len(net['resolved']) == ATOM_MAX_ENTRIES == 10


@pytest.mark.parametrize('answer,error', [
    ((404, b''), 'Atom feed: HTTP 404'),
    ((200, None), 'Atom feed: over the size cap'),
    ((200, b'<feed><entry>'), 'Atom feed: unreadable'),
    ((200, b'<?xml version="1.0"?><!DOCTYPE feed [<!ENTITY x "y">]><feed/>'), 'Atom feed: unreadable'),
], ids=['missing', 'too-big', 'broken', 'dtd'])
def test_a_feed_that_cannot_be_read_records_why_and_fetches_nothing(row, net, answer, error):
    net['feeds'][f'{TARGET}.atom'] = answer

    poll_synced_person(row.user_id)

    assert net['resolved'] == []
    db.session.expire_all()
    row = db.session.get(DiscoveryPersonSync, row.user_id)
    assert row.last_error == error and row.last_polled_at is not None


def test_a_transport_failure_records_why(row, net, monkeypatch):
    def fail(*a, **k):
        raise httpx.ConnectError('down')
    monkeypatch.setattr(people, 'get_request_capped', fail)

    poll_synced_person(row.user_id)

    db.session.expire_all()
    assert db.session.get(DiscoveryPersonSync, row.user_id).last_error == 'Atom feed: ConnectError'


def test_one_posts_failure_does_not_stop_the_rest(row, net, monkeypatch):
    posts = [f'https://{HOST}/p/erin/{n}' for n in (1, 2)]
    net['feeds'][f'{TARGET}.atom'] = (200, atom(*posts))

    def resolve(url):
        net['resolved'].append(url)
        if url.endswith('/1'):
            raise RuntimeError('boom')
    monkeypatch.setattr(people, 'resolve_remote_post_from_search', resolve)

    poll_synced_person(row.user_id)

    assert net['resolved'] == posts


def test_a_row_gone_before_its_poll_is_no_error(db_session, net):
    poll_synced_person(999999)
    assert net['asked'] == []


def test_polls_are_queued_one_per_row_spaced_per_host(db_session, monkeypatch):
    queued = []
    monkeypatch.setattr(people.poll_synced_person, 'apply_async',
                        lambda args, countdown: queued.append((args[0], countdown)))
    rows, instances = [], {}
    for name, host in (('a', HOST), ('b', HOST), ('c', 'other.example')):
        instance = instances.get(host) or instances.setdefault(host, make_instance(host, software='pixelfed'))
        user = make_user(instance, name)
        rows.append(DiscoveryPersonSync(user_id=user.id, follow_target=f'https://{host}/users/{name}'))
    db.session.add_all(rows)
    db.session.commit()

    assert enqueue_person_polls() == 3

    assert sorted(countdown for _, countdown in queued) == [0, 0, people.POLL_SPACING_SECONDS]


def test_queueing_one_poll_runs_it_at_once_in_debug(row, net, app, monkeypatch):
    monkeypatch.setitem(app.config, 'DEBUG', True)   # app.debug reads it
    net['feeds'][f'{TARGET}.atom'] = (200, atom(f'https://{HOST}/p/erin/1'))

    people.queue_person_poll(row.user_id)

    assert net['resolved'] == [f'https://{HOST}/p/erin/1']


def test_a_post_already_here_is_known(row):
    from tests.factories import make_community, make_post
    post = make_post(make_community('microblogs', host=HOST), db.session.get(people.User, row.user_id),
                     ap_id=f'https://{HOST}/p/erin/1')
    assert people._known_post(post.ap_id) is True
    assert people._known_post(f'https://{HOST}/p/erin/2') is False


def test_queueing_one_poll_outside_debug_sends_it_to_celery(row, monkeypatch, app):
    queued = []
    monkeypatch.setitem(app.config, 'DEBUG', False)
    monkeypatch.setattr(people.poll_synced_person, 'apply_async', lambda args, countdown: queued.append(args))
    people.queue_person_poll(row.user_id)
    assert queued == [[row.user_id]]

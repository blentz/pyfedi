"""Pixelfed seeding (spec 2026-10-29 §3.3-3.5): the daily reconcile keeps exactly each server's top N opted-in Pixelfed
people followed by the instance actor, and their Accepts and Refusals land on their rows."""
from datetime import timedelta

import pytest

from app import db
from app.discovery import SYNC_ACCEPTED, SYNC_FAILED, SYNC_PENDING, people
from app.discovery import instance_actor
from app.discovery.instance_answers import instance_actor_answer, record_delivery_refusal
from app.discovery.people import desired_people, reconcile_people
from app.discovery.sync import reconcile_sync_task
from app.models import DiscoveryExclusion, DiscoveryPersonSync, Instance, User, utcnow
from app.utils import set_setting
from tests.discovery_fixtures import add_entry, fresh_cache  # noqa: F401
from tests.factories import make_community, make_instance, make_user

pytestmark = pytest.mark.usefixtures('site', 'fresh_cache')

HOST = 'pixelfed.example'


@pytest.fixture(autouse=True)
def founder(db_session):
    make_user(make_instance('test.piefed.local', software='piefed'), 'founder', local=True)


@pytest.fixture
def fed(monkeypatch):
    """Doubles for everything that leaves this process; records what was asked."""
    log = {'follow': [], 'undo': [], 'resolve': [], 'poll': []}

    def resolve(actor_url, **kwargs):
        """As find_actor_or_create, which keys an account by its canonical id: an alias URL finds the same one."""
        log['resolve'].append(actor_url)
        canonical = actor_url.rstrip('/').lower()
        name = canonical.rsplit('/', 1)[-1]
        host = actor_url.split('/')[2]
        instance = db.session.query(Instance).filter_by(domain=host).first() or make_instance(host, software='pixelfed')
        user = db.session.query(User).filter_by(ap_profile_id=canonical).first() or make_user(instance, name)
        user.ap_profile_id = canonical
        db.session.commit()
        return user

    def follow(row, actor):
        log['follow'].append(row.follow_target)
        row.follow_state = SYNC_PENDING
        row.followed_at = utcnow()
        row.follow_uuid = row.follow_uuid or f'u-{row.user_id}'
        db.session.commit()
        return True

    monkeypatch.setattr(people, 'find_actor_or_create', resolve)
    monkeypatch.setattr(people, 'send_instance_follow', follow)
    monkeypatch.setattr(people, 'send_instance_undo', lambda row, actor: log['undo'].append(row.follow_target))
    monkeypatch.setattr(people, 'queue_person_poll', log['poll'].append)
    return log


def person(name, host=HOST, followers=10, nsfw=False, platform='pixelfed'):
    return add_entry(name, host=host, kind='person', platform=platform, followers=followers, nsfw=nsfw,
                     url=f'https://{host}/users/{name.lower()}')


# --- choosing (§3.3) ---

def test_off_by_default_chooses_and_follows_nobody(db_session, fed):
    person('ann')
    assert desired_people() == {}
    assert reconcile_people() == {'added': 0, 'dropped': 0, 'refollowed': 0, 'failed_hosts': []}
    assert fed['follow'] == []


def test_the_top_n_per_host_by_followers(db_session, fed):
    set_setting('discovery_people_per_host', 2)
    ann, bob, cat = person('ann', followers=5), person('bob', followers=50), person('cat', followers=20)
    other = person('dan', host='pix.example', followers=1)

    desired = desired_people()

    assert [e.actor_url for e in desired[HOST]] == [bob.actor_url, cat.actor_url]
    assert [e.actor_url for e in desired['pix.example']] == [other.actor_url]


def test_only_safe_opted_in_pixelfed_people_are_chosen(db_session, fed):
    set_setting('discovery_people_per_host', 5)
    keep = person('ann')
    person('spicy', nsfw=True)                                  # an Adult server's person
    person('toot', platform='mastodon', host='masto.example')   # Mastodon people come by relays
    add_entry('Chan', host=HOST, platform='peertube')           # a channel, not a person
    excluded = person('gone')
    db.session.add(DiscoveryExclusion(actor_url=excluded.actor_url.lower()))
    db.session.commit()

    assert [e.actor_url for e in desired_people()[HOST]] == [keep.actor_url]


def test_a_blocked_host_and_a_banned_person_are_never_chosen(db_session, fed, monkeypatch):
    set_setting('discovery_people_per_host', 5)
    person('ann', host='blocked.example')
    banned = person('bob')
    fed_user = make_user(make_instance(HOST, software='pixelfed'), 'bob')
    fed_user.ap_profile_id = banned.actor_url.lower()
    fed_user.banned = True
    db.session.commit()
    monkeypatch.setattr(people, 'host_is_excluded', lambda host, extra: host == 'blocked.example')

    assert desired_people() == {}


# --- reconcile (§3.4) ---

def test_adds_follows_and_queues_a_backfill_poll(db_session, fed):
    set_setting('discovery_people_per_host', 1)
    ann = person('ann')

    summary = reconcile_people()

    assert summary['added'] == 1
    row = db.session.query(DiscoveryPersonSync).one()
    assert (row.follow_target, row.follow_state, row.entry_id) == (ann.actor_url, SYNC_PENDING, ann.id)
    assert fed['follow'] == [ann.actor_url] and fed['poll'] == [row.user_id]


def test_a_person_no_longer_wanted_is_unfollowed_and_kept(db_session, fed):
    set_setting('discovery_people_per_host', 1)
    person('ann')
    reconcile_people()
    user_id = db.session.query(DiscoveryPersonSync).one().user_id
    set_setting('discovery_people_per_host', 0)

    assert reconcile_people()['dropped'] == 1

    assert db.session.query(DiscoveryPersonSync).count() == 0
    assert fed['undo'] == ['https://pixelfed.example/users/ann']
    assert db.session.get(User, user_id) is not None   # the account and its posts stay


def test_a_held_person_is_not_followed_again_until_a_week_has_passed(db_session, fed):
    set_setting('discovery_people_per_host', 1)
    person('ann')
    reconcile_people()
    assert reconcile_people()['refollowed'] == 0

    row = db.session.query(DiscoveryPersonSync).one()
    row.followed_at = utcnow() - timedelta(days=8)
    db.session.commit()
    assert reconcile_people()['refollowed'] == 1


@pytest.mark.parametrize('what', ['community', 'local', 'nothing', 'banned'])
def test_an_actor_that_is_not_a_remote_person_is_not_added(db_session, fed, monkeypatch, what):
    set_setting('discovery_people_per_host', 1)
    person('ann')

    def resolve(actor_url, **kwargs):
        if what == 'community':
            return make_community('ann', host=HOST)
        if what == 'local':
            return make_user(make_instance('test.piefed.local', software='piefed'), 'annlocal', local=True)
        if what == 'banned':
            user = make_user(make_instance(HOST, software='pixelfed'), 'ann')
            user.banned = True
            db.session.commit()
            return user
        return None
    monkeypatch.setattr(people, 'find_actor_or_create', resolve)

    assert reconcile_people()['added'] == 0
    assert fed['follow'] == []


def test_an_entry_on_a_hosted_domain_is_never_resolved(db_session, fed, monkeypatch):
    set_setting('discovery_people_per_host', 1)
    person('ann')
    monkeypatch.setattr(people, 'is_hosted_uri', lambda url: True)

    assert reconcile_people()['added'] == 0
    assert fed['resolve'] == []


def test_one_hosts_failure_does_not_stop_the_others(db_session, fed, monkeypatch):
    set_setting('discovery_people_per_host', 1)
    person('ann', host='bad.example')
    good = person('bob', host='good.example')
    real = people.find_actor_or_create

    def resolve(actor_url, **kwargs):
        if 'bad.example' in actor_url:
            raise RuntimeError('boom')
        return real(actor_url, **kwargs)
    monkeypatch.setattr(people, 'find_actor_or_create', resolve)

    summary = reconcile_people()

    assert summary['failed_hosts'] == ['bad.example'] and summary['added'] == 1
    assert fed['follow'] == [good.actor_url]


def test_the_channel_reconcile_task_reconciles_people_too(db_session, fed, monkeypatch):
    calls = []
    monkeypatch.setattr('app.discovery.sync.reconcile_people', lambda: calls.append('people') or {'added': 0})
    monkeypatch.setattr('app.discovery.sync.enqueue_person_polls', lambda: calls.append('polls') or 0)
    reconcile_sync_task()
    assert calls == ['people', 'polls']


# --- the real Follow and Undo go to the person's inbox (§3.4) ---

def test_the_follow_goes_from_the_instance_actor_to_the_persons_inbox(app, db_session, monkeypatch):
    sent = []
    monkeypatch.setattr(instance_actor, '_signing', lambda: ('key', 'https://test.piefed.local/actor#main-key'))
    monkeypatch.setattr(instance_actor, 'send_post_request', lambda inbox, activity, *a, **k: sent.append((inbox, activity)))
    erin = make_user(make_instance(HOST, software='pixelfed'), 'erin')
    erin.ap_inbox_url = f'https://{HOST}/f/inbox'
    db.session.commit()
    row = DiscoveryPersonSync(user_id=erin.id, follow_target=f'https://{HOST}/users/erin')
    db.session.add(row)
    db.session.commit()

    assert instance_actor.send_instance_follow(row, erin) is True

    [(inbox, activity)] = sent
    assert inbox == f'https://{HOST}/f/inbox'
    assert (activity['type'], activity['object']) == ('Follow', f'https://{HOST}/users/erin')
    assert activity['actor'] == f"{app.config['SERVER_URL']}/actor"
    assert row.follow_state == SYNC_PENDING and row.follow_uuid in activity['id']


# --- Accept, Reject and refusal land on the person's row (§3.5) ---

def _held_row(follow_state=SYNC_PENDING):
    erin = make_user(make_instance(HOST, software='pixelfed'), 'erin')
    row = DiscoveryPersonSync(user_id=erin.id, follow_target=f'https://{HOST}/users/erin', follow_uuid='abc-123',
                              follow_state=follow_state)
    db.session.add(row)
    db.session.commit()
    return row


def test_an_accept_naming_the_follow_id_finds_the_persons_row(app, db_session):
    row = _held_row()
    follow_id = f"{app.config['SERVER_URL']}/activities/follow/abc-123"

    ours, found = instance_actor_answer({'type': 'Accept', 'object': follow_id}, f'https://{HOST}/users/erin')

    assert ours is True and found is row


def test_an_accept_embedding_the_follow_finds_the_persons_row_by_its_object(app, db_session):
    row = _held_row()
    follow = {'type': 'Follow', 'actor': f"{app.config['SERVER_URL']}/actor", 'object': f'https://{HOST}/users/erin'}

    assert instance_actor_answer({'type': 'Accept', 'object': follow}, f'https://{HOST}/users/erin') == (True, row)


def test_an_answer_signed_on_another_host_finds_no_row(app, db_session):
    _held_row()
    follow_id = f"{app.config['SERVER_URL']}/activities/follow/abc-123"
    assert instance_actor_answer({'type': 'Accept', 'object': follow_id}, 'https://elsewhere.example/users/x') == \
        (True, None)


def test_a_refused_delivery_marks_the_persons_row_failed(app, db_session):
    row = _held_row()
    body = {'type': 'Follow', 'actor': f"{app.config['SERVER_URL']}/actor",
            'id': f"{app.config['SERVER_URL']}/activities/follow/abc-123"}

    record_delivery_refusal(db.session, body, 403)

    assert (row.follow_state, row.last_error) == (SYNC_FAILED, 'Follow refused: HTTP 403')


def test_a_refused_delivery_leaves_an_accepted_row_alone(app, db_session):
    row = _held_row(SYNC_ACCEPTED)
    body = {'type': 'Follow', 'actor': f"{app.config['SERVER_URL']}/actor",
            'id': f"{app.config['SERVER_URL']}/activities/follow/abc-123"}

    record_delivery_refusal(db.session, body, 403)

    assert row.follow_state == SYNC_ACCEPTED


def test_two_entries_naming_one_account_add_it_once(db_session, fed, monkeypatch):
    """An alias URL in the directory resolves to an account already seeded: one row, one Follow."""
    set_setting('discovery_people_per_host', 2)
    person('ann', followers=9)
    add_entry('Ann again', host=HOST, kind='person', platform='pixelfed', followers=1,
              url=f'https://{HOST}/users/ann/')   # a trailing slash: the same account

    assert reconcile_people()['added'] == 1
    assert db.session.query(DiscoveryPersonSync).count() == 1


def test_a_drop_that_fails_does_not_stop_the_run(db_session, fed, monkeypatch):
    set_setting('discovery_people_per_host', 1)
    person('ann')
    reconcile_people()
    set_setting('discovery_people_per_host', 0)

    def fail(row, actor):
        raise RuntimeError('peer down')
    monkeypatch.setattr(people, 'send_instance_undo', fail)

    assert reconcile_people()['dropped'] == 0
    assert db.session.query(DiscoveryPersonSync).count() == 1   # tried again tomorrow


def test_an_accept_of_a_follow_no_row_holds_finds_none(app, db_session):
    follow = {'type': 'Follow', 'actor': f"{app.config['SERVER_URL']}/actor", 'object': f'https://{HOST}/users/nobody'}
    assert instance_actor_answer({'type': 'Accept', 'object': follow}, f'https://{HOST}/users/nobody') == (True, None)

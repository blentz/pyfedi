"""Where deliveries to an instance go: the shared inbox its actors name, not a guess (P1, Pixelfed spike 2026-10-28).

find_instance_id made every new instance with `inbox = https://<host>/inbox`, and new_instance_profile_task kept
that guess unless the site root answered as an Application actor. Every follower fan-out posts to `instance.inbox`
(app/shared/tasks/pages.py), so a peer whose shared inbox lives elsewhere got nothing. Pixelfed's is `/f/inbox`, its
root is an HTML page, and its instance actor is `/i/actor`: every Create, Update and Delete to a Pixelfed follower
was a 405, while the follower's own row held the right address all along (`endpoints.sharedInbox`).

An instance now takes the sharedInbox one of its actors names, once that is trusted for the actor's own host
(shared_inbox_of on creation, delivery_inbox_of on refresh), and the instance-profile lookup keeps it.
"""
import httpx
import pytest

from app import db
from app.activitypub.util import actor_json_to_model, find_instance_id, new_instance_profile_task, \
    refresh_community_profile_task, refresh_feed_profile_task, refresh_user_profile_task
from app.models import Community, Instance, User
from tests.factories import make_instance, peer_actor_json
from tests.test_ap_actor_json_feed import _owned_feed, _peer_with_one_owner
from tests.test_ap_new_instance_profile import DOMAIN, NODEINFO, answer, env, only_the_actor, reload  # noqa: F401 (env)
from tests.test_ap_refresh_profiles import PEER, _feed_document, _group_document, _person_document, \
    _remote_community, _remote_feed, _remote_user

GUESS = f'https://{PEER}/inbox'
SHARED = f'https://{PEER}/f/inbox'
ELSEWHERE = 'https://elsewhere.example/inbox'


def _guessed_instance():
    """The row find_instance_id leaves for a peer it has just met: the /inbox guess, no shared inbox learned."""
    instance = db.session.query(Instance).filter_by(domain=PEER).first() or make_instance(PEER)
    instance.inbox = GUESS
    instance.shared_inbox = None
    db.session.commit()
    return instance


def _instance():
    db.session.expire_all()
    return db.session.query(Instance).filter_by(domain=PEER).one()


# --- creation ---

def test_a_new_instance_starts_with_the_shared_inbox_its_first_actor_names(app, db_session, monkeypatch):
    spawned = []
    monkeypatch.setattr('app.activitypub.util.new_instance_profile', spawned.append)

    instance_id = find_instance_id(PEER, shared_inbox=SHARED)

    instance = db.session.get(Instance, instance_id)
    assert (instance.inbox, instance.shared_inbox) == (SHARED, SHARED)
    assert spawned == [instance_id]   # the profile lookup still runs for software and admins


def test_a_new_instance_without_a_shared_inbox_keeps_the_guess(app, db_session, monkeypatch):
    monkeypatch.setattr('app.activitypub.util.new_instance_profile', lambda instance_id: None)

    instance = db.session.get(Instance, find_instance_id(PEER))

    assert (instance.inbox, instance.shared_inbox) == (GUESS, None)


@pytest.mark.parametrize('shared_inbox', [ELSEWHERE, f'https://{PEER}/' + 'x' * 256, '', 7],
                         ids=['off-host', 'too-long', 'empty', 'not-a-string'])
def test_a_new_instance_refuses_a_shared_inbox_it_cannot_use(app, db_session, monkeypatch, shared_inbox):
    monkeypatch.setattr('app.activitypub.util.new_instance_profile', lambda instance_id: None)

    instance = db.session.get(Instance, find_instance_id(PEER, shared_inbox=shared_inbox))

    assert (instance.inbox, instance.shared_inbox) == (GUESS, None)


def test_a_person_created_on_a_known_instance_teaches_it_the_shared_inbox(app, db_session):
    _guessed_instance()
    document = peer_actor_json(name='erin', server=PEER,
                               fields={'inbox': f'https://{PEER}/users/erin/inbox', 'endpoints': {'sharedInbox': SHARED}})

    user = actor_json_to_model(document, 'erin', PEER)

    assert user.ap_inbox_url == SHARED
    assert (_instance().inbox, _instance().shared_inbox) == (SHARED, SHARED)


def test_a_group_created_on_a_known_instance_teaches_it_the_shared_inbox(app, db_session):
    _guessed_instance()
    document = peer_actor_json('Group', name='memes', server=PEER, fields={'endpoints': {'sharedInbox': SHARED}})

    actor_json_to_model(document, '!memes', PEER)

    assert (_instance().inbox, _instance().shared_inbox) == (SHARED, SHARED)


def test_a_feed_created_on_a_known_instance_teaches_it_the_shared_inbox(app, db_session, http_mock):
    _peer_with_one_owner(http_mock)
    instance = _instance()
    instance.inbox, instance.shared_inbox = GUESS, None
    db.session.commit()

    actor_json_to_model(_owned_feed(fields={'endpoints': {'sharedInbox': SHARED}}), '~news', PEER)

    assert (_instance().inbox, _instance().shared_inbox) == (SHARED, SHARED)


@pytest.mark.parametrize('fields', [{}, {'endpoints': {'sharedInbox': ELSEWHERE}}], ids=['none', 'off-host'])
def test_a_person_naming_no_usable_shared_inbox_leaves_the_instance_alone(app, db_session, fields):
    _guessed_instance()
    document = peer_actor_json(name='erin', server=PEER, fields={'inbox': f'https://{PEER}/users/erin/inbox', **fields})

    actor_json_to_model(document, 'erin', PEER)

    assert (_instance().inbox, _instance().shared_inbox) == (GUESS, None)


# --- refresh ---

def test_a_user_refresh_teaches_the_instance_its_shared_inbox(app, db_session):
    user = _remote_user()
    _guessed_instance()

    refresh_user_profile_task(user.id, _person_document(fields={'inbox': f'https://{PEER}/u/wakko/inbox',
                                                                'endpoints': {'sharedInbox': SHARED}}))

    db.session.expire_all()
    assert db.session.get(User, user.id).ap_inbox_url == SHARED
    assert (_instance().inbox, _instance().shared_inbox) == (SHARED, SHARED)


def test_a_user_refresh_naming_only_its_own_inbox_leaves_the_instance_alone(app, db_session):
    user = _remote_user()
    _guessed_instance()

    refresh_user_profile_task(user.id, _person_document(fields={'inbox': f'https://{PEER}/u/wakko/inbox'}))

    db.session.expire_all()
    assert db.session.get(User, user.id).ap_inbox_url == f'https://{PEER}/u/wakko/inbox'
    assert (_instance().inbox, _instance().shared_inbox) == (GUESS, None)


def test_a_community_refresh_teaches_the_instance_its_shared_inbox(app, db_session, http_mock):
    community = _remote_community()
    _guessed_instance()
    community_id = community.id
    http_mock.get(community.ap_public_url).respond(200, json=_group_document(
        fields={'inbox': f'https://{PEER}/c/memes/inbox', 'endpoints': {'sharedInbox': SHARED}}))

    refresh_community_profile_task(community_id)

    db.session.expire_all()
    assert db.session.get(Community, community_id).ap_inbox_url == SHARED
    assert (_instance().inbox, _instance().shared_inbox) == (SHARED, SHARED)


def test_a_feed_refresh_teaches_the_instance_its_shared_inbox(app, db_session):
    feed = _remote_feed()
    _guessed_instance()

    refresh_feed_profile_task(feed.id, _feed_document(fields={'inbox': f'https://{PEER}/f/news/inbox',
                                                              'endpoints': {'sharedInbox': SHARED}}))

    assert (_instance().inbox, _instance().shared_inbox) == (SHARED, SHARED)


# --- the instance-profile lookup keeps what was learned ---

def _learned(env):
    env.instance.inbox = env.instance.shared_inbox = LEARNED
    db.session.commit()


LEARNED = f'https://{DOMAIN}/f/inbox'


@pytest.mark.parametrize('root', [
    httpx.Response(200, html='<html>Pixelfed</html>'),
    httpx.Response(200, json={'type': 'Application', 'inbox': f'https://{DOMAIN}/i/actor/inbox'}),
], ids=['html-homepage', 'application-actor'])
def test_the_instance_profile_lookup_keeps_a_learned_shared_inbox(env, root):
    _learned(env)
    answer(env, actor=root)

    new_instance_profile_task(env.instance.id)

    assert reload(env).inbox == LEARNED


@pytest.mark.parametrize('status', [404, 406])
def test_the_instance_profile_lookup_keeps_it_when_the_root_says_nothing(env, status):
    _learned(env)
    only_the_actor(env, httpx.Response(status))
    env.http.get(NODEINFO).mock(return_value=httpx.Response(404))

    new_instance_profile_task(env.instance.id)

    assert reload(env).inbox == LEARNED

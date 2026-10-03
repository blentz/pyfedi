"""Interop D24: a Castopod podcast is a User and a Community with one id. A lookup with no type hint
answers the User (the author); a community lookup answers the Community.

Audit of hint-less lookups that can meet a podcast id (app/activitypub/routes.py unless noted):
  Announce/Accept/Reject preamble -- community_only=True first -> Community (fixed by D24)
  every other activity's preamble -- unhinted -> User, the podcast as author -- correct
  Accept{Follow} requestor -- the local follower, never the podcast
  Undo{Follow} target -- the local user/community unfollowed; a podcast is never an Undo's target here
  HTTP-signature key lookup -- User; both rows carry the same key
  Add/Remove/Move community lookups -- community_only=True -> Community (fixed by D24)
  community/routes.py subscribe/unsubscribe -- Community.ap_id -> Community
  discovery/preload.py, discovery/views.py -- community_only=True -> Community (fixed by D24)
"""
from types import SimpleNamespace

import pytest
from cachelib import SimpleCache

from app import cache, db
from app.activitypub.actor import create_actor_from_remote, find_actor_by_url
from app.activitypub.routes import process_inbox_request
from app.activitypub.util import actor_json_to_model, find_actor_or_create, find_actor_or_create_cached
from app.models import Community, CommunityMember, User, UserFollower
from tests.factories import make_community_join_request, make_follow, make_instance, make_site, make_user, \
    peer_actor_json, peer_instance

PEER = 'peer.example'
PODCAST = f'https://{PEER}/u/mypodcast'


@pytest.fixture(autouse=True)
def fresh_cache():
    cache.clear()  # find_actor_or_create_cached memoizes (id, class) per url and hint
    yield
    cache.clear()


def podcast_document():
    return peer_actor_json('Person', name='mypodcast', server=PEER,
                           fields={'type': 'Podcast', 'name': 'My Podcast', 'inbox': f'{PODCAST}/inbox',
                                   'outbox': f'{PODCAST}/outbox', 'followers': f'{PODCAST}/followers'})


@pytest.fixture
def podcast(app, db_session):
    make_site()
    local = make_instance(app.config['SERVER_NAME'], software='piefed')
    peer_instance(PEER)
    user = actor_json_to_model(podcast_document(), 'mypodcast', PEER)
    return SimpleNamespace(local=local, user=user, community=Community.query.filter_by(ap_profile_id=PODCAST).one())


def local_user(app, instance, name):
    user = make_user(instance, name, local=True)
    user.ap_profile_id = f"https://{app.config['SERVER_NAME']}/u/{name}"
    db.session.commit()
    return user


def test_an_unhinted_lookup_finds_the_user(podcast):
    assert find_actor_by_url(PODCAST) == podcast.user
    assert find_actor_or_create(PODCAST, create_if_not_found=False) == podcast.user


def test_a_community_lookup_finds_the_community(podcast):
    assert find_actor_by_url(PODCAST, community_only=True) == podcast.community
    assert find_actor_or_create(PODCAST, community_only=True, create_if_not_found=False) == podcast.community


def test_a_community_lookup_of_an_unknown_podcast_creates_both_and_answers_the_community(app, db_session, http_mock):
    make_site()
    peer_instance(PEER)
    http_mock.get(PODCAST).respond(json=podcast_document())

    found = find_actor_or_create(PODCAST, community_only=True)

    assert isinstance(found, Community) and found.ap_profile_id == PODCAST
    assert User.query.filter_by(ap_profile_id=PODCAST).count() == 1


def test_an_accept_from_the_podcast_admits_the_join_request(app, podcast):
    joiner = local_user(app, podcast.local, 'joiner')
    make_community_join_request(joiner, podcast.community)

    process_inbox_request({'id': f'{PODCAST}/activities/accept/1', 'type': 'Accept', 'actor': PODCAST,
                           'object': {'id': f"https://{app.config['SERVER_NAME']}/activities/follow/1",
                                      'type': 'Follow', 'actor': joiner.ap_profile_id, 'object': PODCAST}}, True)

    assert CommunityMember.query.filter_by(user_id=joiner.id, community_id=podcast.community.id).count() == 1


def test_an_undo_follow_sent_by_the_podcast_is_the_users(app, podcast):
    followed = local_user(app, podcast.local, 'followed')
    make_follow(followed, podcast.user, is_accepted=True, is_inward=True)

    process_inbox_request({'id': f'{PODCAST}/activities/undo/1', 'type': 'Undo', 'actor': PODCAST,
                           'object': {'id': f'{PODCAST}/activities/follow/1', 'type': 'Follow', 'actor': PODCAST,
                                      'object': followed.ap_profile_id}}, True)

    assert UserFollower.query.count() == 0


def test_a_community_lookup_of_a_podcast_whose_user_is_banned_finds_nothing(podcast):
    podcast.user.banned = True
    db.session.commit()

    assert not find_actor_by_url(PODCAST, community_only=True)
    assert not find_actor_or_create(PODCAST, community_only=True, create_if_not_found=False)


def test_a_community_lookup_of_a_podcast_whose_user_is_deleted_finds_nothing(podcast):
    podcast.user.deleted = True
    db.session.commit()

    assert not find_actor_by_url(PODCAST, community_only=True)


def test_a_fetched_podcast_whose_user_is_banned_yields_no_community(podcast, http_mock):
    podcast.user.banned = True
    db.session.commit()
    http_mock.get(PODCAST).respond(json=podcast_document())

    assert create_actor_from_remote(PODCAST, community_only=True) is None


@pytest.mark.parametrize('field', ['banned', 'deleted'])
def test_a_cached_podcast_community_is_refused_once_its_user_is_banned_or_deleted(app, podcast, field, monkeypatch):
    monkeypatch.setitem(app.extensions['cache'], cache, SimpleCache())  # conftest's NullCache would never hit
    assert find_actor_or_create_cached(PODCAST, community_only=True) == podcast.community  # now cached

    setattr(podcast.user, field, True)
    db.session.commit()

    assert not find_actor_or_create_cached(PODCAST, community_only=True)

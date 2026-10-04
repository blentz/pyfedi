"""Interop D24, ruling R2: a Castopod podcast acting as a PERSON keeps person semantics. Its Community twin
(same ap_profile_id) must not swallow a person-follow's Accept/Reject, its boosts, or third-party mentions."""
from types import SimpleNamespace

import pytest

import app.activitypub.routes as routes_mod
from app import cache, db
from app.activitypub.routes import process_inbox_request
from app.activitypub.util import actor_json_to_model, find_community
from app.discovery.podcast import podcast_twin_user
from app.models import Community, CommunityMember, Post, User, UserFollower, UserFollowRequest
from tests.factories import make_community, make_community_join_request, make_site, make_user, \
    make_user_follow_request, peer_actor_json, peer_instance, seed_community_owner

PEER = 'peer.example'
PODCAST = f'https://{PEER}/u/mypodcast'


@pytest.fixture(autouse=True)
def fresh_cache():
    cache.clear()
    yield
    cache.clear()


def podcast_document():
    return peer_actor_json('Person', name='mypodcast', server=PEER,
                           fields={'type': 'Podcast', 'name': 'My Podcast', 'inbox': f'{PODCAST}/inbox',
                                   'outbox': f'{PODCAST}/outbox', 'followers': f'{PODCAST}/followers'})


@pytest.fixture
def world(app, db_session):
    seed_community_owner('local.example')   # instance 1 and user 1
    make_site()
    peer_instance(PEER)
    podcast = actor_json_to_model(podcast_document(), 'mypodcast', PEER)
    alice = actor_json_to_model(peer_actor_json('Person', name='alice', server=PEER), 'alice', PEER)
    return SimpleNamespace(podcast=podcast, alice=alice,
                           community=Community.query.filter_by(ap_profile_id=PODCAST).one())


def local_user(app, name):
    user = make_user(None, name, local=True)
    user.ap_profile_id = f"https://{app.config['SERVER_NAME']}/u/{name}"
    db.session.commit()
    return user


def follow_reply(app, kind, follower):
    return {'id': f'{PODCAST}/activities/{kind.lower()}/1', 'type': kind, 'actor': PODCAST,
            'object': {'id': f"https://{app.config['SERVER_NAME']}/activities/follow/1",
                       'type': 'Follow', 'actor': follower.ap_profile_id, 'object': PODCAST}}


# ---- R1: the helper -------------------------------------------------------------------------------------------

def test_the_twin_user_of_a_podcast_community_is_the_podcast(world):
    assert podcast_twin_user(world.community) == world.podcast


def test_an_ordinary_community_has_no_twin_user(world):
    assert podcast_twin_user(make_community('ordinary')) is None
    assert podcast_twin_user(None) is None


# ---- F1: a person-follow's Accept / Reject ----------------------------------------------------------------------

def test_an_accept_of_a_person_follow_creates_a_user_follower_and_no_membership(app, world):
    follower = local_user(app, 'follower')
    make_user_follow_request(follower, world.podcast)

    process_inbox_request(follow_reply(app, 'Accept', follower), True)

    follow = UserFollower.query.filter_by(local_user_id=follower.id, remote_user_id=world.podcast.id).one()
    assert follow.is_accepted is True and follow.is_inward is False
    assert CommunityMember.query.filter_by(user_id=follower.id).count() == 0
    db.session.expire_all()
    assert db.session.get(User, follower.id).num_following == 1


def test_a_reject_of_a_person_follow_removes_the_request(app, world):
    follower = local_user(app, 'follower')
    make_user_follow_request(follower, world.podcast)

    process_inbox_request(follow_reply(app, 'Reject', follower), True)

    assert UserFollowRequest.query.count() == 0
    assert CommunityMember.query.filter_by(user_id=follower.id).count() == 0


def test_an_accept_of_a_join_request_still_admits_the_join(app, world):
    joiner = local_user(app, 'joiner')
    make_community_join_request(joiner, world.community)

    process_inbox_request(follow_reply(app, 'Accept', joiner), True)

    assert CommunityMember.query.filter_by(user_id=joiner.id, community_id=world.community.id).count() == 1
    assert UserFollower.query.count() == 0


# ---- F2: a podcast's boost is a person's boost ------------------------------------------------------------------

def test_a_podcast_boost_of_someone_elses_post_is_a_microblog_boost(app, world, monkeypatch):
    seen = []
    monkeypatch.setattr(routes_mod, 'process_announce_of_uri',
                        lambda request_json, community, id, store: seen.append(community))

    process_inbox_request({'id': f'{PODCAST}/activities/announce/1', 'type': 'Announce', 'actor': PODCAST,
                           'object': f'https://{PEER}/users/alice/statuses/1'}, True)

    assert seen == [None]   # the microblog-boost path, not a post filed in the podcast's community


# ---- F3: a third party's mention of a podcast is not a post in the podcast's community --------------------------

def note_create(author_id, note_id, cc):
    obj = {'id': note_id, 'type': 'Note', 'content': '<p>hello @mypodcast</p>', 'attributedTo': author_id,
           'to': ['https://www.w3.org/ns/activitystreams#Public'], 'cc': cc}
    return {'id': f'{note_id}/activity', 'type': 'Create', 'actor': author_id,
            'to': ['https://www.w3.org/ns/activitystreams#Public'], 'cc': cc, 'object': obj}


def test_find_community_skips_a_podcast_twin_named_by_someone_else(world):
    assert find_community(note_create(world.alice.ap_profile_id, f'https://{PEER}/n/1', [PODCAST])) is None
    assert find_community(note_create(world.alice.ap_profile_id, f'https://{PEER}/n/1', [PODCAST])['object']) is None


def test_find_community_keeps_a_podcast_twin_named_by_the_podcast_itself(world):
    assert find_community(note_create(PODCAST, f'https://{PEER}/n/2', [PODCAST])) == world.community


def test_a_mention_of_a_podcast_by_another_user_lands_in_microblogs(world):
    process_inbox_request(note_create(world.alice.ap_profile_id, f'https://{PEER}/n/3', [PODCAST]), True)

    assert Post.query.one().community.name == 'microblogs'


def test_an_episode_from_the_podcast_still_lands_in_its_community(world):
    process_inbox_request(note_create(PODCAST, f'https://{PEER}/n/4', [f'{PODCAST}/followers']), True)

    assert Post.query.one().community_id == world.community.id

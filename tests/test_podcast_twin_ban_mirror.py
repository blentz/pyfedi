"""Interop D24, ruling R3: a ban or deletion of a podcast's User is mirrored onto its Community twin (banned=True),
so every community read path hides it without a per-path check; an unban mirrors back."""
from datetime import timedelta
from types import SimpleNamespace

import pytest
from flask import g
from werkzeug.exceptions import NotFound

from app import cache, db
from app.activitypub.routes import process_delete_request
from app.activitypub.util import actor_json_to_model
from app.community.routes import do_subscribe
from app.models import Community, CommunityJoinRequest, CommunityMember, Site, User, utcnow
from app.shared.tasks.maintenance import unban_expired_users
from tests.factories import make_community, peer_actor_json, peer_instance

PEER = 'peer.example'
PODCAST = f'https://{PEER}/u/mypodcast'
HANDLE = f'mypodcast@{PEER}'


@pytest.fixture
def world(app, api_baseline):
    cache.clear()
    g.site = db.session.get(Site, 1)
    g.site.private_instance = False
    peer_instance(PEER)
    podcast = actor_json_to_model(peer_actor_json('Person', name='mypodcast', server=PEER,
                                                  fields={'type': 'Podcast', 'name': 'My Podcast'}),
                                  'mypodcast', PEER)
    community = Community.query.filter_by(ap_profile_id=PODCAST).one()
    community.post_count = 1
    db.session.commit()
    yield SimpleNamespace(client=app.test_client(), podcast=podcast, community=community,
                          reader=api_baseline.user3)
    cache.clear()


def twin_banned():
    db.session.expire_all()
    return Community.query.filter_by(ap_profile_id=PODCAST).one().banned


@pytest.mark.parametrize('field', ['banned', 'deleted'])
def test_banning_or_deleting_the_podcast_user_bans_its_community(world, field):
    setattr(world.podcast, field, True)
    db.session.commit()

    assert twin_banned() is True


@pytest.mark.parametrize('field', ['banned', 'deleted'])
def test_unbanning_or_undeleting_the_podcast_user_restores_its_community(world, field):
    setattr(world.podcast, field, True)
    db.session.commit()
    setattr(world.podcast, field, False)
    db.session.commit()

    assert twin_banned() is False


def test_unbanning_a_banned_and_deleted_user_keeps_the_community_hidden_until_both_clear(world):
    world.podcast.banned = True
    world.podcast.deleted = True
    db.session.commit()
    world.podcast.banned = False
    db.session.commit()
    assert twin_banned() is True

    world.podcast.deleted = False
    db.session.commit()
    assert twin_banned() is False


def test_an_ordinary_users_ban_touches_no_community(world):
    ordinary = make_community('ordinary')
    alice = actor_json_to_model(peer_actor_json('Person', name='alice', server=PEER), 'alice', PEER)
    alice.banned = True
    db.session.commit()

    db.session.expire_all()
    assert db.session.get(Community, ordinary.id).banned is False
    assert twin_banned() is False


def test_an_expired_temporary_ban_unbans_the_twin(world):
    world.podcast.banned = True
    world.podcast.banned_until = utcnow() - timedelta(days=1)
    db.session.commit()
    assert twin_banned() is True

    unban_expired_users()

    db.session.expire_all()
    assert db.session.get(User, world.podcast.id).banned is False
    assert twin_banned() is False


@pytest.mark.parametrize('field', ['banned', 'deleted'])
def test_the_community_page_is_gone_once_the_podcast_user_is_banned_or_deleted(world, field):
    assert world.client.get(f'/c/{HANDLE}').status_code == 200

    setattr(world.podcast, field, True)
    db.session.commit()

    assert world.client.get(f'/c/{HANDLE}').status_code == 404


@pytest.mark.parametrize('field', ['banned', 'deleted'])
def test_the_communities_listing_drops_it_once_the_podcast_user_is_banned_or_deleted(world, field):
    assert 'My Podcast' in world.client.get('/communities').get_data(as_text=True)

    setattr(world.podcast, field, True)
    db.session.commit()

    assert 'My Podcast' not in world.client.get('/communities').get_data(as_text=True)


def test_joining_is_admitted_while_the_podcast_user_is_in_good_standing(world):
    do_subscribe(HANDLE, world.reader.id)

    assert CommunityJoinRequest.query.filter_by(user_id=world.reader.id).count() == 1


@pytest.mark.parametrize('field', ['banned', 'deleted'])
def test_joining_is_refused_once_the_podcast_user_is_banned_or_deleted(world, field):
    setattr(world.podcast, field, True)
    db.session.commit()

    with pytest.raises(NotFound):   # do_subscribe's "community not found" answer
        do_subscribe(HANDLE, world.reader.id)

    assert CommunityJoinRequest.query.count() == 0
    assert CommunityMember.query.filter_by(user_id=world.reader.id, community_id=world.community.id).count() == 0


def test_an_actor_delete_from_the_podcast_bans_its_community(world):
    process_delete_request({'id': f'{PODCAST}#delete', 'type': 'Delete', 'actor': PODCAST, 'object': PODCAST,
                            'to': ['https://www.w3.org/ns/activitystreams#Public']}, True)

    assert twin_banned() is True

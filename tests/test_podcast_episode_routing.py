"""Interop D24 (spec D4): a Castopod episode Note is a post in the podcast's community, not in
`microblogs`; the episode audio (WP-C, C1) keeps arriving."""
from types import SimpleNamespace

import pytest

from app.activitypub.routes import process_new_content
from app.activitypub.util import actor_json_to_model
from app.api.alpha.utils.misc import get_resolve_object
from app.discovery.podcast import podcast_community_for
from app import db
from flask import current_app as app
from app.models import ActivityPubLog, Community, Post, PostReply
from tests.factories import make_site, peer_actor_json, peer_instance, seed_community_owner

PEER = 'peer.example'
PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'
EPISODE = f'https://{PEER}/@mypodcast/episodes/ep-1'
AUDIO = f'https://{PEER}/media/ep1.mp3'
COVER = f'https://{PEER}/media/ep1.jpg'
ANNOUNCEMENT = f'<a href="{EPISODE}">Episode 1: Hello</a><br/><p>New episode is out!</p>'


@pytest.fixture
def world(app, db_session):
    seed_community_owner('local.example')   # instance 1 and user 1, which the microblogs community needs
    make_site()
    peer_instance(PEER)
    # no rssFeed: credits (a later task) fetch nothing here
    podcast = actor_json_to_model(peer_actor_json('Person', name='mypodcast', server=PEER,
                                                  fields={'type': 'Podcast', 'name': 'My Podcast'}),
                                  'mypodcast', PEER)
    alice = actor_json_to_model(peer_actor_json('Person', name='alice', server=PEER), 'alice', PEER)
    return SimpleNamespace(podcast=podcast, alice=alice)


def create(author, note_id, content='<p>new episode</p>', in_reply_to=None):
    obj = {'id': note_id, 'type': 'Note', 'content': content, 'attributedTo': author.ap_profile_id,
           'to': [PUBLIC], 'cc': []}
    if in_reply_to:
        obj['inReplyTo'] = in_reply_to
    return {'id': f'{note_id}/activity', 'type': 'Create', 'actor': author.ap_profile_id, 'to': [PUBLIC], 'cc': [],
            'object': obj}


def test_an_episode_from_a_podcast_lands_in_its_community(world):
    process_new_content(world.podcast, None, False, create(world.podcast, f'https://{PEER}/n/1'), False)

    post = Post.query.one()
    assert post.community_id == podcast_community_for(world.podcast).id
    assert post.user_id == world.podcast.id


def test_a_note_from_a_person_still_goes_to_microblogs(world):
    process_new_content(world.alice, None, False, create(world.alice, f'https://{PEER}/n/2'), False)

    assert Post.query.one().community.name == 'microblogs'


def test_a_reply_from_a_podcast_keeps_the_reply_routing(world):
    process_new_content(world.alice, None, False, create(world.alice, f'https://{PEER}/n/3'), False)
    parent = Post.query.one()

    process_new_content(world.podcast, None, False,
                        create(world.podcast, f'https://{PEER}/n/4', in_reply_to=parent.ap_id), False)

    assert Post.query.count() == 1                        # no new post, in particular none in the podcast community
    assert PostReply.query.one().community_id == parent.community_id   # the reply stays in microblogs


def test_the_episode_audio_still_arrives(world, http_mock):
    http_mock.get(EPISODE).respond(json={
        'id': EPISODE, 'type': 'PodcastEpisode', 'attributedTo': world.podcast.ap_profile_id,
        'image': {'type': 'Image', 'mediaType': 'image/jpeg', 'url': COVER},
        'audio': {'id': AUDIO, 'type': 'Audio', 'url': {'href': AUDIO, 'type': 'Link', 'mediaType': 'audio/mpeg'}}})
    http_mock.get(COVER).respond(404)

    process_new_content(world.podcast, None, False,
                        create(world.podcast, f'https://{PEER}/@mypodcast/posts/1', ANNOUNCEMENT), False)

    post = Post.query.one()
    assert post.url == AUDIO
    assert post.community_id == podcast_community_for(world.podcast).id


def test_a_banned_podcast_community_drops_its_episode(world, monkeypatch):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    podcast_community_for(world.podcast).banned = True
    db.session.commit()

    process_new_content(world.podcast, None, False, create(world.podcast, f'https://{PEER}/n/5'), False)

    assert Post.query.count() == 0
    assert ActivityPubLog.query.filter_by(result='ignored').count() == 1


def test_a_podcast_without_a_community_row_goes_to_microblogs(world):
    Community.query.filter_by(ap_profile_id=world.podcast.ap_profile_id).delete()
    db.session.commit()

    process_new_content(world.podcast, None, False, create(world.podcast, f'https://{PEER}/n/6'), False)

    assert Post.query.one().community.name == 'microblogs'


def test_the_api_resolves_a_podcasts_own_episode_into_its_community(world, http_mock):
    note_id = f'https://{PEER}/@mypodcast/posts/9'
    note = create(world.podcast, note_id)['object']
    http_mock.get(note_id).respond(json=note)

    post = get_resolve_object(None, {'q': note_id}, user_id=1, recursive=True)

    assert isinstance(post, Post)
    assert post.community_id == podcast_community_for(world.podcast).id
    assert post.user_id == world.podcast.id


def test_the_api_resolves_a_podcasts_episode_addressed_to_itself_into_its_community(world, http_mock):
    note_id = f'https://{PEER}/@mypodcast/posts/10'
    note = create(world.podcast, note_id)['object']
    note['cc'] = [world.podcast.ap_profile_id]
    http_mock.get(note_id).respond(json=note)

    post = get_resolve_object(None, {'q': note_id}, user_id=1, recursive=True)

    assert post.community_id == podcast_community_for(world.podcast).id


def test_the_api_keeps_a_third_partys_note_naming_a_podcast_out_of_its_community(world, http_mock):
    note_id = f'https://{PEER}/@alice/posts/11'
    note = create(world.alice, note_id)['object']
    note['cc'] = [world.podcast.ap_profile_id]   # a mention of the podcast, as a person
    http_mock.get(note_id).respond(json=note)

    try:
        get_resolve_object(None, {'q': note_id}, user_id=1, recursive=True)
    except Exception:
        pass

    assert Post.query.filter_by(community_id=podcast_community_for(world.podcast).id).count() == 0

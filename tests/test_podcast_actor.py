"""G1: Castopod's `Podcast` actor type is accepted as an author."""
from tests.factories import make_community, make_site, peer_actor_json, peer_instance

PEER = 'peer.example'
PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'


def _podcast_document():
    return peer_actor_json('Person', name='mypodcast', server=PEER,
                           fields={'type': 'Podcast', 'rssFeed': f'https://{PEER}/feed.xml',
                                   'language': 'en', 'category': 'Technology', 'episodes': f'https://{PEER}/episodes'})


def test_a_podcast_actor_becomes_a_user(db_session):
    """Castopod's custom `Podcast` actor type is accepted as a person-like author, not a bot"""
    from app.activitypub.util import actor_json_to_model
    from app.models import User
    peer_instance(PEER)

    user = actor_json_to_model(_podcast_document(), 'mypodcast', PEER)

    assert user is not None
    assert User.query.count() == 1
    assert user.bot is False
    assert user.ap_profile_id == f'https://{PEER}/u/mypodcast'


def test_a_note_from_a_podcast_actor_ingests(db_session):
    """A Create{Note} authored by a freshly-resolved Podcast actor is stored"""
    from app.activitypub.util import actor_json_to_model, create_post
    from app.models import Post
    peer_instance(PEER)
    author = actor_json_to_model(_podcast_document(), 'mypodcast', PEER)
    community = make_community()
    make_site()
    activity = {'id': f'https://{PEER}/n/1/activity', 'type': 'Create',
                'object': {'id': f'https://{PEER}/n/1', 'type': 'Note', 'content': '<p>new episode</p>',
                           'attributedTo': author.ap_profile_id, 'to': [PUBLIC], 'cc': []}}

    result = create_post(False, community, activity, author)

    assert result is not None
    assert Post.query.count() == 1


# An embedded `attributedTo` list naming the Podcast actor as a dict. Each walk
# takes the first Person-like dict; a Group on another host comes first, so a
# walk that skipped the Podcast would find no author on the object's host.

def _attributed_to(author_uri):
    return [{'type': 'Group', 'id': 'https://elsewhere.example/c/news'},
            {'type': 'Podcast', 'id': author_uri}]


def test_ensure_domains_match_takes_an_embedded_podcast_author(db_session):
    from app.activitypub.util import ensure_domains_match
    from tests.factories import PEER_ACTOR_URI, PEER_OBJECT_URI

    assert ensure_domains_match({'id': PEER_OBJECT_URI, 'attributedTo': _attributed_to(PEER_ACTOR_URI)})


def test_verify_object_from_source_takes_an_embedded_podcast_author(app, db_session, http_mock, no_real_sleeping):
    from app.activitypub.util import verify_object_from_source
    from tests.factories import PEER_ACTOR_URI, PEER_OBJECT_URI, announce_activity, note_document
    http_mock.get(PEER_OBJECT_URI).respond(200, json=note_document(attributed_to=_attributed_to(PEER_ACTOR_URI)))
    request_json = announce_activity()

    result, reason = verify_object_from_source(request_json)

    assert reason is None
    assert result is request_json


def test_create_resolved_object_takes_an_embedded_podcast_author(app, db_session):
    from app.activitypub.util import create_resolved_object
    from app.models import Post
    from tests.factories import (AS_PUBLIC_URI, PEER_OBJECT_HOST, PEER_OBJECT_URI, note_document,
                                 resolvable_remote_author, seed_community_owner)
    make_site()
    podcast = resolvable_remote_author(seed_community_owner(PEER_OBJECT_HOST), 'alice')
    community = make_community('news', host=PEER_OBJECT_HOST)
    post_data = note_document(attributed_to=_attributed_to(f'https://{PEER_OBJECT_HOST}/users/alice'),
                              uri=PEER_OBJECT_URI, fields={'to': [AS_PUBLIC_URI]})

    result = create_resolved_object(PEER_OBJECT_URI, post_data, PEER_OBJECT_HOST, community, None, False)

    assert result is not None
    assert result.user_id == podcast.id
    assert Post.query.filter_by(ap_id=PEER_OBJECT_URI).count() == 1

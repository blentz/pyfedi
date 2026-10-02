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

"""Interop D24 (spec D4): a Castopod Podcast actor is a User, because it authors the episodes, and a
Community, because it is what a reader subscribes to. Both rows carry the same ActivityPub id."""
from app.activitypub.util import actor_json_to_model, refresh_user_profile_task
from app.discovery.podcast import ensure_podcast_community, podcast_community_for
from app import db
from app.models import Community, User
from tests.factories import make_site, make_user, peer_actor_json, peer_instance

PEER = 'peer.example'
ACTOR = f'https://{PEER}/u/mypodcast'
FEED = f'https://{PEER}/@mypodcast/feed.xml'


def podcast_document(**fields):
    values = {'type': 'Podcast', 'name': 'My Podcast', 'rssFeed': FEED, 'inbox': f'{ACTOR}/inbox',
              'outbox': f'{ACTOR}/outbox', 'followers': f'{ACTOR}/followers'}
    values.update(fields)
    return peer_actor_json('Person', name='mypodcast', server=PEER, fields=values)


def test_a_podcast_actor_gets_a_user_and_a_community_with_one_id(app, db_session):
    peer_instance(PEER)

    user = actor_json_to_model(podcast_document(), 'mypodcast', PEER)

    community = Community.query.one()
    assert isinstance(user, User)
    assert community.ap_profile_id == user.ap_profile_id == ACTOR
    assert community.ap_id == user.ap_id
    assert community.user_id == user.id and community.instance_id == user.instance_id
    assert community.title == 'My Podcast'
    assert community.rss_url == FEED
    assert community.ap_outbox_url == f'{ACTOR}/outbox'
    assert community.ap_followers_url == f'{ACTOR}/followers'
    assert community.ap_fetched_at is not None
    assert podcast_community_for(user).id == community.id


def test_a_person_gets_no_community(app, db_session):
    peer_instance(PEER)

    user = actor_json_to_model(peer_actor_json('Person', name='alice', server=PEER), 'alice', PEER)

    assert Community.query.count() == 0
    assert podcast_community_for(user) is None


def test_seeing_the_podcast_again_does_not_duplicate_and_keeps_the_feed_current(app, db_session):
    peer_instance(PEER)
    actor_json_to_model(podcast_document(), 'mypodcast', PEER)

    actor_json_to_model(podcast_document(rssFeed=f'https://{PEER}/@mypodcast/feed2.xml'), 'mypodcast', PEER)

    assert Community.query.one().rss_url == f'https://{PEER}/@mypodcast/feed2.xml'
    assert User.query.count() == 1


def test_a_feed_that_is_not_https_is_not_kept(app, db_session):
    peer_instance(PEER)

    actor_json_to_model(podcast_document(rssFeed=f'http://{PEER}/feed.xml'), 'mypodcast', PEER)

    assert Community.query.one().rss_url is None


def test_a_sensitive_podcast_on_a_site_without_nsfw_stays_a_user_only(app, db_session):
    make_site()   # enable_nsfw defaults to False
    peer_instance(PEER)

    user = actor_json_to_model(podcast_document(sensitive=True), 'mypodcast', PEER)

    assert user is not None
    assert Community.query.count() == 0


def test_a_podcast_known_from_before_gets_its_community_on_refresh(app, db_session):
    user = make_user(peer_instance(PEER), 'mypodcast')

    refresh_user_profile_task(user.id, podcast_document(id=user.ap_profile_id))

    assert Community.query.one().ap_profile_id == user.ap_profile_id


def test_a_local_user_never_gets_a_podcast_community(app, db_session):
    local = make_user(peer_instance('local.example'), 'me', local=True)

    assert ensure_podcast_community(local, {'type': 'Podcast'}) is None
    assert podcast_community_for(local) is None


def a_known_podcast_user(**flags):
    user = make_user(peer_instance(PEER), 'mypodcast')
    for flag, value in flags.items():
        setattr(user, flag, value)
    db.session.commit()
    return user


def test_a_banned_podcast_user_gets_no_community_when_seen_again(app, db_session):
    user = a_known_podcast_user(banned=True)

    actor_json_to_model(podcast_document(id=user.ap_profile_id), 'mypodcast', PEER)

    assert Community.query.count() == 0
    assert ensure_podcast_community(user, podcast_document(id=user.ap_profile_id)) is None


def test_a_banned_podcast_user_gets_no_community_on_refresh(app, db_session):
    user = a_known_podcast_user(banned=True)

    refresh_user_profile_task(user.id, podcast_document(id=user.ap_profile_id))

    assert Community.query.count() == 0


def test_a_deleted_podcast_user_gets_no_community(app, db_session):
    user = a_known_podcast_user(deleted=True)

    assert ensure_podcast_community(user, podcast_document(id=user.ap_profile_id)) is None
    assert Community.query.count() == 0


def test_a_banned_podcast_community_is_not_handed_out(app, db_session):
    peer_instance(PEER)
    user = actor_json_to_model(podcast_document(), 'mypodcast', PEER)
    community = Community.query.one()
    community.banned = True
    db.session.commit()

    assert ensure_podcast_community(user, podcast_document()) is None
    assert podcast_community_for(user) is None
    assert Community.query.count() == 1

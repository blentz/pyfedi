"""Interop D24 (spec D4): a Castopod Podcast actor is a User, because it authors the episodes, and a
Community, because it is what a reader subscribes to. Both rows carry the same ActivityPub id."""
from types import SimpleNamespace

import pytest
from flask import url_for

import app.discovery.podcast as podcast_module
from app.activitypub.util import actor_json_to_model, refresh_user_profile_task
from app.discovery.podcast import PODCAST_DROP, ensure_podcast_community, podcast_community_for, podcast_route_for
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


# ---- M3: a feed is taken only from the podcast's own host ------------------------------------------------------

@pytest.mark.parametrize('feed', ['https://elsewhere.example/@mypodcast/feed.xml',
                                  'https://peer.example.evil.example/feed.xml'])
def test_a_feed_on_another_host_is_not_kept(app, db_session, feed):
    peer_instance(PEER)

    actor_json_to_model(podcast_document(rssFeed=feed), 'mypodcast', PEER)

    assert Community.query.one().rss_url is None


def test_a_feed_on_the_podcasts_own_host_is_kept_whatever_its_case(app, db_session):
    peer_instance(PEER)

    actor_json_to_model(podcast_document(rssFeed='https://PEER.example/@mypodcast/feed.xml'), 'mypodcast', PEER)

    assert Community.query.one().rss_url == 'https://PEER.example/@mypodcast/feed.xml'


def test_an_existing_feed_is_not_replaced_by_one_on_another_host(app, db_session):
    peer_instance(PEER)
    actor_json_to_model(podcast_document(), 'mypodcast', PEER)

    actor_json_to_model(podcast_document(rssFeed='https://elsewhere.example/feed.xml'), 'mypodcast', PEER)

    assert Community.query.one().rss_url == FEED


# ---- M6: podcast_route_for with a twin whose User is banned or deleted ------------------------------------------

@pytest.mark.parametrize('field', ['banned', 'deleted'])
def test_a_banned_or_deleted_podcast_user_with_a_twin_drops_its_episodes(app, db_session, field):
    peer_instance(PEER)
    user = actor_json_to_model(podcast_document(), 'mypodcast', PEER)
    setattr(user, field, True)
    db.session.commit()
    Community.query.one().banned = False   # even were the twin itself not banned, the User's state decides
    db.session.commit()

    assert podcast_route_for(user) is PODCAST_DROP


def test_a_podcast_in_good_standing_routes_to_its_twin(app, db_session):
    peer_instance(PEER)
    user = actor_json_to_model(podcast_document(), 'mypodcast', PEER)

    assert podcast_route_for(user) == Community.query.one()


# ---- D1: an existing podcast community follows its actor document ---------------------------------------------

def an_enabled_nsfw_site():
    site = make_site()
    site.enable_nsfw = True
    db.session.commit()


def a_changed_document(**fields):
    return podcast_document(name='Renamed Podcast', summary='<p>New about</p>', sensitive=True,
                            outbox=f'{ACTOR}/outbox2', rssFeed=f'https://{PEER}/@mypodcast/feed2.xml', **fields)


def assert_resynced(community):
    assert community.title == 'Renamed Podcast'
    assert community.description == 'New about'
    assert 'New about' in community.description_html
    assert community.nsfw is True
    assert community.ap_outbox_url == f'{ACTOR}/outbox2'
    assert community.rss_url == f'https://{PEER}/@mypodcast/feed2.xml'


def test_seeing_the_podcast_again_resyncs_its_community_from_the_document(app, db_session):
    an_enabled_nsfw_site()
    peer_instance(PEER)
    actor_json_to_model(podcast_document(), 'mypodcast', PEER)

    actor_json_to_model(a_changed_document(), 'mypodcast', PEER)

    assert_resynced(Community.query.one())


def test_a_refresh_resyncs_the_podcast_community_from_the_document(app, db_session):
    an_enabled_nsfw_site()
    peer_instance(PEER)
    user = actor_json_to_model(podcast_document(), 'mypodcast', PEER)

    refresh_user_profile_task(user.id, a_changed_document(id=user.ap_profile_id))

    assert_resynced(Community.query.one())


def test_a_resync_cleans_the_document_as_creation_does(app, db_session):
    peer_instance(PEER)
    user = actor_json_to_model(podcast_document(), 'mypodcast', PEER)

    ensure_podcast_community(user, podcast_document(name='  [deleted] ', summary='<script>x</script><p>ok</p>',
                                                    outbox='http://peer.example/outbox'))

    community = Community.query.one()
    assert community.title == 'mypodcast'
    assert '<script>' not in community.description_html
    assert community.ap_outbox_url == f'{ACTOR}/outbox'   # a document's bad outbox does not erase a good one


def test_a_resync_leaves_the_ban_alone(app, db_session):
    peer_instance(PEER)
    user = actor_json_to_model(podcast_document(), 'mypodcast', PEER)
    Community.query.one().banned = True
    db.session.commit()

    assert ensure_podcast_community(user, a_changed_document()) is None

    community = Community.query.one()
    assert community.banned is True and community.title == 'My Podcast'


class _FirstCommunityLookupMisses:
    """The session, except that the first Community lookup finds nothing: another worker's insert lands between
    ensure_podcast_community's lookup and its commit."""
    def __init__(self, session):
        self._session, self._missed = session, False

    def query(self, *entities):
        if entities == (Community,) and not self._missed:
            self._missed = True
            return SimpleNamespace(filter=lambda *criteria: SimpleNamespace(first=lambda: None))
        return self._session.query(*entities)

    def __getattr__(self, name):
        return getattr(self._session, name)


def test_a_community_created_concurrently_is_handed_out_after_the_insert_fails(app, db_session, monkeypatch):
    peer_instance(PEER)
    user = actor_json_to_model(podcast_document(), 'mypodcast', PEER)
    existing_id = Community.query.one().id
    monkeypatch.setattr(podcast_module, 'db', SimpleNamespace(session=_FirstCommunityLookupMisses(db.session)))

    community = ensure_podcast_community(user, podcast_document())

    assert community.id == existing_id
    assert Community.query.count() == 1


KALIMERA_HOST = 'pirate.mxtthxw.art'
KALIMERA = f'https://{KALIMERA_HOST}/@thekalimerashow'


def kalimera_document():
    return peer_actor_json('Person', name='thekalimerashow', server=KALIMERA_HOST,
                           fields={'id': KALIMERA, 'type': 'Podcast', 'name': 'The Kalimera Show',
                                   'inbox': f'{KALIMERA}/inbox', 'outbox': f'{KALIMERA}/outbox'})


def test_a_twin_gets_a_remote_groups_ap_id_not_the_users(app, db_session):
    """A podcast found by its handle is stored with a User ap_id of '@name@host'; its twin is 'name@host', as
    actor_json_to_model stores every other remote community."""
    peer_instance(KALIMERA_HOST)

    user = actor_json_to_model(kalimera_document(), '@thekalimerashow', KALIMERA_HOST)

    community = Community.query.one()
    assert user.ap_id == '@thekalimerashow@pirate.mxtthxw.art'
    assert community.ap_id == 'thekalimerashow@pirate.mxtthxw.art'


def test_a_twin_stored_with_the_old_at_prefixed_ap_id_is_corrected_on_resync(app, db_session):
    peer_instance(KALIMERA_HOST)
    actor_json_to_model(kalimera_document(), '@thekalimerashow', KALIMERA_HOST)
    community = Community.query.one()
    community.ap_id = '@thekalimerashow@pirate.mxtthxw.art'
    db.session.commit()

    actor_json_to_model(kalimera_document(), '@thekalimerashow', KALIMERA_HOST)

    assert Community.query.one().ap_id == 'thekalimerashow@pirate.mxtthxw.art'


def test_the_twins_link_and_url_have_no_at_prefix_and_resolve_to_it(app, db_session):
    peer_instance(KALIMERA_HOST)
    actor_json_to_model(kalimera_document(), '@thekalimerashow', KALIMERA_HOST)
    community = Community.query.one()

    with app.test_request_context():
        url = url_for('activitypub.community_profile', actor=community.link())

    assert community.link() == 'thekalimerashow@pirate.mxtthxw.art'
    assert url == '/c/thekalimerashow@pirate.mxtthxw.art'
    assert Community.query.filter_by(ap_id=community.link(), banned=False).one().id == community.id

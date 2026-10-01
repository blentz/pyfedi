"""The alpha API over HTTP: communities and feeds.

Sub-project 85, slice C -- the seventeen community routes and the four feed
routes slice B did not reach, driven through the test client with the API
switched on so that each route's body runs and every response is validated
against the schema its route declares.
"""
import pytest
from flask import current_app, g

from app import db
from app.models import (Community, CommunityFlair, CommunityMember, Feed,
                        FeedMember, InstanceBan, Language, Site)
from tests.factories import (a_keypair, make_community, make_community_ban,
                             make_community_flair, make_community_member,
                             make_local_feed, make_post, make_user)

MISSING = 999999


def auth(user):
    return {'Authorization': f'Bearer {user.encode_jwt_token()}'}


@pytest.fixture
def env(app, api_baseline, monkeypatch):
    """A local community `probeland` that `moderator` moderates, a post in it,
    and the API switched on."""
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    monkeypatch.setitem(current_app.config, 'ENABLE_ALPHA_API', 'true')
    for code, name in [('en', 'English'), ('und', 'Undetermined')]:
        db.session.add(Language(code=code, name=name))
    db.session.commit()
    db.session.get(Site, 1).language_id = Language.query.filter_by(
        code='en').one().id
    community = make_community('probeland')
    moderator = api_baseline.user2
    stranger = api_baseline.user3
    target = api_baseline.user4
    for who in (moderator, stranger, target):
        who.private_key, who.public_key = a_keypair()
    db.session.commit()
    membership = make_community_member(moderator, community,
                                       is_moderator=True)
    membership.is_owner = True
    make_community_member(target, community)
    post = make_post(community, target, ap_id='https://test.piefed.local/u/1')
    db.session.commit()
    return SimpleNamespace(client=app.test_client(), community=community,
                           moderator=moderator, stranger=stranger,
                           target=target, post=post,
                           admin=api_baseline.user1,
                           baseline=api_baseline)


class TestReadingCommunities:
    def test_the_listing(self, env):
        response = env.client.get('/api/alpha/community/list')
        assert response.status_code == 200
        assert 'communities' in response.get_json()

    def test_one_community_by_id(self, env):
        response = env.client.get('/api/alpha/community',
                                  query_string={'id': env.community.id})
        assert response.status_code == 200
        assert response.get_json()['community_view'][
            'community']['id'] == env.community.id

    def test_one_community_by_name(self, env):
        response = env.client.get(
            '/api/alpha/community',
            query_string={'name': 'probeland@test.piefed.local'})
        assert response.status_code == 200

    def test_a_community_nobody_holds(self, env):
        response = env.client.get('/api/alpha/community',
                                  query_string={'id': MISSING})
        assert response.status_code == 400
        assert 'unknown community' in response.get_json()['message']


class TestJoiningAndLeaving:
    def test_joining(self, env):
        response = env.client.post('/api/alpha/community/follow',
                                   headers=auth(env.stranger),
                                   json={'community_id': env.community.id,
                                         'follow': True})
        assert response.status_code == 200
        assert CommunityMember.query.filter_by(
            user_id=env.stranger.id,
            community_id=env.community.id).count() == 1

    def test_joining_is_refused_to_a_user_banned_from_the_whole_instance(self, env):
        """D995, owner ruling: the API's follow gate read only CommunityBan
        rows, so an instance-banned user could join."""
        db.session.add(InstanceBan(user_id=env.stranger.id,
                                   instance_id=env.community.instance_id))
        db.session.commit()

        response = env.client.post('/api/alpha/community/follow',
                                   headers=auth(env.stranger),
                                   json={'community_id': env.community.id,
                                         'follow': True})

        assert response.status_code == 400
        assert CommunityMember.query.filter_by(
            user_id=env.stranger.id,
            community_id=env.community.id).count() == 0

    def test_joining_a_community_nobody_holds(self, env):
        response = env.client.post('/api/alpha/community/follow',
                                   headers=auth(env.stranger),
                                   json={'community_id': MISSING,
                                         'follow': True})
        assert response.status_code == 400
        assert response.get_json()['message'] == 'community not found'

    def test_leaving(self, env):
        response = env.client.post('/api/alpha/community/follow',
                                   headers=auth(env.target),
                                   json={'community_id': env.community.id,
                                         'follow': False})
        assert response.status_code == 200
        assert CommunityMember.query.filter_by(
            user_id=env.target.id,
            community_id=env.community.id).count() == 0

    def test_leaving_everything(self, env):
        response = env.client.post('/api/alpha/community/leave_all',
                                   headers=auth(env.target), json={})
        assert response.status_code == 200
        assert CommunityMember.query.filter_by(
            user_id=env.target.id).count() == 0

    def test_leaving_everything_without_an_account(self, env):
        response = env.client.post('/api/alpha/community/leave_all', json={})
        assert response.status_code == 400

    def test_blocking_and_unblocking(self, env):
        for block in (True, False):
            response = env.client.post('/api/alpha/community/block',
                                       headers=auth(env.stranger),
                                       json={'community_id': env.community.id,
                                             'block': block})
            assert response.status_code == 200

    def test_subscribing_and_unsubscribing(self, env):
        for subscribe in (True, False):
            response = env.client.put('/api/alpha/community/subscribe',
                                      headers=auth(env.target),
                                      json={'community_id': env.community.id,
                                            'subscribe': subscribe})
            assert response.status_code == 200


class TestWritingCommunities:
    def test_creating_one(self, env):
        maker = env.admin
        maker.private_key, maker.public_key = a_keypair()
        db.session.commit()
        response = env.client.post('/api/alpha/community',
                                   headers=auth(maker),
                                   json={'name': 'newland',
                                         'title': 'new land'})
        assert response.status_code == 200
        assert Community.query.filter_by(name='newland').count() == 1

    def test_creating_one_without_a_name(self, env):
        maker = env.admin
        maker.private_key, maker.public_key = a_keypair()
        db.session.commit()
        response = env.client.post('/api/alpha/community',
                                   headers=auth(maker),
                                   json={'name': '---', 'title': 'x'})
        assert response.status_code == 400
        assert 'needs a name' in response.get_json()['message']

    def test_editing_one(self, env):
        response = env.client.put('/api/alpha/community',
                                  headers=auth(env.moderator),
                                  json={'community_id': env.community.id,
                                        'title': 'elsewhere'})
        assert response.status_code == 200
        assert db.session.get(Community,
                              env.community.id).title == 'elsewhere'

    def test_editing_one_without_standing(self, env):
        response = env.client.put('/api/alpha/community',
                                  headers=auth(env.stranger),
                                  json={'community_id': env.community.id,
                                        'title': 'mine now'})
        assert response.status_code == 400

    def test_deleting_and_restoring(self, env):
        for deleted in (True, False):
            response = env.client.post('/api/alpha/community/delete',
                                       headers=auth(env.moderator),
                                       json={'community_id': env.community.id,
                                             'deleted': deleted})
            assert response.status_code == 200
        assert db.session.get(Community, env.community.id).banned is False


class TestModerating:
    def test_adding_and_removing_a_moderator(self, env):
        for added in (True, False):
            response = env.client.post('/api/alpha/community/mod',
                                       headers=auth(env.moderator),
                                       json={'community_id': env.community.id,
                                             'person_id': env.stranger.id,
                                             'added': added})
            assert response.status_code == 200
            assert 'moderators' in response.get_json()

    def test_banning_someone(self, env):
        response = env.client.post('/api/alpha/community/moderate/ban',
                                   headers=auth(env.moderator),
                                   json={'community_id': env.community.id,
                                         'user_id': env.target.id,
                                         'reason': 'spam'})
        assert response.status_code == 200
        assert response.get_json()['banned_user']['id'] == env.target.id

    def test_banning_someone_nobody_holds(self, env):
        response = env.client.post('/api/alpha/community/moderate/ban',
                                   headers=auth(env.moderator),
                                   json={'community_id': env.community.id,
                                         'user_id': MISSING,
                                         'reason': 'spam'})
        assert response.status_code == 400
        assert response.get_json()['message'] == 'user not found'

    def test_reading_the_bans(self, env):
        make_community_ban(env.target, env.community,
                           banned_by=env.moderator, reason='spam')
        response = env.client.get('/api/alpha/community/moderate/bans',
                                  headers=auth(env.moderator),
                                  query_string={'community_id':
                                                env.community.id})
        assert response.status_code == 200
        assert len(response.get_json()['items']) == 1

    def test_unbanning_someone(self, env):
        make_community_ban(env.target, env.community,
                           banned_by=env.moderator, reason='spam')
        response = env.client.put('/api/alpha/community/moderate/unban',
                                  headers=auth(env.moderator),
                                  json={'community_id': env.community.id,
                                        'user_id': env.target.id})
        assert response.status_code == 200
        assert response.get_json()['expired'] is True

    def test_marking_a_post_nsfw(self, env):
        response = env.client.post('/api/alpha/community/moderate/post/nsfw',
                                   headers=auth(env.moderator),
                                   json={'post_id': env.post.id,
                                         'nsfw_status': True})
        assert response.status_code == 200
        assert response.get_json()['post']['nsfw'] is True


class TestFlair:
    def test_creating_flair(self, env):
        response = env.client.post('/api/alpha/community/flair',
                                   headers=auth(env.moderator),
                                   json={'community_id': env.community.id,
                                         'flair_title': 'news'})
        assert response.status_code == 200
        assert response.get_json()['flair_title'] == 'news'

    def test_creating_the_same_flair_twice(self, env):
        for _ in range(2):
            response = env.client.post('/api/alpha/community/flair',
                                       headers=auth(env.moderator),
                                       json={'community_id': env.community.id,
                                             'flair_title': 'news'})
        assert response.status_code == 400
        assert 'already exists' in response.get_json()['message']

    def test_editing_flair(self, env):
        flair = make_community_flair(env.community, 'news')
        response = env.client.put('/api/alpha/community/flair',
                                  headers=auth(env.moderator),
                                  json={'flair_id': flair.id,
                                        'flair_title': 'olds'})
        assert response.status_code == 200
        assert response.get_json()['flair_title'] == 'olds'

    def test_deleting_flair(self, env):
        flair = make_community_flair(env.community, 'news')
        response = env.client.post('/api/alpha/community/flair/delete',
                                   headers=auth(env.moderator),
                                   json={'flair_id': flair.id})
        assert response.status_code == 200
        assert db.session.get(CommunityFlair, flair.id) is None


class TestFeeds:
    def test_creating_a_feed(self, env):
        response = env.client.post('/api/alpha/feed',
                                   headers=auth(env.moderator),
                                   json={'title': 'news', 'name': 'news',
                                         'public': True})
        assert response.status_code == 200
        assert Feed.query.filter_by(name='news').count() == 1

    def test_following_a_feed(self, env):
        feed = make_local_feed('newsfeed', public=True)
        feed.user_id = env.admin.id
        feed.ap_domain = current_app.config['SERVER_NAME']
        db.session.commit()
        response = env.client.post('/api/alpha/feed/follow',
                                   headers=auth(env.stranger),
                                   json={'feed_id': feed.id, 'follow': True})
        assert response.status_code == 200
        assert FeedMember.query.filter_by(user_id=env.stranger.id,
                                          feed_id=feed.id).count() == 1

    def test_editing_a_feed(self, env):
        feed = make_local_feed('newsfeed', public=True)
        feed.user_id = env.moderator.id
        feed.ap_domain = current_app.config['SERVER_NAME']
        db.session.commit()
        response = env.client.put('/api/alpha/feed',
                                  headers=auth(env.moderator),
                                  json={'feed_id': feed.id,
                                        'title': 'the news',
                                        'name': 'newsfeed', 'public': True})
        assert response.status_code == 200
        assert db.session.get(Feed, feed.id).title == 'the news'

    def test_deleting_a_feed(self, env):
        feed = make_local_feed('newsfeed', public=True)
        feed.user_id = env.moderator.id
        feed.ap_domain = current_app.config['SERVER_NAME']
        db.session.commit()
        response = env.client.post('/api/alpha/feed/delete',
                                   headers=auth(env.moderator),
                                   json={'feed_id': feed.id, 'deleted': True})
        assert response.status_code == 200


"""The alpha API over HTTP: posts.

Sub-project 85, slice D -- the twenty-two post routes, driven through the test
client with the API switched on, so that each route's body runs and every
response is validated against the schema its route declares.
"""
from unittest.mock import patch

import pytest
from flask import current_app, g

from app.constants import POST_TYPE_POLL, REPORT_STATE_RESOLVED
from app import db
from app.models import (Language, Post, PostBookmark, PostVote, Report, Site,
                        read_posts)
from tests.factories import (a_keypair, make_community, make_community_flair,
                             make_community_member, make_poll,
                             make_poll_choice, make_post, make_post_vote)

MISSING = 999999


def auth(user):
    return {'Authorization': f'Bearer {user.encode_jwt_token()}'}


@pytest.fixture
def env(app, api_baseline, monkeypatch):
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
    author = api_baseline.user2
    moderator = api_baseline.user4
    reader = api_baseline.user3
    for who in (author, moderator, reader):
        who.private_key, who.public_key = a_keypair()
    db.session.commit()
    make_community_member(author, community)
    make_community_member(moderator, community, is_moderator=True)
    post = make_post(community, author, ap_id='https://test.piefed.local/v/1')
    post.body = 'a body'
    post.body_html = '<p>a body</p>'
    db.session.commit()
    return SimpleNamespace(client=app.test_client(), community=community,
                           author=author, moderator=moderator, reader=reader,
                           post=post, admin=api_baseline.user1,
                           baseline=api_baseline)


class TestReadingPosts:
    def test_the_listing(self, env):
        response = env.client.get('/api/alpha/post/list')
        assert response.status_code == 200
        assert 'posts' in response.get_json()

    @pytest.mark.parametrize('path', ['/api/alpha/post/list', '/api/alpha/post/list2'])
    def test_a_cursor_that_does_not_parse_is_a_400(self, env, path):
        """D895 follow-up: invalid client input stays a 400, not an internal 500."""
        response = env.client.get(path, query_string={'page_cursor': 'abc'})
        assert response.status_code == 400
        assert response.get_json()['message'] == 'invalid page_cursor'

    def test_the_other_listing(self, env):
        response = env.client.get('/api/alpha/post/list2')
        assert response.status_code == 200
        assert 'posts' in response.get_json()

    def test_one_post(self, env):
        response = env.client.get('/api/alpha/post',
                                  query_string={'id': env.post.id})
        assert response.status_code == 200
        assert response.get_json()['post_view']['post']['id'] == env.post.id

    def test_a_post_nobody_holds(self, env):
        response = env.client.get('/api/alpha/post',
                                  query_string={'id': MISSING})
        assert response.status_code == 400
        assert response.get_json()['message'] == 'post not found'

    def test_the_replies_to_a_post(self, env):
        response = env.client.get('/api/alpha/post/replies',
                                  query_string={'post_id': env.post.id})
        assert response.status_code == 200
        assert response.get_json()['comments'] == []

    def test_the_metadata_of_a_link(self, env):
        with patch('app.api.alpha.utils.site.opengraph_parse',
                   return_value={'og:title': 'a title'}):
            response = env.client.get(
                '/api/alpha/post/site_metadata',
                query_string={'url': 'https://example.test/x'})
        assert response.status_code == 200
        assert response.get_json()['metadata']['title'] == 'a title'


class TestVotingAndSaving:
    def test_voting(self, env):
        response = env.client.post('/api/alpha/post/like',
                                   headers=auth(env.reader),
                                   json={'post_id': env.post.id, 'score': 1})
        assert response.status_code == 200
        assert response.get_json()['post_view']['my_vote'] == 1

    def test_voting_on_a_post_nobody_holds(self, env):
        response = env.client.post('/api/alpha/post/like',
                                   headers=auth(env.reader),
                                   json={'post_id': MISSING, 'score': 1})
        assert response.status_code == 400
        assert response.get_json()['message'] == 'post not found'

    def test_saving_and_unsaving(self, env):
        for save in (True, False):
            response = env.client.put('/api/alpha/post/save',
                                      headers=auth(env.reader),
                                      json={'post_id': env.post.id,
                                            'save': save})
            assert response.status_code == 200
        assert PostBookmark.query.filter_by(user_id=env.reader.id).count() == 0

    def test_subscribing_and_unsubscribing(self, env):
        for subscribe in (True, False):
            response = env.client.put('/api/alpha/post/subscribe',
                                      headers=auth(env.reader),
                                      json={'post_id': env.post.id,
                                            'subscribe': subscribe})
            assert response.status_code == 200

    def test_marking_as_read(self, env):
        response = env.client.post('/api/alpha/post/mark_as_read',
                                   headers=auth(env.reader),
                                   json={'post_id': env.post.id,
                                         'read': True})
        assert response.status_code == 200
        assert response.get_json() == {'success': True}

    def test_marking_several_as_read(self, env):
        second = make_post(env.community, env.author,
                           ap_id='https://test.piefed.local/v/2')
        db.session.commit()
        response = env.client.post('/api/alpha/post/mark_as_read',
                                   headers=auth(env.reader),
                                   json={'post_ids': [env.post.id, second.id],
                                         'read': True})
        assert response.status_code == 200


class TestWritingPosts:
    def test_writing_one(self, env):
        response = env.client.post('/api/alpha/post',
                                   headers=auth(env.author),
                                   json={'title': 'hello',
                                         'community_id': env.community.id})
        assert response.status_code == 200
        assert response.get_json()['post_view']['post']['title'] == 'hello'

    def test_writing_into_a_community_nobody_holds(self, env):
        response = env.client.post('/api/alpha/post',
                                   headers=auth(env.author),
                                   json={'title': 'hello',
                                         'community_id': MISSING})
        assert response.status_code == 400
        assert response.get_json()['message'] == 'community not found'

    def test_editing_one(self, env):
        response = env.client.put('/api/alpha/post',
                                  headers=auth(env.author),
                                  json={'post_id': env.post.id,
                                        'title': 'a new title'})
        assert response.status_code == 200
        assert db.session.get(Post, env.post.id).title == 'a new title'

    def test_deleting_and_restoring(self, env):
        for deleted in (True, False):
            response = env.client.post('/api/alpha/post/delete',
                                       headers=auth(env.author),
                                       json={'post_id': env.post.id,
                                             'deleted': deleted})
            assert response.status_code == 200
        assert db.session.get(Post, env.post.id).deleted is False


class TestReporting:
    def test_reporting_a_post(self, env):
        response = env.client.post('/api/alpha/post/report',
                                   headers=auth(env.reader),
                                   json={'post_id': env.post.id,
                                         'reason': 'spam'})
        assert response.status_code == 200
        assert response.get_json()['post_report_view'][
            'post_report']['reason'] == 'spam'

    def test_reading_the_reports(self, env):
        env.client.post('/api/alpha/post/report', headers=auth(env.reader),
                        json={'post_id': env.post.id, 'reason': 'spam'})
        response = env.client.get('/api/alpha/post/report/list',
                                  headers=auth(env.moderator),
                                  query_string={'post_id': env.post.id})
        assert response.status_code == 200
        assert len(response.get_json()['post_reports']) == 1

    def test_resolving_a_report(self, env):
        env.client.post('/api/alpha/post/report', headers=auth(env.reader),
                        json={'post_id': env.post.id, 'reason': 'spam'})
        report = Report.query.filter_by(suspect_post_id=env.post.id).one()
        response = env.client.put('/api/alpha/post/report/resolve',
                                  headers=auth(env.moderator),
                                  json={'report_id': report.id,
                                        'resolved': True})
        assert response.status_code == 200
        assert db.session.get(Report,
                              report.id).status == REPORT_STATE_RESOLVED


class TestModeratingPosts:
    def test_locking(self, env):
        response = env.client.post('/api/alpha/post/lock',
                                   headers=auth(env.moderator),
                                   json={'post_id': env.post.id,
                                         'locked': True})
        assert response.status_code == 200
        assert db.session.get(Post, env.post.id).comments_enabled is False

    def test_hiding(self, env):
        response = env.client.post('/api/alpha/post/hide',
                                   headers=auth(env.reader),
                                   json={'post_id': env.post.id,
                                         'hidden': True})
        assert response.status_code == 200

    def test_featuring_in_the_community(self, env):
        response = env.client.post('/api/alpha/post/feature',
                                   headers=auth(env.moderator),
                                   json={'post_id': env.post.id,
                                         'featured': True,
                                         'feature_type': 'Community'})
        assert response.status_code == 200
        assert db.session.get(Post, env.post.id).sticky is True

    def test_featuring_with_a_type_nobody_recognises(self, env):
        response = env.client.post('/api/alpha/post/feature',
                                   headers=auth(env.admin),
                                   json={'post_id': env.post.id,
                                         'featured': True,
                                         'feature_type': 'Global'})
        assert response.status_code == 400

    def test_removing_and_restoring(self, env):
        for removed in (True, False):
            response = env.client.post('/api/alpha/post/remove',
                                       headers=auth(env.moderator),
                                       json={'post_id': env.post.id,
                                             'removed': removed})
            assert response.status_code == 200
        assert db.session.get(Post, env.post.id).deleted is False

    def test_reading_who_voted(self, env):
        make_post_vote(env.reader, env.post, 1)
        response = env.client.get('/api/alpha/post/like/list',
                                  headers=auth(env.moderator),
                                  query_string={'post_id': env.post.id})
        assert response.status_code == 200
        assert len(response.get_json()['post_likes']) == 1

    def test_an_account_with_no_standing_cannot_read_who_voted(self, env):
        make_post_vote(env.reader, env.post, 1)
        response = env.client.get('/api/alpha/post/like/list',
                                  headers=auth(env.reader),
                                  query_string={'post_id': env.post.id})
        assert response.status_code == 400
        assert response.get_json()['message'] == 'Not a moderator'


class TestFlairAndPolls:
    def test_setting_flair(self, env):
        flair = make_community_flair(env.community, 'news')
        response = env.client.post('/api/alpha/post/assign_flair',
                                   headers=auth(env.author),
                                   json={'post_id': env.post.id,
                                         'flair_id_list': [flair.id]})
        assert response.status_code == 200
        assert len(db.session.get(Post, env.post.id).flair) == 1

    def test_voting_in_a_poll(self, env):
        post = make_post(env.community, env.author,
                         ap_id='https://test.piefed.local/v/poll')
        post.type = POST_TYPE_POLL
        make_poll(post)
        choice = make_poll_choice(post, 'a')
        db.session.commit()
        # `choice_id` is a LIST, of length one for a single-choice poll
        # (PollVoteRequest).
        response = env.client.post('/api/alpha/post/poll_vote',
                                   headers=auth(env.reader),
                                   json={'post_id': post.id,
                                         'choice_id': [choice.id]})
        assert response.status_code == 200
        assert response.get_json()['post_view']['post']['id'] == post.id


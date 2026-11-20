"""The alpha API over HTTP: posts.

Sub-project 85, slice D -- the twenty-two post routes, driven through the test
client with the API switched on, so that each route's body runs and every
response is validated against the schema its route declares.
"""
from datetime import datetime
from unittest.mock import patch

import pytest
from flask import current_app, g

from app.constants import POST_TYPE_EVENT, POST_TYPE_POLL, REPORT_STATE_RESOLVED
from app import db
from app.models import (Event, Language, Post, PostBookmark, PostVote, Report, Site,
                        read_posts)
from tests.factories import (a_keypair, make_community, make_community_flair,
                             make_community_member, make_poll,
                             make_poll_choice, make_post, make_post_vote)

MISSING = 999999


def auth(user):
    return {'Authorization': f'Bearer {user.encode_jwt_token()}'}


class TestReadingPosts:
    def test_the_listing(self, api_routes_post_env):
        response = api_routes_post_env.client.get('/api/alpha/post/list')
        assert response.status_code == 200
        assert 'posts' in response.get_json()

    @pytest.mark.parametrize('path', ['/api/alpha/post/list', '/api/alpha/post/list2'])
    def test_a_cursor_that_does_not_parse_is_a_400(self, api_routes_post_env, path):
        """D895 follow-up: invalid client input stays a 400, not an internal 500."""
        response = api_routes_post_env.client.get(path, query_string={'page_cursor': 'abc'})
        assert response.status_code == 400
        assert response.get_json()['message'] == 'invalid page_cursor'

    def test_the_other_listing(self, api_routes_post_env):
        response = api_routes_post_env.client.get('/api/alpha/post/list2')
        assert response.status_code == 200
        assert 'posts' in response.get_json()

    def test_one_post(self, api_routes_post_env):
        response = api_routes_post_env.client.get('/api/alpha/post',
                                  query_string={'id': api_routes_post_env.post.id})
        assert response.status_code == 200
        assert response.get_json()['post_view']['post']['id'] == api_routes_post_env.post.id

    def test_a_post_nobody_holds(self, api_routes_post_env):
        response = api_routes_post_env.client.get('/api/alpha/post',
                                  query_string={'id': MISSING})
        assert response.status_code == 400
        assert response.get_json()['message'] == 'post not found'

    def test_the_replies_to_a_post(self, api_routes_post_env):
        response = api_routes_post_env.client.get('/api/alpha/post/replies',
                                  query_string={'post_id': api_routes_post_env.post.id})
        assert response.status_code == 200
        assert response.get_json()['comments'] == []

    def test_the_metadata_of_a_link(self, api_routes_post_env):
        with patch('app.api.alpha.utils.site.opengraph_parse',
                   return_value={'og:title': 'a title'}):
            response = api_routes_post_env.client.get(
                '/api/alpha/post/site_metadata',
                query_string={'url': 'https://example.test/x'})
        assert response.status_code == 200
        assert response.get_json()['metadata']['title'] == 'a title'


class TestReadingAFederatedEvent:
    """A peer's Event may lack `startTime` or `endTime`, and its `location` may be
    any JSON a peer sent (a list, or an object missing fields); the post must
    still be served, through the response validation the route applies."""

    def _get(self, env, **event_columns):
        env.post.type = POST_TYPE_EVENT
        db.session.add(Event(post_id=env.post.id, timezone='UTC', **event_columns))
        db.session.commit()
        response = env.client.get('/api/alpha/post', query_string={'id': env.post.id})
        assert response.status_code == 200
        return response.get_json()['post_view']['post']['event']

    def test_an_event_without_an_end_time_is_served_with_its_start(self, api_routes_post_env):
        event = self._get(api_routes_post_env, start=datetime(2030, 6, 1, 12, 0), end=None)

        assert event['start'] == '2030-06-01T12:00:00.000000Z'
        assert 'end' not in event

    def test_an_event_without_a_start_time_is_served_with_its_end(self, api_routes_post_env):
        event = self._get(api_routes_post_env, start=None, end=datetime(2030, 6, 1, 13, 0))

        assert event['end'] == '2030-06-01T13:00:00.000000Z'
        assert 'start' not in event

    def test_an_event_with_both_times_serves_both(self, api_routes_post_env):
        event = self._get(api_routes_post_env, start=datetime(2030, 6, 1, 12, 0),
                          end=datetime(2030, 6, 1, 13, 0))

        assert event['start'] == '2030-06-01T12:00:00.000000Z'
        assert event['end'] == '2030-06-01T13:00:00.000000Z'

    def test_a_location_that_is_not_an_object_is_left_out(self, api_routes_post_env):
        event = self._get(api_routes_post_env, start=datetime(2030, 6, 1, 12, 0),
                          end=datetime(2030, 6, 1, 13, 0),
                          location=[{'type': 'Place', 'name': 'a hall'}])

        assert 'location' not in event

    def test_a_location_object_is_served_as_it_is(self, api_routes_post_env):
        location = {'type': 'Place', 'name': 'a hall', 'address': {'streetAddress': '1 Street'}}
        event = self._get(api_routes_post_env, start=datetime(2030, 6, 1, 12, 0),
                          end=datetime(2030, 6, 1, 13, 0), location=location)

        assert event['location'] == location


class TestVotingAndSaving:
    def test_voting(self, api_routes_post_env):
        response = api_routes_post_env.client.post('/api/alpha/post/like',
                                   headers=auth(api_routes_post_env.reader),
                                   json={'post_id': api_routes_post_env.post.id, 'score': 1})
        assert response.status_code == 200
        assert response.get_json()['post_view']['my_vote'] == 1

    def test_voting_on_a_post_nobody_holds(self, api_routes_post_env):
        response = api_routes_post_env.client.post('/api/alpha/post/like',
                                   headers=auth(api_routes_post_env.reader),
                                   json={'post_id': MISSING, 'score': 1})
        assert response.status_code == 400
        assert response.get_json()['message'] == 'post not found'

    def test_saving_and_unsaving(self, api_routes_post_env):
        for save in (True, False):
            response = api_routes_post_env.client.put('/api/alpha/post/save',
                                      headers=auth(api_routes_post_env.reader),
                                      json={'post_id': api_routes_post_env.post.id,
                                            'save': save})
            assert response.status_code == 200
        assert PostBookmark.query.filter_by(user_id=api_routes_post_env.reader.id).count() == 0

    def test_subscribing_and_unsubscribing(self, api_routes_post_env):
        for subscribe in (True, False):
            response = api_routes_post_env.client.put('/api/alpha/post/subscribe',
                                      headers=auth(api_routes_post_env.reader),
                                      json={'post_id': api_routes_post_env.post.id,
                                            'subscribe': subscribe})
            assert response.status_code == 200

    def test_marking_as_read(self, api_routes_post_env):
        response = api_routes_post_env.client.post('/api/alpha/post/mark_as_read',
                                   headers=auth(api_routes_post_env.reader),
                                   json={'post_id': api_routes_post_env.post.id,
                                         'read': True})
        assert response.status_code == 200
        assert response.get_json() == {'success': True}

    def test_marking_several_as_read(self, api_routes_post_env):
        second = make_post(api_routes_post_env.community, api_routes_post_env.author,
                           ap_id='https://test.piefed.local/v/2')
        db.session.commit()
        response = api_routes_post_env.client.post('/api/alpha/post/mark_as_read',
                                   headers=auth(api_routes_post_env.reader),
                                   json={'post_ids': [api_routes_post_env.post.id, second.id],
                                         'read': True})
        assert response.status_code == 200


class TestWritingPosts:
    def test_writing_one(self, api_routes_post_env):
        response = api_routes_post_env.client.post('/api/alpha/post',
                                   headers=auth(api_routes_post_env.author),
                                   json={'title': 'hello',
                                         'community_id': api_routes_post_env.community.id})
        assert response.status_code == 200
        assert response.get_json()['post_view']['post']['title'] == 'hello'

    def test_writing_into_a_community_nobody_holds(self, api_routes_post_env):
        response = api_routes_post_env.client.post('/api/alpha/post',
                                   headers=auth(api_routes_post_env.author),
                                   json={'title': 'hello',
                                         'community_id': MISSING})
        assert response.status_code == 400
        assert response.get_json()['message'] == 'community not found'

    def test_editing_one(self, api_routes_post_env):
        response = api_routes_post_env.client.put('/api/alpha/post',
                                  headers=auth(api_routes_post_env.author),
                                  json={'post_id': api_routes_post_env.post.id,
                                        'title': 'a new title'})
        assert response.status_code == 200
        assert db.session.get(Post, api_routes_post_env.post.id).title == 'a new title'

    def test_deleting_and_restoring(self, api_routes_post_env):
        for deleted in (True, False):
            response = api_routes_post_env.client.post('/api/alpha/post/delete',
                                       headers=auth(api_routes_post_env.author),
                                       json={'post_id': api_routes_post_env.post.id,
                                             'deleted': deleted})
            assert response.status_code == 200
        assert db.session.get(Post, api_routes_post_env.post.id).deleted is False


class TestReporting:
    def test_reporting_a_post(self, api_routes_post_env):
        response = api_routes_post_env.client.post('/api/alpha/post/report',
                                   headers=auth(api_routes_post_env.reader),
                                   json={'post_id': api_routes_post_env.post.id,
                                         'reason': 'spam'})
        assert response.status_code == 200
        assert response.get_json()['post_report_view'][
            'post_report']['reason'] == 'spam'

    def test_reading_the_reports(self, api_routes_post_env):
        api_routes_post_env.client.post('/api/alpha/post/report', headers=auth(api_routes_post_env.reader),
                        json={'post_id': api_routes_post_env.post.id, 'reason': 'spam'})
        response = api_routes_post_env.client.get('/api/alpha/post/report/list',
                                  headers=auth(api_routes_post_env.moderator),
                                  query_string={'post_id': api_routes_post_env.post.id})
        assert response.status_code == 200
        assert len(response.get_json()['post_reports']) == 1

    def test_resolving_a_report(self, api_routes_post_env):
        api_routes_post_env.client.post('/api/alpha/post/report', headers=auth(api_routes_post_env.reader),
                        json={'post_id': api_routes_post_env.post.id, 'reason': 'spam'})
        report = Report.query.filter_by(suspect_post_id=api_routes_post_env.post.id).one()
        response = api_routes_post_env.client.put('/api/alpha/post/report/resolve',
                                  headers=auth(api_routes_post_env.moderator),
                                  json={'report_id': report.id,
                                        'resolved': True})
        assert response.status_code == 200
        assert db.session.get(Report,
                              report.id).status == REPORT_STATE_RESOLVED


class TestModeratingPosts:
    def test_locking(self, api_routes_post_env):
        response = api_routes_post_env.client.post('/api/alpha/post/lock',
                                   headers=auth(api_routes_post_env.moderator),
                                   json={'post_id': api_routes_post_env.post.id,
                                         'locked': True})
        assert response.status_code == 200
        assert db.session.get(Post, api_routes_post_env.post.id).comments_enabled is False

    def test_hiding(self, api_routes_post_env):
        response = api_routes_post_env.client.post('/api/alpha/post/hide',
                                   headers=auth(api_routes_post_env.reader),
                                   json={'post_id': api_routes_post_env.post.id,
                                         'hidden': True})
        assert response.status_code == 200

    def test_featuring_in_the_community(self, api_routes_post_env):
        response = api_routes_post_env.client.post('/api/alpha/post/feature',
                                   headers=auth(api_routes_post_env.moderator),
                                   json={'post_id': api_routes_post_env.post.id,
                                         'featured': True,
                                         'feature_type': 'Community'})
        assert response.status_code == 200
        assert db.session.get(Post, api_routes_post_env.post.id).sticky is True

    def test_featuring_with_a_type_nobody_recognises(self, api_routes_post_env):
        response = api_routes_post_env.client.post('/api/alpha/post/feature',
                                   headers=auth(api_routes_post_env.admin),
                                   json={'post_id': api_routes_post_env.post.id,
                                         'featured': True,
                                         'feature_type': 'Global'})
        assert response.status_code == 400

    def test_removing_and_restoring(self, api_routes_post_env):
        for removed in (True, False):
            response = api_routes_post_env.client.post('/api/alpha/post/remove',
                                       headers=auth(api_routes_post_env.moderator),
                                       json={'post_id': api_routes_post_env.post.id,
                                             'removed': removed})
            assert response.status_code == 200
        assert db.session.get(Post, api_routes_post_env.post.id).deleted is False

    def test_reading_who_voted(self, api_routes_post_env):
        make_post_vote(api_routes_post_env.reader, api_routes_post_env.post, 1)
        response = api_routes_post_env.client.get('/api/alpha/post/like/list',
                                  headers=auth(api_routes_post_env.moderator),
                                  query_string={'post_id': api_routes_post_env.post.id})
        assert response.status_code == 200
        assert len(response.get_json()['post_likes']) == 1

    def test_an_account_with_no_standing_cannot_read_who_voted(self, api_routes_post_env):
        make_post_vote(api_routes_post_env.reader, api_routes_post_env.post, 1)
        response = api_routes_post_env.client.get('/api/alpha/post/like/list',
                                  headers=auth(api_routes_post_env.reader),
                                  query_string={'post_id': api_routes_post_env.post.id})
        assert response.status_code == 400
        assert response.get_json()['message'] == 'Not a moderator'


class TestFlairAndPolls:
    def test_setting_flair(self, api_routes_post_env):
        flair = make_community_flair(api_routes_post_env.community, 'news')
        response = api_routes_post_env.client.post('/api/alpha/post/assign_flair',
                                   headers=auth(api_routes_post_env.author),
                                   json={'post_id': api_routes_post_env.post.id,
                                         'flair_id_list': [flair.id]})
        assert response.status_code == 200
        assert len(db.session.get(Post, api_routes_post_env.post.id).flair) == 1

    def test_voting_in_a_poll(self, api_routes_post_env):
        post = make_post(api_routes_post_env.community, api_routes_post_env.author,
                         ap_id='https://test.piefed.local/v/poll')
        post.type = POST_TYPE_POLL
        make_poll(post)
        choice = make_poll_choice(post, 'a')
        db.session.commit()
        # `choice_id` is a LIST, of length one for a single-choice poll
        # (PollVoteRequest).
        response = api_routes_post_env.client.post('/api/alpha/post/poll_vote',
                                   headers=auth(api_routes_post_env.reader),
                                   json={'post_id': post.id,
                                         'choice_id': [choice.id]})
        assert response.status_code == 200
        assert response.get_json()['post_view']['post']['id'] == post.id


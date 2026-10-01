"""The alpha API over HTTP: comments.

Sub-project 85, slice E -- the seventeen comment routes, driven through the
test client with the API switched on, so that each route's body runs and every
response is validated against the schema its route declares.
"""
import pytest
from flask import current_app, g

from app import db
from app.constants import REPORT_STATE_RESOLVED
from app.models import (Language, PostReply, PostReplyBookmark, Report, Site)
from tests.factories import (a_keypair, make_community, make_community_member,
                             make_post, make_post_reply,
                             make_post_reply_vote)

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
    post = make_post(community, author, ap_id='https://test.piefed.local/w/1')
    reply = make_post_reply(post, author, body='a comment')
    reply.body_html = '<p>a comment</p>'
    reply.path = [0, reply.id]
    reply.depth = 0
    db.session.commit()
    return SimpleNamespace(client=app.test_client(), community=community,
                           author=author, moderator=moderator, reader=reader,
                           post=post, reply=reply, admin=api_baseline.user1,
                           baseline=api_baseline)


class TestReadingComments:
    def test_the_listing(self, env):
        response = env.client.get('/api/alpha/comment/list',
                                  query_string={'post_id': env.post.id})
        assert response.status_code == 200
        assert len(response.get_json()['comments']) == 1

    def test_one_comment(self, env):
        response = env.client.get('/api/alpha/comment',
                                  query_string={'id': env.reply.id})
        assert response.status_code == 200
        assert response.get_json()['comment_view'][
            'comment']['id'] == env.reply.id

    def test_a_comment_nobody_holds(self, env):
        response = env.client.get('/api/alpha/comment',
                                  query_string={'id': MISSING})
        assert response.status_code == 400

    def test_reading_who_voted(self, env):
        make_post_reply_vote(env.reader, env.reply, 1)
        response = env.client.get('/api/alpha/comment/like/list',
                                  headers=auth(env.moderator),
                                  query_string={'comment_id': env.reply.id})
        assert response.status_code == 200
        assert len(response.get_json()['comment_likes']) == 1


class TestVotingAndSaving:
    def test_voting(self, env):
        response = env.client.post('/api/alpha/comment/like',
                                   headers=auth(env.reader),
                                   json={'comment_id': env.reply.id,
                                         'score': 1})
        assert response.status_code == 200
        assert response.get_json()['comment_view']['my_vote'] == 1

    def test_voting_on_a_comment_nobody_holds(self, env):
        response = env.client.post('/api/alpha/comment/like',
                                   headers=auth(env.reader),
                                   json={'comment_id': MISSING, 'score': 1})
        assert response.status_code == 400

    def test_saving_and_unsaving(self, env):
        for save in (True, False):
            response = env.client.put('/api/alpha/comment/save',
                                      headers=auth(env.reader),
                                      json={'comment_id': env.reply.id,
                                            'save': save})
            assert response.status_code == 200
        assert PostReplyBookmark.query.filter_by(
            user_id=env.reader.id).count() == 0

    def test_subscribing_and_unsubscribing(self, env):
        for subscribe in (True, False):
            response = env.client.put('/api/alpha/comment/subscribe',
                                      headers=auth(env.reader),
                                      json={'comment_id': env.reply.id,
                                            'subscribe': subscribe})
            assert response.status_code == 200

    def test_marking_as_read(self, env):
        # `comment_reply_id`, not `comment_id`: three of the seventeen
        # comment routes name it that way (MarkCommentAsReadRequest,
        # MarkCommentAsAnswerRequest, MarkCommentAsDistinguishedRequest).
        response = env.client.post('/api/alpha/comment/mark_as_read',
                                   headers=auth(env.reader),
                                   json={'comment_reply_id': env.reply.id,
                                         'read': True})
        assert response.status_code == 200


class TestWritingComments:
    def test_writing_one(self, env):
        response = env.client.post('/api/alpha/comment',
                                   headers=auth(env.author),
                                   json={'post_id': env.post.id,
                                         'body': 'a new comment'})
        assert response.status_code == 200
        assert response.get_json()['comment_view'][
            'comment']['body'] == 'a new comment'

    def test_replying_to_a_comment(self, env):
        response = env.client.post('/api/alpha/comment',
                                   headers=auth(env.author),
                                   json={'post_id': env.post.id,
                                         'parent_id': env.reply.id,
                                         'body': 'a reply to a comment'})
        assert response.status_code == 200
        written = PostReply.query.filter_by(
            body='a reply to a comment').one()
        assert written.parent_id == env.reply.id

    def test_editing_one(self, env):
        response = env.client.put('/api/alpha/comment',
                                  headers=auth(env.author),
                                  json={'comment_id': env.reply.id,
                                        'body': 'an edited comment'})
        assert response.status_code == 200
        assert db.session.get(PostReply,
                              env.reply.id).body == 'an edited comment'

    def test_deleting_and_restoring(self, env):
        for deleted in (True, False):
            response = env.client.post('/api/alpha/comment/delete',
                                       headers=auth(env.author),
                                       json={'comment_id': env.reply.id,
                                             'deleted': deleted})
            assert response.status_code == 200
        assert db.session.get(PostReply, env.reply.id).deleted is False


class TestReporting:
    def test_reporting_a_comment(self, env):
        response = env.client.post('/api/alpha/comment/report',
                                   headers=auth(env.reader),
                                   json={'comment_id': env.reply.id,
                                         'reason': 'spam'})
        assert response.status_code == 200

    def test_reading_the_reports(self, env):
        env.client.post('/api/alpha/comment/report', headers=auth(env.reader),
                        json={'comment_id': env.reply.id, 'reason': 'spam'})
        response = env.client.get('/api/alpha/comment/report/list',
                                  headers=auth(env.moderator),
                                  query_string={'comment_id': env.reply.id})
        assert response.status_code == 200
        assert len(response.get_json()['comment_reports']) == 1

    def test_resolving_a_report(self, env):
        env.client.post('/api/alpha/comment/report', headers=auth(env.reader),
                        json={'comment_id': env.reply.id, 'reason': 'spam'})
        report = Report.query.filter_by(
            suspect_post_reply_id=env.reply.id).one()
        response = env.client.put('/api/alpha/comment/report/resolve',
                                  headers=auth(env.moderator),
                                  json={'report_id': report.id,
                                        'resolved': True})
        assert response.status_code == 200
        assert db.session.get(Report,
                              report.id).status == REPORT_STATE_RESOLVED


class TestModeratingComments:
    def test_removing_and_restoring(self, env):
        for removed in (True, False):
            response = env.client.post('/api/alpha/comment/remove',
                                       headers=auth(env.moderator),
                                       json={'comment_id': env.reply.id,
                                             'removed': removed})
            assert response.status_code == 200
        assert db.session.get(PostReply, env.reply.id).deleted is False

    def test_marking_as_the_answer(self, env):
        env.post.question_answer = True
        db.session.commit()
        response = env.client.post('/api/alpha/comment/mark_as_answer',
                                   headers=auth(env.author),
                                   json={'comment_reply_id': env.reply.id,
                                         'answer': True})
        assert response.status_code == 200

    def test_the_replys_own_author_may_not_mark_it_as_the_answer(self, env):
        """Fixed (owner ruling): the post's author or a moderator chooses the
        answer, not the person who wrote the comment."""
        env.post.question_answer = True
        readers_comment = make_post_reply(env.post, env.reader, body='an answer')
        readers_comment.path = [0, readers_comment.id]
        db.session.commit()
        response = env.client.post('/api/alpha/comment/mark_as_answer',
                                   headers=auth(env.reader),
                                   json={'comment_reply_id': readers_comment.id,
                                         'answer': True})
        assert response.status_code == 400
        assert db.session.get(PostReply, readers_comment.id).answer is False

    def test_the_posts_author_may_mark_anothers_comment(self, env):
        env.post.question_answer = True
        readers_comment = make_post_reply(env.post, env.reader, body='an answer')
        readers_comment.path = [0, readers_comment.id]
        db.session.commit()
        response = env.client.post('/api/alpha/comment/mark_as_answer',
                                   headers=auth(env.author),
                                   json={'comment_reply_id': readers_comment.id,
                                         'answer': True})
        assert response.status_code == 200
        assert db.session.get(PostReply, readers_comment.id).answer is True

    def test_distinguishing_a_comment(self, env):
        moderators_comment = make_post_reply(env.post, env.moderator,
                                             body='from a moderator')
        moderators_comment.path = [0, moderators_comment.id]
        db.session.commit()
        response = env.client.post('/api/alpha/comment/distinguish',
                                   headers=auth(env.moderator),
                                   json={'comment_reply_id':
                                         moderators_comment.id,
                                         'distinguished': True})
        assert response.status_code == 200

    def test_locking_a_comment(self, env):
        response = env.client.post('/api/alpha/comment/lock',
                                   headers=auth(env.moderator),
                                   json={'comment_id': env.reply.id,
                                         'locked': True})
        assert response.status_code == 200


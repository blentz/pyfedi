"""The alpha API over HTTP: private messages and uploads.

Sub-project 85, slice G -- the thirteen private-message routes and the four
upload routes, driven through the test client with the API switched on.
"""
import io

import pytest
from flask import current_app, g

from app import db
from app.constants import REPORT_STATE_RESOLVED
from datetime import timedelta

from app.models import ChatMessage, Conversation, Report, Site, utcnow
from tests.factories import (a_keypair, make_chat_message, make_conversation,
                             make_user)

MISSING = 999999


def auth(user):
    return {'Authorization': f'Bearer {user.encode_jwt_token()}'}


@pytest.fixture
def env(app, api_baseline, monkeypatch):
    from types import SimpleNamespace

    g.admin_ids = [api_baseline.user1.id]
    g.site = db.session.get(Site, 1)
    monkeypatch.setitem(current_app.config, 'ENABLE_ALPHA_API', 'true')
    sender = api_baseline.user2
    recipient = api_baseline.user3
    for who in (sender, recipient):
        who.private_key, who.public_key = a_keypair()
        # Old enough to send: a fresh account is refused with "likely because
        # your account is too new".
        who.created = utcnow() - timedelta(days=30)
    db.session.commit()
    conversation = make_conversation(sender, recipient)
    message = make_chat_message(sender, recipient,
                                ap_id='https://test.piefed.local/m/1')
    message.conversation_id = conversation.id
    db.session.commit()
    return SimpleNamespace(client=app.test_client(), sender=sender,
                           recipient=recipient, conversation=conversation,
                           message=message, admin=api_baseline.user1,
                           baseline=api_baseline)


class TestReadingMessages:
    def test_the_listing(self, env):
        response = env.client.get('/api/alpha/private_message/list',
                                  headers=auth(env.recipient),
                                  query_string={'unread_only': False})
        assert response.status_code == 200
        assert 'private_messages' in response.get_json()

    def test_one_conversation(self, env):
        response = env.client.get('/api/alpha/private_message/conversation',
                                  headers=auth(env.recipient),
                                  query_string={'person_id': env.sender.id})
        assert response.status_code == 200
        assert 'private_messages' in response.get_json()

    def test_reading_without_an_account(self, env):
        response = env.client.get('/api/alpha/private_message/list',
                                  query_string={'unread_only': False})
        assert response.status_code == 400


class TestWritingMessages:
    def test_writing_one(self, env):
        response = env.client.post('/api/alpha/private_message',
                                   headers=auth(env.sender),
                                   json={'recipient_id': env.recipient.id,
                                         'content': 'hello there'})
        assert response.status_code == 200
        assert response.get_json()['private_message_view'][
            'private_message']['content'] == 'hello there'

    def test_editing_one(self, env):
        response = env.client.put('/api/alpha/private_message',
                                  headers=auth(env.sender),
                                  json={'private_message_id': env.message.id,
                                        'content': 'edited'})
        assert response.status_code == 200
        assert db.session.get(ChatMessage, env.message.id).body == 'edited'

    def test_marking_one_read(self, env):
        response = env.client.post('/api/alpha/private_message/mark_as_read',
                                   headers=auth(env.recipient),
                                   json={'private_message_id': env.message.id,
                                         'read': True})
        assert response.status_code == 200

    def test_deleting_one(self, env):
        response = env.client.post('/api/alpha/private_message/delete',
                                   headers=auth(env.sender),
                                   json={'private_message_id': env.message.id,
                                         'deleted': True})
        assert response.status_code == 200

    def test_leaving_a_conversation(self, env):
        response = env.client.post(
            '/api/alpha/private_message/conversation/leave',
            headers=auth(env.recipient),
            json={'conversation_id': env.conversation.id})
        assert response.status_code == 200


class TestReportingMessages:
    def test_reporting_a_message(self, env):
        response = env.client.post('/api/alpha/private_message/report',
                                   headers=auth(env.recipient),
                                   json={'private_message_id': env.message.id,
                                         'reason': 'spam'})
        assert response.status_code == 200

    def test_reporting_a_conversation(self, env):
        response = env.client.post(
            '/api/alpha/private_message/conversation/report',
            headers=auth(env.recipient),
            json={'conversation_id': env.conversation.id, 'reason': 'spam'})
        assert response.status_code == 200

    def test_reading_the_message_reports(self, env):
        env.client.post('/api/alpha/private_message/report',
                        headers=auth(env.recipient),
                        json={'private_message_id': env.message.id,
                              'reason': 'spam'})
        response = env.client.get('/api/alpha/private_message/report/list',
                                  headers=auth(env.admin),
                                  query_string={'unresolved_only': True})
        assert response.status_code == 200
        assert len(response.get_json()['private_message_reports']) == 1

    def test_reading_the_conversation_reports(self, env):
        env.client.post('/api/alpha/private_message/conversation/report',
                        headers=auth(env.recipient),
                        json={'conversation_id': env.conversation.id,
                              'reason': 'spam'})
        response = env.client.get(
            '/api/alpha/private_message/conversation/report/list',
            headers=auth(env.admin),
            query_string={'unresolved_only': True})
        assert response.status_code == 200
        assert len(response.get_json()['conversation_reports']) == 1

    def test_resolving_a_message_report(self, env):
        env.client.post('/api/alpha/private_message/report',
                        headers=auth(env.recipient),
                        json={'private_message_id': env.message.id,
                              'reason': 'spam'})
        # Both kinds of report carry `type=REPORT_TYPE_MESSAGE` and the
        # conversation's id; which MESSAGE was reported is recorded only in
        # `targets` (app/api/alpha/utils/private_message.py). This test makes
        # one report, so there is one row.
        report = Report.query.one()
        response = env.client.put(
            '/api/alpha/private_message/report/resolve',
            headers=auth(env.admin),
            json={'report_id': report.id, 'resolved': True})
        assert response.status_code == 200
        assert db.session.get(Report,
                              report.id).status == REPORT_STATE_RESOLVED

    def test_resolving_a_conversation_report(self, env):
        env.client.post('/api/alpha/private_message/conversation/report',
                        headers=auth(env.recipient),
                        json={'conversation_id': env.conversation.id,
                              'reason': 'spam'})
        report = Report.query.filter_by(
            suspect_conversation_id=env.conversation.id).one()
        response = env.client.put(
            '/api/alpha/private_message/conversation/report/resolve',
            headers=auth(env.admin),
            json={'report_id': report.id, 'resolved': True})
        assert response.status_code == 200
        assert db.session.get(Report,
                              report.id).status == REPORT_STATE_RESOLVED


class TestUploads:
    """The three upload routes and the delete, which all reach S3."""

    @pytest.mark.parametrize('path', ['/api/alpha/upload/image',
                                      '/api/alpha/upload/community_image',
                                      '/api/alpha/upload/user_image'])
    def test_an_upload_needs_an_account(self, env, path):
        response = env.client.post(
            path, data={'file': (io.BytesIO(b'not really a png'), 'x.png')},
            content_type='multipart/form-data')
        assert response.status_code == 400

    @pytest.mark.parametrize('path', ['/api/alpha/upload/image',
                                      '/api/alpha/upload/community_image',
                                      '/api/alpha/upload/user_image'])
    def test_an_upload_needs_a_file(self, env, path):
        response = env.client.post(path, headers=auth(env.sender),
                                   data={}, content_type='multipart/form-data')
        assert response.status_code == 400

    def test_deleting_an_image_needs_an_account(self, env):
        response = env.client.post('/api/alpha/image/delete',
                                   json={'file_id': 1})
        assert response.status_code == 400



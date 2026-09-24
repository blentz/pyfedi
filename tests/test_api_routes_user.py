"""The alpha API over HTTP: accounts.

Sub-project 85, slice F -- the twenty-six user routes, driven through the test
client with the API switched on, so that each route's body runs and every
response is validated against the schema its route declares.
"""
import pytest
from flask import current_app, g

from app import db
from app.constants import NOTIF_USER
from app.models import (Language, Notification, Site, User, UserBlock,
                        UserFollower, UserNote)
from tests.factories import (a_keypair, make_community, make_community_member,
                             make_domain, make_notification, make_post,
                             make_post_reply, make_user)

MISSING = 999999


def auth(user):
    return {'Authorization': f'Bearer {user.encode_jwt_token()}'}


@pytest.fixture
def env(app, api_baseline, monkeypatch):
    from types import SimpleNamespace

    g.admin_ids = [api_baseline.user1.id]
    g.site = db.session.get(Site, 1)
    monkeypatch.setitem(current_app.config, 'ENABLE_ALPHA_API', 'true')
    for code, name in [('en', 'English'), ('und', 'Undetermined')]:
        db.session.add(Language(code=code, name=name))
    db.session.commit()
    db.session.get(Site, 1).language_id = Language.query.filter_by(
        code='en').one().id
    community = make_community('probeland')
    reader = api_baseline.user2
    other = api_baseline.user3
    for who in (reader, other):
        who.private_key, who.public_key = a_keypair()
    db.session.commit()
    make_community_member(reader, community)
    post = make_post(community, reader, ap_id='https://test.piefed.local/x/1')
    db.session.commit()
    return SimpleNamespace(client=app.test_client(), community=community,
                           reader=reader, other=other, post=post,
                           admin=api_baseline.user1,
                           baseline=api_baseline)


class TestReadingAccounts:
    def test_one_account(self, env):
        response = env.client.get('/api/alpha/user',
                                  query_string={'person_id': env.reader.id})
        assert response.status_code == 200
        assert response.get_json()['person_view'][
            'person']['id'] == env.reader.id

    def test_an_account_nobody_holds(self, env):
        response = env.client.get('/api/alpha/user',
                                  query_string={'person_id': MISSING})
        assert response.status_code == 400

    def test_my_own_account(self, env):
        response = env.client.get('/api/alpha/user/me',
                                  headers=auth(env.reader))
        assert response.status_code == 200
        assert response.get_json()['local_user_view'][
            'person']['id'] == env.reader.id

    def test_my_own_account_without_an_account(self, env):
        response = env.client.get('/api/alpha/user/me')
        assert response.status_code == 400

    def test_the_media_i_have_posted(self, env):
        response = env.client.get('/api/alpha/user/media',
                                  headers=auth(env.reader),
                                  query_string={'person_id': env.reader.id})
        assert response.status_code == 200
        assert 'media' in response.get_json()

    def test_the_replies_to_me(self, env):
        response = env.client.get('/api/alpha/user/replies',
                                  headers=auth(env.reader))
        assert response.status_code == 200
        assert 'replies' in response.get_json()

    def test_the_mentions_of_me(self, env):
        response = env.client.get('/api/alpha/user/mentions',
                                  headers=auth(env.reader))
        assert response.status_code == 200
        # The mentions feed answers under `replies`, as /user/replies does.
        assert 'replies' in response.get_json()


class TestSigningIn:
    def test_signing_in(self, env):
        env.reader.set_password('a good password')
        db.session.commit()
        response = env.client.post('/api/alpha/user/login',
                                   json={'username': env.reader.user_name,
                                         'password': 'a good password'})
        assert response.status_code == 200
        assert response.get_json()['jwt']

    def test_signing_in_with_the_wrong_password(self, env):
        env.reader.set_password('a good password')
        db.session.commit()
        response = env.client.post('/api/alpha/user/login',
                                   json={'username': env.reader.user_name,
                                         'password': 'the wrong one'})
        assert response.status_code == 400

    def test_checking_credentials(self, env):
        env.reader.set_password('a good password')
        db.session.commit()
        response = env.client.post('/api/alpha/user/verify_credentials',
                                   json={'username': env.reader.user_name,
                                         'password': 'a good password'})
        assert response.status_code == 200

    def test_signing_out(self, env):
        response = env.client.post('/api/alpha/user/logout',
                                   headers=auth(env.reader), json={})
        assert response.status_code == 200

    def test_a_captcha(self, env):
        response = env.client.get('/api/alpha/user/get_captcha')
        assert response.status_code in (200, 400)


class TestNotifications:
    def test_the_unread_count(self, env):
        response = env.client.get('/api/alpha/user/unread_count',
                                  headers=auth(env.reader))
        assert response.status_code == 200
        assert 'replies' in response.get_json()

    def test_the_notification_count(self, env):
        response = env.client.get('/api/alpha/user/notifications_count',
                                  headers=auth(env.reader))
        assert response.status_code == 200
        assert 'count' in response.get_json()

    def test_the_notifications(self, env):
        response = env.client.get('/api/alpha/user/notifications',
                                  headers=auth(env.reader),
                                  query_string={'status': 'All'})
        assert response.status_code == 200
        assert 'items' in response.get_json()

    def test_marking_everything_read(self, env):
        response = env.client.post('/api/alpha/user/mark_all_as_read',
                                   headers=auth(env.reader), json={})
        assert response.status_code == 200

    def test_marking_every_notification_read(self, env):
        response = env.client.put(
            '/api/alpha/user/mark_all_notifications_read',
            headers=auth(env.reader), json={})
        assert response.status_code == 200

    def test_changing_one_notification(self, env):
        # Built here rather than with make_notification: that factory writes
        # `targets={'post_id': ...}` for delete_object's cleanup query, and a
        # NOTIF_POST notification as the product writes it (app/activitypub/
        # util.py) also carries `comment_id`, which is what the notification
        # view reads.
        from app.constants import NOTIF_POST
        reply = make_post_reply(env.post, env.other, body='a comment')
        reply.path = [0, reply.id]
        db.session.commit()
        notification = Notification(
            title='a notification', user_id=env.reader.id,
            author_id=env.other.id, notif_type=NOTIF_POST,
            subtype='top_level_comment_on_followed_post',
            targets={'gen': '0', 'post_id': env.post.id,
                     'comment_id': reply.id})
        db.session.add(notification)
        db.session.commit()
        response = env.client.put('/api/alpha/user/notification_state',
                                  headers=auth(env.reader),
                                  json={'notif_id': notification.id,
                                        'read_state': True})
        assert response.status_code == 200


class TestRelationships:
    def test_blocking_and_unblocking_someone(self, env):
        for block in (True, False):
            response = env.client.post('/api/alpha/user/block',
                                       headers=auth(env.reader),
                                       json={'person_id': env.other.id,
                                             'block': block})
            assert response.status_code == 200
        assert UserBlock.query.filter_by(blocker_id=env.reader.id).count() == 0

    def test_subscribing_to_someone(self, env):
        response = env.client.put('/api/alpha/user/subscribe',
                                  headers=auth(env.reader),
                                  json={'person_id': env.other.id,
                                        'subscribe': True})
        assert response.status_code == 200

    def test_following_and_unfollowing(self, env):
        # `user_id`, not `person_id`: these two name it that way
        # (UserFollowRequest, UserUnfollowRequest).
        response = env.client.post('/api/alpha/user/follow',
                                   headers=auth(env.reader),
                                   json={'user_id': env.other.id})
        assert response.status_code == 200
        response = env.client.post('/api/alpha/user/unfollow',
                                   headers=auth(env.reader),
                                   json={'user_id': env.other.id})
        assert response.status_code == 200

    def test_a_note_about_someone(self, env):
        response = env.client.post('/api/alpha/user/note',
                                   headers=auth(env.reader),
                                   json={'person_id': env.other.id,
                                         'note': 'remember this'})
        assert response.status_code == 200
        assert UserNote.query.filter_by(user_id=env.reader.id,
                                        target_id=env.other.id).one().body == \
            'remember this'

    def test_blocking_a_domain(self, env):
        domain = make_domain('example.test')
        # The request names the domain by NAME, not by id
        # (DomainBlockRequest).
        response = env.client.post('/api/alpha/domain/block',
                                   headers=auth(env.reader),
                                   json={'domain': domain.name,
                                         'block': True})
        assert response.status_code == 200


class TestSettingsAndFlair:
    def test_saving_settings(self, env):
        response = env.client.put('/api/alpha/user/save_user_settings',
                                  headers=auth(env.reader),
                                  json={'show_nsfw': False,
                                        'default_sort_type': 'New'})
        assert response.status_code == 200

    def test_setting_flair_in_a_community(self, env):
        response = env.client.post('/api/alpha/user/set_flair',
                                   headers=auth(env.reader),
                                   json={'community_id': env.community.id,
                                         'flair': 'a regular'})
        assert response.status_code == 200


class TestAdministering:
    def test_an_administrator_bans_and_unbans(self, env):
        response = env.client.post('/api/alpha/user/ban',
                                   headers=auth(env.admin),
                                   json={'person_id': env.other.id,
                                         'ban': True,
                                         'remove_data': False,
                                         'ban_ip_address': False,
                                         'purge_content': False,
                                         'reason': 'spam'})
        assert response.status_code == 200
        assert db.session.get(User, env.other.id).banned is True
        response = env.client.post('/api/alpha/user/unban',
                                   headers=auth(env.admin),
                                   json={'person_id': env.other.id,
                                         'ban': False,
                                         'reason': 'sorry'})
        assert response.status_code == 200
        assert db.session.get(User, env.other.id).banned is False

    def test_an_account_with_no_standing_cannot_ban(self, env):
        response = env.client.post('/api/alpha/user/ban',
                                   headers=auth(env.reader),
                                   json={'person_id': env.other.id,
                                         'ban': True,
                                         'remove_data': False,
                                         'ban_ip_address': False,
                                         'purge_content': False,
                                         'reason': 'spam'})
        assert response.status_code == 400
        assert db.session.get(User, env.other.id).banned is False



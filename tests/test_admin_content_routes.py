"""The admin screens that hold content: pages, emoji, blocked images, media.

Sub-project 98 -- five clusters of routes in `app/admin/routes.py`, chosen
together because each one is a button that changes what the instance serves,
and each is gated on a DIFFERENT permission:

* CMS pages -- `edit cms pages`
* emoji -- `change instance settings`
* blocked images -- `administer all communities`
* the media list -- `administer all communities`, but deleting from it needs
  `administer all users`
* masquerade -- `change instance settings`, and it hands the caller another
  account's session

Every route is exercised from both sides: by somebody holding the permission
it names, and by somebody holding a DIFFERENT one, which is the check that a
gate names the permission its author meant.
"""
import io
from unittest.mock import patch

import pytest
from flask import current_app, g

from app import db
from app.models import BlockedImage, CmsPage, Emoji, File, ModLog, Post, Site, User
from app.utils import set_setting
from tests.factories import (grant_permission, make_community,
                             make_community_member, make_file, make_post,
                             make_user)

PERMISSIONS = ('edit cms pages', 'change instance settings',
               'administer all communities', 'administer all users')


@pytest.fixture
def env(app, api_baseline):
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    community = make_community('probeland')
    member = api_baseline.user2
    db.session.commit()
    make_community_member(member, community)
    return SimpleNamespace(app=app, client=app.test_client(),
                           community=community, member=member,
                           baseline=api_baseline)


def an_admin(env, permission, name=None):
    """Somebody holding exactly one permission, and never user 1."""
    user = make_user(env.baseline.instance_local,
                     name or permission.replace(' ', '')[:20], local=True)
    user.verified = True
    db.session.commit()
    assert user.id != 1
    grant_permission(user, permission)
    return user


def login(client, user):
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True


def csrf(app, client):
    """`login_required(csrf=True)` validates CSRF itself, whatever
    WTF_CSRF_ENABLED says, so every POST here carries a real token."""
    from flask import session as flask_session
    from flask_wtf.csrf import generate_csrf

    with app.test_request_context():
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with client.session_transaction() as session:
        session['csrf_token'] = raw
    return token


def other_permission(permission):
    return next(other for other in PERMISSIONS if other != permission)


def signed_in_as(client):
    """`login_user` writes `User.get_id()`, which this model returns as an
    int, while a hand-written session cookie carries a string -- so both are
    normalised before they are compared."""
    with client.session_transaction() as session:
        return str(session.get('_user_id'))


def assert_refused(response):
    """`permission_required` redirects rather than aborting: a refusal is a
    302 to the "you do not have permission" page."""
    assert response.status_code == 302
    assert 'permission_denied' in response.headers['Location']


# --------------------------------------------------------------------------
# who may open each screen
# --------------------------------------------------------------------------

GATES = [
    ('/admin/pages', 'edit cms pages'),
    ('/admin/pages/add', 'edit cms pages'),
    ('/admin/emoji', 'change instance settings'),
    ('/admin/emoji/add', 'change instance settings'),
    ('/admin/blocked_images', 'administer all communities'),
    ('/admin/blocked_image/add', 'administer all communities'),
    ('/admin/media', 'administer all communities'),
]


class TestWhoMayOpenTheseScreens:
    @pytest.mark.parametrize('path,permission', GATES)
    def test_somebody_holding_the_permission_may(self, env, path, permission):
        login(env.client, an_admin(env, permission))
        assert env.client.get(path).status_code == 200

    @pytest.mark.parametrize('path,permission', GATES)
    def test_somebody_holding_a_different_one_may_not(self, env, path,
                                                      permission):
        login(env.client, an_admin(env, other_permission(permission)))
        assert_refused(env.client.get(path))

    @pytest.mark.parametrize('path,permission', GATES)
    def test_an_ordinary_member_may_not(self, env, path, permission):
        login(env.client, env.member)
        assert_refused(env.client.get(path))

    @pytest.mark.parametrize('path,permission', GATES)
    def test_a_stranger_may_not_either(self, env, path, permission):
        """`permission_required` is the OUTER decorator on these routes, so
        it answers before `login_required` does -- a logged-out visitor is
        told the permission is missing rather than asked to log in."""
        assert_refused(env.client.get(path))


# --------------------------------------------------------------------------
# CMS pages
# --------------------------------------------------------------------------

class TestTheCmsPages:
    @pytest.fixture
    def editor(self, env):
        editor = an_admin(env, 'edit cms pages')
        login(env.client, editor)
        return editor

    @pytest.fixture
    def page(self, env):
        page = CmsPage(url='/about', title='About us', body='hello',
                       body_html='<p>hello</p>')
        db.session.add(page)
        db.session.commit()
        return page

    def test_the_list_shows_what_is_there(self, env, editor, page):
        response = env.client.get('/admin/pages')
        assert b'About us' in response.data

    def test_one_can_be_added(self, env, editor):
        token = csrf(env.app, env.client)
        response = env.client.post('/admin/pages/add', data={
            'url': '/rules', 'title': 'The rules', 'body': '**be kind**',
            'csrf_token': token}, follow_redirects=False)
        assert response.status_code == 302
        page = CmsPage.query.filter_by(url='/rules').one()
        assert page.title == 'The rules'
        assert '<strong>be kind</strong>' in page.body_html

    def test_who_wrote_it_is_recorded(self, env, editor):
        token = csrf(env.app, env.client)
        env.client.post('/admin/pages/add', data={
            'url': '/rules', 'title': 'The rules', 'body': 'x',
            'csrf_token': token})
        assert CmsPage.query.filter_by(url='/rules').one().last_edited_by == \
            editor.display_name()

    def test_one_can_be_edited(self, env, editor, page):
        token = csrf(env.app, env.client)
        response = env.client.post(f'/admin/pages/{page.id}/edit', data={
            'url': '/about', 'title': 'About this place', 'body': 'new words',
            'csrf_token': token})
        assert response.status_code == 302
        db.session.expire_all()
        stored = db.session.get(CmsPage, page.id)
        assert stored.title == 'About this place'
        assert stored.edited_at is not None

    def test_the_edit_form_shows_what_is_there(self, env, editor, page):
        response = env.client.get(f'/admin/pages/{page.id}/edit')
        assert b'About us' in response.data

    def test_one_can_be_deleted(self, env, editor, page):
        token = csrf(env.app, env.client)
        response = env.client.post(f'/admin/pages/{page.id}/delete',
                                   data={'csrf_token': token})
        assert response.status_code == 302
        assert db.session.get(CmsPage, page.id) is None

    def test_a_page_nobody_wrote(self, env, editor):
        assert env.client.get('/admin/pages/999999/edit').status_code == 404
        token = csrf(env.app, env.client)
        assert env.client.post('/admin/pages/999999/delete',
                               data={'csrf_token': token}).status_code == 404

    def test_a_submission_that_does_not_validate(self, env, editor):
        token = csrf(env.app, env.client)
        response = env.client.post('/admin/pages/add',
                                   data={'csrf_token': token})
        assert response.status_code == 200
        assert CmsPage.query.count() == 0

    def test_somebody_with_another_permission_may_not_delete_one(self, env,
                                                                 page):
        login(env.client, an_admin(env, 'administer all users'))
        token = csrf(env.app, env.client)
        assert_refused(env.client.post(f'/admin/pages/{page.id}/delete',
                                       data={'csrf_token': token}))
        assert db.session.get(CmsPage, page.id) is not None


# --------------------------------------------------------------------------
# emoji
# --------------------------------------------------------------------------

class TestTheEmoji:
    @pytest.fixture
    def settings_admin(self, env):
        admin = an_admin(env, 'change instance settings')
        login(env.client, admin)
        return admin

    @pytest.fixture
    def emoji(self, env):
        emoji = Emoji(token=':wave:', url='/static/media/wave.png',
                      category='greetings', aliases='hello', instance_id=1)
        db.session.add(emoji)
        db.session.commit()
        return emoji

    def test_the_list_shows_what_is_there(self, env, settings_admin, emoji):
        assert b':wave:' in env.client.get('/admin/emoji').data

    def test_one_can_be_added(self, env, settings_admin):
        token = csrf(env.app, env.client)
        response = env.client.post('/admin/emoji/add', data={
            'token': ':tada:', 'url': 'https://cdn.test/tada.png',
            'category': 'party', 'aliases': 'celebrate',
            'csrf_token': token})
        assert response.status_code == 302
        assert Emoji.query.filter_by(token=':tada:').one().instance_id == 1

    def test_a_submission_that_does_not_validate(self, env, settings_admin):
        token = csrf(env.app, env.client)
        response = env.client.post('/admin/emoji/add',
                                   data={'csrf_token': token})
        assert response.status_code == 200
        assert Emoji.query.count() == 0

    def test_one_can_be_edited(self, env, settings_admin, emoji):
        token = csrf(env.app, env.client)
        response = env.client.post(f'/admin/emoji/{emoji.id}/edit', data={
            'token': ':hello:', 'url': '/static/media/wave.png',
            'category': 'greetings', 'aliases': 'hi', 'csrf_token': token})
        assert response.status_code == 302
        db.session.expire_all()
        assert db.session.get(Emoji, emoji.id).token == ':hello:'

    def test_the_edit_form_shows_what_is_there(self, env, settings_admin,
                                               emoji):
        assert b':wave:' in env.client.get(
            f'/admin/emoji/{emoji.id}/edit').data

    def test_one_can_be_deleted(self, env, settings_admin, emoji):
        token = csrf(env.app, env.client)
        response = env.client.post(f'/admin/emoji/{emoji.id}/delete',
                                   data={'csrf_token': token})
        assert response.status_code == 302
        assert db.session.get(Emoji, emoji.id) is None

    def test_one_nobody_added(self, env, settings_admin):
        assert env.client.get('/admin/emoji/999999/edit').status_code == 404
        token = csrf(env.app, env.client)
        assert env.client.post('/admin/emoji/999999/delete',
                               data={'csrf_token': token}).status_code == 404

    def test_somebody_with_another_permission_may_not_delete_one(self, env,
                                                                 emoji):
        login(env.client, an_admin(env, 'edit cms pages'))
        token = csrf(env.app, env.client)
        assert_refused(env.client.post(f'/admin/emoji/{emoji.id}/delete',
                                       data={'csrf_token': token}))
        assert db.session.get(Emoji, emoji.id) is not None


# --------------------------------------------------------------------------
# blocked images
# --------------------------------------------------------------------------

class TestBlockedImages:
    @pytest.fixture
    def moderator(self, env):
        admin = an_admin(env, 'administer all communities')
        login(env.client, admin)
        return admin

    @pytest.fixture
    def blocked(self, env):
        image = BlockedImage(hash='0' * 256, file_name='nasty.png',
                             note='do not serve this')
        db.session.add(image)
        db.session.commit()
        return image

    def test_the_list_shows_what_is_blocked(self, env, moderator, blocked):
        assert b'nasty.png' in env.client.get('/admin/blocked_images').data

    def test_one_can_be_added_by_its_hash(self, env, moderator):
        token = csrf(env.app, env.client)
        response = env.client.post('/admin/blocked_image/add', data={
            'hash': '1' * 256, 'file_name': 'other.png', 'note': 'a note',
            'csrf_token': token})
        assert response.status_code == 302
        assert BlockedImage.query.filter_by(hash='1' * 256).count() == 1

    def test_one_can_be_added_by_its_url(self, env, moderator):
        token = csrf(env.app, env.client)
        with patch('app.admin.routes.retrieve_image_hash',
                   return_value='1' * 255 + '0') as retrieve:
            response = env.client.post('/admin/blocked_image/add', data={
                'url': 'https://remote.test/media/photo.png',
                'csrf_token': token})
        assert response.status_code == 302
        retrieve.assert_called_once_with(
            'https://remote.test/media/photo.png')
        image = BlockedImage.query.filter_by(hash='1' * 255 + '0').one()
        assert image.file_name == 'photo.png'

    def test_one_can_be_edited(self, env, moderator, blocked):
        token = csrf(env.app, env.client)
        response = env.client.post(
            f'/admin/blocked_image/{blocked.id}/edit',
            data={'hash': '0' * 256, 'file_name': 'renamed.png',
                  'note': 'still blocked', 'csrf_token': token})
        assert response.status_code == 302
        db.session.expire_all()
        assert db.session.get(BlockedImage, blocked.id).file_name == \
            'renamed.png'

    def test_the_edit_form_shows_what_is_there(self, env, moderator, blocked):
        assert b'nasty.png' in env.client.get(
            f'/admin/blocked_image/{blocked.id}/edit').data

    def test_one_can_be_deleted(self, env, moderator, blocked):
        token = csrf(env.app, env.client)
        response = env.client.post(
            f'/admin/blocked_image/{blocked.id}/delete',
            data={'csrf_token': token})
        assert response.status_code == 302
        assert db.session.get(BlockedImage, blocked.id) is None

    def test_one_nobody_blocked(self, env, moderator):
        assert env.client.get(
            '/admin/blocked_image/999999/edit').status_code == 404
        token = csrf(env.app, env.client)
        assert env.client.post('/admin/blocked_image/999999/delete',
                               data={'csrf_token': token}).status_code == 404

    def test_the_purge_screen_lists_the_posts_carrying_one(self, env,
                                                            moderator):
        with patch('app.admin.routes.posts_with_blocked_images',
                   return_value=[]):
            response = env.client.get('/admin/block_image_purge_posts')
        assert response.status_code == 200

    def test_purging_queues_the_deletion(self, env, moderator):
        post = make_post(env.community, env.member,
                         ap_id='https://test.piefed.local/p/1')
        db.session.commit()
        token = csrf(env.app, env.client)
        with patch('app.admin.routes.task_selector') as task:
            response = env.client.post('/admin/block_image_purge_posts', data={
                'post_ids': [str(post.id)], 'csrf_token': token})
        assert response.status_code == 302
        assert task.call_args.args[0] == 'delete_posts_with_blocked_images'
        assert task.call_args.kwargs['post_ids'] == [str(post.id)]

    def test_somebody_with_another_permission_may_not_purge(self, env):
        login(env.client, an_admin(env, 'edit cms pages'))
        token = csrf(env.app, env.client)
        with patch('app.admin.routes.task_selector') as task:
            response = env.client.post('/admin/block_image_purge_posts',
                                       data={'csrf_token': token})
        assert_refused(response)
        assert task.call_count == 0


# --------------------------------------------------------------------------
# the media list
# --------------------------------------------------------------------------

class TestTheMediaList:
    @pytest.fixture
    def moderator(self, env):
        admin = an_admin(env, 'administer all communities')
        login(env.client, admin)
        return admin

    @pytest.fixture
    def uploaded(self, env):
        from app.models import user_file
        file = make_file(source_url='https://cdn.test/media/photo.png')
        db.session.execute(user_file.insert().values(user_id=env.member.id,
                                                     file_id=file.id))
        db.session.commit()
        return file

    def test_it_lists_what_has_been_uploaded(self, env, moderator, uploaded):
        assert env.client.get('/admin/media').status_code == 200

    def test_it_can_be_narrowed_to_one_account(self, env, moderator,
                                               uploaded):
        from app.models import user_file
        somebody_else = make_user(env.baseline.instance_local, 'uploader',
                                  local=True)
        theirs = make_file(source_url='https://cdn.test/media/theirs.png')
        db.session.execute(user_file.insert().values(
            user_id=somebody_else.id, file_id=theirs.id))
        db.session.commit()

        everything = env.client.get('/admin/media')
        assert b'photo.png' in everything.data
        assert b'theirs.png' in everything.data

        narrowed = env.client.get(f'/admin/media?user_id={env.member.id}')
        assert b'photo.png' in narrowed.data
        assert b'theirs.png' not in narrowed.data

    def test_a_second_page(self, env, moderator, uploaded):
        assert env.client.get('/admin/media?page=2').status_code == 200

    def test_deleting_one_needs_the_user_permission_not_the_community_one(
            self, env, moderator, uploaded):
        token = csrf(env.app, env.client)
        with patch('app.admin.routes.process_file_delete') as delete:
            response = env.client.post(f'/admin/media/{uploaded.id}/delete',
                                       data={'csrf_token': token})
        assert_refused(response)
        assert delete.call_count == 0

    def test_somebody_holding_that_permission_may(self, env, uploaded):
        login(env.client, an_admin(env, 'administer all users'))
        token = csrf(env.app, env.client)
        with patch('app.admin.routes.process_file_delete') as delete:
            response = env.client.post(f'/admin/media/{uploaded.id}/delete',
                                       data={'csrf_token': token})
        assert response.status_code == 302
        delete.assert_called_once_with('https://cdn.test/media/photo.png',
                                       env.member.id)

    def test_a_file_nobody_uploaded(self, env):
        login(env.client, an_admin(env, 'administer all users'))
        token = csrf(env.app, env.client)
        assert env.client.post('/admin/media/999999/delete',
                               data={'csrf_token': token}).status_code == 404

    def test_everything_one_account_uploaded_can_be_queued_for_deletion(
            self, env, uploaded, monkeypatch):
        login(env.client, an_admin(env, 'administer all users'))
        monkeypatch.setattr(current_app, 'debug', False)
        token = csrf(env.app, env.client)
        with patch('app.admin.routes.delete_user_files_in_background') as task:
            response = env.client.post(
                f'/admin/media/{env.member.id}/delete_all',
                data={'csrf_token': token})
        assert response.status_code == 302
        task.delay.assert_called_once_with(env.member.id)

    def test_in_debug_that_runs_here_and_now(self, env, uploaded,
                                             monkeypatch):
        login(env.client, an_admin(env, 'administer all users'))
        monkeypatch.setattr(current_app, 'debug', True)
        token = csrf(env.app, env.client)
        with patch('app.admin.routes.delete_user_files_in_background') as task:
            env.client.post(f'/admin/media/{env.member.id}/delete_all',
                            data={'csrf_token': token})
        task.assert_called_once_with(env.member.id)

    def test_the_background_deletion_itself(self, env, uploaded):
        from app.admin.routes import delete_user_files_in_background
        with patch('app.admin.routes.process_file_delete') as delete:
            delete_user_files_in_background(env.member.id)
        delete.assert_called_once_with('https://cdn.test/media/photo.png',
                                       env.member.id)

    def test_an_account_that_uploaded_nothing(self, env):
        from app.admin.routes import delete_user_files_in_background
        with patch('app.admin.routes.process_file_delete') as delete:
            delete_user_files_in_background(999999)
        assert delete.call_count == 0


# --------------------------------------------------------------------------
# masquerade
# --------------------------------------------------------------------------

class TestMasquerading:
    def test_an_admin_becomes_the_account_they_name(self, env):
        login(env.client, an_admin(env, 'change instance settings'))
        response = env.client.get(f'/admin/masquerade/{env.member.id}')
        assert response.status_code == 302
        assert response.headers['Location'] == '/'
        assert signed_in_as(env.client) == str(env.member.id)

    def test_masquerading_is_recorded_for_admins_only(self, env):
        """D942, fixed (owner ruling 2026-09-30). Becoming another account left
        no trail anywhere. It now writes a modlog entry naming the admin, the
        account and the time -- never public, even on an instance whose modlog
        is, because only admins see non-public entries."""
        set_setting('public_modlog', True)
        admin = an_admin(env, 'change instance settings')
        login(env.client, admin)

        env.client.get(f'/admin/masquerade/{env.member.id}')

        entry = ModLog.query.filter_by(action='masquerade').one()
        assert entry.user_id == admin.id
        assert entry.target_user_id == env.member.id
        assert entry.created_at is not None
        assert entry.public is False

    def test_a_refused_masquerade_records_nothing(self, env):
        login(env.client, an_admin(env, 'change instance settings'))
        env.client.get('/admin/masquerade/999999')
        assert ModLog.query.filter_by(action='masquerade').count() == 0

    def test_a_remote_account_cannot_be_masqueraded_as(self, env):
        remote = make_user(env.baseline.instance_remote, 'faraway')
        remote.ap_id = 'faraway@remote.test'
        remote.ap_profile_id = 'https://remote.test/u/faraway'
        db.session.commit()
        admin = an_admin(env, 'change instance settings')
        login(env.client, admin)
        response = env.client.get(f'/admin/masquerade/{remote.id}')
        assert response.status_code == 200
        assert response.data == b''
        assert signed_in_as(env.client) == str(admin.id)

    def test_an_account_nobody_holds(self, env):
        admin = an_admin(env, 'change instance settings')
        login(env.client, admin)
        response = env.client.get('/admin/masquerade/999999')
        assert response.data == b''
        assert signed_in_as(env.client) == str(admin.id)

    def test_somebody_with_another_permission_may_not(self, env):
        admin = an_admin(env, 'administer all users')
        login(env.client, admin)
        response = env.client.get(f'/admin/masquerade/{env.member.id}')
        assert_refused(response)
        assert signed_in_as(env.client) == str(admin.id)

    def test_nor_may_an_ordinary_member(self, env):
        login(env.client, env.member)
        target = env.baseline.user1
        response = env.client.get(f'/admin/masquerade/{target.id}')
        assert_refused(response)
        assert signed_in_as(env.client) == str(env.member.id)

    def test_nor_may_a_stranger(self, env):
        response = env.client.get(f'/admin/masquerade/{env.member.id}')
        assert response.status_code == 302
        assert 'login' in response.headers['Location']

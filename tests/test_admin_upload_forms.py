"""The admin screens that take a file, submitted without one.

Sub-project 108 -- a sweep, not a module. `request.files['name']` raises
`werkzeug.exceptions.BadRequestKeyError` -- a 400 -- for a submission that does
not carry that field. A browser always sends the input, empty or not, so this
only shows up for every OTHER client: a script, a curl, a form whose file input
is rendered conditionally, or an edit screen where the input was left out of the
template.

That shape was found once before and fixed in one place: D1047, in
`app/user/routes.py`, whose comment says exactly this. Fifteen other sites kept
it. They are all `.get` now, and every one of them is followed by an
`if x and x.filename != '':` that already handled None (D1299).

The sites, by file: `app/admin/routes.py` (the site icon, the ban-list import,
a community's icon and banner, an account's avatar and banner),
`app/community/routes.py` (a new community's icon and banner, an edited one's,
and the image on a post being created), `app/feed/routes.py` (a feed's icon and
banner), `app/post/routes.py` (the image on a post being edited).

These tests drive the three admin ones, which is where the edit screens are; the
others are reached through forms whose own sub-projects cover them, and the
shape is asserted absent from all six files at the end.
"""
import io
import json
import os
from pathlib import Path

import pytest
from flask import current_app, g

from app import db
from app.models import BannedInstances, Community, Site, User
from tests.factories import (grant_permission, make_community,
                             make_community_member, make_user)


@pytest.fixture
def env(app, api_baseline):
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    # The community edit route always appends the 'und' language, and
    # `.append(None)` is a FlushError -- a seeded instance has this row, and a
    # test database only has it if the test puts it there.
    from app.models import Language
    for code, name in (('en', 'English'), ('und', 'Undetermined')):
        db.session.add(Language(code=code, name=name))
    db.session.commit()
    community = make_community('probeland')
    member = api_baseline.user2
    db.session.commit()
    make_community_member(member, community)
    return SimpleNamespace(app=app, client=app.test_client(),
                           community=community, member=member,
                           baseline=api_baseline)


def an_admin(env, permission):
    user = make_user(env.baseline.instance_local,
                     permission.replace(' ', '')[:20], local=True)
    user.verified = True
    db.session.commit()
    assert user.id != 1
    grant_permission(user, permission)
    with env.client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True
    return user


def csrf(app, client):
    from flask import session as flask_session
    from flask_wtf.csrf import generate_csrf

    with app.test_request_context():
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with client.session_transaction() as session:
        session['csrf_token'] = raw
    return token


def a_png():
    from PIL import Image
    buffer = io.BytesIO()
    Image.new('RGB', (10, 10), (1, 2, 3)).save(buffer, format='PNG')
    buffer.seek(0)
    return buffer


class TestEditingACommunityAsAnAdmin:
    def payload(self, env, token, **overrides):
        data = {'title': 'A community', 'url': env.community.name,
                'description': '', 'rules': '', 'content_retention': '-1',
                'topic': '-1', 'default_layout': '', 'posting_warning': '',
                'downvote_accept_mode': '0', 'csrf_token': token}
        data.update(overrides)
        return data

    def test_a_submission_with_no_file_fields_at_all(self, env):
        """D1299. `request.files['icon_file']` was a 400 for every client that
        does not send an empty file input -- which is every client that is not
        a browser."""
        an_admin(env, 'administer all communities')
        token = csrf(env.app, env.client)
        response = env.client.post(f'/admin/community/{env.community.id}/edit',
                                   data=self.payload(env, token))
        assert response.status_code == 302
        db.session.expire_all()
        assert db.session.get(Community, env.community.id).title == \
            'A community'

    def test_one_with_the_fields_present_but_empty(self, env):
        """What a browser sends."""
        an_admin(env, 'administer all communities')
        token = csrf(env.app, env.client)
        data = self.payload(env, token)
        data['icon_file'] = (io.BytesIO(b''), '')
        data['banner_file'] = (io.BytesIO(b''), '')
        response = env.client.post(f'/admin/community/{env.community.id}/edit',
                                   data=data,
                                   content_type='multipart/form-data')
        assert response.status_code == 302

    def test_one_that_does_carry_an_icon(self, env):
        an_admin(env, 'administer all communities')
        token = csrf(env.app, env.client)
        data = self.payload(env, token)
        data['icon_file'] = (a_png(), 'icon.png')
        response = env.client.post(f'/admin/community/{env.community.id}/edit',
                                   data=data,
                                   content_type='multipart/form-data')
        assert response.status_code == 302
        db.session.expire_all()
        community = db.session.get(Community, env.community.id)
        try:
            assert community.icon_id is not None
        finally:
            if community.icon and community.icon.file_path and \
                    os.path.exists(community.icon.file_path):
                os.unlink(community.icon.file_path)
            if community.icon and community.icon.thumbnail_path and \
                    os.path.exists(community.icon.thumbnail_path):
                os.unlink(community.icon.thumbnail_path)

    def test_a_community_nobody_hosts(self, env):
        an_admin(env, 'administer all communities')
        token = csrf(env.app, env.client)
        response = env.client.post('/admin/community/999999/edit',
                                   data=self.payload(env, token))
        assert response.status_code == 404

    def test_somebody_with_another_permission_may_not(self, env):
        an_admin(env, 'edit cms pages')
        token = csrf(env.app, env.client)
        response = env.client.post(f'/admin/community/{env.community.id}/edit',
                                   data=self.payload(env, token))
        assert response.status_code == 302
        assert 'permission_denied' in response.headers['Location']


class TestEditingAnAccountAsAnAdmin:
    def payload(self, env, token, **overrides):
        data = {'about': '', 'matrix_user_id': '', 'hide_nsfw': '1',
                'hide_nsfl': '1', 'role': '2', 'csrf_token': token}
        data.update(overrides)
        return data

    def test_a_submission_with_no_file_fields_at_all(self, env):
        """D1299, the avatar and banner sites."""
        an_admin(env, 'administer all users')
        token = csrf(env.app, env.client)
        response = env.client.post(f'/admin/user/{env.member.id}/edit',
                                   data=self.payload(env, token))
        assert response.status_code == 302

    def test_one_with_the_fields_present_but_empty(self, env):
        an_admin(env, 'administer all users')
        token = csrf(env.app, env.client)
        data = self.payload(env, token)
        data['profile_file'] = (io.BytesIO(b''), '')
        data['banner_file'] = (io.BytesIO(b''), '')
        response = env.client.post(f'/admin/user/{env.member.id}/edit',
                                   data=data,
                                   content_type='multipart/form-data')
        assert response.status_code == 302

    def test_an_account_nobody_holds(self, env):
        an_admin(env, 'administer all users')
        token = csrf(env.app, env.client)
        response = env.client.post('/admin/user/999999/edit',
                                   data=self.payload(env, token))
        assert response.status_code == 404


class TestImportingABanList:
    def test_a_submission_with_no_file_at_all(self, env):
        """D1299. The import button with nothing attached was a 400 rather
        than a form that says nothing was chosen."""
        an_admin(env, 'change instance settings')
        token = csrf(env.app, env.client)
        response = env.client.post('/admin/federation/ban_lists',
                                   data={'import_submit': 'Import',
                                         'csrf_token': token})
        assert response.status_code == 302
        assert 'ban_lists' in response.headers['Location']

    def test_one_with_the_field_present_but_empty(self, env):
        an_admin(env, 'change instance settings')
        token = csrf(env.app, env.client)
        response = env.client.post(
            '/admin/federation/ban_lists',
            data={'import_submit': 'Import', 'csrf_token': token,
                  'import_file': (io.BytesIO(b''), '')},
            content_type='multipart/form-data')
        assert response.status_code == 302

    def test_a_file_that_is_not_json(self, env):
        an_admin(env, 'change instance settings')
        token = csrf(env.app, env.client)
        response = env.client.post(
            '/admin/federation/ban_lists',
            data={'import_submit': 'Import', 'csrf_token': token,
                  'import_file': (io.BytesIO(b'not json'), 'bans.txt')},
            content_type='multipart/form-data')
        assert response.status_code == 400

    def test_a_list_that_names_instances_to_ban(self, env):
        """The route saves the upload into `app/static/media` before handing
        it to the import task, so this cleans up after itself --
        tests/test_admin_federation.py asserts that directory holds no json."""
        import glob
        an_admin(env, 'change instance settings')
        before = set(glob.glob('app/static/media/*.json'))
        token = csrf(env.app, env.client)
        payload = json.dumps({'banned_instances': ['nasty.test']}).encode()
        try:
            response = env.client.post(
                '/admin/federation/ban_lists',
                data={'import_submit': 'Import', 'csrf_token': token,
                      'import_file': (io.BytesIO(payload), 'bans.json')},
                content_type='multipart/form-data')
            assert response.status_code in (200, 302)
        finally:
            for path in set(glob.glob('app/static/media/*.json')) - before:
                os.unlink(path)

    def test_exporting_gives_a_file_back(self, env):
        an_admin(env, 'change instance settings')
        db.session.add(BannedInstances(domain='nasty.test'))
        db.session.commit()
        token = csrf(env.app, env.client)
        response = env.client.post('/admin/federation/ban_lists',
                                   data={'export_submit': 'Export',
                                         'csrf_token': token})
        assert response.status_code == 200

    def test_the_screen_itself(self, env):
        an_admin(env, 'change instance settings')
        assert env.client.get('/admin/federation/ban_lists').status_code == 200

    def test_somebody_with_another_permission_may_not(self, env):
        an_admin(env, 'edit cms pages')
        response = env.client.get('/admin/federation/ban_lists')
        assert response.status_code == 302
        assert 'permission_denied' in response.headers['Location']


class TestTheShapeIsGoneEverywhere:
    """The property, so that the sixteenth site is caught when it is written.

    `app/community/forms.py` keeps one `request.files['image_file']`, and that
    one is guarded by `if 'image_file' in request.files:` on the line above it
    -- which is the other correct way to write this.
    """

    FILES = ['app/admin/routes.py', 'app/community/routes.py',
             'app/feed/routes.py', 'app/post/routes.py',
             'app/user/routes.py', 'app/community/forms.py']

    def test_no_route_reads_a_file_field_by_key(self, env):
        offenders = []
        for name in self.FILES:
            lines = Path(name).read_text().splitlines()
            for number, line in enumerate(lines, start=1):
                stripped = line.strip()
                if stripped.startswith('#'):
                    continue
                if "request.files['" not in stripped:
                    continue
                guarded = any("in request.files" in previous
                              for previous in lines[max(0, number - 3):number])
                if not guarded:
                    offenders.append(f'{name}:{number}: {stripped}')
        assert offenders == []

    def test_the_sweep_is_looking_at_files_that_exist(self, env):
        for name in self.FILES:
            assert Path(name).is_file(), name
            assert 'request.files' in Path(name).read_text(), name

"""Round 239: replacing a community's icon, the content listing's day window, and the
registrations page's disposable-domain list.

Three clusters in `app/admin/routes.py`.

    1585, 1591-1595   the admin community form's image replacement. Uploading a new icon
                      or banner DELETES THE OLD FILE FROM DISK first, and then only
                      attaches the new one if saving it returned something.
    1893, 1897        the `?days=` window on the deleted-content listing, which is the only
                      one of the three views whose two filters differ from the others.
    1933-1942         the registrations queue's disposable-domain list, read from a file
                      this repository does not ship.

The icon rows carry the weight: `community.icon.delete_from_disk()` runs before
`save_icon_file`, so a community whose upload is rejected has lost its old icon either way,
and a mistake in which File object is deleted takes somebody else's image with it.
"""
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import g

from app import db
from app.models import (Community, File, Language, Site, User, UserRegistration, utcnow)
from tests.factories import grant_permission, make_community, make_user


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = [api_baseline.user1.id]
    site = db.session.get(Site, 1)
    site.private_instance = False
    site.enable_nsfw = True
    site.enable_nsfl = True
    g.site = site
    for code, name in (('en', 'English'), ('und', 'Undetermined')):
        if Language.query.filter(Language.code == code).first() is None:
            db.session.add(Language(code=code, name=name))
    db.session.commit()
    admin = api_baseline.user1
    grant_permission(admin, 'administer all communities')
    grant_permission(admin, 'approve registrations')
    db.session.commit()
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(admin.id)
        session['_fresh'] = True
    return SimpleNamespace(app=app, client=client, site=site, admin=admin,
                           baseline=api_baseline)


def csrf(app, client):
    from flask import session as flask_session
    from flask_wtf.csrf import generate_csrf

    with app.test_request_context():
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with client.session_transaction() as session:
        session['csrf_token'] = raw
    return token


def _payload(env, community, **overrides):
    data = {'csrf_token': csrf(env.app, env.client),
            'url': community.name, 'title': community.title or 'A Title',
            'description': '', 'rules': '', 'content_retention': -1,
            'topic': -1, 'default_layout': '', 'posting_warning': '',
            'downvote_accept_mode': 0}
    data.update(overrides)
    return {key: value for key, value in data.items() if value is not None}


def _png():
    import io

    return io.BytesIO(
        b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06'
        b'\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00'
        b'\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82')


# --------------------------------------------------------------------------
# Replacing a community's icon and banner
# --------------------------------------------------------------------------


class TestReplacingACommunitysImages:

    @pytest.fixture
    def seeded(self, env):
        community = make_community('picturetown')
        old_icon = File(file_path='app/static/media/communities/old_icon.png',
                        source_url='https://test.piefed.local/old_icon.png')
        old_banner = File(file_path='app/static/media/communities/old_banner.png',
                          source_url='https://test.piefed.local/old_banner.png')
        db.session.add_all([old_icon, old_banner])
        db.session.commit()
        community.icon_id = old_icon.id
        community.image_id = old_banner.id
        db.session.commit()
        env.community = community
        env.old_icon = old_icon
        env.old_banner = old_banner
        return env

    def _edit(self, env, files=None, **overrides):
        data = _payload(env, env.community, **overrides)
        if files:
            data.update(files)
        return env.client.post(f'/admin/community/{env.community.id}/edit',
                               data=data, content_type='multipart/form-data')

    def test_a_new_icon_replaces_the_old_one_and_deletes_it_from_disk(self, seeded):
        """`community.icon.delete_from_disk()` then `save_icon_file(...)`. The old File row
        stays in the database; what goes is the file on disk, which is why the assertion is
        on that call and on the NEW attachment."""
        deleted = []
        new_file = File(file_path='app/static/media/communities/new_icon.png',
                        source_url='https://test.piefed.local/new_icon.png')

        with patch('app.admin.routes.save_icon_file', return_value=new_file), \
                patch.object(File, 'delete_from_disk',
                             autospec=True,
                             side_effect=lambda self: deleted.append(self.file_path)):
            response = self._edit(
                seeded, files={'icon_file': (_png(), 'new_icon.png')})

        assert response.status_code == 302
        db.session.expire_all()
        community = db.session.get(Community, seeded.community.id)
        assert community.icon.file_path == 'app/static/media/communities/new_icon.png'
        assert deleted == ['app/static/media/communities/old_icon.png']

    def test_a_new_banner_does_the_same_for_the_banner(self, seeded):
        """The second copy of the block, on `image_id` rather than `icon_id`. Two copies is
        two places to name the wrong column, and the row asserts the icon is untouched."""
        deleted = []
        new_file = File(file_path='app/static/media/communities/new_banner.png',
                        source_url='https://test.piefed.local/new_banner.png')

        with patch('app.admin.routes.save_banner_file', return_value=new_file), \
                patch.object(File, 'delete_from_disk',
                             autospec=True,
                             side_effect=lambda self: deleted.append(self.file_path)):
            self._edit(seeded, files={'banner_file': (_png(), 'new_banner.png')})

        db.session.expire_all()
        community = db.session.get(Community, seeded.community.id)
        assert community.image.file_path == 'app/static/media/communities/new_banner.png'
        assert community.icon.file_path == 'app/static/media/communities/old_icon.png'
        assert deleted == ['app/static/media/communities/old_banner.png']

    def test_an_empty_file_field_changes_nothing(self, seeded):
        """`if icon_file and icon_file.filename != ''`. A browser submits the field with an
        empty filename when the admin picks nothing, and that must not delete the icon the
        community already has -- which is what the guard is for."""
        deleted = []

        with patch('app.admin.routes.save_icon_file') as save, \
                patch.object(File, 'delete_from_disk',
                             autospec=True,
                             side_effect=lambda self: deleted.append(self.file_path)):
            self._edit(seeded, files={'icon_file': (_png(), '')})

        assert save.call_count == 0
        assert deleted == []
        db.session.expire_all()
        assert db.session.get(Community, seeded.community.id).icon.file_path == \
            'app/static/media/communities/old_icon.png'

    def test_a_save_that_returns_nothing_leaves_the_community_without_an_icon(self,
                                                                             seeded):
        """`if file:` -- and the honest consequence, asserted rather than glossed: the old
        file has ALREADY been deleted from disk by the line above, so a save that returns
        None leaves the community pointing at a File row whose image is gone. Recorded
        because the ordering is the design: the delete cannot be deferred without keeping
        the old path around, and `save_icon_file` aborts rather than returning None for
        every input it rejects."""
        deleted = []

        with patch('app.admin.routes.save_icon_file', return_value=None), \
                patch.object(File, 'delete_from_disk',
                             autospec=True,
                             side_effect=lambda self: deleted.append(self.file_path)):
            self._edit(seeded, files={'icon_file': (_png(), 'new_icon.png')})

        assert deleted == ['app/static/media/communities/old_icon.png']
        db.session.expire_all()
        assert db.session.get(Community, seeded.community.id).icon_id == \
            seeded.old_icon.id

    def test_a_community_with_no_icon_yet_just_gets_one(self, env):
        """`if community.icon_id:` -- the first upload has nothing to delete."""
        community = make_community('newlywed')
        db.session.commit()
        env.community = community
        deleted = []
        new_file = File(file_path='app/static/media/communities/first_icon.png',
                        source_url='https://test.piefed.local/first_icon.png')

        with patch('app.admin.routes.save_icon_file', return_value=new_file), \
                patch.object(File, 'delete_from_disk',
                             autospec=True,
                             side_effect=lambda self: deleted.append(self.file_path)):
            self._edit(env, files={'icon_file': (_png(), 'first_icon.png')})

        assert deleted == []
        db.session.expire_all()
        assert db.session.get(Community, community.id).icon.file_path == \
            'app/static/media/communities/first_icon.png'


# --------------------------------------------------------------------------
# The deleted-content listing's day window
# --------------------------------------------------------------------------


class TestTheDeletedContentWindow:
    """`/admin/content?show=deleted` lists deleted posts and replies. Its `?days=` window
    filters on `posted_at` ALONE, unlike the other two views, which also require the
    AUTHOR to be recent -- so it is the one arm whose filter is not a copy of another.
    """

    @pytest.fixture
    def seeded(self, env):
        from datetime import timedelta

        from tests.factories import make_post

        community = make_community('deletedland')
        author = make_user(env.baseline.instance_local, 'deletedauthor', local=True)
        db.session.commit()
        recent = make_post(community, author,
                           ap_id='https://test.piefed.local/d/recent',
                           title='a recently deleted post')
        old = make_post(community, author, ap_id='https://test.piefed.local/d/old',
                        title='a long deleted post')
        for post in (recent, old):
            post.deleted = True
        recent.posted_at = utcnow() - timedelta(days=1)
        old.posted_at = utcnow() - timedelta(days=90)
        db.session.commit()
        return env

    def test_a_window_hides_content_older_than_it(self, seeded):
        response = seeded.client.get('/admin/content?show=deleted&days=7')

        body = response.get_data(as_text=True)
        assert response.status_code == 200
        assert 'a recently deleted post' in body
        assert 'a long deleted post' not in body

    def test_no_window_shows_everything(self, seeded):
        """`if days > 0`. `?days=0` is how the page offers 'all time', so the filter has to
        be skipped rather than applied with a zero-length window."""
        response = seeded.client.get('/admin/content?show=deleted&days=0')

        body = response.get_data(as_text=True)
        assert 'a recently deleted post' in body
        assert 'a long deleted post' in body

    def test_the_default_window_is_three_days(self, seeded):
        """`request.args.get('days', 3, type=int)`. Asserted because the default is what an
        admin opening the page from the menu gets, and it is narrow."""
        response = seeded.client.get('/admin/content?show=deleted')

        body = response.get_data(as_text=True)
        assert 'a recently deleted post' in body
        assert 'a long deleted post' not in body


# --------------------------------------------------------------------------
# The registrations queue's disposable-domain list
# --------------------------------------------------------------------------


class TestTheRegistrationsQueue:
    """The queue flags applicants whose email domain is on a disposable-address list. That
    list is a file this repository does not ship, read from a hardcoded relative path, and
    only when `FLAG_THROWAWAY_EMAILS` is on -- so on every instance that has not set both
    up, the list is empty and nothing is ever flagged.
    """

    LISTING = 'app/static/tmp/disposable_domains.txt'

    @pytest.fixture
    def applicant(self, env):
        user = make_user(env.baseline.instance_local, 'hopeful', local=True)
        user.email = 'someone@throwaway.example'
        db.session.commit()
        registration = UserRegistration(user_id=user.id, answer='because', status=0)
        db.session.add(registration)
        db.session.commit()
        env.applicant = user
        return env

    def test_the_queue_lists_an_application_awaiting_review(self, applicant):
        response = applicant.client.get('/admin/approve_registrations')

        assert response.status_code == 200
        assert 'hopeful' in response.get_data(as_text=True)

    def test_an_approved_application_is_listed_separately(self, applicant, monkeypatch):
        """`status=1` with `approved_at` descending is the 'recently approved' half; the
        queue itself is `status=0`. The two lists are captured as the template receives
        them, because an approved application appearing in BOTH would still put its name
        on the page once."""
        other = make_user(applicant.baseline.instance_local, 'accepted', local=True)
        db.session.commit()
        db.session.add(UserRegistration(user_id=other.id, answer='sure', status=1,
                                        approved_at=utcnow()))
        db.session.commit()
        captured = {}

        def fake_render(template, **kwargs):
            captured.update(kwargs)
            return ''

        monkeypatch.setattr('app.admin.routes.render_template', fake_render)

        applicant.client.get('/admin/approve_registrations')

        assert [r.user_id for r in captured['registrations']] == \
            [applicant.applicant.id]
        assert other.id in [r.user_id for r in captured['recently_approved']]

    def test_the_domain_list_is_read_when_the_flag_and_the_file_are_both_there(
            self, applicant, monkeypatch):
        """Both halves of `if current_app.config['FLAG_THROWAWAY_EMAILS'] and
        os.path.isfile(...)`. The file is written into the path the route reads, since it
        is hardcoded and relative, and removed afterwards."""
        monkeypatch.setitem(applicant.app.config, 'FLAG_THROWAWAY_EMAILS', True)
        os.makedirs('app/static/tmp', exist_ok=True)
        existed = os.path.isfile(self.LISTING)
        if not existed:
            with open(self.LISTING, 'w', encoding='utf-8') as handle:
                handle.write('throwaway.example\nanother.example\n')
        try:
            captured = {}
            real = __import__('flask').render_template

            def fake_render(template, **kwargs):
                captured.update(kwargs)
                return ''

            monkeypatch.setattr('app.admin.routes.render_template', fake_render)

            applicant.client.get('/admin/approve_registrations')

            assert 'throwaway.example' in captured['disposable_domains']
            # The trailing newline is stripped, so a domain read from the file compares
            # equal to one taken from an email address.
            assert all('\n' not in domain
                       for domain in captured['disposable_domains'])
            assert real is not None
        finally:
            if not existed:
                os.remove(self.LISTING)

    def test_the_list_is_empty_when_the_flag_is_off(self, applicant, monkeypatch):
        """The `else`, which is the state every instance is in until an admin turns the
        flag on AND supplies the file.

        The file is written here as well: with it absent, the second half of the `and`
        answers for the first, and a mutant dropping the flag check entirely would survive.
        """
        monkeypatch.setitem(applicant.app.config, 'FLAG_THROWAWAY_EMAILS', False)
        os.makedirs('app/static/tmp', exist_ok=True)
        existed = os.path.isfile(self.LISTING)
        if not existed:
            with open(self.LISTING, 'w', encoding='utf-8') as handle:
                handle.write('throwaway.example\n')
        try:
            captured = {}

            def fake_render(template, **kwargs):
                captured.update(kwargs)
                return ''

            monkeypatch.setattr('app.admin.routes.render_template', fake_render)

            applicant.client.get('/admin/approve_registrations')

            assert captured['disposable_domains'] == []
        finally:
            if not existed:
                os.remove(self.LISTING)

    def test_an_account_without_the_permission_cannot_see_the_queue(self, applicant):
        ordinary = make_user(applicant.baseline.instance_local, 'nosy', local=True)
        ordinary.verified = True
        db.session.commit()
        client = applicant.app.test_client()
        with client.session_transaction() as session:
            session['_user_id'] = str(ordinary.id)
            session['_fresh'] = True

        response = client.get('/admin/approve_registrations')

        assert response.status_code == 302

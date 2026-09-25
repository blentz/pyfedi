"""What the community and admin forms refuse.

Sub-project 102 -- the `validate` and `validate_*` methods in
`app/community/forms.py` and `app/admin/forms.py`. These are the last thing
between a form submission and a database write, and several of them are the
only place a site-wide setting is enforced: whether NSFW posts are allowed,
whether images may be posted to local communities, whether a name is already
taken.

They are exercised with formdata inside a request context rather than through
a route, for the reason `tests/test_feed_forms.py` gives: the routes drag in
everything other sub-projects already cover, and what is under test here is
the refusal itself and the message that comes with it.

One defect: `CreateImageForm.validate` read `request.files['image_file']`,
and `EditImageForm` overrides that field to `Optional()` while inheriting the
method -- so every edit of an image post that KEPT its existing image was
`werkzeug.exceptions.BadRequestKeyError: 400` (D1293).

Otherwise: every refusal these tests reach both returns False AND says
why -- a validator that returned False silently would leave the submitter with
a form that refuses and explains nothing, which is the shape D1001 had before
it was fixed (it appended the complaint and returned True).
"""
import io
from datetime import timedelta
from unittest.mock import patch

import pytest
from flask import g
from werkzeug.datastructures import MultiDict
from wtforms import ValidationError

from app import db
from app.admin.forms import (AddUserForm, CmsPageForm, EditCommunityForm,
                             MoveCommunityForm)
from app.community.forms import (CreateDiscussionForm, CreateImageForm,
                                 EditImageForm, SearchRemoteCommunity)
from app.models import CmsPage, Community, Site, User, utcnow
from tests.factories import (make_community, make_community_member,
                             make_instance, make_user)

pytestmark = pytest.mark.usefixtures('site')


def a_png():
    from PIL import Image
    buffer = io.BytesIO()
    Image.new('RGB', (10, 10), (1, 2, 3)).save(buffer, format='PNG')
    buffer.seek(0)
    return buffer


@pytest.fixture
def env(app, api_baseline):
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    g.site.enable_nsfw = True
    g.site.enable_nsfl = True
    g.site.enable_chan_image_filter = False
    g.site.allow_local_image_posts = True
    community = make_community('probeland')
    member = api_baseline.user2
    db.session.commit()
    make_community_member(member, community)
    return SimpleNamespace(app=app, community=community, member=member,
                           baseline=api_baseline)


def errors(field):
    return [str(error) for error in (field.errors or [])]


# --------------------------------------------------------------------------
# looking a remote community up
# --------------------------------------------------------------------------

class TestSearchingForARemoteCommunity:
    def form(self, app, address):
        with app.test_request_context('/', method='POST'):
            form = SearchRemoteCommunity(
                formdata=MultiDict({'address': address}))
            return form, form.validate()

    def test_an_address_that_is_only_space(self, env):
        """`DataRequired()` refuses it before the form's own `validate` runs,
        which is why the `Address is required.` arm that used to sit there was
        unreachable and is gone."""
        form, valid = self.form(env.app, '   ')
        assert valid is False
        assert 'This field is required.' in errors(form.address)

    @pytest.mark.parametrize('address', ['https://remote.test/c/faraway',
                                         'http://remote.test/c/faraway'])
    def test_a_url_is_accepted_as_it_stands(self, env, address):
        form, valid = self.form(env.app, address)
        assert valid is True

    def test_a_handle_is_accepted(self, env):
        form, valid = self.form(env.app, '!faraway@remote.test')
        assert valid is True

    def test_something_that_is_neither(self, env):
        form, valid = self.form(env.app, 'faraway')
        assert valid is False
        assert errors(form.address)


# --------------------------------------------------------------------------
# what a post may say
# --------------------------------------------------------------------------

class TestWhatAPostMaySay:
    def a_form(self, env, **overrides):
        data = {'title': 'a title', 'body': '', 'notify_author': 'y',
                'language_id': '1', 'timezone': 'UTC',
                'communities': str(env.community.id)}
        data.update({k: v for k, v in overrides.items() if v is not None})
        with env.app.test_request_context('/', method='POST'):
            form = CreateDiscussionForm(formdata=MultiDict(data))
            form.language_id.choices = [(1, 'English')]
            form.timezone.choices = [('UTC', 'UTC')]
            form.repeat.choices = [('none', 'None')]
            form.communities.choices = [(env.community.id, 'probeland')]
            return form, form.validate()

    def test_an_nsfw_post_when_the_instance_allows_them(self, env):
        form, valid = self.a_form(env, nsfw='y')
        assert valid is True

    def test_and_when_it_does_not(self, env):
        g.site.enable_nsfw = False
        db.session.commit()
        form, valid = self.a_form(env, nsfw='y')
        assert valid is False
        assert 'NSFW posts are not allowed.' in errors(form.nsfw)

    def test_an_nsfl_post_when_the_instance_does_not_allow_them(self, env):
        g.site.enable_nsfl = False
        db.session.commit()
        form, valid = self.a_form(env, nsfl='y')
        assert valid is False
        assert 'NSFL posts are not allowed.' in errors(form.nsfl)

    def test_neither_flag_set_is_fine_either_way(self, env):
        g.site.enable_nsfw = False
        g.site.enable_nsfl = False
        db.session.commit()
        form, valid = self.a_form(env)
        assert valid is True

    def test_a_post_scheduled_for_the_past(self, env):
        past = (utcnow() - timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
        form, valid = self.a_form(env, scheduled_for=past)
        assert valid is False
        assert 'Choose a time in the future.' in errors(form.scheduled_for)

    def test_one_scheduled_for_the_future(self, env):
        future = (utcnow() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
        form, valid = self.a_form(env, scheduled_for=future)
        assert valid is True

    def test_one_with_no_schedule_at_all(self, env):
        form, valid = self.a_form(env)
        assert valid is True


# --------------------------------------------------------------------------
# an image post
# --------------------------------------------------------------------------

class TestAnImagePost:
    def a_form(self, env, filename='photo.png', content=None, **overrides):
        data = {'title': 'a title', 'body': '', 'language_id': '1',
                'timezone': 'UTC', 'communities': str(env.community.id)}
        data.update(overrides)
        data['image_file'] = (content or a_png(), filename)
        with env.app.test_request_context(
                '/', method='POST', data=data,
                content_type='multipart/form-data'):
            form = CreateImageForm()
            form.language_id.choices = [(1, 'English')]
            form.timezone.choices = [('UTC', 'UTC')]
            form.communities.choices = [(env.community.id, 'probeland')]
            return form, form.validate()

    def test_an_image_a_local_community_accepts(self, env):
        form, valid = self.a_form(env)
        assert valid is True

    def test_one_a_local_community_does_not(self, env):
        """D1001's site. The complaint used to be appended and True returned
        anyway, so the setting recorded an objection and accepted the image."""
        g.site.allow_local_image_posts = False
        db.session.commit()
        form, valid = self.a_form(env)
        assert valid is False
        assert 'Images cannot be posted to local communities.' in \
            errors(form.communities)

    def test_a_remote_community_is_unaffected_by_that_setting(self, env):
        g.site.allow_local_image_posts = False
        remote = make_community('faraway', host='remote.test')
        remote.ap_id = 'faraway@remote.test'
        remote.ap_profile_id = 'https://remote.test/c/faraway'
        db.session.commit()
        data = {'title': 'a title', 'body': '', 'language_id': '1',
                'timezone': 'UTC', 'communities': str(remote.id),
                'image_file': (a_png(), 'photo.png')}
        with env.app.test_request_context('/', method='POST', data=data,
                                          content_type='multipart/form-data'):
            form = CreateImageForm()
            form.language_id.choices = [(1, 'English')]
            form.timezone.choices = [('UTC', 'UTC')]
            form.communities.choices = [(remote.id, 'faraway')]
            assert form.validate() is True

    def test_a_gif_within_the_size_limit(self, env):
        from PIL import Image
        buffer = io.BytesIO()
        Image.new('P', (10, 10)).save(buffer, format='GIF')
        buffer.seek(0)
        form, valid = self.a_form(env, filename='animated.gif',
                                  content=buffer)
        assert valid is True

    def test_a_gif_that_is_too_large(self, env):
        oversized = io.BytesIO(b'GIF89a' + b'\x00' * (11 * 1024 * 1024))
        form, valid = self.a_form(env, filename='animated.gif',
                                  content=oversized)
        assert valid is False
        assert 'This image filesize is too large.' in errors(form.image_file)

    def test_an_svg_skips_the_image_inspection(self, env):
        form, valid = self.a_form(env, filename='logo.svg',
                                  content=io.BytesIO(b'<svg/>'))
        assert valid is True


class TestEditingAnImagePost:
    """`EditImageForm` carries its own copy of the same check, and an edit may
    leave the image alone -- the field is optional here."""

    def a_form(self, env, with_image=True):
        data = {'title': 'a title', 'body': '', 'language_id': '1',
                'timezone': 'UTC', 'communities': str(env.community.id)}
        if with_image:
            data['image_file'] = (a_png(), 'photo.png')
        with env.app.test_request_context(
                '/', method='POST', data=data,
                content_type='multipart/form-data'):
            form = EditImageForm()
            form.language_id.choices = [(1, 'English')]
            form.timezone.choices = [('UTC', 'UTC')]
            form.communities.choices = [(env.community.id, 'probeland')]
            return form, form.validate()

    def test_an_edit_that_replaces_the_image(self, env):
        form, valid = self.a_form(env)
        assert valid is True

    def test_an_edit_that_leaves_it_alone(self, env):
        """D1293. The inherited `validate` read
        `request.files['image_file']`, which is a
        `werkzeug.exceptions.BadRequestKeyError: 400` when the field is absent
        -- and it is absent on every edit that keeps the existing image, which
        is what the Optional() on this form's field is for."""
        form, valid = self.a_form(env, with_image=False)
        assert valid is True

    def test_an_edit_a_local_community_does_not_allow(self, env):
        g.site.allow_local_image_posts = False
        db.session.commit()
        form, valid = self.a_form(env)
        assert valid is False
        assert 'Images cannot be posted to local communities.' in \
            errors(form.communities)


class TestTheFourChanFilter:
    def a_form(self, env, text):
        data = {'title': 'a title', 'body': '', 'language_id': '1',
                'timezone': 'UTC', 'communities': str(env.community.id),
                'image_file': (a_png(), 'photo.png')}
        with env.app.test_request_context(
                '/', method='POST', data=data,
                content_type='multipart/form-data'):
            form = CreateImageForm()
            form.language_id.choices = [(1, 'English')]
            form.timezone.choices = [('UTC', 'UTC')]
            form.communities.choices = [(env.community.id, 'probeland')]
            with patch('app.community.forms.pytesseract.image_to_string',
                       return_value=text):
                return form, form.validate()

    def test_an_image_carrying_the_giveaway_text(self, env):
        g.site.enable_chan_image_filter = True
        db.session.commit()
        form, valid = self.a_form(env, 'Anonymous No.12345 something')
        assert valid is False
        assert 'This image is from 4chan.' in errors(form.image_file)

    def test_the_other_spelling_of_the_post_number(self, env):
        g.site.enable_chan_image_filter = True
        db.session.commit()
        form, valid = self.a_form(env, 'Anonymous N012345')
        assert valid is False

    def test_an_image_carrying_only_half_of_it(self, env):
        g.site.enable_chan_image_filter = True
        db.session.commit()
        form, valid = self.a_form(env, 'Anonymous said something')
        assert valid is True

    def test_an_image_with_no_text_at_all(self, env):
        g.site.enable_chan_image_filter = True
        db.session.commit()
        form, valid = self.a_form(env, '')
        assert valid is True

    def test_an_image_the_inspector_cannot_read(self, env):
        g.site.enable_chan_image_filter = True
        db.session.commit()
        data = {'title': 'a title', 'body': '', 'language_id': '1',
                'timezone': 'UTC', 'communities': str(env.community.id),
                'image_file': (io.BytesIO(b'not an image'), 'photo.png')}
        with env.app.test_request_context(
                '/', method='POST', data=data,
                content_type='multipart/form-data'):
            form = CreateImageForm()
            form.language_id.choices = [(1, 'English')]
            form.timezone.choices = [('UTC', 'UTC')]
            form.communities.choices = [(env.community.id, 'probeland')]
            assert form.validate() is True

    def test_the_inspector_not_being_installed(self, env):
        g.site.enable_chan_image_filter = True
        db.session.commit()
        data = {'title': 'a title', 'body': '', 'language_id': '1',
                'timezone': 'UTC', 'communities': str(env.community.id),
                'image_file': (a_png(), 'photo.png')}
        with env.app.test_request_context(
                '/', method='POST', data=data,
                content_type='multipart/form-data'):
            form = CreateImageForm()
            form.language_id.choices = [(1, 'English')]
            form.timezone.choices = [('UTC', 'UTC')]
            form.communities.choices = [(env.community.id, 'probeland')]
            with patch('app.community.forms.pytesseract.image_to_string',
                       side_effect=FileNotFoundError('no tesseract here')):
                assert form.validate() is True

    def test_the_filter_turned_off_reads_nothing(self, env):
        data = {'title': 'a title', 'body': '', 'language_id': '1',
                'timezone': 'UTC', 'communities': str(env.community.id),
                'image_file': (a_png(), 'photo.png')}
        with env.app.test_request_context(
                '/', method='POST', data=data,
                content_type='multipart/form-data'):
            form = CreateImageForm()
            form.language_id.choices = [(1, 'English')]
            form.timezone.choices = [('UTC', 'UTC')]
            form.communities.choices = [(env.community.id, 'probeland')]
            with patch('app.community.forms.pytesseract.image_to_string') \
                    as read:
                form.validate()
        assert read.call_count == 0


# --------------------------------------------------------------------------
# the admin forms
# --------------------------------------------------------------------------

class TestEditingACommunityAsAnAdmin:
    def form(self, env, url):
        with env.app.test_request_context('/', method='POST'):
            form = EditCommunityForm(formdata=MultiDict({
                'title': 'A community', 'url': url, 'description': '',
                'rules': '', 'content_retention': '-1',
                'topic': '0', 'default_layout': '',
                'posting_warning': '', 'downvote_accept_mode': '0'}))
            form.topic.choices = [(0, 'None')]
            form.languages.choices = [(1, 'English')]
            return form, form.validate()

    def test_a_url_that_is_only_space(self, env):
        """As above: `DataRequired()` gets there first, so the form's own
        `Url is required.` arm was unreachable and is gone."""
        form, valid = self.form(env, '   ')
        assert valid is False
        assert 'This field is required.' in errors(form.url)

    def test_a_url_with_a_dash_in_it_is_allowed(self, env):
        """PeerTube and NodeBB both use dashes, so the check that refused them
        is commented out in the form -- pinned so that uncommenting it is a
        decision rather than an accident."""
        form, valid = self.form(env, 'some-community')
        assert valid is True


class TestAddingAnAccountAsAnAdmin:
    def form(self, env, **overrides):
        data = {'user_name': 'newcomer', 'email': 'newcomer@example.test',
                'password': 'a-good-password', 'password2': 'a-good-password',
                'about': '', 'matrix_user_id': '', 'bot': '', 'verified': 'y'}
        data.update(overrides)
        with env.app.test_request_context('/', method='POST'):
            form = AddUserForm(formdata=MultiDict(data))
            return form, form.validate()

    def test_an_ordinary_one(self, env):
        form, valid = self.form(env)
        assert valid is True

    def test_a_name_somebody_already_holds(self, env):
        form, valid = self.form(env, user_name=env.member.user_name)
        assert valid is False
        assert 'An account with this user name already exists.' in \
            errors(form.user_name)

    def test_a_name_somebody_held_and_deleted(self, env):
        env.member.deleted = True
        db.session.commit()
        form, valid = self.form(env, user_name=env.member.user_name)
        assert valid is False
        assert 'This username was used in the past and cannot be reused.' in \
            errors(form.user_name)

    def test_a_name_a_community_holds(self, env):
        form, valid = self.form(env, user_name=env.community.name)
        assert valid is False
        assert 'A community with this name exists so it cannot be used for a user.' \
            in errors(form.user_name)

    @pytest.mark.parametrize('password', ['password', '12345678',
                                          '1234567890'])
    def test_a_password_everybody_uses(self, env, password):
        form, valid = self.form(env, password=password)
        assert valid is False
        assert 'This password is too common.' in errors(form.password)

    def test_a_password_that_is_one_character_repeated(self, env):
        form, valid = self.form(env, password='aaaaaaaaaa')
        assert valid is False
        assert 'This password is not secure.' in errors(form.password)

    def test_an_email_address_somebody_already_uses(self, env):
        form, valid = self.form(env, email=env.member.email)
        assert valid is False
        assert 'An account with this email address already exists.' in \
            errors(form.email)

    def test_a_user_name_with_an_at_sign_in_it(self, env):
        """A local account whose name contains @ would be indistinguishable
        from a remote handle."""
        form, valid = self.form(env, user_name='someone@elsewhere')
        assert valid is False
        assert 'User names cannot contain @.' in errors(form.user_name)

    def test_a_user_name_the_charset_refuses(self, env):
        form, valid = self.form(env, user_name='no spaces allowed')
        assert valid is False
        assert errors(form.user_name)

    def test_an_account_with_no_password_at_all(self, env):
        """The field carries DataRequired(), so an admin creating an account
        must set a password -- which is why the `if not password.data:
        return` that used to open `validate_password` was unreachable."""
        form, valid = self.form(env, password='', password2='')
        assert valid is False
        assert 'This field is required.' in errors(form.password)


class TestMovingACommunity:
    def form(self, env, new_url):
        with env.app.test_request_context('/', method='POST'):
            form = MoveCommunityForm(formdata=MultiDict({
                'new_url': new_url}))
            return form, form.validate()

    def test_a_url_nobody_here_uses(self, env):
        form, valid = self.form(env, 'somewhere-new')
        assert valid is True

    def test_one_a_local_community_already_has(self, env):
        form, valid = self.form(env, env.community.name)
        assert valid is False
        assert 'A local community at that url already exists' in \
            errors(form.new_url)

    def test_the_comparison_is_made_in_lower_case(self, env):
        form, valid = self.form(env, env.community.name.upper())
        assert valid is False


class TestACmsPage:
    def form(self, env, url, original_page=None):
        with env.app.test_request_context('/', method='POST'):
            form = CmsPageForm(original_page=original_page,
                              formdata=MultiDict({'url': url,
                                                  'title': 'A page',
                                                  'body': 'some words'}))
            return form, form.validate()

    def test_a_path_with_no_leading_slash_is_given_one(self, env):
        form, valid = self.form(env, 'about-us')
        assert valid is True
        assert form.url.data == '/about-us'

    def test_a_path_nobody_uses(self, env):
        form, valid = self.form(env, '/about-us')
        assert valid is True

    def test_a_path_another_page_already_has(self, env):
        db.session.add(CmsPage(url='/about-us', title='About', body='x'))
        db.session.commit()
        form, valid = self.form(env, '/about-us')
        assert valid is False
        assert 'A page with this URL already exists.' in errors(form.url)

    def test_the_page_being_edited_may_keep_its_own_path(self, env):
        page = CmsPage(url='/about-us', title='About', body='x')
        db.session.add(page)
        db.session.commit()
        form, valid = self.form(env, '/about-us', original_page=page)
        assert valid is True

"""Covers app.utils.apply_feed_url_rules (app/utils.py:4486-4516).

apply_feed_url_rules is a bound form validator, not a plain function: it reads
self.url, self.public, current_user.user_name and (via try/except AttributeError)
self.feed_id. It is called from two form classes in app/feed/forms.py --
AddCopyFeedForm (line 34) and EditFeedForm (line 87) -- and the try/except
AttributeError branch is selected by WHICH CLASS is instantiated, not by any
runtime state: AddCopyFeedForm has no feed_id attribute at all (raises
AttributeError), while EditFeedForm declares `feed_id = 0` as a class attribute
(line 63) and only routes.py:137 (feed_edit) ever overwrites it with the real id.
So both form classes are built here rather than one form mutated at runtime.

apply_feed_url_rules MUTATES self.url.data before validating it, so every test
below asserts on that mutation as well as on the True/False return -- a test
that only checks the return value would leave half the function unobserved.

WTF_CSRF_ENABLED is False in TestConfig (tests/conftest.py), so forms are built
directly with no formdata and no CSRF token; apply_feed_url_rules is called
directly rather than through form.validate(), since it makes no use of the
other field validators.
"""
import re

from flask_login import login_user

from app import db
from app.feed.forms import AddCopyFeedForm, EditFeedForm
from app.models import Feed
from app.utils import apply_feed_url_rules
from tests.factories import make_instance, make_user


def local_user(name):
    """A local, logged-in-capable user. make_user(None, ...) defaults
    instance_id=1, which only exists once an Instance row has been inserted
    in this (freshly truncated) test database -- make_instance() first
    guarantees that row.
    """
    instance = make_instance(f'{name}.example')
    return make_user(instance, name, local=True)


def make_feed(owner, name, public=True):
    feed = Feed(user_id=owner.id, title=name, name=name, public=public, instance_id=1)
    db.session.add(feed)
    db.session.commit()
    return feed


def create_form(app, viewer, url, public):
    """AddCopyFeedForm instance -- current_user is logged in, self.feed_id is
    ABSENT (no class attribute), so apply_feed_url_rules's try: self.feed_id
    takes the except AttributeError branch.
    """
    with app.test_request_context('/'):
        login_user(viewer)
        form = AddCopyFeedForm(meta={'csrf': False})
        form.url.data = url
        form.public.data = public
        # WTForms' Field.errors is class-level `tuple()` until the field's own
        # validate() runs (which super().validate() always does before
        # apply_feed_url_rules is reached in production, via
        # AddCopyFeedForm.validate()/EditFeedForm.validate()). Calling
        # apply_feed_url_rules directly, bypassing form.validate(), skips
        # that -- so it is reproduced here rather than exercising the whole
        # form's other validators (DataRequired, Length, communities parsing)
        # this function has nothing to do with.
        form.url.errors = []
        result = apply_feed_url_rules(form)
        return result, form.url.data, list(form.url.errors)


def edit_form(app, viewer, url, public, feed_id=None):
    """EditFeedForm instance -- feed_id = 0 is a class attribute (present
    unless overwritten), so apply_feed_url_rules's try: self.feed_id succeeds
    and the uniqueness query excludes Feed.id != self.feed_id.
    """
    with app.test_request_context('/'):
        login_user(viewer)
        form = EditFeedForm(meta={'csrf': False})
        if feed_id is not None:
            form.feed_id = feed_id
        form.url.data = url
        form.public.data = public
        form.url.errors = []  # see create_form's comment on this line
        result = apply_feed_url_rules(form)
        return result, form.url.data, list(form.url.errors)


class TestDashIsRejected:
    """Rule at 4487-4489: a literal '-' anywhere in the stripped url rejects
    outright, before any of the mutation/regex/uniqueness logic runs.
    """

    def test_a_dash_in_the_url_is_rejected(self, app, db_session):
        viewer = local_user('dashviewer')
        result, url_data, errors = create_form(app, viewer, 'my-feed', True)

        assert result is False
        assert len(errors) == 1
        # The url is untouched -- the '-' check runs before the mutation
        # block, so a false positive here would not be caught by a test that
        # only inspected the return value.
        assert url_data == 'my-feed'
        # A '-' also fails the downstream alphanumeric regex (it is not in
        # [a-zA-Z0-9_]), so disabling this early guard entirely still
        # produces result=False and one error -- the return value and error
        # COUNT alone cannot discriminate the two. The message is what
        # distinguishes "rejected by the '-' guard" from "rejected by the
        # regex", so it must be asserted, not just its presence.
        assert str(errors[0]) == '- cannot be in Url. Use _ instead?'

    def test_a_url_with_no_dash_is_not_rejected_by_this_rule(self, app, db_session):
        viewer = local_user('nodashviewer')
        result, url_data, errors = create_form(app, viewer, 'myfeed', True)

        assert result is True
        assert errors == []


class TestPrivateNoSlashAppendsUsername:
    """Rule at 4491-4492: not public and no '/' in the url appends
    '/<username>' (lowercased) to the stripped/lowered url.
    """

    def test_private_url_with_no_slash_gets_the_username_appended(self, app, db_session):
        viewer = local_user('privateappend')
        result, url_data, errors = create_form(app, viewer, 'MyFeed', False)

        assert url_data == 'myfeed/privateappend'
        assert result is True
        assert errors == []


class TestPublicWithSlashStripsToFirstSegment:
    """Rule at 4493-4494: public and '/' present in the url keeps only the
    text before the first '/'.
    """

    def test_public_url_with_a_slash_is_truncated_to_the_first_segment(self, app, db_session):
        viewer = local_user('publicslash')
        result, url_data, errors = create_form(app, viewer, 'myfeed/extra', True)

        assert url_data == 'myfeed'
        assert result is True
        assert errors == []


class TestNeitherRuleStripsAndLowers:
    """Rule at 4495-4496 (the else branch): public url with no '/', or
    private url that already has a '/', is merely stripped and lowercased --
    no segment is added or removed.
    """

    def test_public_url_with_no_slash_is_only_stripped_and_lowered(self, app, db_session):
        viewer = local_user('elseviewer')
        result, url_data, errors = create_form(app, viewer, '  MyFeed  ', True)

        assert url_data == 'myfeed'
        assert result is True
        assert errors == []

    def test_private_url_that_already_has_a_slash_is_left_alone(self, app, db_session):
        """The other path into the else branch: not public.data and '/' IS
        already in the url -- neither of the first two conditions holds.
        """
        viewer = local_user('elseviewer2')
        result, url_data, errors = create_form(app, viewer, 'myfeed/elseviewer2', False)

        assert url_data == 'myfeed/elseviewer2'
        assert result is True
        assert errors == []


class TestRegexRejectsNonAlphanumeric:
    """Rule at 4498-4503: after the mutation block, the (possibly re-shaped)
    url must match an alphanumeric+underscore regex -- a public-mode pattern
    with no suffix allowed, a private-mode pattern that allows exactly
    '/<username>' as an optional suffix.
    """

    def test_public_regex_rejects_a_non_alphanumeric_character(self, app, db_session):
        viewer = local_user('regexpublic')
        # public + '/' would normally truncate to the first segment (rule
        # above), so use a character the mutation block cannot remove: '!'.
        result, url_data, errors = create_form(app, viewer, 'my!feed', True)

        assert result is False
        assert len(errors) == 1
        assert url_data == 'my!feed'

    def test_private_regex_rejects_a_non_alphanumeric_character(self, app, db_session):
        viewer = local_user('regexprivate')
        result, url_data, errors = create_form(app, viewer, 'my!feed', False)

        assert result is False
        assert len(errors) == 1
        # not public and '/' not in 'my!feed', so the append-username branch
        # ran first: url_data is 'my!feed/regexprivate', not the raw input.
        assert url_data == 'my!feed/regexprivate'


class TestUniquenessNoFeedId:
    """Rule at 4505-4509, except-AttributeError branch: AddCopyFeedForm has
    no feed_id, so the uniqueness query has no Feed.id exclusion at all.
    """

    def test_an_existing_name_is_rejected(self, app, db_session):
        viewer = local_user('uniqnofeedid')
        make_feed(viewer, 'takenname', public=True)

        result, url_data, errors = create_form(app, viewer, 'TakenName', True)

        assert result is False
        assert len(errors) == 1
        assert url_data == 'takenname'

    def test_an_unused_name_is_accepted(self, app, db_session):
        viewer = local_user('uniqnofeedidfree')

        result, url_data, errors = create_form(app, viewer, 'freshname', True)

        assert result is True
        assert errors == []


class TestUniquenessWithFeedId:
    """Rule at 4505-4509, else branch (try: self.feed_id succeeds):
    EditFeedForm always has feed_id, defaulting to the class attribute 0.
    """

    def test_same_name_different_feed_id_is_rejected(self, app, db_session):
        viewer = local_user('uniqdifferentid')
        existing = make_feed(viewer, 'sharedname', public=True)

        result, url_data, errors = edit_form(app, viewer, 'SharedName', True, feed_id=existing.id + 1)

        assert result is False
        assert len(errors) == 1
        assert url_data == 'sharedname'

    def test_same_name_same_feed_id_is_accepted(self, app, db_session):
        """The feed being edited keeping its own name: Feed.id != self.feed_id
        excludes the feed's own row from the collision check, so this must
        return True even though a Feed with this exact name already exists.
        """
        viewer = local_user('uniqsameid')
        existing = make_feed(viewer, 'ownname', public=True)

        result, url_data, errors = edit_form(app, viewer, 'OwnName', True, feed_id=existing.id)

        assert result is True
        assert errors == []


class TestEditFormDefaultFeedIdZero:
    """A consequence flagged in the task brief: an EditFeedForm whose feed_id
    is still the default 0 (never overwritten by feed_edit's
    edit_feed_form.feed_id = feed_id, app/feed/routes.py:137) runs the
    uniqueness query as Feed.name == url AND Feed.id != 0. Since Feed rows
    are never assigned id 0 (Postgres serial primary keys start at 1), that
    exclusion excludes nothing -- functionally identical to the no-feed_id
    branch for any real Feed row.
    """

    def test_default_feed_id_zero_still_rejects_a_real_collision(self, app, db_session):
        viewer = local_user('defaultfeedid')
        make_feed(viewer, 'zeroidname', public=True)
        assert Feed.query.filter(Feed.name == 'zeroidname').first().id != 0

        # feed_id left at the class default (0) -- edit_form's feed_id=None
        # means "do not overwrite it".
        result, url_data, errors = edit_form(app, viewer, 'ZeroIdName', True, feed_id=None)

        assert result is False
        assert len(errors) == 1
        assert url_data == 'zeroidname'


class TestUsernameRegexMetacharacterProbe:
    """Probes the private regex's string interpolation:

        regex = r'^[a-zA-Z0-9_]+(?:/' + current_user.user_name.lower() + ')?$'

    A username containing a regex metacharacter changes what that group
    matches. Two self-registration paths were checked and both enforce
    ^[a-zA-Z0-9_]+$ on user_name, so a metacharacter can never reach this
    function through them:

    - app/auth/forms.py:59 (RegistrationForm.validate_user_name)
    - app/feed/forms.py itself imposes no username constraint of its own;
      it is current_user's *existing* user_name that matters here, not
      anything on this form.

    But a THIRD path has no such charset check: app/admin/forms.py's
    AddUserForm.validate_user_name (lines 305-317) validates only that '@'
    is absent -- an instance admin creating a user through the admin panel
    can set an arbitrary user_name, including one containing '.', '(', ')',
    '|', etc. That user then logs in normally and is a fully valid
    current_user for this form. This test builds that state directly (bypassing
    the admin form, which is not itself exercised here) and demonstrates the
    consequence: because '.' is unescaped in the interpolated regex, it
    matches any character instead of a literal '.', so a url segment that is
    NOT the user's real username is wrongly accepted as if it were.

    This is a validation-bypass DEFECT, reported here and not fixed.
    """

    def test_username_regex_metacharacters_are_not_escaped(self, app, db_session):
        viewer = local_user('a.b')  # '.' is a regex metachar

        # Sanity check this is really an unescaped metachar in the built
        # pattern: 'a.b' matches 'aXb' under re, but not under literal
        # equality.
        assert re.match(r'^[a-zA-Z0-9_]+(?:/' + viewer.user_name.lower() + ')?$', 'myfeed/aXb')
        assert viewer.user_name != 'aXb'

        # 'myfeed/aXb' is NOT this user's '<name>/<username>' url (that would
        # be 'myfeed/a.b'), and it already contains a '/' so the append-
        # username mutation branch does not fire -- the else branch merely
        # lowers it, leaving 'myfeed/axb' unchanged in shape.
        result, url_data, errors = create_form(app, viewer, 'myfeed/aXb', False)

        # DEFECT: this wrongly validates. A correctly escaped pattern (using
        # re.escape on the username) would reject it, since 'axb' is not this
        # user's username.
        assert result is True
        assert url_data == 'myfeed/axb'
        assert errors == []

    def test_a_locally_registered_username_cannot_contain_metacharacters(self):
        """Establishes the self-registration path is closed: app/auth/forms.py
        RegistrationForm.validate_user_name enforces
        re.match(r'^[a-zA-Z0-9_]+$', user_name.data) (line 59), which rejects
        every regex metacharacter. Read directly from source rather than
        trusting a paraphrase.
        """
        import inspect

        from app.auth.forms import RegistrationForm

        source = inspect.getsource(RegistrationForm.validate_user_name)
        assert r"re.match(r'^[a-zA-Z0-9_]+$', user_name.data)" in source

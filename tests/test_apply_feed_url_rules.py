"""Covers app.utils.apply_feed_url_rules (app/utils.py:4623-4660).

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

import pytest
from flask_login import login_user
from wtforms.validators import ValidationError

from app import db
from app.admin.forms import AddUserForm
from app.auth.forms import RegistrationForm
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
    """Rule at 4624-4626: a literal '-' anywhere in the stripped url rejects
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
        # Identity rewrite (already lowercase, no slash) -- asserted for
        # consistency with the file's mutation standard even though nothing
        # here reshapes the string.
        assert url_data == 'myfeed'


class TestPrivateNoSlashAppendsUsername:
    """Rule at 4628-4629: not public and no '/' in the url appends
    '/<username>' (lowercased) to the stripped/lowered url.
    """

    def test_private_url_with_no_slash_gets_the_username_appended(self, app, db_session):
        viewer = local_user('privateappend')
        result, url_data, errors = create_form(app, viewer, 'MyFeed', False)

        assert url_data == 'myfeed/privateappend'
        assert result is True
        assert errors == []


class TestPublicWithSlashStripsToFirstSegment:
    """Rule at 4630-4631: public and '/' present in the url keeps only the
    text before the first '/'.
    """

    def test_public_url_with_a_slash_is_truncated_to_the_first_segment(self, app, db_session):
        viewer = local_user('publicslash')
        result, url_data, errors = create_form(app, viewer, 'myfeed/extra', True)

        assert url_data == 'myfeed'
        assert result is True
        assert errors == []


class TestNeitherRuleStripsAndLowers:
    """Rule at 4632-4633 (the else branch): public url with no '/', or
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
    """Rule at 4635-4649: after the mutation block, the (possibly re-shaped)
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
        # 'my!feed' with public.data=True is an identity rewrite (no '-', no
        # '/'), so under a mutation that forces the '-' guard to always fire
        # (`if True or '-' in ...`), result stays False, the error count
        # stays 1, and url_data stays 'my!feed' too -- the dash guard runs
        # BEFORE the mutation block, so nothing here changes shape. Return
        # value, count and url_data all agree with a rejection from the
        # WRONG guard. Only the message distinguishes "rejected by the
        # regex" from "rejected by the dash guard", so it must be pinned.
        assert str(errors[0]) == 'Feed urls can only contain letters, numbers, and underscores.'

    def test_private_regex_rejects_a_non_alphanumeric_character(self, app, db_session):
        viewer = local_user('regexprivate')
        result, url_data, errors = create_form(app, viewer, 'my!feed', False)

        assert result is False
        assert len(errors) == 1
        # not public and '/' not in 'my!feed', so the append-username branch
        # ran first: url_data is 'my!feed/regexprivate', not the raw input.
        assert url_data == 'my!feed/regexprivate'


class TestUniquenessNoFeedId:
    """Rule at 4651-4657, except-AttributeError branch: AddCopyFeedForm has
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
        # Identity rewrite -- asserted for consistency with the file's
        # mutation standard.
        assert url_data == 'freshname'


class TestUniquenessWithFeedId:
    """Rule at 4651-4657, else branch (try: self.feed_id succeeds):
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
        # Non-identity rewrite (case-folded) -- asserted for consistency
        # with the file's mutation standard.
        assert url_data == 'ownname'


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

        regex = r'^[a-zA-Z0-9_]+(?:/' + re.escape(current_user.user_name.lower()) + ')?$'

    **This class used to pin a live DEFECT and now pins its fix.** Its
    docstring and its first test asserted, deliberately, that a username
    containing a regex metacharacter was interpolated UNESCAPED -- so user
    `a.b`'s pattern was `^[a-zA-Z0-9_]+(?:/a.b)?$`, in which `.` matches any
    character, and the private feed url `myfeed/aXb` wrongly validated: a feed
    claimed in a url namespace that reads as user `aXb`'s. That was reported
    and left unfixed at the time. It has since been fixed at both ends, so the
    assertion below is inverted rather than deleted -- the same call, the same
    fixtures, the same branch, now asserting the behaviour that holds.

    Two paths could produce such a username. Self-registration never could:
    `app/auth/forms.py` `RegistrationForm.validate_user_name` has always
    enforced `^[a-zA-Z0-9_]+$`. `app/admin/forms.py` `AddUserForm.validate_user_name`
    could -- it checked only that '@' was absent -- and now enforces the same
    charset through the shared `app.utils.validate_user_name_charset`.

    `re.escape` in `apply_feed_url_rules` is the load-bearing half: the admin
    charset check governs names created from now on, while any database may
    already hold a metacharacter name created before it. The wider case set --
    other metacharacters, the crash that `(` and `[` used to cause, the
    admin form itself, and the over-correction guards -- lives in
    `tests/test_username_regex_metacharacters.py`.
    """

    def test_username_regex_metacharacters_are_escaped(self, app, db_session):
        viewer = local_user('a.b')  # '.' is a regex metachar

        # The pre-fix pattern, built here, so this test carries its own proof
        # that 'aXb' really was matched by an unescaped 'a.b' rather than
        # relying on the prose above.
        assert re.match(r'^[a-zA-Z0-9_]+(?:/' + viewer.user_name.lower() + ')?$', 'myfeed/aXb')
        assert viewer.user_name != 'aXb'

        # 'myfeed/aXb' is NOT this user's '<name>/<username>' url (that would
        # be 'myfeed/a.b'), and it already contains a '/' so the append-
        # username mutation branch does not fire -- the else branch merely
        # lowers it, leaving 'myfeed/axb' unchanged in shape.
        result, url_data, errors = create_form(app, viewer, 'myfeed/aXb', False)

        # Pre-fix this was `assert result is True`, with a comment naming it
        # as the defect.
        assert result is False
        assert len(errors) == 1
        assert str(errors[0]) == 'Feed urls can only contain letters, numbers, and underscores.'
        assert url_data == 'myfeed/axb'

    def test_both_user_creation_paths_enforce_the_charset(self, app, db_session):
        """Establishes that neither path can put a metacharacter into
        `user_name` any more. This used to be a source-text assertion on
        `RegistrationForm.validate_user_name` alone (the admin path was the
        open one, so there was nothing to assert about it); it is now driven
        as behaviour against both validators, which is what actually matters
        and survives a refactor of either form.
        """
        with app.test_request_context('/'):
            for form in (RegistrationForm(meta={'csrf': False}), AddUserForm(meta={'csrf': False})):
                form.user_name.data = 'a.b'
                with pytest.raises(ValidationError) as caught:
                    form.validate_user_name(form.user_name)
                assert str(caught.value) == 'User names can only contain letters, numbers, and underscores.'

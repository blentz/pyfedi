"""A username containing a regex metacharacter, interpolated unescaped into a
validation pattern, let a user claim a private feed url in someone else's
namespace.

Two sites, one chain:

1. `app/admin/forms.py` `AddUserForm.validate_user_name` -- the admin
   user-creation path -- checked only that '@' was absent. No charset
   restriction, so an admin-created username could contain any regex
   metacharacter. (`app/auth/forms.py` `RegistrationForm.validate_user_name`,
   the self-registration path, has always enforced `^[a-zA-Z0-9_]+$`.)
2. `app/utils.py` `apply_feed_url_rules` builds its private-mode pattern by
   string concatenation:

       regex = r'^[a-zA-Z0-9_]+(?:/' + current_user.user_name.lower() + ')?$'

   With a username of `a.b` that pattern is `^[a-zA-Z0-9_]+(?:/a.b)?$`, in
   which `.` matches ANY character -- so the private feed url `myfeed/aXb`
   validates for user `a.b`, planting a feed in a url namespace that reads as
   user `aXb`'s.

The reachable path is private-mode with a '/' already in the url: it satisfies
neither of the two rewrite branches, falls through to the bare `else`, and the
url is carried into the regex check as typed (lowercased).

**Both sites are fixed, and only one of them is load-bearing.** `re.escape()`
in `apply_feed_url_rules` is the defence that holds no matter what usernames
exist -- including rows already sitting in a production database, created
before the admin form learned to refuse them. New validation does not clean old
rows. The admin-form charset check is defence in depth: it stops NEW rows of
that shape, and it closes the gap between the two user-creation paths, but on
its own it would leave every pre-existing metacharacter username live.

**The over-correction guard is the test that matters most.** A wrong
`re.escape()`, or a fix that simply refused to build the private pattern for
such a username, would pass every "the claim is refused" test below while
locking user `a.b` out of their own namespace entirely. Every metacharacter
case here is therefore paired: the foreign namespace is refused AND the user's
own is still accepted.
"""
import re

import pytest
from werkzeug.datastructures import MultiDict
from wtforms.validators import ValidationError

from app.admin.forms import AddUserForm
from app.auth.forms import RegistrationForm
from tests.test_apply_feed_url_rules import create_form, local_user

REGEX_ERROR = 'Feed urls can only contain letters, numbers, and underscores.'
CHARSET_ERROR = 'User names can only contain letters, numbers, and underscores.'


def admin_form_errors(app, user_name):
    """Drive AddUserForm end to end over a complete, otherwise-valid
    submission that varies exactly one thing: the user name. Everything else
    is filled so that a rejection can only have come from the user_name rule
    -- a partially-filled form would be rejected for the wrong reason and a
    test asserting "invalid" would pass against a form that rejects
    everything.

    Returns (form.validate() result, the user_name field's error strings).
    """
    with app.test_request_context('/'):
        form = AddUserForm(
            formdata=MultiDict({
                'user_name': user_name,
                'email': 'newaccount@example.com',
                'password': 'Str0ngPassw0rd',
                'password2': 'Str0ngPassw0rd',
                'role': '2',
                'ignore_bots': '0',
                'hide_nsfw': '1',
                'hide_nsfl': '1',
            }),
            meta={'csrf': False},
        )
        result = form.validate()
        return result, [str(e) for e in form.user_name.errors]


class TestTheNamespaceClaimIsRefused:
    """The reported defect itself: user `a.b` claiming `myfeed/aXb`."""

    def test_a_metacharacter_username_cannot_claim_a_foreign_namespace(self, app, db_session):
        viewer = local_user('a.b')

        # The pre-fix pattern, built here rather than described, so this test
        # states what it is defending against instead of trusting a comment:
        # unescaped, 'a.b' matches the unrelated segment 'aXb'.
        assert re.match(r'^[a-zA-Z0-9_]+(?:/' + viewer.user_name.lower() + ')?$', 'myfeed/aXb')
        assert viewer.user_name.lower() != 'axb'

        result, url_data, errors = create_form(app, viewer, 'myfeed/aXb', False)

        assert result is False
        assert len(errors) == 1
        # The message, not just the count: apply_feed_url_rules' dash guard
        # rejects with the same (False, one error) shape, so only the text
        # says WHICH guard refused this.
        assert str(errors[0]) == REGEX_ERROR
        # Private + already contains '/', so the else branch merely lowered
        # it -- the url reaching the regex is 'myfeed/axb', unchanged in
        # shape.
        assert url_data == 'myfeed/axb'


class TestTheLegitimateCaseStillWorks:
    """The over-correction guard. `a.b` is a real user; escaping the pattern
    must not cost them their own feed namespace.

    Neither test here fails before the fix -- they cannot, since the pre-fix
    pattern is strictly more permissive. They exist to reject a fix that
    over-corrects, which is the failure mode `re.escape()` is easiest to get
    wrong in.
    """

    def test_a_metacharacter_user_can_still_claim_their_own_namespace(self, app, db_session):
        viewer = local_user('a.b')
        result, url_data, errors = create_form(app, viewer, 'myfeed/a.b', False)

        assert result is True
        assert errors == []
        assert url_data == 'myfeed/a.b'

    def test_the_append_username_branch_still_produces_a_valid_url(self, app, db_session):
        """The ordinary private-feed flow: no '/' typed, so the function
        appends '/<username>' itself and must then accept what it built. A
        fix that escaped the pattern but rejected the metacharacter user
        outright would fail here.
        """
        viewer = local_user('a.b')
        result, url_data, errors = create_form(app, viewer, 'MyFeed', False)

        assert url_data == 'myfeed/a.b'
        assert result is True
        assert errors == []


class TestOrdinaryUsernamesAreUnaffected:
    """`re.escape()` on a name that contains nothing to escape must be a
    no-op in both directions.
    """

    def test_an_ordinary_user_can_claim_their_own_namespace(self, app, db_session):
        viewer = local_user('alice')
        result, url_data, errors = create_form(app, viewer, 'myfeed/alice', False)

        assert result is True
        assert errors == []
        assert url_data == 'myfeed/alice'

    def test_an_ordinary_user_cannot_claim_someone_elses(self, app, db_session):
        viewer = local_user('alice')
        result, url_data, errors = create_form(app, viewer, 'myfeed/bob', False)

        assert result is False
        assert len(errors) == 1
        assert str(errors[0]) == REGEX_ERROR
        assert url_data == 'myfeed/bob'


class TestOtherMetacharactersThatBypassed:
    """Not just '.'. Each metacharacter below reshapes the interpolated group
    differently, and each let a DIFFERENT foreign url through before the fix.

    '|' is the sharpest of them: `a|b` splits the pattern into
    `^[a-zA-Z0-9_]+(?:/a` OR `b)?$`, so the optional group no longer has
    anything to do with the username at all -- `other/a` validates, claiming
    a namespace named after a completely unrelated user `a`.
    """

    # (username, a foreign url the unescaped pattern wrongly accepted)
    BYPASSES = [
        ('a.b', 'myfeed/aXb'),   # '.' is any character
        ('a+b', 'myfeed/aaab'),  # '+' repeats the preceding 'a'
        ('a*b', 'myfeed/b'),     # '*' makes the preceding 'a' optional
        ('a|b', 'other/a'),      # '|' alternates the whole group away
    ]

    @pytest.mark.parametrize('user_name,foreign_url', BYPASSES)
    def test_the_foreign_url_is_refused(self, app, db_session, user_name, foreign_url):
        viewer = local_user(user_name)
        # Pin that the pre-fix pattern really did accept this pairing, so a
        # future reader can see each row of BYPASSES is a live bypass and not
        # a guess.
        assert re.match(r'^[a-zA-Z0-9_]+(?:/' + user_name + ')?$', foreign_url)

        result, url_data, errors = create_form(app, viewer, foreign_url, False)

        assert result is False
        assert len(errors) == 1
        assert str(errors[0]) == REGEX_ERROR

    @pytest.mark.parametrize('user_name,foreign_url', BYPASSES)
    def test_each_of_them_keeps_their_own_namespace(self, app, db_session, user_name, foreign_url):
        viewer = local_user(user_name)
        result, url_data, errors = create_form(app, viewer, f'myfeed/{user_name}', False)

        assert result is True
        assert errors == []
        assert url_data == f'myfeed/{user_name}'


class TestMetacharactersThatCrashed:
    """'(' and '[' do not merely widen the pattern -- they make it
    syntactically invalid, so `re.match` raised `re.error` ("missing ),
    unterminated subpattern" / "unterminated character set") straight out of
    apply_feed_url_rules and the feed form 500'd. Escaping fixes that too:
    the pattern is now well-formed and the url is refused normally.
    """

    @pytest.mark.parametrize('user_name', ['a(b', 'a[b'])
    def test_an_unbalanced_metacharacter_no_longer_raises(self, app, db_session, user_name):
        viewer = local_user(user_name)
        with pytest.raises(re.error):
            re.match(r'^[a-zA-Z0-9_]+(?:/' + user_name + ')?$', 'myfeed/whatever')

        result, url_data, errors = create_form(app, viewer, 'myfeed/whatever', False)

        assert result is False
        assert len(errors) == 1
        assert str(errors[0]) == REGEX_ERROR

    @pytest.mark.parametrize('user_name', ['a(b', 'a[b'])
    def test_they_keep_their_own_namespace_too(self, app, db_session, user_name):
        viewer = local_user(user_name)
        result, url_data, errors = create_form(app, viewer, f'myfeed/{user_name}', False)

        assert result is True
        assert errors == []
        assert url_data == f'myfeed/{user_name}'


class TestTheAdminFormEnforcesTheCharset:
    """Site 1. `AddUserForm` is the only admin path that sets a user_name --
    `EditUserForm` has no such field -- so this is where new metacharacter
    usernames were entering the database.
    """

    @pytest.mark.parametrize('user_name', ['a.b', 'a+b', 'a*b', 'a|b', 'a(b', 'a[b', 'a b', 'a-b'])
    def test_a_metacharacter_username_is_rejected(self, app, db_session, user_name):
        result, errors = admin_form_errors(app, user_name)

        assert result is False
        assert CHARSET_ERROR in errors

    @pytest.mark.parametrize('user_name', ['alice', 'service_account', 'Feed_Bot_2'])
    def test_an_ordinary_username_is_still_accepted(self, app, db_session, user_name):
        result, errors = admin_form_errors(app, user_name)

        assert errors == []
        assert result is True

    def test_the_at_sign_rule_it_already_had_still_applies(self, app, db_session):
        """The pre-existing '@' check must not be lost to the new one, and
        its own message must survive -- '@' is not in the charset either, so
        a careless fix would silently replace one rejection reason with
        another.
        """
        result, errors = admin_form_errors(app, 'alice@example.com')

        assert result is False
        assert 'User names cannot contain @.' in errors


class TestBothUserCreationPathsShareOneRule:
    """The decision recorded in the module docstring: the admin path enforces
    exactly the self-registration charset, not a more permissive variant.
    Asserted as behaviour on both validators rather than by comparing source
    text, so it keeps holding if either form is refactored.
    """

    @pytest.mark.parametrize('user_name', ['a.b', 'a|b', 'a(b'])
    def test_both_forms_refuse_the_same_name_with_the_same_message(self, app, db_session, user_name):
        with app.test_request_context('/'):
            registration = RegistrationForm(meta={'csrf': False})
            registration.user_name.data = user_name
            with pytest.raises(ValidationError) as registration_error:
                registration.validate_user_name(registration.user_name)

            admin = AddUserForm(meta={'csrf': False})
            admin.user_name.data = user_name
            with pytest.raises(ValidationError) as admin_error:
                admin.validate_user_name(admin.user_name)

        assert str(registration_error.value) == CHARSET_ERROR
        assert str(admin_error.value) == CHARSET_ERROR

    @pytest.mark.parametrize('user_name', ['alice', 'service_account'])
    def test_both_forms_accept_the_same_ordinary_name(self, app, db_session, user_name):
        with app.test_request_context('/'):
            registration = RegistrationForm(meta={'csrf': False})
            registration.user_name.data = user_name
            registration.validate_user_name(registration.user_name)

            admin = AddUserForm(meta={'csrf': False})
            admin.user_name.data = user_name
            admin.validate_user_name(admin.user_name)

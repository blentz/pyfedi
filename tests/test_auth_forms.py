"""app/auth/forms.py -- the registration form's validators.

MEASUREMENT BASIS. The module stood at 58.91% on the full-suite --cov=app run
at e6afdb22f, carrying 28 missing statements and 25 missing arcs -- the
lowest-covered form file in the repo.

Two defects are pinned here and repaired together:

  P1  `if len(password.data) == 128` rejects a password of EXACTLY the stated
      maximum length and admits 129 and 130. The guard is wrong in both
      directions at once.
  P2  the common-password check is written twice, character for character, on
      unmodified data -- so the second copy is unreachable.

WTFORMS ORDER. A field's own validator chain runs BEFORE its `validate_<name>`
method, so a value the chain rejects never reaches the method under test here.
`validate_password` is therefore exercised directly, with the field's data set
on a form built inside a request context; that is the only way to see the
custom validator's own boundary rather than `Length(min=8, max=129)`'s.
"""
import pytest
from wtforms.validators import ValidationError

from app import db
from app.auth.forms import LoginForm, RegistrationForm
from app.models import Community, Feed, Site, User
from tests.factories import make_community, make_instance, make_local_feed, make_user

pytestmark = pytest.mark.usefixtures('site')


def _seed():
    instance = make_instance('test.piefed.local', software='piefed')
    burn = make_user(instance, 'burnseat', local=True)
    assert burn.id == 1
    return instance, burn


def _form(app, **data):
    """A RegistrationForm with CSRF off, built inside a request context.

    FlaskForm reads the request during construction, so a bare instantiation
    outside a context raises. meta={'csrf': False} is what lets a test set
    field data directly instead of posting a token.
    """
    form = RegistrationForm(meta={'csrf': False})
    for name, value in data.items():
        getattr(form, name).data = value
    return form


def _password_error(app, value):
    """The message validate_password raises for `value`, or None."""
    with app.test_request_context('/'):
        form = _form(app, password=value)
        try:
            form.validate_password(form.password)
        except ValidationError as error:
            return str(error)
        return None


def _user_name_error(app, value):
    with app.test_request_context('/'):
        form = _form(app, user_name=value)
        try:
            form.validate_user_name(form.user_name)
        except ValidationError as error:
            return str(error)
        return None


def _email_error(app, value):
    with app.test_request_context('/'):
        form = _form(app, real_email=value)
        try:
            form.validate_real_email(form.real_email)
        except ValidationError as error:
            return str(error)
        return None


# --------------------------------------------------------------------------
# P1: the length boundary
# --------------------------------------------------------------------------


def _password_of(length):
    """A password of exactly `length` characters that trips no other rule."""
    password = 'aB3!' + 'x' * (length - 4)
    assert len(password) == length
    return password


@pytest.mark.parametrize('length, rejected', [
    (127, False),
    (128, False),
    (129, True),
    (130, True),
])
def test_the_maximum_password_length_is_the_one_the_form_advertises(app, db_session,
                                                                    length, rejected):
    """Before the repair the guard read `== 128`, and measured across the
    boundary that is wrong in both directions at once:

        PROBE w1 len=127 errors=[]
        PROBE w1 len=128 errors=['Maximum password length is 128 characters.']
        PROBE w1 len=129 errors=[]
        PROBE w1 len=130 errors=[]

    128 is the maximum the field's own title promises the user -- "Minimum
    length 8, maximum 128" -- so it is the one length that must be accepted,
    and 129 upward are the ones that must not be.
    """
    error = _password_error(app, _password_of(length))

    assert (error is not None) is rejected
    if rejected:
        assert error == 'Maximum password length is 128 characters.'


def test_a_password_at_the_maximum_is_not_rejected_by_the_form_either(app, db_session):
    """The custom validator is not the only length rule: the field carries
    `Length(min=8, max=129)`. This row proves the two agree at 128, so the
    repair did not simply move the refusal from one validator to the other.
    """
    _seed()
    with app.test_request_context('/'):
        form = _form(app, password=_password_of(128))
        form.password.validate(form)

    assert form.password.errors == []


# --------------------------------------------------------------------------
# P2: the check that was written twice
# --------------------------------------------------------------------------


@pytest.mark.parametrize('password', ['password', '12345678', '1234567890'])
def test_a_common_password_is_refused(app, db_session, password):
    assert _password_error(app, password) == 'This password is too common.'


def test_the_common_password_check_appears_once(app, db_session):
    """P2. `:97-98` repeated `:81-82` character for character, on data nothing
    between them modifies, so the second copy could only be reached when the
    first had already declined to fire -- i.e. never. Coverage reported the arc
    [97, 98] missing and no test could close it.

    Asserted on the SOURCE, because a duplicate that raises the same message
    from the same input is invisible to any behavioural assertion: that is
    exactly why it survived.
    """
    import inspect

    source = inspect.getsource(RegistrationForm.validate_password)

    assert source.count("_l('This password is too common.')") == 1


# --------------------------------------------------------------------------
# validate_password: the rest
# --------------------------------------------------------------------------


@pytest.mark.parametrize('password, rejected', [
    ('aaaaaaaa', True),
    ('11111111', True),
    ('aaaaaaab', False),
    ('baaaaaaa', False),
])
def test_a_password_of_one_repeated_character_is_refused(app, db_session, password,
                                                         rejected):
    """The `all_the_same` loop. The two accepted rows differ from the refused
    ones in ONE character, at each end, so a loop that stopped early or started
    late would show up here.
    """
    error = _password_error(app, password)

    assert (error == 'This password is not secure.') is rejected


def test_an_empty_password_is_left_to_the_required_validator(app, db_session):
    """`if not password.data: return` -- the early exit. An empty password is
    DataRequired()'s to refuse, and this validator would otherwise index [0] of
    an empty string.
    """
    assert _password_error(app, '') is None


def test_a_password_is_stripped_before_it_is_checked(app, db_session):
    """D862, fixed. The validator rewrites the submitted value, and it used to
    be the only path that did, so a password set elsewhere kept its spaces.
    `User.set_password` and `check_password` now strip as well, so every path
    agrees (owner ruling 2026-09-30).
    """
    with app.test_request_context('/'):
        form = _form(app, password='  secretpw  ')
        form.validate_password(form.password)

        assert form.password.data == 'secretpw'


def test_an_ordinary_password_raises_nothing(app, db_session):
    assert _password_error(app, 'aB3!xyzzy') is None


# --------------------------------------------------------------------------
# validate_user_name
# --------------------------------------------------------------------------


@pytest.mark.parametrize('user_name, message', [
    ('has space', 'User names cannot contain spaces.'),
    ('has@at', 'User names cannot contain @.'),
])
def test_a_user_name_with_a_forbidden_character_is_refused(app, db_session, user_name,
                                                           message):
    _seed()
    assert _user_name_error(app, user_name) == message


def test_a_user_name_is_stripped_before_it_is_checked(app, db_session):
    """`user_name.data = user_name.data.strip()` at :50, which is why '  bob  '
    is not refused for containing spaces.
    """
    _seed()
    with app.test_request_context('/'):
        form = _form(app, user_name='  bob  ')
        form.validate_user_name(form.user_name)

        assert form.user_name.data == 'bob'


def test_a_user_name_outside_the_allowed_charset_is_refused(app, db_session):
    """Delegated to app/utils.py's validate_user_name_charset, which is the one
    rule shared with the admin user-creation path.
    """
    _seed()
    assert _user_name_error(app, 'bad-name!') is not None


@pytest.mark.parametrize('deleted, message', [
    (False, 'An account with this user name already exists.'),
    (True, 'This username was used in the past and cannot be reused.'),
])
def test_a_taken_user_name_is_refused_and_says_which_kind(app, db_session, deleted,
                                                          message):
    """Both arms of `if user.deleted:`. A deleted account's name is burned
    rather than freed, and the two messages are the only thing telling the two
    apart.
    """
    instance, burn = _seed()
    taken = make_user(instance, 'taken', local=True)
    taken.deleted = deleted
    db.session.commit()

    assert _user_name_error(app, 'taken') == message


def test_a_user_name_matching_a_remote_account_is_allowed(app, db_session):
    """`.filter_by(ap_id=None)` -- the lookup is for LOCAL accounts only, so a
    remote user of the same name does not block registration.
    """
    instance, burn = _seed()
    peer = make_instance('remote.example', software='lemmy')
    make_user(peer, 'elsewhere', local=False)

    assert _user_name_error(app, 'elsewhere') is None


def test_a_user_name_is_matched_without_regard_to_case(app, db_session):
    instance, burn = _seed()
    make_user(instance, 'taken', local=True)

    assert _user_name_error(app, 'TAKEN') == 'An account with this user name already exists.'


def test_a_user_name_that_is_a_local_communitys_name_is_refused(app, db_session):
    _seed()
    make_community('microblogs')

    assert _user_name_error(app, 'microblogs') == 'This name is in use already.'


def test_a_user_name_that_is_a_remote_communitys_name_is_allowed(app, db_session):
    """`Community.ap_id == None` -- the same local-only rule as the user
    lookup, and the row that makes that conjunct load-bearing.
    """
    _seed()
    community = make_community('remotecomm')
    community.ap_id = 'remotecomm@peer.example'
    db.session.commit()

    assert _user_name_error(app, 'remotecomm') is None


def test_a_user_name_that_is_a_local_feeds_name_is_refused(app, db_session):
    _seed()
    make_local_feed('localfeed')

    assert _user_name_error(app, 'localfeed') == 'This name is in use already.'


def test_a_user_name_that_is_a_remote_feeds_name_is_allowed(app, db_session):
    _seed()
    feed = make_local_feed('remotefeed')
    feed.ap_id = 'remotefeed@peer.example'
    db.session.commit()

    assert _user_name_error(app, 'remotefeed') is None


def test_a_feeds_name_is_matched_in_lower_case(app, db_session):
    """The feed lookup differs from the two above it: it lowercases the INPUT
    (`Feed.name == user_name.data.lower()`) rather than both sides, so a feed
    stored with capitals is not found. Pinned as the behaviour it is.
    """
    _seed()
    feed = make_local_feed('MixedCase')
    db.session.commit()

    assert feed.name == 'MixedCase'
    assert _user_name_error(app, 'MixedCase') is None


def test_an_unused_user_name_raises_nothing(app, db_session):
    _seed()
    assert _user_name_error(app, 'brandnew') is None


# --------------------------------------------------------------------------
# validate_real_email, __init__ and filter_user_name
# --------------------------------------------------------------------------


def test_an_email_already_registered_is_refused(app, db_session):
    instance, burn = _seed()
    taken = make_user(instance, 'taken', local=True)
    taken.email = 'taken@example.com'
    db.session.commit()

    assert _email_error(app, 'taken@example.com') == 'An account with this email address already exists.'


def test_an_email_is_matched_without_regard_to_case_or_padding(app, db_session):
    """`func.lower(...) == func.lower(email.data.strip())` -- both halves in one
    row, since either alone would let a duplicate through.
    """
    instance, burn = _seed()
    taken = make_user(instance, 'taken', local=True)
    taken.email = 'taken@example.com'
    db.session.commit()

    assert _email_error(app, '  TAKEN@Example.COM  ') is not None


def test_an_unused_email_raises_nothing(app, db_session):
    _seed()
    assert _email_error(app, 'brandnew@example.com') is None


@pytest.mark.parametrize('captcha_enabled, present', [
    (True, True),
    (False, False),
])
def test_the_captcha_field_is_removed_when_the_site_turns_it_off(app, db_session,
                                                                 captcha_enabled, present):
    """`__init__`'s `delattr(self, 'captcha')`.

    ASSERTED ON `_fields`, NOT ON `hasattr`. WTForms' `__delattr__` removes the
    field from the form's registry but leaves the name resolving to `None`
    rather than raising, so `hasattr(form, 'captcha')` is True either way and a
    test written on it fails against correct code:

        PROBE x1 hasattr captcha: True
        PROBE x1 type: <class 'NoneType'>
        PROBE x1 in form._fields: False
        PROBE x1 iterated fields: [... no captcha ...]

    `_fields` is what the template iterates and what validation walks, so it is
    the observable that matches what the removal is for.
    """
    from unittest.mock import patch

    _seed()
    with app.test_request_context('/'):
        with patch('app.auth.forms.get_setting', return_value=captcha_enabled):
            form = RegistrationForm(meta={'csrf': False})

        assert ('captcha' in form._fields) is present
        assert ('captcha' in [field.name for field in form]) is present


@pytest.mark.parametrize('value, expected', [
    ('  bob  ', 'bob'),
    ('bob', 'bob'),
    (None, None),
])
def test_the_user_name_filter_strips_only_strings(app, db_session, value, expected):
    """`filter_user_name` runs before validation and has to survive a non-string,
    which is what the isinstance guard is for.
    """
    with app.test_request_context('/'):
        form = _form(app)

        assert form.filter_user_name(value) == expected


# --------------------------------------------------------------------------
# Rows the mutation pass asked for
# --------------------------------------------------------------------------


def test_the_captcha_is_on_when_the_site_has_never_said_otherwise(app, db_session):
    """`get_setting('captcha_enabled', True)` -- the DEFAULT, which the two
    rows above cannot see because they patch get_setting outright. With no
    setting stored, the captcha has to be present: a default of False would
    silently drop the captcha from every fresh install.
    """
    from app.models import Settings

    _seed()
    assert Settings.query.filter_by(name='captcha_enabled').first() is None

    with app.test_request_context('/'):
        form = RegistrationForm(meta={'csrf': False})

    assert 'captcha' in form._fields


def test_a_registered_email_is_matched_whatever_case_it_was_stored_in(app, db_session):
    """`func.lower(User.email)` -- the lower() on the STORED side. Every other
    row here seeds a lowercase address, which the comparison finds with or
    without it; an address stored with capitals is what makes it load-bearing.
    """
    instance, burn = _seed()
    taken = make_user(instance, 'taken', local=True)
    taken.email = 'Taken@Example.COM'
    db.session.commit()

    assert _email_error(app, 'taken@example.com') == 'An account with this email address already exists.'


def test_a_community_name_is_matched_whatever_case_it_was_stored_in(app, db_session):
    """`func.lower(Community.name)` -- the same lower()-on-the-stored-side as
    the email row above, and unexercised for the same reason.
    """
    _seed()
    community = make_community('microblogs')
    community.name = 'MicroBlogs'
    db.session.commit()

    assert _user_name_error(app, 'microblogs') == 'This name is in use already.'


@pytest.mark.parametrize('password', ['aaaaaaaa', 'aaaaaaab', 'baaaaaaa', 'abababab'])
def test_the_repeated_character_check_does_not_depend_on_which_end_it_starts_from(
        app, db_session, password):
    """EQUIVALENCE PROOF for seeding `first_char` from `password.data[-1]`
    instead of `[0]`.

    The loop asks "is every character equal to the seed". If they all are, both
    ends hold the same character and both seeds answer True. If they are not,
    two characters differ, so SOME character differs from either seed and both
    answer False. There is no input on which the two disagree, which is why the
    mutant survived a suite that already covers both outcomes and both ends.

    Registered with the mutation pass rather than fixed.
    """
    from app.auth.forms import RegistrationForm as Form

    def all_same_from(seed_index):
        seed = password[seed_index]
        return all(char == seed for char in password)

    assert all_same_from(0) == all_same_from(-1)
    assert (_password_error(app, password) == 'This password is not secure.') is all_same_from(0)

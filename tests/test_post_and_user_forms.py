"""app/post/forms.py and app/user/forms.py -- the report helpers and the
profile validators.

MEASUREMENT BASIS, from the full-suite --cov=app run at cf8392f06:

    app/post/forms.py   73.171   missing 14 lines, 8 arcs
    app/user/forms.py   85.311   missing 14 lines, 12 arcs

One defect is pinned here and repaired with it:

  P1  ProfileForm.validate_matrix_user_id has NEVER RUN. WTForms binds an
      inline validator by name -- for a field called `matrixuserid` it looks
      for `validate_matrixuserid` -- and the method is called
      `validate_matrix_user_id`, so nothing binds it. Sub-project 45's class:
      a guard that does not guard.

Fact 333 applies to every row here: assert on `form._fields` and on
`field.errors`, never on `hasattr`, because WTForms leaves an unbound name
resolving rather than raising.
"""
import pytest
from flask_login import login_user
from wtforms.validators import ValidationError

from app import db
from app.models import Site
from app.post.forms import NewReminderForm, ReportPostForm
from app.user.forms import ProfileForm, ReportUserForm
from tests.factories import make_instance, make_user

pytestmark = pytest.mark.usefixtures('site')


def _seed():
    instance = make_instance('test.piefed.local', software='piefed')
    burn = make_user(instance, 'burnseat', local=True)
    assert burn.id == 1
    alice = make_user(instance, 'alice', local=True)
    db.session.commit()
    return instance, alice


# --------------------------------------------------------------------------
# P1: the validator that never ran
# --------------------------------------------------------------------------


def _profile_errors(app, user, **data):
    """ProfileForm's errors for one set of field values.

    SUBMITTED AS FORMDATA, not assigned to `.data`. WTForms' `Optional()` reads
    `field.raw_data`, which assigning `.data` leaves empty -- so an Optional
    field built that way is treated as blank, its errors are cleared and
    StopValidation ends its chain before any inline validator runs. Measured:
    with `.data` set to 'no-at-sign' the form reported only
    {'timezone': ['Not a valid choice.']} while a direct call to the validator
    raised. Fact 336.

    login_user is required: validate_email calls
    current_user.another_account_using_email.
    """
    from werkzeug.datastructures import MultiDict

    fields = MultiDict({
        'title': data.get('title', 'Alice'),
        'email': data.get('email', 'alice@example.com'),
        'matrixuserid': data.get('matrixuserid', ''),
        'timezone': data.get('timezone', 'Europe/London'),
    })
    with app.test_request_context('/'):
        login_user(user)
        form = ProfileForm(formdata=fields, meta={'csrf': False})
        form.validate()
        return form


@pytest.mark.parametrize('value, rejected', [
    ('@alice:matrix.org', False),
    ('  @alice:matrix.org  ', False),
    ('alice:matrix.org', True),
    ('no-at-sign-here', True),
])
def test_a_matrix_id_must_start_with_an_at_sign(app, db_session, value, rejected):
    """Before the repair this validator was never bound, so EVERY value was
    accepted:

        PROBE y1 validator sought by wtforms: validate_matrixuserid -> False
        PROBE y1 validator actually defined: validate_matrix_user_id -> True
        PROBE y1 matrixuserid errors: []

    The padded row is here because the check strips before testing, so a value
    the user typed with spaces is judged on its content.
    """
    instance, alice = _seed()

    form = _profile_errors(app, alice, matrixuserid=value)

    assert (form.matrixuserid.errors != []) is rejected
    if rejected:
        assert 'Matrix user ids start with @' in str(form.matrixuserid.errors[0])


def test_an_empty_matrix_id_is_still_optional(app, db_session):
    """THE OTHER HALF OF THE REPAIR, and the reason the rename alone would have
    been wrong. The field is `Optional()`, and the method as written rejects
    the empty string -- `''.strip().startswith('@')` is False -- so binding it
    without this guard would have made a Matrix ID mandatory for every profile
    save, for everyone who has never used Matrix.
    """
    instance, alice = _seed()

    form = _profile_errors(app, alice, matrixuserid='')

    assert form.matrixuserid.errors == []


def test_a_matrix_id_of_only_spaces_is_treated_as_blank(app, db_session):
    """The SECOND half of the empty guard, and the only half a submitted form
    can reach.

    `Optional()` short-circuits on empty raw_data, so a truly blank field never
    reaches this validator at all -- the row above proves the field stays
    optional, but it does so through Optional() rather than through the guard.
    Whitespace-only data IS raw_data, so Optional lets it through and
    `not matrix_user_id.data.strip()` is what stops it being rejected for
    lacking an '@'.
    """
    instance, alice = _seed()

    form = _profile_errors(app, alice, matrixuserid='   ')

    assert form.matrixuserid.errors == []


@pytest.mark.parametrize('value', [None, '', '   '])
def test_the_empty_guard_answers_every_shape_of_blank(app, db_session, value):
    """BOTH halves of the guard, exercised DIRECTLY because neither is
    reachable through a submitted form.

    `Optional()` does not merely check for an empty string: its `string_check`
    strips first, so a field of only spaces is blank to it too, and it clears
    the errors and ends the chain before any inline validator runs. That makes
    both `not data` and `not data.strip()` defensive here -- the mutant
    dropping the strip half survived a whitespace-only row submitted through
    the form for exactly that reason.

    They still matter: the validator is an ordinary method, and a caller that
    reaches it with None -- which a field built without formdata carries -- or
    with padding gets a return rather than a spurious "start with @".
    """
    instance, alice = _seed()
    with app.test_request_context('/'):
        login_user(alice)
        form = ProfileForm(meta={'csrf': False})
        form.matrixuserid.data = value

        assert form.validate_matrixuserid(form.matrixuserid) is None


def test_the_matrix_validator_is_bound_to_its_field(app, db_session):
    """The wiring itself, asserted directly: WTForms looks up
    `validate_<field name>`, and the whole defect was that no such attribute
    existed. Asserting on the errors alone would not say WHY a future rename
    broke it again.
    """
    instance, alice = _seed()
    with app.test_request_context('/'):
        login_user(alice)
        form = ProfileForm(meta={'csrf': False})

        assert 'matrixuserid' in form._fields
        assert callable(getattr(form, 'validate_matrixuserid', None))


# --------------------------------------------------------------------------
# Every inline validator in app/, not just this one
# --------------------------------------------------------------------------


def test_every_inline_validator_names_a_field_that_exists():
    """THE GUARD THAT WOULD HAVE CAUGHT P1, over the whole of app/.

    WTForms binds `validate_<name>` to the field called `<name>` and says
    nothing when there is no such field -- the method simply never runs, and
    the only symptom is coverage the round never explains.

    The scan ignores DECORATED methods, because marshmallow's
    `@validates_schema` in app/api/alpha/schema.py binds by decorator rather
    than by name, and it accepts any call to something ending in `Field`, which
    is what a first version missed: `DateTimeLocalField` was not in its list of
    field types and `CreatePostForm.validate_scheduled_for` was reported as
    dead when it is correctly wired. Both false positives are the reason this
    reads the AST rather than grepping.
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    unwired = []
    for path in sorted((root / 'app').rglob('*.py')):
        if '__pycache__' in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.ClassDef):
                continue
            fields = set()
            for item in node.body:
                if isinstance(item, ast.Assign) and isinstance(item.value, ast.Call):
                    func = item.value.func
                    name = func.id if isinstance(func, ast.Name) else getattr(func, 'attr', '')
                    if name.endswith('Field'):
                        fields.update(t.id for t in item.targets if isinstance(t, ast.Name))
            for item in node.body:
                if (isinstance(item, ast.FunctionDef)
                        and item.name.startswith('validate_')
                        and not item.decorator_list):
                    target = item.name[len('validate_'):]
                    if target and target not in fields:
                        unwired.append(
                            f'{path.relative_to(root).as_posix()}:{item.lineno} '
                            f'{node.name}.{item.name}')

    assert unwired == [], (
        'these inline validators name no field on their own class, so WTForms '
        'never binds them and they never run: ' + '; '.join(unwired))


# --------------------------------------------------------------------------
# ProfileForm's other two validators
# --------------------------------------------------------------------------


def test_an_email_used_by_another_account_is_refused(app, db_session):
    instance, alice = _seed()
    other = make_user(instance, 'bob', local=True)
    other.email = 'bob@example.com'
    db.session.commit()

    form = _profile_errors(app, alice, email='bob@example.com')

    assert 'already in use by another account' in str(form.email.errors[0])


def test_a_users_own_email_is_not_a_conflict(app, db_session):
    """The other arm: `another_account_using_email` excludes the current user,
    so saving a profile without changing the address is not a duplicate.
    """
    instance, alice = _seed()
    alice.email = 'alice@example.com'
    db.session.commit()

    form = _profile_errors(app, alice, email='alice@example.com')

    assert form.email.errors == []


@pytest.mark.parametrize('title, rejected', [
    ('[deleted]', True),
    ('  [DELETED]  ', True),
    ('deleted', False),
    ('Alice', False),
])
def test_the_deleted_display_name_is_reserved(app, db_session, title, rejected):
    """`field.data.strip().lower() == '[deleted]'` -- both transformations, in
    one parametrize: the padded uppercase row fails if either is dropped, and
    the bare word proves the brackets are load-bearing.
    """
    instance, alice = _seed()

    form = _profile_errors(app, alice, title=title)

    assert (form.title.errors != []) is rejected


def test_the_timezone_choices_are_filled_in(app, db_session):
    """`__init__` assigns `self.timezone.choices = get_timezones()`. The field
    is declared with no choices at all, so a SelectField would reject every
    submission if this stopped running.
    """
    instance, alice = _seed()
    with app.test_request_context('/'):
        login_user(alice)
        form = ProfileForm(meta={'csrf': False})

        # choices is an OrderedDict of GROUPS -- {'Africa': [(value, label), ...]}
        # -- not a flat list, so the membership test has to walk the groups.
        # There is no bare 'UTC' entry among the 505: the list is Olson region
        # names, which is why this asserts on one of those rather than on the
        # abbreviation a first version of this row guessed at.
        assert form.timezone.choices
        every_value = [value for group in form.timezone.choices.values()
                       for value, label in group]
        assert 'Europe/London' in every_value
        assert len(every_value) > 100


# --------------------------------------------------------------------------
# reasons_to_string, in both of its copies
# --------------------------------------------------------------------------


@pytest.mark.parametrize('form_class', [ReportPostForm, ReportUserForm])
def test_report_reasons_are_joined_in_the_order_they_were_submitted(app, db_session,
                                                                    form_class):
    """The OUTER loop is over `reason_data`, so the output follows the order the
    ids arrived in, not the order they appear on the form. Asserted with the
    two ids reversed, which is the only arrangement that tells the two
    orderings apart. Both copies of the method are exercised: R1 records that
    there are four.
    """
    with app.test_request_context('/'):
        form = form_class(meta={'csrf': False})
        first_id, first_label = form.reason_choices[0][0], str(form.reason_choices[0][1])
        third_id, third_label = form.reason_choices[2][0], str(form.reason_choices[2][1])

        result = form.reasons_to_string([third_id, first_id])

    assert result == f'{third_label}, {first_label}'


@pytest.mark.parametrize('form_class', [ReportPostForm, ReportUserForm])
def test_an_unknown_report_reason_contributes_nothing(app, db_session, form_class):
    """`if choice[0] == reason_id` from the other side -- an id matching no
    choice is skipped rather than raising or appearing blank.
    """
    with app.test_request_context('/'):
        form = form_class(meta={'csrf': False})
        known = form.reason_choices[0][0]

        assert form.reasons_to_string(['999']) == ''
        assert form.reasons_to_string([known, '999']) == str(form.reason_choices[0][1])


@pytest.mark.parametrize('form_class', [ReportPostForm, ReportUserForm])
def test_no_reasons_at_all_is_an_empty_string(app, db_session, form_class):
    with app.test_request_context('/'):
        form = form_class(meta={'csrf': False})

        assert form.reasons_to_string([]) == ''


@pytest.mark.parametrize('form_class', [ReportPostForm, ReportUserForm])
def test_the_joined_reasons_are_truncated_to_the_columns_width(app, db_session,
                                                               form_class):
    """`[:255]` -- the description column's width. Every choice is submitted,
    which is the only way to exceed it.
    """
    with app.test_request_context('/'):
        form = form_class(meta={'csrf': False})
        every_id = [choice[0] for choice in form.reason_choices]

        result = form.reasons_to_string(every_id)

    assert len(result) == 255


# --------------------------------------------------------------------------
# validate_remind_at
# --------------------------------------------------------------------------


def _remind_error(app, value):
    with app.test_request_context('/'):
        form = NewReminderForm(meta={'csrf': False})
        form.remind_at.data = value
        try:
            form.validate_remind_at(form.remind_at)
        except ValidationError as error:
            return str(error)
        return None


@pytest.mark.parametrize('value', ['in 2 weeks', 'in 3 days'])
def test_a_reminder_in_the_future_is_accepted(app, db_session, value):
    assert _remind_error(app, value) is None


@pytest.mark.parametrize('value', [
    '2 weeks ago',
    'yesterday',
])
def test_a_reminder_in_the_past_is_refused(app, db_session, value):
    """`pendulum.instance(x).in_tz('UTC') < utcnow(naive=False)` -- the second
    half of the guard, which a date that parses cleanly is the only way to
    reach.
    """
    assert _remind_error(app, value) == 'Invalid.'


def test_a_reminder_that_cannot_be_parsed_is_refused(app, db_session):
    """`x is None` -- the first half. dateparser returns None rather than
    raising for a string it cannot read.
    """
    assert _remind_error(app, 'not a date at all') == 'Invalid.'


def test_a_reminder_that_makes_the_parser_raise_is_refused(app, db_session):
    """The bare `except Exception`. R3 records that the ValidationError raised
    inside the try is itself caught here and re-raised identically, so this row
    pins the handler rather than distinguishing the two paths -- which nothing
    can, since they produce the same message.
    """
    from unittest.mock import patch

    with patch('dateparser.parse', side_effect=RuntimeError('parser exploded')):
        assert _remind_error(app, 'in 2 weeks') == 'Invalid.'


def test_an_unparseable_reminder_is_refused_by_either_route(app, db_session):
    """EQUIVALENCE PROOF for dropping `x is None` from the guard.

    The check is redundant under its own handler. With it, a string dateparser
    cannot read short-circuits to ValidationError('Invalid.'). Without it,
    `pendulum.instance(None)` raises, the bare `except Exception` catches that
    and raises ValidationError('Invalid.') -- the same class, the same message,
    from the same input. No assertion can separate them, which is R3 seen from
    the mutation side: a catch-all handler makes the guard in front of it
    unkillable.

    Pinned both ways round so the redundancy is recorded rather than rediscovered.
    """
    import pendulum

    with pytest.raises(Exception):
        pendulum.instance(None)

    assert _remind_error(app, 'not a date at all') == 'Invalid.'

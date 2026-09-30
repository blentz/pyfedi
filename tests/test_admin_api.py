"""app/api/alpha/utils/admin.py and app/api/alpha/__init__.py.

MEASUREMENT BASIS, from the full-suite --cov=app run at 50df198e3:

    app/api/alpha/utils/admin.py   11.429   62 gaps
    app/api/alpha/__init__.py      50.000   38 gaps

Taken together because the second answers for the first: every failure in the
admin API -- including every authorization refusal -- becomes a response through
`shared_error_handler`.

WHAT THIS CODE DOES. `put_registration_approve` DELETES A USER ACCOUNT on its
denial path, and `get_registration_list` exposes every pending applicant's email
and their answer to the signup question. Both sit behind
`user_access("approve registrations", user.id)`, so the rows here are about who
can reach them as much as what they do once reached: a guard that does not guard
is this campaign's most repeated finding, and its two most recent instances
(D863, D884) were both guards that could never fire.

Each refusal is therefore asserted on the SIDE EFFECT as well as the message --
a guard that returns the right error after doing the work is exactly the shape
sub-project 45 was named for.

No production change. Three shapes are registered; see the design note.
"""
import pytest
from unittest.mock import MagicMock, patch

from flask_limiter import RateLimitExceeded
from sqlalchemy.orm.exc import NoResultFound

from app import db
from app.api.alpha.utils.admin import get_registration_list, put_registration_approve
from app.models import Notification, User, UserRegistration
from tests.factories import (bearer, grant_permission, make_user_registration,
                             make_instance, make_user)

pytestmark = pytest.mark.usefixtures('site')


def _approver(user):
    """A user `user_access("approve registrations", ...)` lets through.

    NOTE the caller must not pass `api_baseline.user1`: id 1 passes EVERY
    permission check, so a guard naming any other permission would let it
    through too and the row could not tell which permission is enforced. Use
    `_permitted_approver` below for that.
    """
    grant_permission(user, 'approve registrations')
    return user


def _permitted_approver(instance, name='approver'):
    """An approver who is NOT user 1 and holds ONLY 'approve registrations'.

    This is what makes the guard's permission STRING load-bearing. With the
    founding account as the approver and a roleless user as the refused party,
    a mutant changing the guard to `user_access("some other permission", ...)`
    survives: user 1 passes everything, and a user with no roles fails
    everything, so neither row distinguishes the two permissions. Measured --
    that mutant survived until this helper existed.
    """
    user = make_user(instance, name, local=True)
    user.verified = True
    db.session.commit()
    assert user.id != 1
    grant_permission(user, 'approve registrations')
    return user


def _nobody(instance, name='ordinary'):
    """A user with NO permissions -- which `api_baseline.user1` is not.

    `user_access` short-circuits on `user_id == 1` and returns True for every
    permission (app/utils.py:1652), so the founding account is omnipotent
    regardless of its roles: `user1` has no roles at all and the database holds
    no role_permission rows, yet
    `user_access('approve registrations', 1)` is True. An authorization test
    that used user1 as the unprivileged party would assert nothing. Fact 347.
    """
    user = make_user(instance, name, local=True)
    user.verified = True
    db.session.commit()
    assert user.id != 1
    return user


def _applicant(instance, name='hopeful', answer='because I like it here',
               verified=True):
    user = make_user(instance, name, local=True)
    user.verified = verified
    user.email = f'{name}@example.com'
    db.session.commit()
    registration = make_user_registration(user, answer=answer, status=0)
    return user, registration


# --------------------------------------------------------------------------
# get_registration_list -- who may read the applications
# --------------------------------------------------------------------------


def test_an_approver_can_read_the_pending_applications(app, db_session, api_baseline):
    instance = api_baseline.instance_local
    admin = _permitted_approver(instance)
    applicant, registration = _applicant(instance)

    with app.test_request_context('/'):
        result = get_registration_list(bearer(admin), {})

    assert [r['user_id'] for r in result['registrations']] == [applicant.id]
    assert result['registrations'][0]['answer'] == 'because I like it here'


def test_a_user_without_the_permission_cannot_read_them(app, db_session, api_baseline):
    """THE GUARD, from the refused side. The listing carries every applicant's
    email address and their answer to the signup question, so a reader who
    should not have it must get nothing -- asserted by the exception rather
    than by an empty list, because an empty list would also satisfy a handler
    that ran the query and filtered afterwards.
    """
    instance = api_baseline.instance_local
    ordinary = _nobody(instance)
    _applicant(instance)

    with app.test_request_context('/'):
        with pytest.raises(Exception, match='Insufficient permissions'):
            get_registration_list(bearer(ordinary), {})


def test_an_unauthenticated_caller_cannot_read_them(app, db_session, api_baseline):
    """`authorise_api_user` runs before the permission check, so a bad token is
    refused with a different message -- and the row pins which, because the two
    refusals reach the caller through the same handler.
    """
    instance = api_baseline.instance_local
    _applicant(instance)

    with app.test_request_context('/'):
        with pytest.raises(Exception, match='incorrect_login'):
            get_registration_list('Bearer not-a-real-token', {})


def test_only_pending_applications_are_listed_by_default(app, db_session, api_baseline):
    """`pending_only` defaults True: an approved application is history, and
    the queue is what the admin is working through.
    """
    instance = api_baseline.instance_local
    admin = _permitted_approver(instance)
    pending, _ = _applicant(instance, name='pending')
    done, done_registration = _applicant(instance, name='done')
    done_registration.status = 1
    db.session.commit()

    with app.test_request_context('/'):
        result = get_registration_list(bearer(admin), {})

    assert [r['user_id'] for r in result['registrations']] == [pending.id]


def test_every_application_can_be_asked_for(app, db_session, api_baseline):
    """`pending_only: False` -- the other arm, which reaches the two `else`
    branches of the sort chain.
    """
    instance = api_baseline.instance_local
    admin = _permitted_approver(instance)
    pending, _ = _applicant(instance, name='pending')
    done, done_registration = _applicant(instance, name='done')
    done_registration.status = 1
    db.session.commit()

    with app.test_request_context('/'):
        result = get_registration_list(bearer(admin), {'pending_only': False})

    assert {r['user_id'] for r in result['registrations']} == {pending.id, done.id}


@pytest.mark.parametrize('pending_only, sort, oldest_first', [
    (True, 'New', False),
    (True, 'Old', True),
    (False, 'New', False),
    (False, 'Old', True),
])
def test_the_applications_can_be_sorted_either_way(app, db_session, api_baseline,
                                                   pending_only, sort, oldest_first):
    """All four arms of the sort chain, which is two independent choices --
    pending-only and direction -- written as a four-branch if/elif.
    """
    from datetime import timedelta
    from app.models import utcnow

    instance = api_baseline.instance_local
    admin = _permitted_approver(instance)
    first, first_registration = _applicant(instance, name='early')
    second, second_registration = _applicant(instance, name='late')
    first_registration.created_at = utcnow() - timedelta(days=2)
    second_registration.created_at = utcnow()
    db.session.commit()

    with app.test_request_context('/'):
        result = get_registration_list(bearer(admin),
                                       {'pending_only': pending_only, 'sort': sort})

    ids = [r['user_id'] for r in result['registrations']]
    assert ids == ([first.id, second.id] if oldest_first else [second.id, first.id])


def test_the_listing_is_paginated(app, db_session, api_baseline):
    """`limit` and `page` both come from the caller. R3 records that `limit` is
    unchecked; this row pins that it is honoured, which is what makes the
    absence of a ceiling worth registering.
    """
    instance = api_baseline.instance_local
    admin = _permitted_approver(instance)
    for index in range(3):
        _applicant(instance, name=f'hopeful{index}')

    with app.test_request_context('/'):
        page_one = get_registration_list(bearer(admin), {'limit': 2, 'page': 1})
        page_two = get_registration_list(bearer(admin), {'limit': 2, 'page': 2})

    assert len(page_one['registrations']) == 2
    assert len(page_two['registrations']) == 1


# --------------------------------------------------------------------------
# put_registration_approve -- who may approve, and what approval does
# --------------------------------------------------------------------------


def test_a_user_without_the_permission_cannot_approve(app, db_session, api_baseline):
    """THE GUARD on the endpoint that can DELETE AN ACCOUNT. Asserted on the
    side effect as well as the message: a handler that refused after acting
    would satisfy the exception alone.
    """
    instance = api_baseline.instance_local
    ordinary = _nobody(instance)
    applicant, registration = _applicant(instance)

    with app.test_request_context('/'):
        with pytest.raises(Exception, match='Insufficient permissions'):
            put_registration_approve(bearer(ordinary),
                                     {'user_id': applicant.id, 'approve': True})

    db.session.expire_all()
    assert UserRegistration.query.filter_by(user_id=applicant.id).first().status == 0


def test_a_user_without_the_permission_cannot_deny(app, db_session, api_baseline):
    """The same guard on the denial path, which is the destructive one: denial
    deletes the account outright. The applicant must still exist afterwards.
    """
    instance = api_baseline.instance_local
    ordinary = _nobody(instance)
    applicant, registration = _applicant(instance)
    applicant_id = applicant.id

    with app.test_request_context('/'):
        with pytest.raises(Exception, match='Insufficient permissions'):
            put_registration_approve(bearer(ordinary),
                                     {'user_id': applicant_id, 'approve': False})

    db.session.expire_all()
    assert db.session.get(User, applicant_id) is not None


def test_an_unauthenticated_caller_cannot_approve(app, db_session, api_baseline):
    instance = api_baseline.instance_local
    applicant, registration = _applicant(instance)

    with app.test_request_context('/'):
        with pytest.raises(Exception, match='incorrect_login'):
            put_registration_approve('Bearer not-a-real-token',
                                     {'user_id': applicant.id, 'approve': True})

    db.session.expire_all()
    assert UserRegistration.query.filter_by(user_id=applicant.id).first().status == 0


def test_approving_an_application_records_who_approved_it(app, db_session,
                                                          api_baseline):
    """The audit trail: status, timestamp and approver. `approved_by` is the
    only record of which admin let an account in.
    """
    instance = api_baseline.instance_local
    admin = _permitted_approver(instance)
    applicant, registration = _applicant(instance)

    with app.test_request_context('/'):
        with patch('app.api.alpha.utils.admin.finalize_user_setup'):
            with patch('app.api.alpha.utils.admin.send_registration_approved_email'):
                put_registration_approve(bearer(admin),
                                         {'user_id': applicant.id, 'approve': True})

    db.session.expire_all()
    stored = UserRegistration.query.filter_by(user_id=applicant.id).first()
    assert stored.status == 1
    assert stored.approved_by == admin.id
    assert stored.approved_at is not None


def test_approving_a_verified_applicant_finishes_their_setup(app, db_session,
                                                             api_baseline):
    """`if new_user.verified:` -- the true arm. Both calls are patched because
    finalize_user_setup writes keys and the email sender reaches SMTP; what
    this row is about is that they are reached at all, and for whom.
    """
    instance = api_baseline.instance_local
    admin = _permitted_approver(instance)
    applicant, registration = _applicant(instance, verified=True)

    with app.test_request_context('/'):
        with patch('app.api.alpha.utils.admin.finalize_user_setup') as finalize:
            with patch('app.api.alpha.utils.admin.send_registration_approved_email') as email:
                put_registration_approve(bearer(admin),
                                         {'user_id': applicant.id, 'approve': True})

    assert finalize.call_args.args[0].id == applicant.id
    assert email.call_args.args[0].id == applicant.id


def test_approving_an_unverified_applicant_does_not_finish_their_setup(app, db_session,
                                                                        api_baseline):
    """The false arm. An applicant who has not confirmed their email address is
    approved but not activated -- the verification step still has to happen, so
    finalize_user_setup must NOT run.
    """
    instance = api_baseline.instance_local
    admin = _permitted_approver(instance)
    applicant, registration = _applicant(instance, verified=False)

    with app.test_request_context('/'):
        with patch('app.api.alpha.utils.admin.finalize_user_setup') as finalize:
            with patch('app.api.alpha.utils.admin.send_registration_approved_email') as email:
                put_registration_approve(bearer(admin),
                                         {'user_id': applicant.id, 'approve': True})

    assert finalize.call_args_list == []
    assert email.call_args_list == []

    db.session.expire_all()
    assert UserRegistration.query.filter_by(user_id=applicant.id).first().status == 1


def test_denying_an_application_removes_the_account(app, db_session, api_baseline):
    """The destructive path, which exists so a rejected username becomes
    available again.
    """
    instance = api_baseline.instance_local
    admin = _permitted_approver(instance)
    applicant, registration = _applicant(instance)
    applicant_id, registration_id = applicant.id, registration.id

    with app.test_request_context('/'):
        put_registration_approve(bearer(admin),
                                 {'user_id': applicant_id, 'approve': False})

    db.session.expire_all()
    assert db.session.get(User, applicant_id) is None
    assert db.session.get(UserRegistration, registration_id) is None


def test_denying_an_application_clears_the_notifications_it_caused(app, db_session,
                                                                    api_baseline):
    """The signup raises a notification for every admin; denying has to take
    them away AND correct each recipient's unread counter, or the badge counts
    something that no longer exists.
    """
    instance = api_baseline.instance_local
    admin = _permitted_approver(instance)
    applicant, registration = _applicant(instance)
    admin.unread_notifications = 1
    db.session.add(Notification(user_id=admin.id, author_id=applicant.id,
                                title='New registration', url='/admin/registrations',
                                read=False))
    db.session.commit()
    applicant_id = applicant.id

    with app.test_request_context('/'):
        put_registration_approve(bearer(admin),
                                 {'user_id': applicant_id, 'approve': False})

    db.session.expire_all()
    assert Notification.query.filter_by(author_id=applicant_id).count() == 0
    assert db.session.get(User, admin.id).unread_notifications == 0


def test_a_notification_already_read_does_not_change_the_counter(app, db_session,
                                                                  api_baseline):
    """`if not n.read` -- the unread filter. A read notification is deleted
    with the rest but must not decrement a counter it never incremented, or the
    badge goes negative.
    """
    instance = api_baseline.instance_local
    admin = _permitted_approver(instance)
    applicant, registration = _applicant(instance)
    admin.unread_notifications = 0
    db.session.add(Notification(user_id=admin.id, author_id=applicant.id,
                                title='New registration', url='/admin/registrations',
                                read=True))
    db.session.commit()
    applicant_id = applicant.id

    with app.test_request_context('/'):
        put_registration_approve(bearer(admin),
                                 {'user_id': applicant_id, 'approve': False})

    db.session.expire_all()
    assert Notification.query.filter_by(author_id=applicant_id).count() == 0
    assert db.session.get(User, admin.id).unread_notifications == 0


def test_an_application_that_is_not_pending_cannot_be_acted_on(app, db_session,
                                                               api_baseline):
    """`filter_by(status=0, ...)` -- the lookup only finds PENDING rows, so an
    already-approved account cannot be deleted by replaying the denial. The
    row asserts the account survives, because that is what the guard protects.
    """
    instance = api_baseline.instance_local
    admin = _permitted_approver(instance)
    applicant, registration = _applicant(instance)
    registration.status = 1
    db.session.commit()
    applicant_id = applicant.id

    with app.test_request_context('/'):
        with pytest.raises(Exception, match='Problem finding registration'):
            put_registration_approve(bearer(admin),
                                     {'user_id': applicant_id, 'approve': False})

    db.session.expire_all()
    assert db.session.get(User, applicant_id) is not None


def test_a_user_id_with_no_application_is_refused(app, db_session, api_baseline):
    """The same guard reached by a different route: a user id that never
    applied. This is what stops the endpoint being a general account-deletion
    tool for anyone holding the permission.
    """
    instance = api_baseline.instance_local
    admin = _permitted_approver(instance)
    bystander = make_user(instance, 'bystander', local=True)
    db.session.commit()
    bystander_id = bystander.id

    with app.test_request_context('/'):
        with pytest.raises(Exception, match='Problem finding registration'):
            put_registration_approve(bearer(admin),
                                     {'user_id': bystander_id, 'approve': False})

    db.session.expire_all()
    assert db.session.get(User, bystander_id) is not None


# --------------------------------------------------------------------------
# shared_error_handler -- what a failure tells the caller
# --------------------------------------------------------------------------


def _handle(app, exception):
    from app.api.alpha import shared_error_handler

    with app.test_request_context('/api/alpha/site'):
        response, status = shared_error_handler(exception)
        return response.get_json(), status


def test_a_rate_limited_caller_is_told_to_slow_down(app, db_session):
    """The only branch that does not answer 400: a 429 is what a client needs
    in order to back off.
    """
    body, status = _handle(app, RateLimitExceeded(type('Limit', (), {
        'limit': '1 per second', 'error_message': None, 'scope': None, 'key': 'k'})()))

    assert status == 429
    assert body['code'] == 429


def test_a_missing_row_is_reported_as_not_found(app, db_session):
    body, status = _handle(app, NoResultFound('no such row'))

    assert status == 400
    assert body['status'] == 'Not found'


def test_a_blocking_io_error_is_reported_as_bad_credentials(app, db_session):
    """BlockingIOError is what the LDAP path raises for a refused bind, which
    is why an IO error carries a credentials message here.
    """
    body, status = _handle(app, BlockingIOError('bind failed'))

    assert status == 400
    assert body['status'] == 'Bad credentials'


def test_a_validation_failure_reports_the_fields_that_failed(app, db_session):
    from werkzeug.exceptions import UnprocessableEntity

    error = UnprocessableEntity()
    error.data = {'messages': {'json': {'name': ['Missing data for required field.']}}}

    body, status = _handle(app, error)

    assert status == 400
    assert body['message'] == 'Validation failed'
    assert 'Missing data' in body['status']


def test_an_authorization_refusal_reaches_the_caller_as_its_message(app, db_session):
    """R2, pinned as the behaviour it is: `user_access` failing raises a plain
    Exception, so a refusal lands in the same `else` as a malformed request and
    answers **400**, not 403. A client cannot tell "you may not" from "you
    asked wrongly", and neither can monitoring.
    """
    body, status = _handle(app, Exception('Insufficient permissions to manage registrations'))

    assert status == 400
    assert body['status'] == 'Bad Request'
    assert body['message'] == 'Insufficient permissions to manage registrations'


def test_an_internal_failures_text_is_not_echoed_to_the_caller(app, db_session):
    """D895, fixed. The final `else` used to return `str(e)` whatever the
    exception was, so an internal failure's text -- which may name a table, a
    column, a constraint or a path -- reached an unauthenticated client. Only the
    application's own refusals keep their text now; anything else is a generic
    500, and the detail is logged.
    """
    body, status = _handle(app, RuntimeError('relation "user_role" does not exist'))

    assert status == 500
    assert body['message'] == 'internal error'


@pytest.mark.parametrize('message, logged', [
    ('incorrect_login', False),
    ('No object found.', False),
    ('something genuinely broke', True),
])
def test_only_unexpected_failures_are_logged(app, db_session, message, logged):
    """`if str(e) != 'incorrect_login' and str(e) != 'No object found.'` -- both
    conjuncts, plus the arm that logs. The two exclusions are the application's
    own routine refusals: logging them would bury a real fault in noise, and
    capturing them to Sentry would bill for it.
    """
    from app.api.alpha import shared_error_handler

    with app.test_request_context('/api/alpha/site'):
        with patch('app.api.alpha.current_app', new_callable=MagicMock) as current_app_mock:
            current_app_mock.config = {'SENTRY_DSN': None}
            shared_error_handler(Exception(message))

            assert bool(current_app_mock.logger.exception.call_args_list) is logged


@pytest.mark.parametrize('dsn, captured', [
    ('https://public@sentry.example/1', True),
    (None, False),
])
def test_sentry_is_told_only_when_it_is_configured(app, db_session, dsn, captured):
    from app.api.alpha import shared_error_handler

    with app.test_request_context('/api/alpha/site'):
        with patch('app.api.alpha.sentry_sdk') as sentry:
            with patch('app.api.alpha.current_app', new_callable=MagicMock) as current_app_mock:
                current_app_mock.config = {'SENTRY_DSN': dsn}
                shared_error_handler(Exception('something genuinely broke'))

            assert bool(sentry.capture_exception.call_args_list) is captured


def test_a_validation_failure_is_captured_when_sentry_is_configured(app, db_session):
    """The UnprocessableEntity branch has its own Sentry call, separate from
    the one in the final else -- so it needs its own row.
    """
    from werkzeug.exceptions import UnprocessableEntity

    from app.api.alpha import shared_error_handler

    error = UnprocessableEntity()
    error.data = {'messages': {'json': {'name': ['Missing data.']}}}

    with app.test_request_context('/api/alpha/site'):
        with patch('app.api.alpha.sentry_sdk') as sentry:
            with patch('app.api.alpha.current_app', new_callable=MagicMock) as current_app_mock:
                current_app_mock.config = {'SENTRY_DSN': 'https://public@sentry.example/1'}
                shared_error_handler(error)

            assert sentry.capture_exception.call_args.args[0] is error


def test_the_founding_account_passes_every_permission_check(app, db_session,
                                                            api_baseline):
    """The rule the refusal rows above had to work around, pinned explicitly so
    it is recorded rather than rediscovered.

    `user_access` returns True for `user_id == 1` before looking at any role
    (app/utils.py:1650-1653). It is the instance-owner bootstrap -- the first
    account created can always administer the site -- but it means the founding
    account holds every permission permanently, and that ANY authorization test
    using it as the unprivileged party proves nothing.
    """
    from app.utils import user_access

    founder = api_baseline.user1

    assert founder.id == 1
    # .count(), not == [] : User.roles is another lazy='dynamic' relationship,
    # so the attribute is an AppenderQuery and never equals a list. Fact 344,
    # met again one round after D884 -- this time in the test rather than in
    # the code under test.
    assert founder.roles.count() == 0
    assert user_access('approve registrations', founder.id) is True
    assert user_access('a permission that does not exist', founder.id) is True
    assert user_access('approve registrations', 0) is False


def test_the_newest_application_is_listed_first_by_default(app, db_session,
                                                           api_baseline):
    """`sort` defaults to "New". Every sorting row above passes `sort`
    explicitly, so the DEFAULT was never exercised and a mutant changing it to
    "Old" survived -- the queue would have silently reversed.
    """
    from datetime import timedelta
    from app.models import utcnow

    instance = api_baseline.instance_local
    admin = _permitted_approver(instance)
    first, first_registration = _applicant(instance, name='early')
    second, second_registration = _applicant(instance, name='late')
    first_registration.created_at = utcnow() - timedelta(days=2)
    second_registration.created_at = utcnow()
    db.session.commit()

    with app.test_request_context('/'):
        result = get_registration_list(bearer(admin), {})

    assert [r['user_id'] for r in result['registrations']] == [second.id, first.id]


def test_the_denial_path_deletes_the_registration_twice_over(app, db_session,
                                                             api_baseline):
    """EQUIVALENCE PROOF for two mutants on the denial path.

    `db.session.delete(registration)` is REDUNDANT: `User.delete_dependencies()`
    (app/models.py:1501) already runs
    `db.session.query(UserRegistration).filter(UserRegistration.user_id ==
    self.id).delete()`, so removing the explicit delete leaves the row removed
    anyway and nothing can observe the difference.

    `new_user.deleted = True` is likewise unobservable: the flag is written to
    a row that `db.session.delete(new_user)` removes in the same transaction,
    and `delete_dependencies()` never reads it.

    Both are registered as equivalent rather than contorted into a kill. This
    row states the mechanism and pins the outcome they share.
    """
    import inspect

    from app.models import User as UserModel

    source = inspect.getsource(UserModel.delete_dependencies)
    assert 'UserRegistration' in source
    assert 'self.deleted' not in source

    instance = api_baseline.instance_local
    admin = _permitted_approver(instance)
    applicant, registration = _applicant(instance)
    applicant_id, registration_id = applicant.id, registration.id

    with app.test_request_context('/'):
        put_registration_approve(bearer(admin),
                                 {'user_id': applicant_id, 'approve': False})

    db.session.expire_all()
    assert db.session.get(UserRegistration, registration_id) is None
    assert db.session.get(User, applicant_id) is None

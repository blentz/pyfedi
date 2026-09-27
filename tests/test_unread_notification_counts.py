"""`User.unread_notifications` when a registration is approved or denied.

`decrement_unread_counts` in `app/utils.py` and its three callers:
`finalize_user_setup` (approve, web and API), `admin_approve_registrations_denied`
(deny, web) and `approve_registration_application`'s denial arm (deny, API).

D1360. All three did it as

    update(User).where(User.id.in_(user_ids))
                .values({User.unread_notifications: User.unread_notifications - 1})

and an `IN` list is a SET: the arithmetic runs once per matching USER, however many
times their id appears. So an admin holding two notifications from one registration
lost one of them and kept a badge for a notification that had been marked read.
There was no floor either. Measured through `finalize_user_setup`, with one admin:

    2 unread  ->  1   (expected 0)
    1 unread  ->  0   (expected 0)
    0 unread  -> -1   (expected 0)

The last one is the visible failure: approving a registration whose notification an
admin had already read left that admin's count NEGATIVE.
"""
import pytest
from flask import g

from app import db
from app.constants import NOTIF_REGISTRATION, NOTIF_REPORT
from app.models import Notification, Site, User, UserRegistration
from app.utils import decrement_unread_counts, finalize_user_setup


@pytest.fixture
def env(app, api_baseline):
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    db.session.commit()
    return SimpleNamespace(baseline=api_baseline, client=app.test_client(),
                           app=app)


def csrf(app, client):
    """A real CSRF token in the session and in the form.

    Without it these POSTs are a 400, and the tests that assert "nothing changed"
    PASS on that -- which is a false pass, because a request the route never saw
    changes nothing either. Two of the tests below were written that way first.
    """
    from flask import session as flask_session
    from flask_wtf.csrf import generate_csrf

    with app.test_request_context():
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with client.session_transaction() as session:
        session['csrf_token'] = raw
    return token


def counts(*users):
    for user in users:
        db.session.refresh(user)
    return [user.unread_notifications for user in users]


class TestTheHelperItself:
    def test_one_id_takes_one_off(self, env):
        user = env.baseline.user1
        user.unread_notifications = 3
        db.session.commit()

        decrement_unread_counts([user.id])

        assert counts(user) == [2]

    def test_an_id_twice_takes_two_off(self, env):
        """The whole defect: `User.id.in_([7, 7])` matched user 7 once."""
        user = env.baseline.user1
        user.unread_notifications = 3
        db.session.commit()

        decrement_unread_counts([user.id, user.id])

        assert counts(user) == [1]

    def test_several_users_each_get_their_own_total(self, env):
        one, two = env.baseline.user1, env.baseline.user2
        one.unread_notifications = 5
        two.unread_notifications = 5
        db.session.commit()

        decrement_unread_counts([one.id, two.id, one.id, one.id])

        assert counts(one, two) == [2, 4]

    def test_it_never_goes_below_zero(self, env):
        user = env.baseline.user1
        user.unread_notifications = 1
        db.session.commit()

        decrement_unread_counts([user.id, user.id, user.id])

        assert counts(user) == [0]

    def test_a_user_with_nothing_unread_stays_at_zero(self, env):
        user = env.baseline.user1
        user.unread_notifications = 0
        db.session.commit()

        decrement_unread_counts([user.id])

        assert counts(user) == [0]

    def test_an_empty_list_touches_nothing(self, env):
        user = env.baseline.user1
        user.unread_notifications = 4
        db.session.commit()

        decrement_unread_counts([])

        assert counts(user) == [4]

    def test_an_id_that_names_no_user(self, env):
        decrement_unread_counts([999999])  # no row, no error


def a_registration(env, unread=1, already_read=0):
    """An applicant awaiting approval, and one admin holding notifications about
    them: `unread` of them unread and `already_read` of them already read."""
    applicant = env.baseline.user3
    applicant.verified = True
    db.session.add(UserRegistration(user_id=applicant.id, status=0))
    admin = env.baseline.user1
    admin.unread_notifications = unread
    for index in range(unread + already_read):
        db.session.add(Notification(title=f'reg {index}', url='/admin',
                                    user_id=admin.id, author_id=applicant.id,
                                    notif_type=NOTIF_REGISTRATION,
                                    read=index >= unread))
    db.session.commit()
    return applicant, admin


class TestApprovingARegistration:
    def test_two_unread_notifications_both_come_off(self, env):
        applicant, admin = a_registration(env, unread=2)

        finalize_user_setup(applicant)

        assert counts(admin) == [0]

    def test_one_unread_notification_comes_off(self, env):
        applicant, admin = a_registration(env, unread=1)

        finalize_user_setup(applicant)

        assert counts(admin) == [0]

    def test_an_already_read_notification_takes_nothing_off(self, env):
        """The negative count. The update used to match read notifications too,
        and each of those took another one off."""
        applicant, admin = a_registration(env, unread=0, already_read=1)

        finalize_user_setup(applicant)

        assert counts(admin) == [0]

    def test_a_mixture_only_counts_the_unread_ones(self, env):
        applicant, admin = a_registration(env, unread=1, already_read=2)

        finalize_user_setup(applicant)

        assert counts(admin) == [0]

    def test_the_notifications_are_marked_read(self, env):
        applicant, admin = a_registration(env, unread=2)

        finalize_user_setup(applicant)

        unread = Notification.query.filter_by(author_id=applicant.id,
                                              read=False).count()
        assert unread == 0

    def test_a_notification_from_somebody_else_is_left_alone(self, env):
        """The WHERE names this author and this type, so an admin's other unread
        notifications must survive -- otherwise approving one registration would
        clear the whole queue."""
        applicant, admin = a_registration(env, unread=1)
        other = Notification(title='a report', url='/admin/reports',
                             user_id=admin.id, author_id=env.baseline.user2.id,
                             notif_type=NOTIF_REPORT, read=False)
        db.session.add(other)
        admin.unread_notifications = 2
        db.session.commit()

        finalize_user_setup(applicant)

        assert counts(admin) == [1]
        db.session.refresh(other)
        assert other.read is False

    def test_two_admins_each_lose_their_own(self, env):
        applicant, admin = a_registration(env, unread=2)
        second = env.baseline.user2
        second.unread_notifications = 1
        db.session.add(Notification(title='reg', url='/admin', user_id=second.id,
                                    author_id=applicant.id,
                                    notif_type=NOTIF_REGISTRATION, read=False))
        db.session.commit()

        finalize_user_setup(applicant)

        assert counts(admin, second) == [0, 0]


class TestDenyingARegistration:
    """The web route. It deletes the notifications rather than marking them read,
    and filters the unread ones in Python, so the list it builds is per-ROW -- which
    is exactly the list `User.id.in_(...)` collapsed."""

    def login(self, env, user):
        with env.client.session_transaction() as session:
            session['_user_id'] = str(user.id)
            session['_fresh'] = True

    def post(self, env, path):
        token = csrf(env.app, env.client)
        return env.client.post(path, data={'csrf_token': token})

    def test_two_unread_notifications_both_come_off(self, env):
        applicant, admin = a_registration(env, unread=2)
        self.login(env, admin)

        response = self.post(env, f'/admin/approve_registrations/{applicant.id}/deny')

        assert response.status_code == 302
        assert counts(admin) == [0]

    def test_an_already_read_notification_takes_nothing_off(self, env):
        applicant, admin = a_registration(env, unread=0, already_read=2)
        self.login(env, admin)

        self.post(env, f'/admin/approve_registrations/{applicant.id}/deny')

        assert counts(admin) == [0]

    def test_the_applicant_is_gone(self, env):
        applicant, admin = a_registration(env, unread=1)
        applicant_id = applicant.id
        self.login(env, admin)

        self.post(env, f'/admin/approve_registrations/{applicant_id}/deny')

        assert db.session.get(User, applicant_id) is None

    def test_the_registration_row_is_gone(self, env):
        applicant, admin = a_registration(env, unread=1)
        applicant_id = applicant.id
        self.login(env, admin)

        self.post(env, f'/admin/approve_registrations/{applicant_id}/deny')

        assert UserRegistration.query.filter_by(user_id=applicant_id).count() == 0

    def test_a_user_id_that_does_not_exist(self, env):
        applicant, admin = a_registration(env, unread=1)
        self.login(env, admin)

        response = self.post(env, '/admin/approve_registrations/999999/deny')

        assert response.status_code == 404

    def test_a_user_with_no_pending_registration_changes_nothing(self, env):
        applicant, admin = a_registration(env, unread=1)
        UserRegistration.query.filter_by(user_id=applicant.id).delete()
        db.session.commit()
        self.login(env, admin)

        self.post(env, f'/admin/approve_registrations/{applicant.id}/deny')

        assert db.session.get(User, applicant.id) is not None
        assert counts(admin) == [1]


class TestApprovingThroughTheRoute:
    def login(self, env, user):
        with env.client.session_transaction() as session:
            session['_user_id'] = str(user.id)
            session['_fresh'] = True

    def post(self, env, path):
        token = csrf(env.app, env.client)
        return env.client.post(path, data={'csrf_token': token})

    def test_the_registration_is_marked_approved(self, env):
        applicant, admin = a_registration(env, unread=1)
        self.login(env, admin)

        response = self.post(env, f'/admin/approve_registrations/{applicant.id}/approve')

        assert response.status_code == 302
        registration = UserRegistration.query.filter_by(user_id=applicant.id).one()
        assert registration.status == 1
        assert registration.approved_by == admin.id

    def test_the_count_comes_off(self, env):
        applicant, admin = a_registration(env, unread=2)
        self.login(env, admin)

        self.post(env, f'/admin/approve_registrations/{applicant.id}/approve')

        assert counts(admin) == [0]

    def test_an_unverified_applicant_is_approved_but_not_set_up(self, env):
        """`if user.verified:` -- the setup waits for the email. The registration
        row still moves to approved, so the admin does not see it again."""
        applicant, admin = a_registration(env, unread=1)
        applicant.verified = False
        db.session.commit()
        self.login(env, admin)

        self.post(env, f'/admin/approve_registrations/{applicant.id}/approve')

        assert UserRegistration.query.filter_by(user_id=applicant.id).one().status == 1
        assert counts(admin) == [1]

    def test_a_user_id_that_does_not_exist(self, env):
        applicant, admin = a_registration(env, unread=1)
        self.login(env, admin)

        assert self.post(
            env, '/admin/approve_registrations/999999/approve').status_code == 404


class TestTheCountsThatAreNotMaskedByTheFloor:
    """The tests above could not see a decrement that was too LARGE.

    `GREATEST(..., 0)` floors the result, so an admin whose only unread
    notifications are the ones being cleared reaches 0 whether the arithmetic
    counted one notification or five. Every test here leaves the admin holding an
    unrelated unread notification, so the remainder is a number rather than a
    floor, and an over-count shows up as 0 where 1 is expected.
    """

    def an_admin_with_an_unrelated_notification(self, env, applicant,
                                                registration_unread,
                                                already_read=0):
        admin = env.baseline.user1
        for index in range(registration_unread + already_read):
            db.session.add(Notification(
                title=f'reg {index}', url='/admin', user_id=admin.id,
                author_id=applicant.id, notif_type=NOTIF_REGISTRATION,
                read=index >= registration_unread))
        db.session.add(Notification(title='a report', url='/admin/reports',
                                    user_id=admin.id,
                                    author_id=env.baseline.user2.id,
                                    notif_type=NOTIF_REPORT, read=False))
        admin.unread_notifications = registration_unread + 1
        db.session.commit()
        return admin

    def an_applicant(self, env):
        applicant = env.baseline.user3
        applicant.verified = True
        db.session.add(UserRegistration(user_id=applicant.id, status=0))
        db.session.commit()
        return applicant

    def test_approving_leaves_the_unrelated_one(self, env):
        applicant = self.an_applicant(env)
        admin = self.an_admin_with_an_unrelated_notification(env, applicant, 2)

        finalize_user_setup(applicant)

        assert counts(admin) == [1]

    def test_approving_does_not_count_the_already_read_ones(self, env):
        """With three read notifications beside one unread, an update that matched
        them all would take four off a count of two and floor at 0."""
        applicant = self.an_applicant(env)
        admin = self.an_admin_with_an_unrelated_notification(env, applicant, 1,
                                                            already_read=3)

        finalize_user_setup(applicant)

        assert counts(admin) == [1]

    def test_approving_ignores_a_registration_notification_from_someone_else(self, env):
        """`Notification.author_id == user.id`. Another applicant's notification is
        another admin task, and clearing it here would hide it for ever."""
        applicant = self.an_applicant(env)
        admin = self.an_admin_with_an_unrelated_notification(env, applicant, 1)
        other_applicant = env.baseline.user4
        other = Notification(title='another reg', url='/admin',
                             user_id=admin.id, author_id=other_applicant.id,
                             notif_type=NOTIF_REGISTRATION, read=False)
        db.session.add(other)
        admin.unread_notifications = 3  # this reg, the report, the other reg
        db.session.commit()

        finalize_user_setup(applicant)

        assert counts(admin) == [2]
        db.session.refresh(other)
        assert other.read is False

    def test_approving_ignores_another_notification_type_from_the_same_author(self, env):
        """`Notification.notif_type == NOTIF_REGISTRATION`. The applicant may also
        have been reported; that is a separate item in the queue."""
        applicant = self.an_applicant(env)
        admin = self.an_admin_with_an_unrelated_notification(env, applicant, 1)
        other = Notification(title='reported', url='/admin/reports',
                             user_id=admin.id, author_id=applicant.id,
                             notif_type=NOTIF_REPORT, read=False)
        db.session.add(other)
        admin.unread_notifications = 3
        db.session.commit()

        finalize_user_setup(applicant)

        assert counts(admin) == [2]
        db.session.refresh(other)
        assert other.read is False

    def test_denying_does_not_count_the_already_read_ones(self, env):
        applicant = self.an_applicant(env)
        admin = self.an_admin_with_an_unrelated_notification(env, applicant, 1,
                                                             already_read=3)
        with env.client.session_transaction() as session:
            session['_user_id'] = str(admin.id)
            session['_fresh'] = True
        token = csrf(env.app, env.client)

        env.client.post(f'/admin/approve_registrations/{applicant.id}/deny',
                        data={'csrf_token': token})

        assert counts(admin) == [1]

    def test_the_api_denial_does_not_count_the_already_read_ones(self, env,
                                                                monkeypatch):
        """The API's own copy of the denial, called directly: `authorise_api_user`
        wants a JWT, and who the admin is is not what this test is about."""
        from app.api.alpha.utils import admin as admin_api

        applicant = self.an_applicant(env)
        admin = self.an_admin_with_an_unrelated_notification(env, applicant, 1,
                                                             already_read=3)
        monkeypatch.setattr(admin_api, 'authorise_api_user',
                            lambda auth, return_type=None: admin)

        admin_api.put_registration_approve(
            'Bearer x', {'user_id': applicant.id, 'approve': False})

        assert counts(admin) == [1]

    def test_the_api_approval_leaves_the_unrelated_one(self, env, monkeypatch):
        from app.api.alpha.utils import admin as admin_api

        applicant = self.an_applicant(env)
        admin = self.an_admin_with_an_unrelated_notification(env, applicant, 2)
        monkeypatch.setattr(admin_api, 'authorise_api_user',
                            lambda auth, return_type=None: admin)

        admin_api.put_registration_approve(
            'Bearer x', {'user_id': applicant.id, 'approve': True})

        assert counts(admin) == [1]

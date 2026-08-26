"""Covers app/utils.py's can_upload_video: which of four admin-configured
policies (the `allow_video_file_uploads` Setting) governs who may upload a
video file.

Read directly from app/utils.py:

    upload_access = get_setting('allow_video_file_uploads', 'no')
    upload_user = user or current_user
    if upload_access == 'no':
        return False
    elif upload_access == 'user 1' and upload_user.get_id() != 1:
        return False
    elif upload_access == 'admins' and not upload_user.is_admin_or_staff():
        return False
    elif upload_access == 'users' and not current_user.is_authenticated and user is None:
        return False
    return True

Four policies, each an elif arm. `no` is a single condition (no user check at
all); the other three are compounds with two independently-falsifiable
halves, per this project's condition-coverage convention (branch coverage
alone would pass with a sub-condition of a compound guard deleted).

`upload_user.get_id()` matters here in a way it does not for can_upvote /
can_downvote / can_create_post elsewhere in this sub-project: called directly
on a real User instance (not through the current_user proxy),
User.get_id() (app/models.py) returns self.id whenever self.is_authenticated
-- and UserMixin.is_authenticated is True unconditionally for any User
object, logged in or not. So `upload_user.get_id() != 1` really is testing
"is this the user with database id 1", and factories hand out id 1 first --
every test below is deliberate about which user lands on id 1.

--- THE BUG (suspected-defects list; documented here, NOT fixed) ---

`upload_user = user or current_user` is computed once at the top and used by
the `admins` branch, but the `users` branch ignores it entirely:

    elif upload_access == 'users' and not current_user.is_authenticated and user is None:
        return False

Read that compound with `user` (the injected parameter) held not-None: `user
is None` is False, so the whole `and` chain is False regardless of
`current_user`, so the `elif` doesn't match, so execution falls through to
`return True`. Concretely: under the 'users' policy,
`can_upload_video(some_user)` returns True for ANY user object passed in --
banned, anonymous-in-the-request-sense, doesn't matter -- because the
function never actually inspects `some_user`. It only ever inspects
`current_user` (the ambient Flask-Login proxy) and whether `user` was passed
at all. TestCanUploadVideoUsersPolicy.test_users_policy_grants_any_injected_user_under_the_bug
below demonstrates this directly and is deliberately named to say what it
is: a pinned description of CURRENT behaviour that is under review, not an
endorsement of it as correct. If this is ever fixed to consult `upload_user`
(as the `admins` branch does), that test will start failing and should be
updated as part of the fix, not treated as a regression to chase.
"""

from flask_login import current_user, login_user

from app.models import Role, user_role
from app import db
from app.utils import can_upload_video, get_setting, set_setting
from tests.factories import make_instance, make_user


def _make_admin(user):
    """Assign a real Role named 'Admin' to `user`. User.is_admin()
    (app/models.py) special-cases id==1 -- every test that uses this gives
    the admin user a filler id-1 user first, so a pass can only be explained
    by the role, not the id shortcut."""
    role = Role(name='Admin', weight=0)
    db.session.add(role)
    db.session.commit()
    db.session.execute(user_role.insert().values(user_id=user.id, role_id=role.id))
    db.session.commit()
    return role


class TestCanUploadVideoNoPolicy:
    """`if upload_access == 'no': return False` -- a single condition, not a
    compound: there is no user-identity half to independently falsify. The
    'no' policy refuses unconditionally, so there is no "permitted case" for
    it as such; instead, direction coverage here means (a) 'no' really does
    refuse, even a user who would be granted access under every other
    policy, and (b) changing away from 'no' for that exact same user changes
    the outcome -- proving the branch's condition, not something else about
    the user, is what causes the refusal.
    """

    def test_no_policy_refuses_unconditionally_even_for_an_id_1_admin(self, app, db_session):
        """id 1 would satisfy 'user 1'; an Admin role would satisfy 'admins'.
        Neither matters under 'no' -- it returns False before either is
        ever consulted."""
        make_instance('test.piefed.local', software='piefed')
        user = make_user(None, 'noadmin', local=True)  # occupies id 1
        _make_admin(user)
        original = get_setting('allow_video_file_uploads')
        try:
            set_setting('allow_video_file_uploads', 'no')
            assert can_upload_video(user) is False
        finally:
            set_setting('allow_video_file_uploads', original)

    def test_a_different_policy_does_not_hit_the_no_branch(self, app, db_session):
        """Same id-1 user as above, only the setting changes. Proves the
        refusal above is really caused by upload_access == 'no', not by
        something inherent to this user -- switching to 'user 1' (which this
        id-1 user satisfies) flips the outcome."""
        make_instance('test.piefed.local', software='piefed')
        user = make_user(None, 'noswitch', local=True)  # occupies id 1
        original = get_setting('allow_video_file_uploads')
        try:
            set_setting('allow_video_file_uploads', 'user 1')
            assert can_upload_video(user) is True
        finally:
            set_setting('allow_video_file_uploads', original)


class TestCanUploadVideoUser1Policy:
    """`elif upload_access == 'user 1' and upload_user.get_id() != 1: return
    False` -- two sub-conditions: the policy string, and whether the acting
    user's id is 1. Each direction gets its own case, plus a cross-policy
    control showing a non-id-1 user is NOT refused by 'user 1' logic under a
    different policy."""

    def test_user_1_policy_permits_the_user_with_id_1(self, app, db_session):
        make_instance('test.piefed.local', software='piefed')
        user = make_user(None, 'firstuser', local=True)  # occupies id 1
        original = get_setting('allow_video_file_uploads')
        try:
            set_setting('allow_video_file_uploads', 'user 1')
            assert user.get_id() == 1
            assert can_upload_video(user) is True
        finally:
            set_setting('allow_video_file_uploads', original)

    def test_user_1_policy_refuses_a_user_who_is_not_id_1(self, app, db_session):
        """A filler user occupies id 1 first, so the subject here is
        deliberately id 2 or later."""
        make_instance('test.piefed.local', software='piefed')
        make_user(None, 'filler', local=True)  # occupies id 1
        user = make_user(None, 'seconduser', local=True)
        original = get_setting('allow_video_file_uploads')
        try:
            set_setting('allow_video_file_uploads', 'user 1')
            assert user.get_id() != 1
            assert can_upload_video(user) is False
        finally:
            set_setting('allow_video_file_uploads', original)

    def test_a_different_policy_does_not_gate_by_id_1(self, app, db_session):
        """Same non-id-1 user as the refused case above, but under 'admins'
        with an Admin role granted -- permitted despite failing 'user 1's
        own id check, proving 'user 1' logic is not what is consulted here."""
        make_instance('test.piefed.local', software='piefed')
        make_user(None, 'filler2', local=True)  # occupies id 1
        user = make_user(None, 'seconduseradmin', local=True)
        _make_admin(user)
        original = get_setting('allow_video_file_uploads')
        try:
            set_setting('allow_video_file_uploads', 'admins')
            assert user.get_id() != 1
            assert can_upload_video(user) is True
        finally:
            set_setting('allow_video_file_uploads', original)


class TestCanUploadVideoAdminsPolicy:
    """`elif upload_access == 'admins' and not upload_user.is_admin_or_staff():
    return False` -- two sub-conditions: the policy string, and admin/staff
    status. Both users here are deliberately off id 1 (a filler user takes
    it) so a pass cannot be explained by User.is_admin()'s id==1 special
    case -- only the granted Admin role can explain it."""

    def test_admins_policy_permits_an_admin_user(self, app, db_session):
        make_instance('test.piefed.local', software='piefed')
        make_user(None, 'filler', local=True)  # occupies id 1
        admin = make_user(None, 'realadmin', local=True)
        _make_admin(admin)
        original = get_setting('allow_video_file_uploads')
        try:
            set_setting('allow_video_file_uploads', 'admins')
            assert admin.get_id() != 1
            assert can_upload_video(admin) is True
        finally:
            set_setting('allow_video_file_uploads', original)

    def test_admins_policy_refuses_an_ordinary_user(self, app, db_session):
        make_instance('test.piefed.local', software='piefed')
        make_user(None, 'filler2', local=True)  # occupies id 1
        user = make_user(None, 'ordinaryuploader', local=True)
        original = get_setting('allow_video_file_uploads')
        try:
            set_setting('allow_video_file_uploads', 'admins')
            assert can_upload_video(user) is False
        finally:
            set_setting('allow_video_file_uploads', original)


class TestCanUploadVideoUsersPolicy:
    """`elif upload_access == 'users' and not current_user.is_authenticated
    and user is None: return False` -- the branch documented as buggy at
    the top of this file. Unlike the other three branches, this one reads
    `current_user` (Flask-Login's ambient proxy) rather than `upload_user`,
    so establishing a real logged-in current_user needs a request context
    and login_user, not just constructing a User row.

    The two tests below cover the branch as its authors evidently intended
    it to work: called with no injected user, so `user is None` is True and
    `current_user`'s authentication state is what decides. The third test
    holds `current_user` fixed at anonymous and flips only whether `user` is
    injected -- which is the direct demonstration of the bug described at
    the top of this file: injecting ANY user, authenticated or not, bypasses
    this branch's refusal entirely.
    """

    def test_users_policy_permits_an_authenticated_current_user(self, app, db_session):
        make_instance('test.piefed.local', software='piefed')
        user = make_user(None, 'loggedinuploader', local=True)
        original = get_setting('allow_video_file_uploads')
        try:
            set_setting('allow_video_file_uploads', 'users')
            with app.test_request_context('/'):
                login_user(user)
                assert current_user_is_authenticated_via_login(user)
                assert can_upload_video() is True
        finally:
            set_setting('allow_video_file_uploads', original)

    def test_users_policy_refuses_an_anonymous_current_user(self, app, db_session):
        """No login_user call: current_user is Flask-Login's
        AnonymousUserMixin, is_authenticated False, get_id() None. Called
        with no injected user, so `user is None` is True -- both halves of
        the compound are satisfied and the refusal fires."""
        make_instance('test.piefed.local', software='piefed')
        original = get_setting('allow_video_file_uploads')
        try:
            set_setting('allow_video_file_uploads', 'users')
            with app.test_request_context('/'):
                assert can_upload_video() is False
        finally:
            set_setting('allow_video_file_uploads', original)

    def test_users_policy_grants_any_injected_user_under_the_bug(self, app, db_session):
        """THE BUG, demonstrated directly. Same anonymous current_user as
        the refused case immediately above -- the ONLY difference is that a
        user object is now injected. `upload_user = user or current_user`
        is computed but never consulted by this branch: `user is None` is
        False, so `not current_user.is_authenticated and user is None`
        short-circuits to False regardless of current_user, the elif does
        not match, and execution falls through to `return True`.

        This is CURRENT, ACTUAL behaviour, pinned so it is visible and under
        review -- not a statement that granting upload access to an
        unauthenticated caller's arbitrarily-injected user is correct
        policy. Compare directly against
        test_users_policy_refuses_an_anonymous_current_user above: identical
        setup, identical current_user state, and the outcome flips from
        False to True purely because a user was injected.
        """
        make_instance('test.piefed.local', software='piefed')
        injected = make_user(None, 'injecteduploader', local=True)
        original = get_setting('allow_video_file_uploads')
        try:
            set_setting('allow_video_file_uploads', 'users')
            with app.test_request_context('/'):
                # current_user is anonymous here -- no login_user call.
                assert can_upload_video(injected) is True
        finally:
            set_setting('allow_video_file_uploads', original)

    def test_a_different_policy_does_not_hit_the_users_branch(self, app, db_session):
        """An authenticated current_user, but under 'admins' with no admin
        role granted -- refused, proving 'users' logic (which would permit
        any authenticated caller) is not what is consulted under a
        different policy. A filler user takes id 1 first so User.is_admin()'s
        id==1 special case cannot explain a pass here."""
        make_instance('test.piefed.local', software='piefed')
        make_user(None, 'filler3', local=True)  # occupies id 1
        user = make_user(None, 'notadminuploader', local=True)
        original = get_setting('allow_video_file_uploads')
        try:
            set_setting('allow_video_file_uploads', 'admins')
            with app.test_request_context('/'):
                login_user(user)
                assert can_upload_video() is False
        finally:
            set_setting('allow_video_file_uploads', original)


def current_user_is_authenticated_via_login(user):
    """Small readability helper: confirms login_user actually put `user`
    behind Flask-Login's current_user proxy, rather than trusting login_user
    silently."""
    return current_user.is_authenticated and current_user.get_id() == user.get_id()

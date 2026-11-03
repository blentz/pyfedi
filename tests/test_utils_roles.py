from app import db
from app.constants import ROLE_STAFF_NAME
from app.models import Role, Site, role_grant
from app.utils import role_access, user_access
from tests.factories import grant_permission, make_instance, make_user


class TestUserAccess:
    def test_user_zero_is_always_denied(self, app, db_session):
        """The `user_id == 0` guard. Fails if it is removed, since there is no
        user 0 and the query would simply return no rows -- so this test only
        discriminates together with the query-path tests below."""
        assert user_access('change instance settings', 0) is False

    def test_user_one_is_always_permitted(self, app, db_session):
        """A hardcoded superuser bypass: user 1 is permitted WITHOUT any role.

        Fails if the bypass is removed. Documented rather than endorsed --
        see the spec's suspected-defects list.

        Deleting the `if user_id == 1: return True` guard fails this test
        plus one other, suite-wide: `tests/test_redirect_policy.py::
        TestAdminFormRoundTrip::test_saving_the_policy_persists_it_and_it_loads_back`,
        which drives an admin form that relies on user 1 being an
        unconditional superadmin. (An earlier measurement against
        `tests/test_utils_roles.py` alone reported "exactly one" -- that was
        the module-scoped count, not the suite-wide one; verified here with
        `./run_tests.sh tests/ -q --ignore=tests/test_activitypub_util.py`
        against the mutated guard: `2 failed, 1656 passed`. The command and
        the count are quoted as the run that was actually made; that run
        predates `tests/test_activitypub_util.py` being repaired, and the
        standard command today is plain `./run_tests.sh tests/ -q`.)
        """
        assert user_access('change instance settings', 1) is True

    def test_a_granted_permission_is_permitted(self, app, db_session):
        make_instance('test.piefed.local', software='piefed')  # user.instance_id FK target
        # user_access() special-cases user_id == 1 as the instance superadmin
        # (app/utils.py:1513), which would make this assertion pass even if
        # grant_permission/the role_permission join were broken. A throwaway
        # user consumes id 1 so 'granted' below lands on an ordinary id and
        # the True actually comes from the granted role.
        make_user(None, 'filler', local=True)
        user = make_user(None, 'granted', local=True)
        grant_permission(user, 'change instance settings')
        assert user_access('change instance settings', user.id) is True

    def test_a_different_permission_is_denied(self, app, db_session):
        """Fails if the query stops filtering on rp.permission."""
        make_instance('test.piefed.local', software='piefed')  # user.instance_id FK target
        # user_access() special-cases user_id == 1 as the instance superadmin
        # and returns True unconditionally -- if 'granted2' below landed on
        # id 1 this assertion would fail outright regardless of the query.
        # A throwaway user consumes id 1 so the test exercises the real path.
        make_user(None, 'filler', local=True)
        user = make_user(None, 'granted2', local=True)
        grant_permission(user, 'change instance settings')
        assert user_access('ban users', user.id) is False

    def test_a_user_with_no_role_is_denied(self, app, db_session):
        make_instance('test.piefed.local', software='piefed')  # user.instance_id FK target
        # user_access() special-cases user_id == 1 as the instance superadmin
        # (app/utils.py:1513) -- a throwaway user consumes id 1 so 'norole'
        # below lands on an ordinary id and the negative assertion is real.
        make_user(None, 'filler', local=True)
        user = make_user(None, 'norole', local=True)
        assert user_access('change instance settings', user.id) is False

    def test_another_users_permission_does_not_leak(self, app, db_session):
        """Fails if the query stops filtering on ur.user_id."""
        make_instance('test.piefed.local', software='piefed')  # user.instance_id FK target
        # user_access() special-cases user_id == 1 as the instance superadmin
        # (app/utils.py:1513) -- a throwaway user consumes id 1 so 'other'
        # below lands on an ordinary id and the negative assertion is real.
        make_user(None, 'filler', local=True)
        granted = make_user(None, 'has', local=True)
        other = make_user(None, 'hasnot', local=True)
        grant_permission(granted, 'ban users')
        assert user_access('ban users', other.id) is False


class TestRoleAccess:
    def test_a_role_with_the_permission(self, app, db_session):
        make_instance('test.piefed.local', software='piefed')  # user.instance_id FK target
        user = make_user(None, 'roleholder', local=True)
        role = grant_permission(user, 'ban users')
        assert role_access('ban users', role.id) is True

    def test_a_role_without_the_permission(self, app, db_session):
        make_instance('test.piefed.local', software='piefed')  # user.instance_id FK target
        user = make_user(None, 'roleholder2', local=True)
        role = grant_permission(user, 'ban users')
        assert role_access('change instance settings', role.id) is False

    def test_an_unknown_role(self, app, db_session):
        assert role_access('ban users', 999999) is False


class TestSiteAdmins:
    def test_user_one_with_no_role_row_is_an_admin(self, app, db_session):
        """D442, fixed: `Site.admins()` inner-joined `user_role` before its
        `User.id == 1` disjunct, so the founding account with no role row --
        an admin by `is_admin()` and by `g.admin_ids` -- was dropped. It is now
        listed, and a role-less ordinary user still is not."""
        make_instance('test.piefed.local', software='piefed')  # user.instance_id FK target
        founder = make_user(None, 'founder', local=True)
        make_user(None, 'ordinary', local=True)
        assert founder.id == 1 and founder.is_admin()

        assert [u.id for u in Site.admins()] == [founder.id]

    def test_admins_are_matched_by_the_built_in_role_and_staff_by_name(self, app, db_session):
        """D481, then RB16 (spec 2026-11-03-rbac-design.md). `Site.admins()` and `is_admin()` once matched the
        role NAME 'Admin'. Admin is now the one built-in role, found by `Role.built_in`: renamed, it still makes an
        admin, and an ordinary role that takes the name 'Admin' makes none. Staff is still matched by name until
        `is_staff()` goes (RBAC step 6)."""
        make_instance('test.piefed.local', software='piefed')  # user.instance_id FK target
        make_user(None, 'founder', local=True)                 # user 1, listed regardless
        built_in_admin = make_user(None, 'built-in-admin', local=True)
        named_staff = make_user(None, 'named-staff', local=True)
        name_only = make_user(None, 'name-only', local=True)
        admin_role = Role(name='Superuser', built_in=True)
        staff_role = Role(name=ROLE_STAFF_NAME)
        impostor = Role(name='Admin')
        db.session.add_all([admin_role, staff_role, impostor])
        db.session.commit()
        for user, role in ((built_in_admin, admin_role), (named_staff, staff_role), (name_only, impostor)):
            db.session.execute(role_grant.insert().values(user_id=user.id, role_id=role.id))
        db.session.commit()

        assert (built_in_admin.is_admin(), name_only.is_admin()) == (True, False)
        assert [u.id for u in Site.admins()] == [1, built_in_admin.id]
        assert [u.id for u in Site.staff()] == [named_staff.id]

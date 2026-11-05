"""RBAC build step 2: role grants, and Admin as the one built-in role.

Spec docs/superpowers/specs/2026-11-03-rbac-design.md (RB1, RB2, RB11, RB16). The migration
(b4e8d2a6f1c9_role_grants.py) turns `user_role` into `role_grant` and makes the role named Admin the built-in role;
the model reads instance-wide, unexpired grants; user_access, is_admin and Site.admins find Admin by `built_in`.
"""
from datetime import timedelta
from unittest.mock import patch

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text

from app import db
from app.admin.forms import AddUserForm, GrantForm
from app.community.util import notify_admins_of_post_needing_approval
from app.models import Notification, Role, RolePermission, Site, SiteDomain, role_grant, utcnow
from app.roles import STAFF_PERMISSIONS, seed_default_roles
from app.utils import roles_with, user_access
from tests.factories import (admin_role, grant_permission, grant_role, make_community, make_instance, make_post,
                             make_user)
from tests.test_branding import load_migration
from tests.test_role_permissions import csrf

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture
def people(db_session):
    instance = make_instance('test.piefed.local', software='piefed')
    founder = make_user(instance, 'founder', local=True)
    assert founder.id == 1
    return [make_user(instance, f'person{n}', local=True) for n in range(4)]


# --------------------------------------------------------------------------
# The migration
# --------------------------------------------------------------------------

def _rows(sql, **params):
    return [tuple(row) for row in db.session.execute(text(sql), params)]


def _columns(table):
    return {column['name'] for column in inspect(db.session.connection()).get_columns(table)}


def _old_roles(*rows):
    """Roles as the schema before this revision held them: (id, name, weight)."""
    for role_id, name in rows:
        db.session.execute(text('INSERT INTO role (id, name, weight) VALUES (:id, :name, 0)'),
                           {'id': role_id, 'name': name})


def _migrate(old_shape):
    """Downgrade to the old schema, let `old_shape()` fill it, and upgrade again."""
    migration = load_migration('b4e8d2a6f1c9_role_grants.py')
    later = load_migration('d6a1f4c8e2b3_permission_scope.py')
    db.session.commit()
    with Operations.context(MigrationContext.configure(db.session.connection())):
        later.downgrade()
        migration.downgrade()
        old_shape()
        migration.upgrade()
        later.upgrade()


def test_the_migration_turns_assignments_into_grants_and_admin_into_the_built_in_role(people):
    first, second, third, fourth = people

    def old_shape():
        _old_roles((1, 'Anonymous user'), (2, 'Authenticated user'), (3, 'Staff'), (4, 'Admin'), (5, 'Moderator'),
                   (6, 'moderator'))
        for role_id, permission in ((3, 'ban users'), (4, 'ban users'), (4, 'edit cms pages'), (5, 'manage users')):
            db.session.execute(text('INSERT INTO role_permission (role_id, permission) VALUES (:r, :p)'),
                               {'r': role_id, 'p': permission})
        for user, role_id in ((first, 2), (second, 3), (third, 4), (fourth, 5), (fourth, 6)):
            db.session.execute(text('INSERT INTO user_role (user_id, role_id) VALUES (:u, :r)'),
                               {'u': user.id, 'r': role_id})
    try:
        _migrate(old_shape)

        assert _rows('SELECT id, name, built_in, listed_publicly FROM role ORDER BY id') == [
            (3, 'Staff', False, True), (4, 'Admin', True, False), (5, 'Moderator', False, False),
            (6, 'moderator (6)', False, False)]
        # Admin implies every permission, so its rows go; every other role keeps its own
        assert _rows('SELECT role_id, permission FROM role_permission ORDER BY role_id') == [
            (3, 'ban users'), (5, 'manage users')]
        # every assignment is an instance-wide grant; role 2's, which conferred nothing, is gone
        assert _rows('SELECT user_id, role_id FROM role_grant ORDER BY user_id, role_id') == [
            (second.id, 3), (third.id, 4), (fourth.id, 5), (fourth.id, 6)]
        assert not inspect(db.session.connection()).has_table('user_role')
        assert 'weight' not in _columns('role')
        assert 'site_domain_id' in _columns('mod_log')
        db.session.expire_all()
        assert (first.is_admin(), second.is_admin(), third.is_admin()) == (False, False, True)
        db.session.commit()
    finally:
        db.session.rollback()


def test_without_a_role_named_admin_the_seeded_id_becomes_it(people):
    def old_shape():
        _old_roles((3, 'Staff'), (4, 'Administrators'))
    try:
        _migrate(old_shape)
        assert _rows('SELECT id, name FROM role WHERE built_in') == [(4, 'Admin')]
        db.session.commit()
    finally:
        db.session.rollback()


def test_without_any_admin_role_one_is_made(people):
    def old_shape():
        _old_roles((3, 'Staff'))
    try:
        _migrate(old_shape)
        assert _rows('SELECT name FROM role WHERE built_in') == [('Admin',)]
        db.session.commit()
    finally:
        db.session.rollback()


def test_a_role_1_or_2_that_holds_a_permission_is_kept(people):
    def old_shape():
        _old_roles((1, 'Anonymous user'), (2, 'Authenticated user'), (4, 'Admin'))
        db.session.execute(text("INSERT INTO role_permission (role_id, permission) VALUES (2, 'ban users')"))
        db.session.execute(text('INSERT INTO user_role (user_id, role_id) VALUES (:u, 2)'), {'u': people[0].id})
    try:
        _migrate(old_shape)
        assert _rows('SELECT id FROM role ORDER BY id') == [(2,), (4,)]
        assert _rows('SELECT user_id, role_id FROM role_grant') == [(people[0].id, 2)]
        db.session.commit()
    finally:
        db.session.rollback()


def test_the_downgrade_keeps_instance_wide_grants_and_gives_admin_its_rows_back(people):
    first, second = people[:2]
    migration = load_migration('b4e8d2a6f1c9_role_grants.py')
    admin, staff = seed_default_roles().values()
    db.session.commit()
    grant_role(first, admin)
    shop = SiteDomain(domain='shop.grants.example', scheme='https', federates=False)
    db.session.add(shop)
    db.session.commit()
    shop_id, second_id, staff_id = shop.id, second.id, staff.id
    first_id, admin_id = first.id, admin.id   # read now: the ORM cannot refresh a role from the old schema
    try:
        with Operations.context(MigrationContext.configure(db.session.connection())):
            later = load_migration('d6a1f4c8e2b3_permission_scope.py')
            later.downgrade()
            db.session.execute(text('INSERT INTO role_grant (user_id, role_id, site_domain_id) VALUES (:u, :r, :d)'),
                               {'u': second_id, 'r': staff_id, 'd': shop_id})   # the old schema has no place for it
            migration.downgrade()
            assert _rows('SELECT user_id, role_id FROM user_role') == [(first_id, admin_id)]
            assert 'role_grant' not in inspect(db.session.connection()).get_table_names()
            admin_rows = {p for (p,) in _rows('SELECT permission FROM role_permission WHERE role_id = :r',
                                              r=admin_id)}
            assert 'change user roles' in admin_rows and 'ban users' in admin_rows
            migration.upgrade()
            later.upgrade()
        db.session.commit()
    finally:
        db.session.rollback()


# --------------------------------------------------------------------------
# The model and the checks
# --------------------------------------------------------------------------

def test_the_seeded_roles_are_the_built_in_admin_and_a_public_staff(db_session):
    roles = seed_default_roles()
    db.session.commit()
    assert (roles['Admin'].built_in, roles['Admin'].permissions) == (True, [])
    assert roles['Staff'].listed_publicly and not roles['Staff'].built_in
    assert sorted(p.permission for p in roles['Staff'].permissions) == sorted(STAFF_PERMISSIONS)
    assert Role.admin() is roles['Admin']


def test_the_built_in_role_holds_every_permission(people):
    grant_role(people[0], admin_role())
    assert user_access('edit cms pages', people[0].id) and user_access('anything at all', people[0].id)
    assert people[0].is_admin()
    assert people[0].id in [u.id for u in Site.admins()]


def test_roles_hold_only_unexpired_grants(people):
    holder = people[0]
    expired = Role(name='Lapsed')
    db.session.add(expired)
    db.session.commit()
    db.session.add(RolePermission(role_id=expired.id, permission='manage users'))
    db.session.execute(role_grant.insert().values(user_id=holder.id, role_id=expired.id,
                                                  expires_at=utcnow() - timedelta(days=1)))
    current = Role(name='Current')
    db.session.add(current)
    db.session.commit()
    db.session.add(RolePermission(role_id=current.id, permission='approve registrations'))
    db.session.execute(role_grant.insert().values(user_id=holder.id, role_id=current.id,
                                                  expires_at=utcnow() + timedelta(days=1)))
    db.session.commit()

    assert [role.name for role in holder.roles] == ['Current']
    assert user_access('approve registrations', holder.id) and not user_access('manage users', holder.id)


def test_appending_a_role_grants_it(people):
    role = Role(name='Helper')
    db.session.add(role)
    db.session.commit()
    people[0].roles.append(role)
    db.session.commit()
    assert _rows('SELECT user_id, role_id FROM role_grant') == [(people[0].id, role.id)]


def test_a_role_name_is_unique_ignoring_case(db_session):
    db.session.add(Role(name='Helper'))
    db.session.commit()
    db.session.add(Role(name='helper'))
    with pytest.raises(Exception, match='uq_role_name_lower'):
        db.session.commit()
    db.session.rollback()


def test_only_one_role_is_built_in(db_session):
    db.session.add(Role(name='Admin', built_in=True))
    db.session.commit()
    db.session.add(Role(name='Second', built_in=True))
    with pytest.raises(Exception, match='uq_role_one_built_in'):
        db.session.commit()
    db.session.rollback()


def test_the_grant_forms_offer_admin_then_the_rest_by_name(app, db_session):
    seed_default_roles()
    db.session.add(Role(name='Moderator'))
    db.session.commit()
    with app.test_request_context():
        assert [choice.label for choice in GrantForm().role.choices] == ['Admin', 'Moderator', 'Staff']
        assert [choice.label for choice in AddUserForm().grants.choices] == ['Admin', 'Moderator', 'Staff']


def test_roles_with_names_admin_first(db_session):
    seed_default_roles()
    db.session.commit()
    assert roles_with('ban users') == 'Admin, Staff'
    assert roles_with('edit cms pages') == 'Admin'


def test_a_held_post_is_shown_to_whoever_may_open_the_queue(people):
    """notify_admins_of_post_needing_approval: the admins always, and the holders of 'administer communities pages',
    which opens the approval queue it links to (RBAC step 8). The seeded Staff role holds it on the first domain."""
    first = SiteDomain(domain='first.grants.example', scheme='https', federates=True, is_default=True)
    db.session.add(first)
    db.session.commit()
    roles = seed_default_roles()
    db.session.commit()
    admin, staffer, author = people[:3]
    grant_role(admin, roles['Admin'])
    grant_role(staffer, roles['Staff'])
    community = make_community('held')
    community.site_domain_id = first.id
    post = make_post(community, author, 'https://test.piefed.local/post/9')

    def told():
        return sorted(n.user_id for n in Notification.query.filter_by(subtype='new_post_for_approval'))

    notify_admins_of_post_needing_approval(post)
    assert told() == sorted([1, admin.id, staffer.id])

    Notification.query.delete()
    RolePermission.query.filter_by(role_id=roles['Staff'].id, permission='administer communities pages').delete()
    db.session.commit()
    notify_admins_of_post_needing_approval(post)
    assert told() == sorted([1, admin.id])

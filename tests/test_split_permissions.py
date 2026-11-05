"""RBAC build step 4: the permission catalogue, and every role migrated onto it keeping what it can do today.

Spec docs/superpowers/specs/2026-11-03-rbac-design.md: Permission catalogue (note 1), Seeded roles, RB2, RB5, RB7.
"""
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from app import db
from app.models import Role, SiteDomain
from app.permissions import (BY_NAME, CATALOGUE, INSTANCE_ONLY_PERMISSIONS, ROLE_PERMISSIONS, SCOPABLE_PERMISSIONS,
                             STAFF_PERMISSIONS)
from app.roles import seed_default_roles
from app.utils import roles_with
from tests.factories import grant_role, make_instance, make_user
from tests.test_branding import load_migration
from tests.test_role_permissions import login

pytestmark = pytest.mark.usefixtures('site')

LEGACY_STAFF = ('approve registrations', 'ban users', 'administer all communities', 'manage users')


# --------------------------------------------------------------------------
# The catalogue
# --------------------------------------------------------------------------

def test_the_catalogue_names_each_permission_once_and_says_what_it_guards():
    assert len(ROLE_PERMISSIONS) == len(set(ROLE_PERMISSIONS)) == len(BY_NAME)
    assert all(permission.guards and permission.group for permission in CATALOGUE)
    assert SCOPABLE_PERMISSIONS | INSTANCE_ONLY_PERMISSIONS == set(ROLE_PERMISSIONS)


def test_the_catalogue_flags_scopable_and_instance_only_as_the_spec_does():
    assert SCOPABLE_PERMISSIONS == {
        'edit domain branding', 'approve registrations', 'administer all users', 'ban users', 'manage users',
        'view user details', 'create local communities', 'moderate all communities', 'remove and restore content',
        'turn comments off', 'administer all communities',
        'administer communities pages', 'view modlog', 'shown as admin'}
    assert INSTANCE_ONLY_PERMISSIONS == {
        'change instance settings', 'manage domains', 'masquerade', 'send newsletter', 'manage remote instances',
        'manage topics', 'manage blocklists', 'view vote analytics', 'manage media', 'edit cms pages',
        'read private messages', 'add remote communities', 'manage instance feeds', 'feature instance-wide',
        'exempt from upload limits', 'exempt from account limits', 'generate referral codes'}


def test_change_user_roles_is_no_permission_any_more():
    """RB5: roles and grants are Admin's alone, so no role can be given them."""
    assert 'change user roles' not in ROLE_PERMISSIONS


# --------------------------------------------------------------------------
# The migration
# --------------------------------------------------------------------------

@pytest.fixture
def people(db_session):
    instance = make_instance('test.piefed.local', software='piefed')
    assert make_user(instance, 'founder', local=True).id == 1
    return instance


def _rows(sql, **params):
    return [tuple(row) for row in db.session.execute(text(sql), params)]


def _held(role_id):
    return {p for (p,) in _rows('SELECT permission FROM role_permission WHERE role_id = :r', r=role_id)}


def _role(name, *permissions, built_in=False):
    role = Role(name=name, built_in=built_in)
    db.session.add(role)
    db.session.flush()
    for permission in permissions:
        db.session.execute(text('INSERT INTO role_permission (role_id, permission) VALUES (:r, :p)'),
                           {'r': role.id, 'p': permission})
    return role.id


def _migrate(old_shape):
    """Downgrade to the eight legacy permissions, let `old_shape()` write rows in them, and upgrade again."""
    migration = load_migration('c5f9e3b7a2d1_split_permissions.py')
    later = load_migration('d6a1f4c8e2b3_permission_scope.py')
    latest = load_migration('e4c8a1d7f3b6_split_moderation_permissions.py')
    db.session.commit()
    with Operations.context(MigrationContext.configure(db.session.connection())):
        latest.downgrade()
        later.downgrade()
        migration.downgrade()
        ids = old_shape()
        migration.upgrade()
        later.upgrade()
        latest.upgrade()
    return ids


def test_the_default_staff_role_becomes_exactly_the_seeded_one(people):
    """An upgraded instance and a new one agree on what Staff may do, permission for permission."""
    db.session.add(SiteDomain(domain='first.split.example', scheme='https', federates=True, is_default=True))
    db.session.commit()
    try:
        staff = _migrate(lambda: _role('Staff', *LEGACY_STAFF))
        assert _held(staff) == set(STAFF_PERMISSIONS)
        db.session.commit()
    finally:
        db.session.rollback()


def test_each_role_gains_what_its_legacy_permissions_were_split_into(people):
    def old_shape():
        return dict(
            communities=_role('Community admins', 'administer all communities'),
            settings=_role('Settings', 'change instance settings'),
            users=_role('User admins', 'administer all users'),
            banners=_role('Banners', 'ban users'),
            roles=_role('Role editors', 'change user roles', 'edit cms pages'),
            renamed_staff=_role('Helpers', 'approve registrations'),
            admin=_role('Admin', built_in=True))
    try:
        ids = _migrate(old_shape)
        assert _held(ids['communities']) == {
            'moderate all communities', 'remove and restore content', 'turn comments off', 'manage topics',
            'manage blocklists', 'view vote analytics', 'manage media', 'manage remote instances',
            'administer communities pages'}   # not the narrowed one (note 4)
        assert _held(ids['settings']) == {
            'change instance settings', 'manage domains', 'masquerade', 'send newsletter', 'manage remote instances',
            'edit domain branding'}
        # not `read private messages`: other people's conversations were an admin's alone on the web, and the API's
        # report lists ask `administer all users` themselves
        assert _held(ids['users']) == {'administer all users', 'manage media'}
        assert _held(ids['banners']) == {'ban users', 'manage blocklists'}
        assert _held(ids['roles']) == {'edit cms pages'}   # RB5
        # only the role named Staff passed is_staff(); a renamed one never did, so gains nothing for it
        assert _held(ids['renamed_staff']) == {'approve registrations'}
        assert _held(ids['admin']) == set()   # holds them all without rows
        assert set().union(*map(_held, ids.values())) <= set(ROLE_PERMISSIONS)
        db.session.commit()
    finally:
        db.session.rollback()


def test_the_downgrade_gives_back_administer_all_communities_and_drops_the_rest(people):
    staff, helpers = seed_default_roles()['Staff'], Role(name='Helpers')
    db.session.add(helpers)
    db.session.commit()
    helpers_id = helpers.id
    db.session.execute(text("INSERT INTO role_permission (role_id, permission) VALUES (:r, 'view modlog')"),
                       {'r': helpers_id})
    staff_id = staff.id
    migration = load_migration('c5f9e3b7a2d1_split_permissions.py')
    db.session.commit()
    try:
        with Operations.context(MigrationContext.configure(db.session.connection())):
            later = load_migration('d6a1f4c8e2b3_permission_scope.py')
            later.downgrade()
            migration.downgrade()
            assert _held(staff_id) == set(LEGACY_STAFF)
            assert _held(helpers_id) == set()
            migration.upgrade()
            later.upgrade()
        db.session.commit()
    finally:
        db.session.rollback()


# --------------------------------------------------------------------------
# RB5: the permissions page is Admin's alone
# --------------------------------------------------------------------------

def test_holding_every_permission_does_not_open_the_permissions_page(app, people):
    editor = make_user(people, 'everything', local=True)
    editor.verified = True
    role = Role(name='Everything')
    db.session.add(role)
    db.session.commit()
    for permission in ROLE_PERMISSIONS:
        db.session.execute(text('INSERT INTO role_permission (role_id, permission) VALUES (:r, :p)'),
                           {'r': role.id, 'p': permission})
    grant_role(editor, role)
    client = app.test_client()
    login(client, editor)
    response = client.get('/admin/permissions')
    assert response.status_code == 302 and '/auth/permission_denied' in response.location


def test_nobody_signed_in_is_refused_the_permissions_page(app, people):
    response = app.test_client().get('/admin/permissions')
    assert response.status_code == 302


def test_an_admin_only_page_names_only_admin_as_able_to_reach_it(people):
    seed_default_roles()
    db.session.commit()
    assert roles_with(None) == 'Admin'


def test_with_no_admin_role_an_admin_only_page_names_nobody(people):
    assert roles_with(None) == ''


# --------------------------------------------------------------------------
# d6a1f4c8e2b3: a permission says which domains it reaches, and a grant only who holds the role
# --------------------------------------------------------------------------

def _downgrade_to_grant_scope(fill):
    migration = load_migration('d6a1f4c8e2b3_permission_scope.py')
    db.session.commit()
    with Operations.context(MigrationContext.configure(db.session.connection())):
        migration.downgrade()
        fill()
        migration.upgrade()


def _limits(role_id, permission):
    return [row[0] for row in db.session.execute(text(
        'SELECT site_domain_id FROM role_permission_domain WHERE role_id = :r AND permission = :p'),
        {'r': role_id, 'p': permission})]


def test_the_upgrade_limits_staff_to_the_default_domain_and_gives_it_user_administration(people):
    first = SiteDomain(domain='first.split.example', scheme='https', federates=True, is_default=True)
    db.session.add(first)
    db.session.commit()
    first_id = first.id
    staff = _role('Staff', 'moderate all communities', 'manage topics')
    other = _role('Others', 'moderate all communities')
    try:
        _downgrade_to_grant_scope(lambda: None)
        assert _limits(staff, 'moderate all communities') == [first_id]
        assert _limits(staff, 'administer all users') == [first_id]
        assert _limits(staff, 'manage topics') == []   # instance-only: untouched
        wide = db.session.execute(text('SELECT permission, all_domains FROM role_permission WHERE role_id = :r'),
                                  {'r': staff}).all()
        assert dict(wide) == {'moderate all communities': False, 'administer all users': False, 'manage topics': True}
        assert _limits(other, 'moderate all communities') == []   # another role is not Staff
        db.session.commit()
    finally:
        db.session.rollback()


def test_without_a_default_row_staff_is_left_alone(people):
    staff = _role('Staff', 'moderate all communities')
    try:
        _downgrade_to_grant_scope(lambda: None)
        assert _held(staff) == {'moderate all communities'}
        db.session.commit()
    finally:
        db.session.rollback()


def test_a_role_granted_on_a_domain_becomes_a_role_limited_to_it(people):
    """Nobody gains or loses anything: the holder on X keeps X, under a role of its own."""
    shop = SiteDomain(domain='shop.split.example', scheme='https', federates=False)
    db.session.add(shop)
    db.session.commit()
    shop_id = shop.id
    role = _role('Helpers', 'ban users')
    holder = make_user(people, 'scopedone', local=True)
    wide = make_user(people, 'wideone', local=True)
    holder_id, wide_id = holder.id, wide.id
    db.session.commit()

    def fill():
        db.session.execute(text('INSERT INTO role_grant (user_id, role_id, site_domain_id) VALUES (:u, :r, :d)'),
                           {'u': holder_id, 'r': role, 'd': shop_id})
        db.session.execute(text('INSERT INTO role_grant (user_id, role_id) VALUES (:u, :r)'),
                           {'u': wide_id, 'r': role})
    try:
        _downgrade_to_grant_scope(fill)
        made = db.session.execute(text("SELECT id FROM role WHERE name = 'Helpers on shop.split.example'")).scalar()
        assert _limits(made, 'ban users') == [shop_id]
        grants = dict(db.session.execute(text('SELECT user_id, role_id FROM role_grant')).all())
        assert grants == {holder_id: made, wide_id: role}
        db.session.commit()
    finally:
        db.session.rollback()


def test_the_registration_setting_merges_into_closed_and_the_downgrade_cannot_widen(people):
    shop = SiteDomain(domain='shop.split.example', scheme='https', federates=False)
    db.session.add(shop)
    db.session.commit()
    shop_id = shop.id
    limited = _role('Limited', 'ban users')
    staff = _role('Staff', 'ban users')
    migration = load_migration('d6a1f4c8e2b3_permission_scope.py')
    for role_id in (limited, staff):
        db.session.execute(text('UPDATE role_permission SET all_domains = false WHERE role_id = :r'), {'r': role_id})
    db.session.commit()
    try:
        with Operations.context(MigrationContext.configure(db.session.connection())):
            migration.downgrade()
            db.session.execute(text("UPDATE site_domain SET registration = 'staff' WHERE id = :i"), {'i': shop_id})
            assert _held(limited) == set()   # dropped, rather than become an instance-wide permission
            assert _held(staff) == {'ban users'}   # Staff gets its old reach back
            migration.upgrade()
            assert db.session.execute(text('SELECT registration FROM site_domain WHERE id = :i'),
                                      {'i': shop_id}).scalar() == 'closed'
        db.session.commit()
    finally:
        db.session.rollback()


@pytest.mark.parametrize('held_before', [True, False], ids=['held it before', 'gained it'])
def test_the_downgrade_takes_staffs_user_administration_only_if_the_upgrade_gave_it(people, held_before):
    """Found on a copy of production, whose Staff already held `administer all users`."""
    db.session.add(SiteDomain(domain='first.split.example', scheme='https', federates=True, is_default=True))
    db.session.commit()
    migration = load_migration('d6a1f4c8e2b3_permission_scope.py')
    staff = _role('Staff', 'ban users')
    db.session.commit()
    try:
        with Operations.context(MigrationContext.configure(db.session.connection())):
            migration.downgrade()
            if held_before:
                db.session.execute(text("INSERT INTO role_permission (role_id, permission) "
                                        "VALUES (:r, 'administer all users')"), {'r': staff})
            migration.upgrade()
            assert 'administer all users' in _held(staff)
            migration.downgrade()
            assert ('administer all users' in _held(staff)) is held_before
            assert 'ban users' in _held(staff)
            migration.upgrade()
        db.session.commit()
    finally:
        db.session.rollback()

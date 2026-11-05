"""RBAC build step 7: making, changing and removing roles and grants (app/roles.py).

Spec docs/superpowers/specs/2026-11-03-rbac-design.md: Admin UI (Roles, Grants), RB2, RB5, RB7, RB9, RB10, RB16,
RB17. The routes (tests/test_role_admin_routes.py) only translate forms into these calls, so the rules are tested
here, once.
"""
from datetime import timedelta
from unittest.mock import patch

import pytest
from cachelib import SimpleCache

from app import db, permission_cache
from app.models import ModLog, Role, RoleGrant, RolePermission, RolePermissionDomain, SiteDomain, utcnow
from app.permission_cache import permission_version
from app.permissions import user_access
from app.roles import (RoleError, change_expiry, create_role, delete_role, grant_role, permission_scopes, revoke_grant,
                       seed_default_roles, update_role)
from app.storefront import clear_domain_cache
from tests.factories import admin_role, make_instance, make_user, staff_role

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture(autouse=True)
def versions():
    """The test app's cache keeps nothing, so every version reads 0; these tests watch the versions move."""
    with patch.object(permission_cache, 'cache', SimpleCache()):
        yield


@pytest.fixture
def people(app, db_session):
    instance = make_instance('test.piefed.local', software='piefed')
    founder = make_user(instance, 'founder', local=True)
    assert founder.id == 1
    admin = make_user(instance, 'boss', local=True)
    holder = make_user(instance, 'holder', local=True)
    domain = SiteDomain(domain='x.roles.example', scheme='https', federates=False)
    db.session.add(domain)
    db.session.commit()
    clear_domain_cache()
    from tests.factories import grant_role as grant_directly
    grant_directly(admin, admin_role())
    yield type('People', (), dict(founder=founder, admin=admin, holder=holder, domain=domain, instance=instance))
    clear_domain_cache()


def permissions_of(role):
    return sorted(p.permission for p in RolePermission.query.filter_by(role_id=role.id))


def logged(action):
    return ModLog.query.filter_by(action=action).order_by(ModLog.id).all()


# --------------------------------------------------------------------------
# Roles
# --------------------------------------------------------------------------

def test_a_role_is_created_with_its_permissions_and_logged(people):
    role = create_role(people.admin, 'Helpers', 'They help', True, ['ban users', 'view modlog'])
    assert (role.name, role.description, role.listed_publicly, role.built_in) == ('Helpers', 'They help', True, False)
    assert permissions_of(role) == ['ban users', 'view modlog']
    entry, = logged('create_role')
    assert (entry.user_id, entry.link_text, entry.type, entry.public) == (people.admin.id, 'Helpers', 'admin', False)


@pytest.mark.parametrize('name', ['', '   ', 'Admin', 'admin', ' ADMIN ', 'x' * 51])
def test_a_role_name_must_be_given_short_and_not_admin(people, name):
    with pytest.raises(RoleError):
        create_role(people.admin, name, '', False, [])


def test_a_role_name_is_unique_whatever_its_case(people):
    create_role(people.admin, 'Helpers', '', False, [])
    with pytest.raises(RoleError, match='already'):
        create_role(people.admin, 'HELPERS', '', False, [])


def test_the_built_in_role_keeps_its_name_even_when_renamed(people):
    """RB16: Admin is found by its flag; whatever it is called, no other role may take that name or 'Admin'."""
    admin = Role.admin()
    admin.name = 'Superusers'
    db.session.commit()
    with pytest.raises(RoleError):
        create_role(people.admin, 'superusers', '', False, [])
    with pytest.raises(RoleError):
        create_role(people.admin, 'Admin', '', False, [])


def test_an_unknown_permission_is_refused(people):
    with pytest.raises(RoleError, match='no such permission'):
        create_role(people.admin, 'Helpers', '', False, ['rule the world'])


def test_an_edit_replaces_the_permissions_and_takes_effect_for_holders(people):
    role = create_role(people.admin, 'Helpers', '', False, ['ban users'])
    grant_role(people.admin, people.holder, role)
    assert user_access('ban users', people.holder.id) is True
    before = permission_version(people.holder.id)
    update_role(people.admin, role, 'Assistants', 'new', True, ['view modlog'])
    assert permission_version(people.holder.id) > before
    assert permissions_of(role) == ['view modlog']
    assert (role.name, role.description, role.listed_publicly) == ('Assistants', 'new', True)
    assert user_access('ban users', people.holder.id) is False
    assert user_access('view modlog', people.holder.id) is True
    entry, = logged('edit_role')
    assert entry.link_text == 'Assistants'


def test_an_edit_leaves_every_other_role_as_it_was(people):
    """D938's lesson: the old page's save cleared every role's rows and wrote back only two."""
    edited = create_role(people.admin, 'Helpers', '', False, ['ban users'])
    other = create_role(people.admin, 'Others', '', False, ['view modlog', 'manage users'])
    update_role(people.admin, edited, 'Helpers', '', False, [])
    assert permissions_of(other) == ['manage users', 'view modlog']


def test_each_role_action_has_a_modlog_label_and_stays_off_the_public_modlog(people):
    for action in ('create_role', 'edit_role', 'delete_role', 'grant_role', 'change_grant_expiry', 'revoke_role'):
        assert action in ModLog.action_map and action in ModLog.admin_only_actions


def test_an_edit_may_keep_its_own_name(people):
    role = create_role(people.admin, 'Helpers', '', False, [])
    update_role(people.admin, role, 'helpers', '', False, [])
    assert role.name == 'helpers'


def test_the_built_in_role_cannot_be_edited_or_deleted(people):
    with pytest.raises(RoleError):
        update_role(people.admin, Role.admin(), 'Admin', '', False, [])
    with pytest.raises(RoleError):
        delete_role(people.admin, Role.admin(), revoke=True)


def test_a_permission_is_limited_to_selected_domains_and_all_is_the_default(people):
    role = create_role(people.admin, 'Helpers', '', False, {'ban users': {people.domain.id}, 'view modlog': None})
    assert permission_scopes(role) == {'ban users': {people.domain.id}, 'view modlog': None}
    entry, = logged('create_role')
    assert entry.reason == f'ban users ({people.domain.domain}), view modlog'
    update_role(people.admin, role, 'Helpers', '', False, {'ban users': None})
    assert permission_scopes(role) == {'ban users': None}
    assert RolePermissionDomain.query.filter_by(role_id=role.id).count() == 0


def test_an_instance_only_permission_cannot_be_limited(people):
    """RB7 as the owner ruled it: it applies to the whole instance, in any role."""
    with pytest.raises(RoleError, match='masquerade'):
        create_role(people.admin, 'Mixed', '', False, {'masquerade': {people.domain.id}})
    assert permission_scopes(create_role(people.admin, 'Mixed', '', False, {'masquerade': None})) == {'masquerade': None}


def test_a_limited_permission_names_at_least_one_domain_and_only_real_ones(people):
    with pytest.raises(RoleError, match='at least one domain'):
        create_role(people.admin, 'Helpers', '', False, {'ban users': set()})
    with pytest.raises(RoleError, match='no such domain'):
        create_role(people.admin, 'Helpers', '', False, {'ban users': {987654}})


def test_an_edit_narrowing_a_permission_takes_effect_for_holders(people):
    role = create_role(people.admin, 'Helpers', '', False, ['ban users'])
    grant_role(people.admin, people.holder, role)
    people.holder.site_domain_id = people.domain.id
    db.session.commit()
    other = make_user(people.instance, 'other', local=True)
    assert user_access('ban users', people.holder.id, on=other) is True
    before = permission_version(people.holder.id)
    update_role(people.admin, role, 'Helpers', '', False, {'ban users': {people.domain.id}})
    assert permission_version(people.holder.id) > before
    assert user_access('ban users', people.holder.id, on=people.holder) is True
    assert user_access('ban users', people.holder.id) is False


def test_the_seeded_staff_role_reaches_the_first_domain_and_holds_user_administration(people):
    """Owner ruling 2026-11-05: its scopable permissions are the first domain's; its instance-only ones stay."""
    first = SiteDomain(domain='first.roles.example', scheme='https', federates=True, is_default=True)
    db.session.add(first)
    db.session.commit()
    Role.query.filter(Role.built_in.is_(True)).update({'name': 'Old admin'})
    db.session.commit()
    RoleGrant.query.delete()
    Role.query.delete()
    db.session.commit()
    staff = seed_default_roles()['Staff']
    db.session.commit()
    scopes = permission_scopes(staff)
    assert scopes['administer all users'] == {first.id}
    assert scopes['moderate all communities'] == {first.id}
    assert scopes['manage topics'] is None
    assert staff.built_in is False and staff.listed_publicly is True


def test_deleting_a_role_with_grants_needs_their_revocation(people):
    role = create_role(people.admin, 'Helpers', '', False, ['ban users'])
    grant_role(people.admin, people.holder, role)
    with pytest.raises(RoleError, match='grant'):
        delete_role(people.admin, role, revoke=False)
    assert db.session.get(Role, role.id) is not None


def test_deleting_a_role_revokes_its_grants_and_logs_each(people):
    role = create_role(people.admin, 'Helpers', '', False, ['ban users'])
    role_id = role.id
    grant_role(people.admin, people.holder, role)
    grant_role(people.admin, people.founder, role)
    before = permission_version(people.holder.id)
    delete_role(people.admin, role, revoke=True)
    assert db.session.get(Role, role_id) is None
    assert RolePermission.query.filter_by(role_id=role_id).count() == 0
    assert RoleGrant.query.filter_by(role_id=role_id).count() == 0
    assert sorted(e.target_user_id for e in logged('revoke_role')) == sorted([people.holder.id, people.founder.id])
    assert [e.link_text for e in logged('delete_role')] == ['Helpers']
    assert permission_version(people.holder.id) > before
    assert user_access('ban users', people.holder.id) is False


def test_staff_is_an_ordinary_role_an_admin_may_delete(people):
    """RB16."""
    staff = staff_role()
    delete_role(people.admin, staff, revoke=True)
    assert Role.query.filter_by(name='Staff').first() is None


# --------------------------------------------------------------------------
# Grants
# --------------------------------------------------------------------------

def test_a_grant_takes_effect_at_once_and_is_logged(people):
    role = create_role(people.admin, 'Helpers', '', False, ['ban users'])
    assert user_access('ban users', people.holder.id, on=people.holder) is False
    grant = grant_role(people.admin, people.holder, role)
    assert (grant.user_id, grant.role_id, grant.granted_by) == (people.holder.id, role.id, people.admin.id)
    assert user_access('ban users', people.holder.id, on=people.holder) is True
    entry, = logged('grant_role')
    assert (entry.target_user_id, entry.public, entry.reason) == (people.holder.id, False, 'Helpers')


def test_the_same_grant_twice_is_refused(people):
    role = create_role(people.admin, 'Helpers', '', False, [])
    grant_role(people.admin, people.holder, role)
    with pytest.raises(RoleError, match='already'):
        grant_role(people.admin, people.holder, role)


def test_an_expiry_in_the_past_is_refused(people):
    role = create_role(people.admin, 'Helpers', '', False, [])
    with pytest.raises(RoleError, match='future'):
        grant_role(people.admin, people.holder, role, expires_at=utcnow() - timedelta(hours=1))


def test_a_remote_account_is_never_granted_a_role(people):
    peer = make_instance('peer.example', software='piefed')
    remote = make_user(peer, 'remy')
    with pytest.raises(RoleError, match='local'):
        grant_role(people.admin, remote, create_role(people.admin, 'Helpers', '', False, []))


def test_an_expiry_is_set_and_cleared_and_logged(people):
    role = create_role(people.admin, 'Helpers', '', False, ['ban users'])
    grant = grant_role(people.admin, people.holder, role)
    later = (utcnow() + timedelta(days=3)).replace(microsecond=0)
    before = permission_version(people.holder.id)
    change_expiry(people.admin, grant, later)
    assert grant.expires_at == later
    assert permission_version(people.holder.id) > before
    change_expiry(people.admin, grant, None)
    assert grant.expires_at is None
    assert len(logged('change_grant_expiry')) == 2
    with pytest.raises(RoleError, match='future'):
        change_expiry(people.admin, grant, utcnow() - timedelta(days=1))


def test_a_revoke_takes_effect_at_once_and_is_logged(people):
    role = create_role(people.admin, 'Helpers', '', False, ['ban users'])
    grant = grant_role(people.admin, people.holder, role)
    grant_id = grant.id
    assert user_access('ban users', people.holder.id, anywhere=True) is True
    revoke_grant(people.admin, grant)
    assert db.session.get(RoleGrant, grant_id) is None
    assert user_access('ban users', people.holder.id, anywhere=True) is False
    entry, = logged('revoke_role')
    assert entry.target_user_id == people.holder.id


def test_an_admin_cannot_revoke_their_own_admin_grant_but_another_admin_can(people):
    own = RoleGrant.query.filter_by(user_id=people.admin.id, role_id=Role.admin().id).one()
    with pytest.raises(RoleError, match='own'):
        revoke_grant(people.admin, own)
    revoke_grant(people.founder, own)
    assert people.admin.is_admin() is False


def test_only_an_admin_changes_roles_or_grants(people):
    """RB5, in the functions as well as on the routes."""
    role = create_role(people.admin, 'Helpers', '', False, [])
    grant = grant_role(people.admin, people.holder, role)
    for attempt in (lambda: create_role(people.holder, 'Mine', '', False, []),
                    lambda: update_role(people.holder, role, 'Mine', '', False, []),
                    lambda: delete_role(people.holder, role, revoke=True),
                    lambda: grant_role(people.holder, people.founder, role),
                    lambda: change_expiry(people.holder, grant, None),
                    lambda: revoke_grant(people.holder, grant)):
        with pytest.raises(RoleError, match='Only an admin'):
            attempt()

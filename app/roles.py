"""Roles and grants: the roles a new instance starts with, and every change an admin makes to them.

`flask init-db` creates the default roles, and the RBAC parity table (tests/rbac_parity.py) seeds the same ones, so
that table measures what a freshly installed instance's Staff and Admin may do.

Admin is the one built-in role: it holds every permission and is found by `Role.built_in`, never by its name
(spec 2026-11-03-rbac-design.md RB2, RB16). Staff is seeded, not built in: from then on it is an ordinary role an
admin may edit, rename or delete.

The rest of this module is what the role and grant pages (app/admin/roles_routes.py) do, so that the rules hold
however a change arrives: only an admin changes roles or grants (RB5); a role granted on a domain holds only
scopable permissions (RB7); every change is in the modlog (RB9) and is seen on the holders' next request (RB10).
"""
from datetime import datetime

from sqlalchemy import func

from app import db
from app.models import Role, RoleGrant, RolePermission, User, utcnow
from app.permission_cache import permissions_changed
from app.permissions import BY_NAME, INSTANCE_ONLY_PERMISSIONS, STAFF_PERMISSIONS
from app.storefront import domain_rows
from app.utils import add_to_modlog, set_setting

NAME_LENGTH = 50
RESERVED_NAME = 'admin'   # RB16: no role but the built-in one may be called this, in any case


def seed_default_roles() -> dict[str, Role]:
    """Add Admin and Staff to the session, and return them by name. The caller commits."""
    admin = Role(name='Admin', built_in=True)
    staff = Role(name='Staff', listed_publicly=True)
    for permission in STAFF_PERMISSIONS:
        staff.permissions.append(RolePermission(permission=permission))
    db.session.add_all([admin, staff])
    return {admin.name: admin, staff.name: staff}


class RoleError(Exception):
    """A change the rules refuse, in words to show the admin. `grants` are the grants in the way, if any."""

    def __init__(self, message: str, grants: list[RoleGrant] | None = None):
        super().__init__(message)
        self.grants = grants or []


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------

def scope_name(site_domain_id: int | None) -> str:
    if site_domain_id is None:
        return 'instance-wide'
    row = next((row for row in domain_rows() if row.id == site_domain_id), None)
    return f'on {row.domain}' if row else f'on domain #{site_domain_id}'


def grant_holder_names(grants: list[RoleGrant]) -> str:
    users = {user.id: user for user in User.query.filter(User.id.in_({grant.user_id for grant in grants}))}
    return ', '.join(f'{users[grant.user_id].user_name} {scope_name(grant.site_domain_id)}' for grant in grants)


def holder_ids(role: Role) -> list[int]:
    return list(db.session.scalars(db.select(RoleGrant.user_id).where(RoleGrant.role_id == role.id).distinct()))


def domain_grants(role: Role) -> list[RoleGrant]:
    return RoleGrant.query.filter(RoleGrant.role_id == role.id, RoleGrant.site_domain_id.isnot(None)) \
        .order_by(RoleGrant.id).all()


def instance_only_in(permissions) -> list[str]:
    return [permission for permission in permissions if permission in INSTANCE_ONLY_PERMISSIONS]


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------

def _must_be_admin(actor: User) -> None:
    if actor is None or not actor.is_admin():
        raise RoleError('Only an admin may change roles or grants.')


def _clean_name(name: str, role: Role | None = None) -> str:
    name = (name or '').strip()
    if not name:
        raise RoleError('A role needs a name.')
    if len(name) > NAME_LENGTH:
        raise RoleError(f'A role name is at most {NAME_LENGTH} characters.')
    clash = Role.query.filter(func.lower(Role.name) == name.lower())
    if role is not None:
        clash = clash.filter(Role.id != role.id)
    built_in = Role.admin()
    if name.lower() == RESERVED_NAME or (built_in is not None and built_in is not role
                                         and name.lower() == (built_in.name or '').lower()):
        raise RoleError('That name is the built-in Admin role\'s.')
    if clash.first() is not None:
        raise RoleError(f'A role called "{name}" already exists.')
    return name


def _clean_permissions(permissions) -> list[str]:
    chosen = sorted(set(permissions))
    unknown = [permission for permission in chosen if permission not in BY_NAME]
    if unknown:
        raise RoleError(f'There is no such permission: {", ".join(unknown)}.')
    return chosen


def _clean_expiry(expires_at: datetime | None) -> datetime | None:
    if expires_at is not None and expires_at <= utcnow():
        raise RoleError('An expiry must be in the future.')
    return expires_at


def _admins_changed(role: Role) -> None:
    """The `admin_ids` setting lists Admin's holders for every request (app/request_hooks.py); emptied, the next
    request reads them afresh (RB10)."""
    if role.built_in:
        set_setting('admin_ids', None)


def _log(action: str, actor: User, *, role_name: str, role: Role | None = None, target: User | None = None,
         site_domain_id: int | None = None, reason: str = '') -> None:
    if target is not None:
        link, link_text = f'u/{target.link()}', target.display_name()
    else:
        link, link_text = (f'admin/roles/{role.id}' if role is not None else ''), role_name
    add_to_modlog(action, actor=actor, target_user=target, reason=reason, link=link, link_text=link_text,
                  site_domain_id=site_domain_id)


# --------------------------------------------------------------------------
# Roles
# --------------------------------------------------------------------------

def create_role(actor: User, name: str, description: str, listed_publicly: bool, permissions) -> Role:
    _must_be_admin(actor)
    role = Role(name=_clean_name(name), description=(description or '').strip()[:512],
                listed_publicly=bool(listed_publicly))
    for permission in _clean_permissions(permissions):
        role.permissions.append(RolePermission(permission=permission))
    db.session.add(role)
    db.session.commit()
    _log('create_role', actor, role_name=role.name, role=role, reason=', '.join(_clean_permissions(permissions)))
    return role


def update_role(actor: User, role: Role, name: str, description: str, listed_publicly: bool, permissions) -> Role:
    """Replace the role's name, description, listing and permissions. RB7: gaining an instance-only permission is
    refused while the role is granted on any domain, and the error lists those grants."""
    _must_be_admin(actor)
    if role.built_in:
        raise RoleError('The built-in Admin role holds every permission and cannot be edited.')
    name = _clean_name(name, role)
    chosen = _clean_permissions(permissions)
    held = {p.permission for p in role.permissions}
    gained = instance_only_in(set(chosen) - held)
    if gained:
        blocking = domain_grants(role)
        if blocking:
            raise RoleError(f'{", ".join(gained)} cannot be granted on a domain, and this role is: '
                            f'{grant_holder_names(blocking)}. Revoke those grants first.', blocking)
    RolePermission.query.filter_by(role_id=role.id).delete()
    role.name, role.description, role.listed_publicly = name, (description or '').strip()[:512], bool(listed_publicly)
    for permission in chosen:
        db.session.add(RolePermission(role_id=role.id, permission=permission))
    db.session.commit()
    db.session.expire(role, ['permissions'])
    permissions_changed(*holder_ids(role))
    _log('edit_role', actor, role_name=role.name, role=role, reason=', '.join(chosen))
    return role


def delete_role(actor: User, role: Role, revoke: bool) -> None:
    """Delete the role. With grants, only when `revoke` confirms revoking them all, in the same transaction."""
    _must_be_admin(actor)
    if role.built_in:
        raise RoleError('The built-in Admin role cannot be deleted.')
    grants = RoleGrant.query.filter_by(role_id=role.id).order_by(RoleGrant.id).all()
    if grants and not revoke:
        raise RoleError(f'This role has {len(grants)} grant(s); confirm revoking them to delete it.', grants)
    holders = {user.id: user for user in User.query.filter(User.id.in_({grant.user_id for grant in grants}))}
    name, revoked = role.name, [(holders[grant.user_id], grant.site_domain_id) for grant in grants]
    for grant in grants:
        db.session.delete(grant)
    db.session.flush()   # before the role: nothing tells the session the grants depend on it
    RolePermission.query.filter_by(role_id=role.id).delete()
    db.session.delete(role)
    db.session.commit()
    permissions_changed(*holders)
    for user, site_domain_id in revoked:
        _log('revoke_role', actor, role_name=name, target=user, site_domain_id=site_domain_id,
             reason=f'{name} {scope_name(site_domain_id)} (role deleted)')
    _log('delete_role', actor, role_name=name)


# --------------------------------------------------------------------------
# Grants
# --------------------------------------------------------------------------

def scope_refusal(role: Role, site_domain_id: int | None) -> str | None:
    """Why `role` cannot be granted on this domain, or None: Admin is instance-wide only (RB2), and a role holding an
    instance-only permission is too (RB7)."""
    if site_domain_id is None:
        return None
    if role.built_in:
        return 'Admin is granted instance-wide only.'
    refused = instance_only_in(p.permission for p in role.permissions)
    if refused:
        return f'{role.name} cannot be granted on a domain: {", ".join(sorted(refused))} cannot be held on one domain.'
    if not any(row.id == site_domain_id for row in domain_rows()):
        return 'There is no such domain.'
    return None


def grant_role(actor: User, user: User, role: Role, site_domain_id: int | None = None,
               expires_at: datetime | None = None) -> RoleGrant:
    _must_be_admin(actor)
    if user is None or not user.is_local():
        raise RoleError('Roles are granted to local accounts only.')
    refused = scope_refusal(role, site_domain_id)
    if refused:
        raise RoleError(refused)
    expires_at = _clean_expiry(expires_at)
    same_scope = RoleGrant.site_domain_id.is_(None) if site_domain_id is None \
        else RoleGrant.site_domain_id == site_domain_id
    if RoleGrant.query.filter(RoleGrant.user_id == user.id, RoleGrant.role_id == role.id, same_scope).first():
        raise RoleError(f'{user.user_name} already holds {role.name} {scope_name(site_domain_id)}.')
    grant = RoleGrant(user_id=user.id, role_id=role.id, site_domain_id=site_domain_id, granted_by=actor.id,
                      expires_at=expires_at)
    db.session.add(grant)
    db.session.commit()
    permissions_changed(user.id)
    _admins_changed(role)
    _log('grant_role', actor, role_name=role.name, target=user, site_domain_id=site_domain_id,
         reason=f'{role.name} {scope_name(site_domain_id)}' + (f', until {expires_at:%Y-%m-%d %H:%M} UTC'
                                                               if expires_at else ''))
    return grant


def change_expiry(actor: User, grant: RoleGrant, expires_at: datetime | None) -> RoleGrant:
    _must_be_admin(actor)
    grant.expires_at = _clean_expiry(expires_at)
    db.session.commit()
    permissions_changed(grant.user_id)
    role, user = db.session.get(Role, grant.role_id), db.session.get(User, grant.user_id)
    _admins_changed(role)
    _log('change_grant_expiry', actor, role_name=role.name, target=user, site_domain_id=grant.site_domain_id,
         reason=f'{role.name} {scope_name(grant.site_domain_id)}: '
                + (f'until {expires_at:%Y-%m-%d %H:%M} UTC' if expires_at else 'no expiry'))
    return grant


def revoke_grant(actor: User, grant: RoleGrant) -> None:
    """RB2: an admin may not revoke their own Admin grant, against accidents; another admin, or user 1, may."""
    _must_be_admin(actor)
    role, user = db.session.get(Role, grant.role_id), db.session.get(User, grant.user_id)
    if role.built_in and grant.user_id == actor.id and actor.id != 1:
        raise RoleError('You cannot revoke your own Admin grant; another admin can.')
    site_domain_id = grant.site_domain_id
    db.session.delete(grant)
    db.session.commit()
    permissions_changed(user.id)
    _admins_changed(role)
    _log('revoke_role', actor, role_name=role.name, target=user, site_domain_id=site_domain_id,
         reason=f'{role.name} {scope_name(site_domain_id)}')


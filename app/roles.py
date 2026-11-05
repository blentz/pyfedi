"""Roles and grants: the roles a new instance starts with, and every change an admin makes to them.

`flask init-db` creates the default roles, and the RBAC parity table (tests/rbac_parity.py) seeds the same ones.

Admin is the one built-in role: it holds every permission and is found by `Role.built_in`, never by its name
(spec 2026-11-03-rbac-design.md RB2, RB16). Staff is seeded, not built in: from then on it is an ordinary role an
admin may edit, rename or delete. Its scopable permissions start out limited to the first (default) domain.

A role says which domains each of its permissions reaches; a grant only says who holds the role. The rest of this
module is what the role and grant pages (app/admin/roles_routes.py) do, so that the rules hold however a change
arrives: only an admin changes roles or grants (RB5); an instance-only permission cannot be limited to domains (RB7);
every change is in the modlog (RB9) and is seen on the holders' next request (RB10).
"""
from datetime import datetime

from sqlalchemy import func

from app import cache, db
from app.api.alpha.views import user_view
from app.models import ModLog, Role, RoleGrant, RolePermission, RolePermissionDomain, SiteDomain, User, utcnow
from app.permission_cache import permissions_changed
from app.permissions import BY_NAME, INSTANCE_ONLY_PERMISSIONS, STAFF_PERMISSIONS, STAFF_SCOPABLE_PERMISSIONS, \
    home_domain_id
from app.storefront import domain_rows
from app.utils import add_to_modlog, set_setting

NAME_LENGTH = 50
RESERVED_NAME = 'admin'   # RB16: no role but the built-in one may be called this, in any case


def seed_default_roles() -> dict[str, Role]:
    """Add Admin and Staff to the session, and return them by name. The caller commits. Staff's scopable permissions
    reach the default domain alone; with no such row yet they reach nowhere, rather than everywhere."""
    admin = Role(name='Admin', built_in=True)
    staff = Role(name='Staff', listed_publicly=True)
    first = db.session.query(SiteDomain.id).filter(SiteDomain.is_default.is_(True)).scalar()
    for permission in STAFF_PERMISSIONS:
        scoped = permission in STAFF_SCOPABLE_PERMISSIONS
        row = RolePermission(permission=permission, all_domains=not scoped)
        if scoped and first is not None:
            row.domains.append(RolePermissionDomain(permission=permission, site_domain_id=first))
        staff.permissions.append(row)
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

def holder_ids(role: Role) -> list[int]:
    return list(db.session.scalars(db.select(RoleGrant.user_id).where(RoleGrant.role_id == role.id).distinct()))


def permission_scopes(role: Role) -> dict[str, set[int] | None]:
    """What the role holds: each permission, with the domains it is limited to, or None for every domain."""
    return {row.permission: None if row.all_domains else {d.site_domain_id for d in row.domains}
            for row in role.permissions}


def describe_scopes(scopes: dict[str, set[int] | None]) -> str:
    """For the modlog: each permission, and its domains when it is limited to some."""
    names = {row.id: row.domain for row in domain_rows()}
    return ', '.join(permission if domains is None else
                     f"{permission} ({', '.join(sorted(names.get(d, f'#{d}') for d in domains))})"
                     for permission, domains in sorted(scopes.items()))


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


def _clean_scopes(permissions) -> dict[str, set[int] | None]:
    """`permissions` as {permission: domain ids, or None for every domain}; a plain list means every domain. RB7: an
    instance-only permission cannot be limited. A limited one names at least one domain, and only domains that exist."""
    scopes = dict(permissions) if isinstance(permissions, dict) else dict.fromkeys(permissions)
    unknown = sorted(permission for permission in scopes if permission not in BY_NAME)
    if unknown:
        raise RoleError(f'There is no such permission: {", ".join(unknown)}.')
    known = {row.id for row in domain_rows()}
    for permission, domains in scopes.items():
        if domains is None:
            continue
        if permission in INSTANCE_ONLY_PERMISSIONS:
            raise RoleError(f'{permission} applies to the whole instance and cannot be limited to domains.')
        if not domains:
            raise RoleError(f'Choose at least one domain for {permission}, or all of them.')
        if not set(domains) <= known:
            raise RoleError('There is no such domain.')
    return scopes


def _write_permissions(role: Role, scopes: dict[str, set[int] | None]) -> None:
    for permission, domains in sorted(scopes.items()):
        row = RolePermission(permission=permission, all_domains=domains is None)
        for domain_id in sorted(domains or ()):
            row.domains.append(RolePermissionDomain(permission=permission, site_domain_id=domain_id))
        role.permissions.append(row)


def _clean_expiry(expires_at: datetime | None) -> datetime | None:
    if expires_at is not None and expires_at <= utcnow():
        raise RoleError('An expiry must be in the future.')
    return expires_at


def _admins_changed(role: Role) -> None:
    """The `admin_ids` setting lists Admin's holders for every request (app/request_hooks.py); emptied, the next
    request reads them afresh (RB10)."""
    if role.built_in:
        set_setting('admin_ids', None)


def _shown_changed() -> None:
    """user_view is cached for ten minutes, with its `is_admin` in it: emptied, so the next request sees the change (RB15)."""
    cache.delete_memoized(user_view)


def _log(action: str, actor: User, *, role_name: str, role: Role | None = None, target: User | None = None,
         reason: str = '') -> None:
    if target is not None:
        link, link_text = f'u/{target.link()}', target.display_name()
    else:
        link, link_text = (f'admin/roles/{role.id}' if role is not None else ''), role_name
    add_to_modlog(action, actor=actor, target_user=target, reason=reason, link=link, link_text=link_text)


# --------------------------------------------------------------------------
# Roles
# --------------------------------------------------------------------------

def create_role(actor: User, name: str, description: str, listed_publicly: bool, permissions) -> Role:
    _must_be_admin(actor)
    scopes = _clean_scopes(permissions)
    role = Role(name=_clean_name(name), description=(description or '').strip()[:512],
                listed_publicly=bool(listed_publicly))
    _write_permissions(role, scopes)
    db.session.add(role)
    db.session.commit()
    _log('create_role', actor, role_name=role.name, role=role, reason=describe_scopes(scopes))
    return role


def update_role(actor: User, role: Role, name: str, description: str, listed_publicly: bool, permissions) -> Role:
    """Replace the role's name, description, listing and permissions, each with the domains it reaches."""
    _must_be_admin(actor)
    if role.built_in:
        raise RoleError('The built-in Admin role holds every permission and cannot be edited.')
    name = _clean_name(name, role)
    scopes = _clean_scopes(permissions)
    role.permissions.clear()
    db.session.flush()   # the old rows go before rows with the same keys arrive
    role.name, role.description, role.listed_publicly = name, (description or '').strip()[:512], bool(listed_publicly)
    _write_permissions(role, scopes)
    db.session.commit()
    permissions_changed(*holder_ids(role))
    _shown_changed()
    _log('edit_role', actor, role_name=role.name, role=role, reason=describe_scopes(scopes))
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
    name, revoked = role.name, [holders[grant.user_id] for grant in grants]
    for grant in grants:
        db.session.delete(grant)
    db.session.flush()   # before the role: nothing tells the session the grants depend on it
    role.permissions.clear()
    db.session.flush()
    db.session.delete(role)
    db.session.commit()
    permissions_changed(*holders)
    _shown_changed()
    for user in revoked:
        _log('revoke_role', actor, role_name=name, target=user, reason=f'{name} (role deleted)')
    _log('delete_role', actor, role_name=name)


# --------------------------------------------------------------------------
# Grants
# --------------------------------------------------------------------------

def grant_role(actor: User, user: User, role: Role, expires_at: datetime | None = None) -> RoleGrant:
    _must_be_admin(actor)
    if user is None or not user.is_local():
        raise RoleError('Roles are granted to local accounts only.')
    expires_at = _clean_expiry(expires_at)
    if RoleGrant.query.filter_by(user_id=user.id, role_id=role.id).first():
        raise RoleError(f'{user.user_name} already holds {role.name}.')
    grant = RoleGrant(user_id=user.id, role_id=role.id, granted_by=actor.id, expires_at=expires_at)
    db.session.add(grant)
    db.session.commit()
    permissions_changed(user.id)
    _admins_changed(role)
    _shown_changed()
    _log('grant_role', actor, role_name=role.name, target=user,
         reason=role.name + (f', until {expires_at:%Y-%m-%d %H:%M} UTC' if expires_at else ''))
    return grant


def change_expiry(actor: User, grant: RoleGrant, expires_at: datetime | None) -> RoleGrant:
    _must_be_admin(actor)
    grant.expires_at = _clean_expiry(expires_at)
    db.session.commit()
    permissions_changed(grant.user_id)
    role, user = db.session.get(Role, grant.role_id), db.session.get(User, grant.user_id)
    _admins_changed(role)
    _shown_changed()
    _log('change_grant_expiry', actor, role_name=role.name, target=user,
         reason=f'{role.name}: ' + (f'until {expires_at:%Y-%m-%d %H:%M} UTC' if expires_at else 'no expiry'))
    return grant


def revoke_grant(actor: User, grant: RoleGrant) -> None:
    """RB2: an admin may not revoke their own Admin grant, against accidents; another admin, or user 1, may."""
    _must_be_admin(actor)
    role, user = db.session.get(Role, grant.role_id), db.session.get(User, grant.user_id)
    if role.built_in and grant.user_id == actor.id and actor.id != 1:
        raise RoleError('You cannot revoke your own Admin grant; another admin can.')
    db.session.delete(grant)
    db.session.commit()
    permissions_changed(user.id)
    _admins_changed(role)
    _shown_changed()
    _log('revoke_role', actor, role_name=role.name, target=user, reason=role.name)


def remove_expired_grants() -> int:
    """RB11: delete the grants past their expiry, each with a modlog entry; `flask send-queue` runs this. The checks
    already ignore them. No admin did it, so the entry names none. Answers how many went."""
    expired = RoleGrant.query.filter(RoleGrant.expires_at <= utcnow()).order_by(RoleGrant.id).all()
    if not expired:
        return 0
    holders, built_in = set(), False
    for grant in expired:
        role, user = db.session.get(Role, grant.role_id), db.session.get(User, grant.user_id)
        db.session.add(ModLog(type='admin', action='grant_expired', target_user_id=user.id, reason=role.name,
                              link=f'u/{user.link()}', link_text=user.display_name(), public=False,
                              site_domain_id=home_domain_id(user)))
        holders.add(user.id)
        built_in = built_in or role.built_in
        db.session.delete(grant)
    db.session.commit()
    permissions_changed(*holders)
    _shown_changed()
    if built_in:
        set_setting('admin_ids', None)
    return len(expired)

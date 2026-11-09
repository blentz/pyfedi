"""The admin scopes of an app's token (owner ruling 2026-11-09): Mastodon's `admin:read` and `admin:write` and their
parts map to one RBAC role, which an admin chooses on the misc settings page (`oauth_admin_role_id`; the role named
Staff until one is chosen). A person is granted them only while holding that role (Admin holds every role's
permissions); asked by anyone else, they are left out of the token. What they let a token do is never more than the
role's permissions that the person holds themselves (`admin_permissions`), which any admin API route asks."""
from sqlalchemy import func

from app import db
from app.models import Role, RoleGrant, User, unexpired_grant
from app.oauth_scopes import covers, is_admin
from app.permissions import CATALOGUE, user_access
from app.utils import get_setting

SETTING = 'oauth_admin_role_id'
DEFAULT_ROLE = 'staff'


def admin_role() -> Role | None:
    """The role the admin scopes map to: the one chosen, else the role named Staff, else none (nobody gets them)."""
    chosen = get_setting(SETTING)
    role = db.session.get(Role, int(chosen)) if str(chosen or '').isdigit() else None
    return role or Role.query.filter(func.lower(Role.name) == DEFAULT_ROLE).first()


def _holds(user: User, role: Role) -> bool:
    return db.session.query(RoleGrant.id).filter(RoleGrant.user_id == user.id, RoleGrant.role_id == role.id,
                                                 unexpired_grant(RoleGrant.__table__)).first() is not None


def may_hold(user: User | None) -> bool:
    """Whether `user` may be granted the admin scopes: they hold the mapped role, or Admin."""
    role = admin_role()
    if user is None or role is None:
        return False
    admin = Role.admin()
    return _holds(user, role) or (admin is not None and _holds(user, admin))


def granted(scope: str, user: User | None) -> str:
    """`scope` as `user` may be granted it: the admin scopes left out unless they may hold them."""
    names = scope.split()
    if may_hold(user):
        return scope
    return ' '.join(name for name in names if not is_admin(name))


def _role_permissions(role: Role) -> set[str]:
    if role.built_in:   # Admin holds every permission implicitly (RB2)
        return {permission.name for permission in CATALOGUE}
    return {held.permission for held in role.permissions}


def admin_permissions(token_scope: str, user: User | None, writing: bool) -> set[str]:
    """What a token may do through an admin API, reading or writing: the mapped role's permissions the person holds
    themselves, if the token holds `admin:write` (or `admin:read` to read), and nothing otherwise."""
    if user is None or not covers(token_scope, 'admin:write' if writing else 'admin:read') or not may_hold(user):
        return set()
    role = admin_role()
    return {name for name in _role_permissions(role) if user_access(name, user.id, anywhere=True)}

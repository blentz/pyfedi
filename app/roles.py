"""The roles a new instance starts with.

`flask init-db` creates them, and the RBAC parity test (tests/test_rbac_parity.py) seeds the same ones, so that test
measures what a freshly installed instance's Staff and Admin may do.
"""
from app import db
from app.constants import ROLE_ADMIN_NAME, ROLE_STAFF_NAME
from app.models import Role, RolePermission

# These create rows in the 'role' table with ids 1, 2, 3 and 4, in this order. Some constants (ROLE_*) in
# constants.py rely on those ids.
DEFAULT_ROLES = (
    ('Anonymous user', 0, ()),
    ('Authenticated user', 1, ()),
    (ROLE_STAFF_NAME, 2, ('approve registrations', 'ban users', 'administer all communities', 'manage users')),
    (ROLE_ADMIN_NAME, 3, ('approve registrations', 'change user roles', 'ban users', 'manage users',
                          'change instance settings', 'administer all communities', 'administer all users',
                          'edit cms pages')),
)


def seed_default_roles() -> dict[str, Role]:
    """Add the default roles to the session, and return them by name. The caller commits."""
    roles = {}
    for name, weight, permissions in DEFAULT_ROLES:
        role = Role(name=name, weight=weight)
        for permission in permissions:
            role.permissions.append(RolePermission(permission=permission))
        db.session.add(role)
        roles[name] = role
    return roles

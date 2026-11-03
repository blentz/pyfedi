"""The roles a new instance starts with.

`flask init-db` creates them, and the RBAC parity table (tests/rbac_parity.py) seeds the same ones, so that table
measures what a freshly installed instance's Staff and Admin may do.

Admin is the one built-in role: it holds every permission and is found by `Role.built_in`, never by its name
(spec 2026-11-03-rbac-design.md RB2, RB16). Staff is seeded, not built in: from then on it is an ordinary role an
admin may edit, rename or delete.
"""
from app import db
from app.constants import ROLE_STAFF_NAME
from app.models import Role, RolePermission

STAFF_PERMISSIONS = ('approve registrations', 'ban users', 'administer all communities', 'manage users')


def seed_default_roles() -> dict[str, Role]:
    """Add Admin and Staff to the session, and return them by name. The caller commits."""
    admin = Role(name='Admin', built_in=True)
    staff = Role(name=ROLE_STAFF_NAME, listed_publicly=True)
    for permission in STAFF_PERMISSIONS:
        staff.permissions.append(RolePermission(permission=permission))
    db.session.add_all([admin, staff])
    return {admin.name: admin, staff.name: staff}

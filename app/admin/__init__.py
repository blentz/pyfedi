from flask import Blueprint

bp = Blueprint('admin', __name__)

from app.admin import routes
from app.admin import roles_routes  # noqa: E402,F401  /admin/roles (RBAC, RB17)
from app.admin import payments_routes  # noqa: E402,F401  /admin/payments (monetization)
from app.admin import sidebar_routes  # noqa: E402,F401  /admin/sidebar (the sidebars' boxes)
from app.discovery import admin_views as discovery_admin_views  # noqa: E402,F401  fork (interop D24): /admin/federation/discovery
from app.relays import admin_views as relay_admin_views  # noqa: E402,F401  fork: /admin/federation/relays

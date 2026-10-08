from flask import Blueprint

bp = Blueprint('admin', __name__)

from app.admin import routes
from app.discovery import admin_views as discovery_admin_views  # noqa: E402,F401  fork (interop D24): /admin/federation/discovery
from app.relays import admin_views as relay_admin_views  # noqa: E402,F401  fork: /admin/federation/relays

"""OAuth 2 authorization server for client apps (plan docs/superpowers/plans/2026-11-07-api-completeness.md, W0.2)."""
from flask import Blueprint

bp = Blueprint('oauth_server', __name__)

from app.oauth_server import routes  # noqa: E402,F401  cycle: the routes register themselves on bp
from app.oauth_server.server import init_app  # noqa: E402  cycle: the server reads the models, which need this package

bp.record_once(lambda state: init_app(state.app))

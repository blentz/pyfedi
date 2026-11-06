"""How an instance takes money (spec docs/superpowers/specs/2026-11-05-monetization-infrastructure-design.md)."""
from flask import Blueprint

bp = Blueprint('payments', __name__)

from app.payments import routes  # noqa: E402,F401  cycle: the routes register themselves on bp

"""Lemmy's REST API, /api/v3, for Jerboa (plan docs/superpowers/plans/2026-11-07-api-completeness.md, W3; spec
docs/superpowers/specs/2026-11-10-lemmy-v3-design.md): an adapter over /api/alpha's handlers, which began as
Lemmy's."""
from flask import Blueprint

bp = Blueprint('lemmy', __name__)

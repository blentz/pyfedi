"""PeerTube's REST API, read-only, for NewPipe (plan docs/superpowers/plans/2026-11-07-api-completeness.md, W2.1;
spec docs/superpowers/specs/2026-11-09-peertube-read-api-design.md)."""
from flask import Blueprint

bp = Blueprint('peertube', __name__)

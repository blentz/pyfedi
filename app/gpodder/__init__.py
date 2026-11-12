"""gpodder.net's sync API, /api/2, for podcast apps (plan docs/superpowers/plans/2026-11-07-api-completeness.md, W4;
spec docs/superpowers/specs/2026-11-12-gpodder-sync-design.md): a person's devices, podcast subscriptions and episode
actions, kept in step across their apps."""
from flask import Blueprint

bp = Blueprint('gpodder', __name__)

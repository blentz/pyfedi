"""Mastodon's client API, for Mastodon and Pixelfed apps (plan docs/superpowers/plans/2026-11-07-api-completeness.md,
W1; spec docs/superpowers/specs/2026-11-07-mastodon-client-api-design.md)."""
from flask import Blueprint

bp = Blueprint('mastodon', __name__)

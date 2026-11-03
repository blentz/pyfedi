"""The per-user version every cached permission answer is keyed under (spec 2026-11-03-rbac-design.md RB10).

Bumped by any change to a user's grants, or to a role they hold, so the next request on every worker computes the
answer afresh. Its own module, on the cache alone, so that app.models (User.is_admin) and app.permissions can both
use it without importing each other.
"""
from app import cache

_VERSION_KEY = 'permission_version:{}'


def permission_version(user_id: int) -> int:
    return cache.get(_VERSION_KEY.format(user_id)) or 0


def permissions_changed(*user_ids: int) -> None:
    """Call after any change to these users' grants, or to a role they hold: their cached answers stop being read."""
    for user_id in user_ids:
        cache.set(_VERSION_KEY.format(user_id), permission_version(user_id) + 1, timeout=0)

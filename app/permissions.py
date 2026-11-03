"""Who may do what, and on which domain (spec docs/superpowers/specs/2026-11-03-rbac-design.md, Check API).

A user passes a permission check when they are user 1, hold the built-in Admin role, hold an unexpired instance-wide
grant of a role carrying the permission, or -- for a scopable permission asked about an object -- hold an unexpired
grant carrying it on the object's home domain.

`on=None` asks about the whole instance: only instance-wide holders pass. That is what every check written before
RBAC asks, so a check not yet converted to name its object can only refuse a domain-scoped holder, never let one
through. A menu asking "does this user hold it anywhere" says so with `anywhere=True`; nothing that authorises an
action may.

Answers are memoized per (permission, user, domain) under a per-user version that every change to the user's grants,
or to a role they hold, bumps (RB10), so a revocation is seen on the next request on every worker.
"""
from sqlalchemy import text

from app import cache, db
from app.models import Community, Feed, Post, PostReply, Report, SiteDomain, User, UserRegistration
from app.permission_cache import permission_version, permissions_changed  # noqa: F401 (re-exported)

# The legacy permissions a grant on one domain may carry (RB7). Everything else is instance-only.
SCOPABLE_PERMISSIONS = frozenset({
    'administer all communities',
    'administer all users',
    'approve registrations',
    'ban users',
    'manage users',
})

INSTANCE_ONLY_PERMISSIONS = frozenset({
    'change instance settings',
    'change user roles',
    'edit cms pages',
})

REPORT_ON_USER = 0


def is_scopable(permission: str) -> bool:
    return permission in SCOPABLE_PERMISSIONS


def home_domain_id(on) -> int | None:
    """The `site_domain.id` an object belongs to, or None when it belongs to no domain of ours: remote rows, and the
    kinds of object that are the whole instance's (conversations, instances, topics, link domains, tags, files)."""
    if isinstance(on, SiteDomain):
        return on.id
    if isinstance(on, (Community, User, Feed)):
        return on.site_domain_id
    if isinstance(on, (Post, PostReply)):
        community = db.session.get(Community, on.community_id) if on.community_id else None
        return community.site_domain_id if community else None
    if isinstance(on, UserRegistration):
        user = db.session.get(User, on.user_id) if on.user_id else None
        return user.site_domain_id if user else None
    if isinstance(on, Report):
        if on.type == REPORT_ON_USER:
            user = db.session.get(User, on.suspect_user_id) if on.suspect_user_id else None
            return user.site_domain_id if user else None
        community = db.session.get(Community, on.in_community_id) if on.in_community_id else None
        return community.site_domain_id if community else None
    return None


def user_access(permission: str, user_id: int, on=None, anywhere: bool = False) -> bool:
    if not user_id:
        return False
    if user_id == 1:
        return True
    if anywhere:
        scope = 'anywhere'
    elif on is not None and is_scopable(permission):
        domain_id = home_domain_id(on)
        scope = domain_id if domain_id is not None else 'instance'
    else:
        scope = 'instance'
    return _access(permission, user_id, scope, permission_version(user_id))


@cache.memoize(timeout=50)
def _access(permission: str, user_id: int, scope, version: int) -> bool:
    """`scope` is 'instance' (instance-wide grants only), 'anywhere' (a grant on any domain will do) or a domain id
    (an instance-wide grant, or one on that domain). `version` only keys the cache."""
    if scope == 'instance':
        scope_clause = 'rg.site_domain_id IS NULL'
    elif scope == 'anywhere':
        scope_clause = 'TRUE'
    else:
        scope_clause = '(rg.site_domain_id IS NULL OR rg.site_domain_id = :domain_id)'
    row = db.session.execute(text(
        'SELECT 1 FROM role_grant rg JOIN role r ON r.id = rg.role_id '
        f'WHERE rg.user_id = :user_id AND {scope_clause} '
        "AND (rg.expires_at IS NULL OR rg.expires_at > timezone('utc', now())) "
        # the built-in Admin role holds everything, but only instance-wide: it is never scoped (RB2)
        'AND ((r.built_in AND rg.site_domain_id IS NULL) OR EXISTS (SELECT 1 FROM role_permission rp '
        '                          WHERE rp.role_id = r.id AND rp.permission = :permission)) LIMIT 1'),
        {'user_id': user_id, 'permission': permission,
         'domain_id': scope if isinstance(scope, int) else None}).first()
    return row is not None


def holds_any_grant(user_id: int) -> bool:
    """Whether a user holds any unexpired grant, of any role, on any scope -- or is user 1."""
    if user_id == 1:
        return True
    return db.session.execute(text(
        'SELECT 1 FROM role_grant WHERE user_id = :user_id '
        "AND (expires_at IS NULL OR expires_at > timezone('utc', now())) LIMIT 1"),
        {'user_id': user_id}).first() is not None


def may_act_on_user(actor: User, target: User, permission: str) -> bool:
    """RB6. The actor holds `permission` on the target's home domain; and an actor who holds it only through a grant
    on a domain may not act on anyone who holds any grant at all, nor on user 1 -- only an instance-wide holder may.
    A remote target belongs to no domain of ours, so only an instance-wide holder may act on one (RB4)."""
    if actor is None or target is None or not actor.id:
        return False
    if user_access(permission, actor.id):   # instance-wide, the built-in Admin, or user 1
        return True
    if not user_access(permission, actor.id, on=target):
        return False
    return not holds_any_grant(target.id)

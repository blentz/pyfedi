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
from typing import NamedTuple

from flask import g
from sqlalchemy import and_, false, or_, text, true

from app import cache, db
from app.permission_cache import permission_version, permissions_changed  # noqa: F401 (re-exported)



class Permission(NamedTuple):
    name: str
    group: str        # the role editor groups its checklist by this
    scopable: bool    # whether a grant on one domain may carry it (RB7)
    staff: bool       # whether the seeded Staff role holds it, which keeps what Staff can do today (RB2)
    guards: str       # what it lets its holder do, in the role editor's words


# The permission catalogue (spec, Permission catalogue). Each permission guards code paths, so the list is code, not
# data (RB15). Admin holds every one of them implicitly and appears in no row of role_permission.
#
# It is a constant and not `SELECT DISTINCT permission FROM role_permission`, which is where the permissions page
# used to get it: unticking every box for a permission deleted the last row naming it, so it vanished from the page
# and could never be granted again (D937). A permission nobody holds is still a permission.
# tests/test_role_permissions.py fails if this drifts from the strings the code checks.
CATALOGUE = (
    # site
    Permission('change instance settings', 'site', False, False,
               'Site profile, misc settings, the instance chooser, federation, activities, emoji, moving a community'),
    Permission('masquerade', 'site', False, False, 'Log in as another user'),
    Permission('send newsletter', 'site', False, False, 'Write and send the newsletter'),
    Permission('edit cms pages', 'site', False, False, 'Create, edit and delete the CMS pages'),
    # domains
    Permission('manage domains', 'domains', False, False, 'Add and delete domains, and whether each federates'),
    Permission('edit domain branding', 'domains', True, False,
               "A domain's name, description, logos and registration mode"),
    # users
    Permission('approve registrations', 'users', True, True, 'Approve and refuse registration applications'),
    Permission('administer all users', 'users', True, True,
               'The user list, editing and deleting accounts, adding users, referrals, reports on users'),
    Permission('ban users', 'users', True, True, 'Ban and unban accounts'),
    Permission('manage users', 'users', True, True, 'Delete or purge a profile, and the profile admin panel'),
    Permission('view user details', 'users', True, True,
               "Accounts sharing an IP address, a user's subscriptions and moderated communities, deleted items on a "
               'profile'),
    Permission('generate referral codes', 'users', False, True,
               "Generate referral codes when the setting is \"Admins and holders of 'generate referral codes'\""),
    # communities
    Permission('create local communities', 'communities', True, False,
               'Create local communities when only admins may'),
    Permission('add remote communities', 'communities', False, True,
               'Add remote communities when ordinary users may not'),
    Permission('moderate all communities', 'communities', True, True,
               "Everything a community's moderators may do, in every community: bans, moderators, flair, approvals, "
               'removing and restoring content, seeing deleted content'),
    Permission('administer all communities', 'communities', True, False,
               "What only admins may do in a community: delete or restore it, edit its settings, its wiki and RSS "
               'feeds, kick users, edit and purge posts'),
    Permission('administer communities pages', 'communities', True, True,
               'The admin pages listing and editing communities and their content'),
    Permission('manage topics', 'communities', False, True, 'Create, edit, import and delete topics'),
    # content
    Permission('feature instance-wide', 'content', False, False, 'Pin a post to the whole instance'),
    Permission('manage media', 'content', False, True, "The media list, and deleting a file or all of a user's"),
    Permission('view modlog', 'content', True, True, 'The private entries in the modlog'),
    # federation
    Permission('manage remote instances', 'federation', False, True,
               'Edit and contact remote instances, create offline ones, ban lists'),
    # blocklists
    Permission('manage blocklists', 'blocklists', False, True, 'Blocked images, banned link domains and banned tags'),
    # analytics
    Permission('view vote analytics', 'analytics', False, True,
               'Votes, serial downvoters, posting patterns, and who upvoted what'),
    # private messages
    Permission('read private messages', 'private messages', False, False,
               "Read and act on other people's conversations, and the reports on them"),
    # feeds
    Permission('manage instance feeds', 'feeds', False, False,
               "Instance feeds, copying feeds, editing and seeing others' feeds, private ones included"),
    # exemptions
    Permission('exempt from upload limits', 'exemptions', False, True,
               'Upload when only admins may, and send private messages past their restrictions'),
    Permission('exempt from account limits', 'exemptions', False, False,
               'No referral caps or new-account limits, and always trusted'),
    # display
    Permission('shown as admin', 'display', True, False,
               "Shown among the domain's admins on its own host. Gives no power by itself"),
)

BY_NAME = {permission.name: permission for permission in CATALOGUE}
ROLE_PERMISSIONS = tuple(permission.name for permission in CATALOGUE)
SCOPABLE_PERMISSIONS = frozenset(p.name for p in CATALOGUE if p.scopable)
INSTANCE_ONLY_PERMISSIONS = frozenset(p.name for p in CATALOGUE if not p.scopable)
STAFF_PERMISSIONS = tuple(p.name for p in CATALOGUE if p.staff)
STAFF_SCOPABLE_PERMISSIONS = frozenset(p.name for p in CATALOGUE if p.staff and p.scopable)   # on the first domain alone

REPORT_ON_USER = 0


def is_scopable(permission: str) -> bool:
    return permission in SCOPABLE_PERMISSIONS


def default_domain_id() -> int | None:
    """The default domain's row, the first domain. It owns the local rows no domain was stamped on (storefront.on_domain)."""
    return db.session.execute(text('SELECT id FROM site_domain WHERE is_default')).scalar()


def _domain_of(table: str, row_id) -> int | None:
    if not row_id:
        return None
    row = db.session.execute(text(f'SELECT site_domain_id, ap_id FROM "{table}" WHERE id = :id'), {'id': row_id}).first()
    if row is None:
        return None
    return row.site_domain_id if row.site_domain_id is not None or row.ap_id is not None else default_domain_id()


def home_domain_id(on) -> int | None:
    """The `site_domain.id` an object belongs to, or None when it belongs to no domain of ours: remote rows, and the
    kinds of object that are the whole instance's (conversations, instances, topics, link domains, tags, files).

    By table name, not by class, so that app.models may import this module."""
    if type(on).__name__ == 'DomainInfo':   # app.storefront's view of a site_domain row (None: the default's own)
        return on.id
    table = getattr(on, '__tablename__', None)
    if table == 'site_domain':
        return on.id
    if table in ('community', 'user', 'feed'):
        if on.site_domain_id is None and on.ap_id is None:   # local, unstamped: the default domain's
            return default_domain_id()
        return on.site_domain_id
    if table in ('post', 'post_reply'):
        return _domain_of('community', on.community_id)
    if table == 'user_registration':
        return _domain_of('user', on.user_id)
    if table == 'report':
        if on.type == REPORT_ON_USER:
            return _domain_of('user', on.suspect_user_id)
        return _domain_of('community', on.in_community_id)
    return None


def here():
    """The domain this request is served on: what a check asks about when the object acted on is the domain itself,
    such as where a new community will be homed."""
    return getattr(g, 'storefront', None)


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
    """`scope` is 'instance' (the permission reaches every domain), 'anywhere' (it reaches some domain) or a domain id
    (it reaches every domain, or that one). `version` only keys the cache."""
    if scope == 'instance':
        scope_clause = 'rp.all_domains'
    elif scope == 'anywhere':
        scope_clause = ('(rp.all_domains OR EXISTS (SELECT 1 FROM role_permission_domain d WHERE d.role_id = rp.role_id '
                        'AND d.permission = rp.permission))')
    else:
        scope_clause = ('(rp.all_domains OR EXISTS (SELECT 1 FROM role_permission_domain d WHERE d.role_id = rp.role_id '
                        'AND d.permission = rp.permission AND d.site_domain_id = :domain_id))')
    row = db.session.execute(text(
        'SELECT 1 FROM role_grant rg JOIN role r ON r.id = rg.role_id '
        'WHERE rg.user_id = :user_id '
        "AND (rg.expires_at IS NULL OR rg.expires_at > timezone('utc', now())) "
        # the built-in Admin role holds everything, everywhere (RB2)
        'AND (r.built_in OR EXISTS (SELECT 1 FROM role_permission rp '
        f'                          WHERE rp.role_id = r.id AND rp.permission = :permission AND {scope_clause})) LIMIT 1'),
        {'user_id': user_id, 'permission': permission,
         'domain_id': scope if isinstance(scope, int) else None}).first()
    return row is not None


def domains_for(permission: str, user_id: int) -> frozenset[int] | None:
    """The domains a list is filtered to for this user (spec, What scoped holders see): None when they hold
    `permission` instance-wide (or are Admin, or user 1), so nothing is filtered out; otherwise the domains they hold it
    on through an unexpired grant, which is empty for someone who holds it nowhere."""
    if not user_id:
        return frozenset()
    if user_access(permission, user_id):
        return None
    if not is_scopable(permission):
        return frozenset()
    return _domains(permission, user_id, permission_version(user_id))


@cache.memoize(timeout=50)
def _domains(permission: str, user_id: int, version: int) -> frozenset[int]:
    return frozenset(db.session.execute(text(
        'SELECT DISTINCT d.site_domain_id FROM role_grant rg '
        'JOIN role_permission_domain d ON d.role_id = rg.role_id AND d.permission = :permission '
        'WHERE rg.user_id = :user_id '
        "AND (rg.expires_at IS NULL OR rg.expires_at > timezone('utc', now()))"),
        {'user_id': user_id, 'permission': permission}).scalars())


def domain_clause(column, domains: frozenset[int] | None):
    """`column` (a `site_domain_id`) within `domains`, as `domains_for` gives them: anything for None, nothing for an
    empty set. Remote rows (NULL) are only in an unfiltered list; local rows no domain was stamped on are the default
    domain's (storefront.on_domain)."""
    if domains is None:
        return true()
    if not domains:
        return false()
    model = getattr(column, 'class_', None)
    if hasattr(model, 'ap_id') and default_domain_id() in domains:
        return or_(column.in_(sorted(domains)), and_(column.is_(None), model.ap_id.is_(None)))
    return column.in_(sorted(domains))


def recipient_ids(permission: str, on=None) -> list[int]:
    """Who is told of something `permission` deals with (spec 2026-11-03-rbac-design.md, Notifications): the admins
    (user 1 and the built-in role's holders), every instance-wide holder of `permission`, and, when it is scopable and
    `on` belongs to a domain, its holders on that domain. Active accounts only, by id."""
    domain_id = home_domain_id(on) if on is not None and is_scopable(permission) else None
    # The admins as Site.admins() has them: the request's `g.admin_ids` when it has one, else user 1 and the built-in
    # role's holders, read here
    request_admins = getattr(g, 'admin_ids', None) if g else None
    admins_clause = 'FALSE' if request_admins is not None else \
        '(u.id = 1 OR EXISTS (SELECT 1 FROM role_grant ag JOIN role ar ON ar.id = ag.role_id ' \
        "WHERE ag.user_id = u.id AND ar.built_in " \
        "AND (ag.expires_at IS NULL OR ag.expires_at > timezone('utc', now()))))"
    held = set(db.session.execute(text(
        f'SELECT u.id FROM "user" u WHERE u.deleted = false AND u.banned = false AND ({admins_clause} OR EXISTS ('
        '  SELECT 1 FROM role_grant rg WHERE rg.user_id = u.id '
        "  AND (rg.expires_at IS NULL OR rg.expires_at > timezone('utc', now())) "
        '  AND EXISTS (SELECT 1 FROM role_permission rp WHERE rp.role_id = rg.role_id AND rp.permission = :permission '
        '              AND (rp.all_domains OR EXISTS (SELECT 1 FROM role_permission_domain d WHERE d.role_id = rp.role_id '
        '                   AND d.permission = rp.permission AND d.site_domain_id = :domain_id))))) ORDER BY u.id'),
        {'permission': permission, 'domain_id': domain_id}).scalars())
    return sorted(held | set(request_admins or ()))


def shown_as_admin_ids(domain_id: int | None) -> list[int]:
    """The active holders of `shown as admin` on this domain, by id (RB12). Admins hold it only implicitly, so they are
    not among them: which admins a domain names is the caller's to say (O1)."""
    return list(db.session.execute(text(
        'SELECT DISTINCT u.id FROM "user" u JOIN role_grant rg ON rg.user_id = u.id '
        "JOIN role_permission rp ON rp.role_id = rg.role_id AND rp.permission = 'shown as admin' "
        'WHERE u.deleted = false AND u.banned = false '
        "AND (rg.expires_at IS NULL OR rg.expires_at > timezone('utc', now())) "
        'AND (rp.all_domains OR EXISTS (SELECT 1 FROM role_permission_domain d WHERE d.role_id = rp.role_id '
        '     AND d.permission = rp.permission AND d.site_domain_id = :domain_id)) ORDER BY u.id'),
        {'domain_id': domain_id}).scalars())


def holds_any_grant(user_id: int) -> bool:
    """Whether a user holds an unexpired grant, on any scope, of a role that confers something -- the built-in Admin,
    or a role with at least one permission -- or is user 1. A role with no permissions makes nobody privileged."""
    if not user_id:
        return False
    if user_id == 1:
        return True
    return db.session.execute(text(
        'SELECT 1 FROM role_grant rg JOIN role r ON r.id = rg.role_id WHERE rg.user_id = :user_id '
        "AND (rg.expires_at IS NULL OR rg.expires_at > timezone('utc', now())) "
        'AND (r.built_in OR EXISTS (SELECT 1 FROM role_permission rp WHERE rp.role_id = r.id)) LIMIT 1'),
        {'user_id': user_id}).first() is not None


def may_act_on_user(actor, target, permission: str) -> bool:
    """RB6. The actor holds `permission` on the target's home domain; and an actor who holds it only through a grant
    on a domain may not act on anyone who holds any grant at all, nor on user 1 -- only an instance-wide holder may.
    A remote target belongs to no domain of ours, so only an instance-wide holder may act on one (RB4). Nobody but an
    Admin acts on an Admin (D1178)."""
    if actor is None or target is None or not actor.id:
        return False
    if target.is_admin() and not actor.is_admin():
        return False
    if user_access(permission, actor.id):   # instance-wide, the built-in Admin, or user 1
        return True
    if not user_access(permission, actor.id, on=target):
        return False
    return not holds_any_grant(target.id)


def may_ban(actor, target) -> bool:
    """An instance ban: `ban users`, or `manage users`, which may purge and so may ban (RB6 through may_act_on_user)."""
    return may_act_on_user(actor, target, 'ban users') or may_act_on_user(actor, target, 'manage users')

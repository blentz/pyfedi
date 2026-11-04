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

from sqlalchemy import text

from app import cache, db
from app.models import Community, Feed, Post, PostReply, Report, SiteDomain, User, UserRegistration
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
    Permission('administer all users', 'users', True, False,
               'The user list, editing and deleting accounts, adding users, referrals, reports on users'),
    Permission('ban users', 'users', True, True, 'Ban and unban accounts instance-wide'),
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

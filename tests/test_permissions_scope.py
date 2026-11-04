"""RBAC build step 3: permission checks against an object's home domain (app/permissions.py).

Spec docs/superpowers/specs/2026-11-03-rbac-design.md: Check API, Domain resolution, RB4, RB6, RB7, RB10.
"""
from datetime import timedelta
from unittest.mock import patch

import pytest
from cachelib import SimpleCache
from flask import g

from app import db, permission_cache
from app.models import (Conversation, Report, Role, RolePermission, SiteDomain, Topic, UserRegistration, role_grant,
                        utcnow)
from app.permissions import (INSTANCE_ONLY_PERMISSIONS, SCOPABLE_PERMISSIONS, here, holds_any_grant, home_domain_id,
                             is_scopable, may_act_on_user, may_ban, user_access)
from app.storefront import clear_domain_cache, domain_for_host
from tests.factories import (admin_role, grant_role, make_community, make_instance, make_local_feed, make_post,
                             make_post_reply, make_user)

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture
def world(db_session):
    """Two domains, X and Y. A community and a user homed on each, a remote user and a remote community."""
    local = make_instance('test.piefed.local', software='piefed')
    peer = make_instance('peer.example', software='piefed')
    founder = make_user(local, 'founder', local=True)
    assert founder.id == 1
    x = SiteDomain(domain='x.parity.example', scheme='https', federates=False)
    y = SiteDomain(domain='y.parity.example', scheme='https', federates=False)
    db.session.add_all([x, y])
    db.session.commit()
    clear_domain_cache()

    def homed(name, domain):
        user = make_user(local, name, local=True)
        user.site_domain_id = domain.id
        community = make_community(f'{name}comm')
        community.site_domain_id = domain.id
        db.session.commit()
        return user, community

    on_x, community_x = homed('onx', x)
    on_y, community_y = homed('ony', y)
    remote = make_user(peer, 'remy')
    far = make_community('far', host='peer.example')
    far.ap_id = 'far@peer.example'
    far.instance_id = peer.id
    db.session.commit()
    yield type('World', (), dict(x=x, y=y, founder=founder, on_x=on_x, on_y=on_y, community_x=community_x,
                                 community_y=community_y, remote=remote, far=far, local=local, peer=peer))
    clear_domain_cache()


def role_with(name, *permissions):
    role = Role(name=name)
    db.session.add(role)
    db.session.commit()
    for permission in permissions:
        db.session.add(RolePermission(role_id=role.id, permission=permission))
    db.session.commit()
    return role


def scoped_holder(world, name, domain, *permissions, expires_at=None):
    holder = make_user(world.local, name, local=True)
    role = role_with(f'role-{name}', *permissions)
    db.session.execute(role_grant.insert().values(user_id=holder.id, role_id=role.id, site_domain_id=domain.id,
                                                  expires_at=expires_at))
    db.session.commit()
    return holder


# --------------------------------------------------------------------------
# Domain resolution
# --------------------------------------------------------------------------

def test_each_kind_of_object_resolves_to_its_home_domain(world):
    post = make_post(world.community_x, world.on_y, 'https://test.piefed.local/post/1')
    reply = make_post_reply(post, world.remote)
    remote_post = make_post(world.far, world.remote, 'https://peer.example/p/1')
    feed = make_local_feed('xfeed', public=True)
    feed.site_domain_id = world.x.id
    registration = UserRegistration(user_id=world.on_y.id, answer='because')
    user_report = Report(type=0, suspect_user_id=world.on_y.id, reasons='r')
    content_report = Report(type=1, suspect_post_id=post.id, in_community_id=world.community_x.id, reasons='r')
    orphan_report = Report(type=1, reasons='r')
    db.session.add_all([registration, user_report, content_report, orphan_report])
    db.session.commit()

    assert home_domain_id(world.x) == world.x.id
    assert home_domain_id(world.community_x) == world.x.id
    assert home_domain_id(world.on_y) == world.y.id
    assert home_domain_id(feed) == world.x.id
    assert home_domain_id(post) == world.x.id            # its community's, whoever wrote it (RB4)
    assert home_domain_id(reply) == world.x.id
    assert home_domain_id(remote_post) is None           # a remote community belongs to no domain of ours
    assert home_domain_id(world.remote) is None
    assert home_domain_id(world.far) is None
    assert home_domain_id(registration) == world.y.id
    assert home_domain_id(user_report) == world.y.id
    assert home_domain_id(content_report) == world.x.id
    assert home_domain_id(orphan_report) is None


def test_objects_of_the_whole_instance_and_dangling_rows_resolve_to_no_domain(world):
    assert home_domain_id(Topic(name='t')) is None
    assert home_domain_id(Conversation()) is None
    assert home_domain_id(None) is None
    assert home_domain_id(UserRegistration(answer='no user')) is None
    assert home_domain_id(Report(type=0, reasons='no suspect')) is None
    assert home_domain_id(object()) is None   # anything else at all


def test_the_catalogue_splits_scopable_from_instance_only():
    assert SCOPABLE_PERMISSIONS.isdisjoint(INSTANCE_ONLY_PERMISSIONS)
    assert is_scopable('ban users') and not is_scopable('change instance settings')


# --------------------------------------------------------------------------
# user_access
# --------------------------------------------------------------------------

def test_nobody_and_user_1(world):
    assert user_access('ban users', 0) is False
    assert user_access('ban users', None) is False
    assert user_access('anything', world.founder.id, on=world.community_y) is True


def test_a_scoped_grant_answers_only_on_its_domain(world):
    holder = scoped_holder(world, 'xmod', world.x, 'administer all communities')
    assert user_access('administer all communities', holder.id, on=world.community_x) is True
    assert user_access('administer all communities', holder.id, on=world.community_y) is False
    assert user_access('administer all communities', holder.id, on=world.far) is False     # remote: instance-only
    assert user_access('ban users', holder.id, on=world.community_x) is False              # not in the role


def test_a_check_that_names_no_object_asks_about_the_whole_instance(world):
    """Fail closed: a check written before RBAC (no `on`) lets no domain-scoped holder through."""
    holder = scoped_holder(world, 'xmod', world.x, 'administer all communities')
    assert user_access('administer all communities', holder.id) is False
    assert user_access('administer all communities', holder.id, anywhere=True) is True   # menus ask this


def test_an_instance_only_permission_never_answers_for_a_scoped_grant(world):
    """RB7 refuses such a grant; should one exist anyway, it confers nothing."""
    holder = scoped_holder(world, 'xsettings', world.x, 'change instance settings')
    assert user_access('change instance settings', holder.id, on=world.x) is False


def test_an_instance_wide_grant_covers_every_domain(world):
    holder = make_user(world.local, 'everywhere', local=True)
    grant_role(holder, role_with('Moderators', 'administer all communities'))
    assert user_access('administer all communities', holder.id) is True
    assert user_access('administer all communities', holder.id, on=world.community_y) is True
    assert user_access('administer all communities', holder.id, on=world.far) is True


def test_an_expired_scoped_grant_answers_nothing(world):
    holder = scoped_holder(world, 'lapsed', world.x, 'ban users', expires_at=utcnow() - timedelta(minutes=1))
    assert user_access('ban users', holder.id, on=world.on_x) is False
    assert holds_any_grant(holder.id) is False


def test_the_built_in_role_counts_only_instance_wide(world):
    """RB2: Admin is never scoped. A stray domain grant of it confers nothing, here or anywhere."""
    holder = make_user(world.local, 'strayadmin', local=True)
    grant_role(holder, admin_role(), site_domain_id=world.x.id)
    assert user_access('ban users', holder.id, on=world.on_x) is False
    assert holder.is_admin() is False
    grant_role(holder, admin_role())
    assert user_access('ban users', holder.id, on=world.on_y) is True


# --------------------------------------------------------------------------
# RB6: may_act_on_user
# --------------------------------------------------------------------------

def test_a_scoped_holder_acts_on_a_plain_account_of_its_domain_only(world):
    holder = scoped_holder(world, 'xbanner', world.x, 'ban users')
    assert may_act_on_user(holder, world.on_x, 'ban users') is True
    assert may_act_on_user(holder, world.on_y, 'ban users') is False     # another domain's
    assert may_act_on_user(holder, world.remote, 'ban users') is False   # remote: instance-wide holders only (RB4)


def test_a_scoped_holder_never_acts_on_a_grant_holder_or_user_1(world):
    holder = scoped_holder(world, 'xbanner', world.x, 'ban users')
    peer = scoped_holder(world, 'xpeer', world.x, 'approve registrations')
    peer.site_domain_id = world.x.id
    world.founder.site_domain_id = world.x.id
    db.session.commit()
    assert may_act_on_user(holder, peer, 'ban users') is False
    assert may_act_on_user(holder, world.founder, 'ban users') is False


def test_an_instance_wide_holder_acts_on_anyone(world):
    holder = make_user(world.local, 'banner', local=True)
    grant_role(holder, role_with('Banners', 'ban users'))
    peer = scoped_holder(world, 'xpeer', world.x, 'approve registrations')
    assert may_act_on_user(holder, peer, 'ban users') is True
    assert may_act_on_user(holder, world.remote, 'ban users') is True


def test_nobody_acts_without_the_permission_or_without_a_party(world):
    assert may_act_on_user(world.on_x, world.on_y, 'ban users') is False
    assert may_act_on_user(None, world.on_y, 'ban users') is False
    assert may_act_on_user(world.on_x, None, 'ban users') is False


# --------------------------------------------------------------------------
# RB10: the cache version
# --------------------------------------------------------------------------

def test_a_change_bumps_each_users_version(world):
    with patch.object(permission_cache, 'cache', SimpleCache()):
        assert permission_cache.permission_version(world.on_x.id) == 0
        permission_cache.permissions_changed(world.on_x.id, world.on_y.id)
        permission_cache.permissions_changed(world.on_x.id)
        assert permission_cache.permission_version(world.on_x.id) == 2
        assert permission_cache.permission_version(world.on_y.id) == 1


def test_every_cached_answer_is_keyed_by_the_version(world):
    """The memoized functions take the version as an argument, so a bump makes the next call a cache miss."""
    with patch('app.permissions.permission_version', return_value=7), \
            patch('app.permissions._access', return_value=True) as access:
        user_access('ban users', world.on_x.id, on=world.on_x)
    assert access.call_args.args == ('ban users', world.on_x.id, world.x.id, 7)
    with patch('app.models.permission_version', return_value=9), \
            patch('app.models._holds_built_in_role', return_value=False) as holds:
        world.on_x.is_admin()
    assert holds.call_args.args == (world.on_x.id, 9)


# --------------------------------------------------------------------------
# Step 5: the helpers the converted call sites use
# --------------------------------------------------------------------------

def test_nobody_but_an_admin_acts_on_an_admin(world):
    """D1178, now in may_act_on_user: an instance-wide holder may act on anyone else, but not on an Admin."""
    banner = make_user(world.local, 'banner', local=True)
    grant_role(banner, role_with('Banners', 'ban users'))
    admin = make_user(world.local, 'anadmin', local=True)
    grant_role(admin, admin_role())
    other_admin = make_user(world.local, 'otheradmin', local=True)
    grant_role(other_admin, admin_role())
    assert may_act_on_user(banner, admin, 'ban users') is False
    assert may_act_on_user(other_admin, admin, 'ban users') is True


def test_may_ban_takes_either_permission(world):
    purger = make_user(world.local, 'purger', local=True)
    grant_role(purger, role_with('Purgers', 'manage users'))
    assert may_ban(purger, world.on_x) is True
    assert may_ban(world.on_y, world.on_x) is False


def test_a_role_that_confers_nothing_makes_nobody_privileged(world):
    holder = make_user(world.local, 'emptyrole', local=True)
    grant_role(holder, role_with('Empty'))
    assert holds_any_grant(holder.id) is False
    assert holds_any_grant(None) is False
    grant_role(holder, admin_role())
    assert holds_any_grant(holder.id) is True


def test_a_storefront_resolves_to_its_own_domain(app, world):
    """`g.storefront` is app.storefront's view of a site_domain row; `here()` asks about the domain served on."""
    holder = scoped_holder(world, 'xcreator', world.x, 'create local communities')
    info = domain_for_host(world.x.domain)
    assert home_domain_id(info) == world.x.id
    with app.test_request_context('/'):
        g.storefront = info
        assert user_access('create local communities', holder.id, on=here()) is True
        g.storefront = domain_for_host(world.y.domain)
        assert user_access('create local communities', holder.id, on=here()) is False

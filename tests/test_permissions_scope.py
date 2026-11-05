"""RBAC build step 3: permission checks against an object's home domain (app/permissions.py).

Spec docs/superpowers/specs/2026-11-03-rbac-design.md: Check API, Domain resolution, RB4, RB6, RB7, RB10.
"""
from datetime import timedelta
from unittest.mock import patch

import pytest
from cachelib import SimpleCache
from flask import g

from app import db, permission_cache
from app.models import (Conversation, Report, Role, RolePermission, RolePermissionDomain, SiteDomain, Topic, UserRegistration, role_grant,
                        utcnow)
from app.permissions import (INSTANCE_ONLY_PERMISSIONS, SCOPABLE_PERMISSIONS, here, holds_any_grant, home_domain_id,
                             is_scopable, may_act_on_user, may_ban, user_access)
from app.storefront import clear_domain_cache, domain_for_host
from tests.factories import (admin_role, grant_role, limit_role, make_community, make_instance, make_local_feed, make_post,
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
    role = limit_role(role_with(f'role-{name}', *permissions), domain)
    db.session.execute(role_grant.insert().values(user_id=holder.id, role_id=role.id, expires_at=expires_at))
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


def test_the_built_in_role_reaches_everything(world):
    """RB2: Admin holds every permission on every domain, with no rows of its own to limit."""
    holder = make_user(world.local, 'anadmin', local=True)
    assert user_access('ban users', holder.id, on=world.on_y) is False
    grant_role(holder, admin_role())
    assert user_access('ban users', holder.id, on=world.on_y) is True
    assert holder.is_admin() is True


def test_a_permission_limited_to_several_domains_reaches_each_and_no_other(world):
    """The owner's three scopes: one domain, a selection, or all."""
    holder = make_user(world.local, 'selected', local=True)
    third = SiteDomain(domain='z.parity.example', scheme='https', federates=False)
    db.session.add(third)
    db.session.commit()
    role = limit_role(role_with('Selected', 'administer all users'), world.x, world.y)
    grant_role(holder, role)
    assert user_access('administer all users', holder.id, on=world.x) is True
    assert user_access('administer all users', holder.id, on=world.y) is True
    assert user_access('administer all users', holder.id, on=third) is False
    assert user_access('administer all users', holder.id) is False


def test_a_role_can_mix_a_limited_permission_with_one_on_every_domain(world):
    """Why scope lives on the permission: Staff's shape, one permission narrowed, another not."""
    holder = make_user(world.local, 'mixed', local=True)
    role = role_with('Mixed', 'administer all users', 'view modlog')
    row = RolePermission.query.filter_by(role_id=role.id, permission='administer all users').one()
    row.all_domains = False
    row.domains.append(RolePermissionDomain(permission=row.permission, site_domain_id=world.x.id))
    db.session.commit()
    grant_role(holder, role)
    assert user_access('administer all users', holder.id, on=world.x) is True
    assert user_access('administer all users', holder.id, on=world.y) is False
    assert user_access('view modlog', holder.id, on=world.y) is True
    assert user_access('view modlog', holder.id) is True


def test_a_limited_permission_with_no_domain_left_reaches_nowhere(world):
    """Removing a domain can never widen a role."""
    holder = scoped_holder(world, 'emptied', world.x, 'ban users')
    RolePermissionDomain.query.delete()
    db.session.commit()
    assert user_access('ban users', holder.id, on=world.x) is False
    assert user_access('ban users', holder.id) is False
    assert user_access('ban users', holder.id, anywhere=True) is False


def test_a_local_row_no_domain_was_stamped_on_is_the_default_domains(world):
    """storefront.on_domain has them on the default domain; so do the checks and the list filter. A remote row has no
    domain, and is on no one's list."""
    from app.models import User
    from app.permissions import domain_clause
    first = SiteDomain(domain='first.parity.example', scheme='https', federates=True, is_default=True)
    db.session.add(first)
    db.session.commit()
    unstamped = make_user(world.local, 'unstamped', local=True)
    assert unstamped.site_domain_id is None and home_domain_id(unstamped) == first.id
    assert home_domain_id(world.remote) is None
    listed = {u.id for u in User.query.filter(domain_clause(User.site_domain_id, frozenset({first.id})))}
    assert unstamped.id in listed and world.remote.id not in listed and world.on_x.id not in listed


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


# --------------------------------------------------------------------------
# The API's report lists and resolutions ask about the post, comment or community they name
# --------------------------------------------------------------------------

@pytest.fixture
def reported(world):
    """A post and a comment reported in a community on each of two domains, and a moderator scoped to the first."""
    from app.models import Site
    g.site = db.session.get(Site, 1)
    g.admin_ids = []
    # X and Y are local-only, and a local-only domain's rows are hidden off its storefront: report on two that federate
    fx = SiteDomain(domain='fx.parity.example', scheme='https', federates=True)
    fy = SiteDomain(domain='fy.parity.example', scheme='https', federates=True)
    db.session.add_all([fx, fy])
    db.session.commit()
    clear_domain_cache()
    holder = scoped_holder(world, 'xmod', fx, 'moderate all communities')

    def homed(name, domain):
        community = make_community(name)
        community.site_domain_id = domain.id
        author = make_user(world.local, f'{name}author', local=True)
        author.site_domain_id = domain.id
        db.session.commit()
        return community, author

    def report_in(community, author, n):
        post = make_post(community, author, f'https://test.piefed.local/post/r{n}')
        reply = make_post_reply(post, author)
        on_post = Report(reasons='spam', type=1, reporter_id=world.founder.id, suspect_post_id=post.id,
                         suspect_user_id=author.id, in_community_id=community.id, source_instance_id=1)
        on_reply = Report(reasons='spam', type=2, reporter_id=world.founder.id, suspect_post_id=post.id,
                          suspect_post_reply_id=reply.id, suspect_user_id=author.id, in_community_id=community.id,
                          source_instance_id=1)
        db.session.add_all([on_post, on_reply])
        db.session.commit()
        return post, reply, on_post, on_reply

    (on_x, author_x), (on_y, author_y) = homed('fxcomm', fx), homed('fycomm', fy)
    return holder, on_x, on_y, report_in(on_x, author_x, 1), report_in(on_y, author_y, 2)


def bearer(user):
    return f'Bearer {user.encode_jwt_token()}'


def test_a_scoped_moderator_reads_the_post_reports_on_their_domain_only(world, reported):
    from app.api.alpha.utils.post import get_post_report_list, put_post_report_resolve
    holder, community_x, community_y, (post_x, _, report_x, _), (post_y, _, report_y, _) = reported
    by_post = get_post_report_list(bearer(holder), {'post_id': post_x.id})
    assert [r['post_report']['id'] for r in by_post['post_reports']] == [report_x.id]
    by_community = get_post_report_list(bearer(holder), {'community_id': community_x.id})
    assert [r['post_report']['id'] for r in by_community['post_reports']] == [report_x.id]
    put_post_report_resolve(bearer(holder), {'report_id': report_x.id, 'resolved': True})
    for refused in ({'post_id': post_y.id}, {'community_id': community_y.id}):
        with pytest.raises(Exception, match='incorrect login'):
            get_post_report_list(bearer(holder), refused)
    with pytest.raises(Exception, match='incorrect login'):
        put_post_report_resolve(bearer(holder), {'report_id': report_y.id, 'resolved': True})


def test_a_scoped_moderator_reads_the_comment_reports_on_their_domain_only(world, reported):
    from app.api.alpha.utils.reply import get_reply_report_list, put_reply_report_resolve
    holder, community_x, community_y, (_, reply_x, _, report_x), (_, reply_y, _, report_y) = reported
    by_reply = get_reply_report_list(bearer(holder), {'comment_id': reply_x.id})
    assert [r['comment_report']['id'] for r in by_reply['comment_reports']] == [report_x.id]
    by_community = get_reply_report_list(bearer(holder), {'community_id': community_x.id})
    assert [r['comment_report']['id'] for r in by_community['comment_reports']] == [report_x.id]
    put_reply_report_resolve(bearer(holder), {'report_id': report_x.id, 'resolved': True})
    for refused in ({'comment_id': reply_y.id}, {'community_id': community_y.id}):
        with pytest.raises(Exception, match='incorrect login'):
            get_reply_report_list(bearer(holder), refused)
    with pytest.raises(Exception, match='incorrect login'):
        put_reply_report_resolve(bearer(holder), {'report_id': report_y.id, 'resolved': True})


# --------------------------------------------------------------------------
# domains_for: which domains a list is filtered to (spec, What scoped holders see)
# --------------------------------------------------------------------------

def test_domains_for_names_the_domains_a_holder_is_granted_on(world):
    from app.permissions import domains_for
    holder = scoped_holder(world, 'xlister', world.x, 'administer all users')
    expired = limit_role(role_with('Expired lister', 'administer all users'), world.y)
    db.session.execute(role_grant.insert().values(user_id=holder.id, role_id=expired.id,
                                                  expires_at=utcnow() - timedelta(days=1)))   # not counted
    db.session.commit()
    assert domains_for('administer all users', holder.id) == frozenset({world.x.id})
    assert domains_for('approve registrations', holder.id) == frozenset()


def test_domains_for_is_everything_for_an_instance_wide_holder_or_admin_and_nothing_for_nobody(world):
    from app.permissions import domains_for
    wide = make_user(world.local, 'widelister', local=True)
    grant_role(wide, role_with('Wide', 'administer all users'))
    assert domains_for('administer all users', wide.id) is None
    assert domains_for('administer all users', world.founder.id) is None
    assert domains_for('administer all users', None) == frozenset()
    assert domains_for('administer all users', world.on_x.id) == frozenset()


def test_an_instance_only_permission_held_on_a_domain_names_no_domain(world):
    """A domain grant never carries one (RB7); were a row to say so, it would still reach nothing."""
    from app.permissions import domains_for
    holder = scoped_holder(world, 'oddlister', world.x, 'masquerade')
    assert domains_for('masquerade', holder.id) == frozenset()


def test_domain_clause_filters_a_query_to_the_domains(world):
    from app.models import User
    from app.permissions import domain_clause
    homed = User.query.filter(User.id.in_([world.on_x.id, world.on_y.id]))
    assert {u.id for u in homed.filter(domain_clause(User.site_domain_id, None))} == {world.on_x.id, world.on_y.id}
    assert [u.id for u in homed.filter(domain_clause(User.site_domain_id, frozenset({world.x.id})))] == [world.on_x.id]
    assert homed.filter(domain_clause(User.site_domain_id, frozenset())).count() == 0


# --------------------------------------------------------------------------
# recipient_ids: who is told (spec, Notifications)
# --------------------------------------------------------------------------

def test_the_admins_the_wide_holders_and_the_holders_on_its_domain_are_told(world):
    from app.permissions import recipient_ids
    admin = make_user(world.local, 'toldadmin', local=True)
    grant_role(admin, admin_role())
    wide = make_user(world.local, 'toldwide', local=True)
    grant_role(wide, role_with('Wide approvers', 'approve registrations'))
    on_x = scoped_holder(world, 'toldx', world.x, 'approve registrations')
    on_y = scoped_holder(world, 'toldy', world.y, 'approve registrations')
    other = scoped_holder(world, 'toldother', world.x, 'ban users')
    assert recipient_ids('approve registrations', on=world.on_x) == sorted([1, admin.id, wide.id, on_x.id])
    assert recipient_ids('approve registrations', on=world.on_y) == sorted([1, admin.id, wide.id, on_y.id])
    assert recipient_ids('approve registrations') == sorted([1, admin.id, wide.id])
    assert other.id not in recipient_ids('approve registrations', on=world.on_x)


def test_a_content_report_goes_to_moderate_all_communities_on_its_domain(world):
    from app.permissions import recipient_ids
    post = make_post(world.community_x, world.on_x, 'https://test.piefed.local/post/told')
    on_x = scoped_holder(world, 'modx', world.x, 'moderate all communities')
    on_y = scoped_holder(world, 'mody', world.y, 'moderate all communities')
    told = recipient_ids('moderate all communities', on=post)
    assert on_x.id in told and on_y.id not in told


def test_the_banned_the_deleted_and_the_expired_are_not_told(world):
    from app.permissions import recipient_ids
    banned = scoped_holder(world, 'bannedtold', world.x, 'approve registrations')
    banned.banned = True
    gone = scoped_holder(world, 'gonetold', world.x, 'approve registrations')
    gone.deleted = True
    expired = scoped_holder(world, 'expiredtold', world.x, 'approve registrations',
                            expires_at=utcnow() - timedelta(days=1))
    db.session.commit()
    told = recipient_ids('approve registrations', on=world.on_x)
    assert not {banned.id, gone.id, expired.id} & set(told)


def test_the_requests_admin_list_is_told_as_site_admins_reads_it(app, world):
    from app.permissions import recipient_ids
    with app.test_request_context('/'):
        g.admin_ids = [world.on_y.id]
        assert world.on_y.id in recipient_ids('approve registrations')

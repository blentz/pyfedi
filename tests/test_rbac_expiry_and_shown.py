"""RBAC build step 10: expired grants are removed (RB11), and `shown as admin` (RB12).

Spec docs/superpowers/specs/2026-11-03-rbac-design.md: API and federation, O1.
"""
from datetime import timedelta
from unittest.mock import patch

import pytest
from flask import g

from app import db
from app.models import ModLog, Role, RoleGrant, RolePermission, Site, SiteDomain, utcnow
from app.federation_urls import admins_shown_on
from app.permissions import shown_as_admin_ids, user_access
from app.roles import remove_expired_grants, update_role, grant_role as grant_through_ui, revoke_grant
from app.storefront import clear_domain_cache, domain_by_id
from app.utils import get_setting, set_setting
from tests.factories import admin_role, grant_role, limit_role, make_instance, make_user

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture
def world(app, db_session):
    local = make_instance('test.piefed.local', software='piefed')
    founder = make_user(local, 'founder', local=True)
    assert founder.id == 1
    first = SiteDomain(domain='first.shown.example', scheme='https', federates=True, is_default=True)
    x = SiteDomain(domain='x.shown.example', scheme='https', federates=True)
    y = SiteDomain(domain='y.shown.example', scheme='https', federates=True)
    db.session.add_all([first, x, y])
    db.session.commit()
    clear_domain_cache()
    holder = make_user(local, 'holder', local=True)
    yield type('World', (), dict(local=local, founder=founder, first=first, x=x, y=y, holder=holder))
    clear_domain_cache()


def role_with(name, *permissions):
    role = Role(name=name)
    db.session.add(role)
    db.session.commit()
    for permission in permissions:
        db.session.add(RolePermission(role_id=role.id, permission=permission))
    db.session.commit()
    return role


def a_grant(user, role, expires_at):
    grant = RoleGrant(user_id=user.id, role_id=role.id, expires_at=expires_at)
    db.session.add(grant)
    db.session.commit()
    return grant


# --------------------------------------------------------------------------
# RB11
# --------------------------------------------------------------------------

def test_an_expired_grant_is_removed_and_logged_and_a_live_one_kept(world):
    lapsed, kept, forever = role_with('Lapsed', 'ban users'), role_with('Kept', 'view modlog'), role_with('Always')
    a_grant(world.holder, lapsed, utcnow() - timedelta(minutes=1))
    a_grant(world.holder, kept, utcnow() + timedelta(days=1))
    a_grant(world.holder, forever, None)

    assert remove_expired_grants() == 1

    assert {g_.role_id for g_ in RoleGrant.query.all()} == {kept.id, forever.id}
    entry, = ModLog.query.filter_by(action='grant_expired').all()
    assert (entry.user_id, entry.target_user_id, entry.type, entry.public) == (None, world.holder.id, 'admin', False)
    assert entry.reason == 'Lapsed' and entry.get_correct_link().startswith('u/')


def test_with_nothing_expired_nothing_happens(world):
    a_grant(world.holder, role_with('Kept'), utcnow() + timedelta(days=1))
    assert remove_expired_grants() == 0
    assert ModLog.query.count() == 0


def test_an_expired_grant_is_ignored_before_the_task_runs(world):
    """RB11: the check itself reads `expires_at`."""
    a_grant(world.holder, role_with('Lapsed', 'ban users'), utcnow() - timedelta(minutes=1))
    assert user_access('ban users', world.holder.id) is False


def test_an_expired_admin_grant_refreshes_the_admin_list_and_tells_the_caches(world):
    set_setting('admin_ids', [1, world.holder.id])
    a_grant(world.holder, admin_role(), utcnow() - timedelta(minutes=1))
    with patch('app.roles.permissions_changed') as changed:
        remove_expired_grants()
    assert get_setting('admin_ids', None) is None
    assert [call.args for call in changed.call_args_list] == [(world.holder.id,)]


def test_the_send_queue_removes_expired_grants(app, world):
    """It runs every few minutes from cron: the project has no Celery beat."""
    from app.cli import register
    if 'send-queue' not in app.cli.commands:
        register(app)
    a_grant(world.holder, role_with('Lapsed'), utcnow() - timedelta(minutes=1))
    with patch('app.cli.app_pkg.redis_client') as redis:
        redis.memory_stats.return_value = {'total.allocated': 0}
        result = app.test_cli_runner().invoke(args=['send-queue'])
    assert result.exit_code == 0, (result.output, result.exception)
    assert RoleGrant.query.count() == 0


# --------------------------------------------------------------------------
# RB12
# --------------------------------------------------------------------------

def shown(world, name, *domains):
    role = role_with(f'Shown {name}', 'shown as admin')
    if domains:
        limit_role(role, *domains)
    user = make_user(world.local, name, local=True)
    grant_role(user, role)
    return user


def test_shown_as_admin_names_its_holders_on_their_domains(world):
    on_x, everywhere = shown(world, 'onx', world.x), shown(world, 'everywhere')
    assert shown_as_admin_ids(world.x.id) == sorted([on_x.id, everywhere.id])
    assert shown_as_admin_ids(world.y.id) == [everywhere.id]


def test_an_admin_or_an_expired_holder_is_not_a_holder_of_it(world):
    admin = make_user(world.local, 'boss', local=True)
    grant_role(admin, admin_role())
    a_grant(world.holder, role_with('Lapsed shown', 'shown as admin'), utcnow() - timedelta(minutes=1))
    assert shown_as_admin_ids(world.x.id) == []


def test_a_sister_domain_names_the_admins_homed_on_it_and_its_holders(world):
    """O1: another domain's admin would name that domain."""
    homed = make_user(world.local, 'homed', local=True)
    homed.site_domain_id = world.x.id
    elsewhere = make_user(world.local, 'elsewhere', local=True)
    elsewhere.site_domain_id = world.y.id
    db.session.commit()
    for admin in (homed, elsewhere):
        grant_role(admin, admin_role())
    on_x = shown(world, 'onx', world.x)
    named = [user.id for user in admins_shown_on(domain_by_id(world.x.id))]
    assert named == [homed.id, on_x.id]


def test_the_default_domain_names_every_admin_and_its_own_holders(world):
    other_admin = make_user(world.local, 'elsewhere', local=True)
    other_admin.site_domain_id = world.y.id
    db.session.commit()
    grant_role(other_admin, admin_role())
    on_first, on_x = shown(world, 'onfirst', world.first), shown(world, 'onx', world.x)
    named = {user.id for user in admins_shown_on(domain_by_id(world.first.id))}
    assert {1, other_admin.id, on_first.id} <= named and on_x.id not in named


def test_someone_who_is_both_is_named_once(world):
    both = shown(world, 'both', world.first)
    grant_role(both, admin_role())
    assert [user.id for user in admins_shown_on(domain_by_id(world.first.id))].count(both.id) == 1


def test_shown_as_admin_gives_no_power(world):
    on_x = shown(world, 'onx', world.x)
    assert not any(user_access(p, on_x.id, on=world.x) for p in ('ban users', 'manage users', 'administer all users'))
    assert on_x.is_admin() is False


def test_the_api_site_and_user_view_say_so_on_that_domain(app, world):
    from app.api.alpha.views import site_view, user_view
    on_x = shown(world, 'onx', world.x)
    with app.test_request_context('/', base_url='https://x.shown.example/'):
        g.site, g.admin_ids, g.storefront = db.session.get(Site, 1), [1], domain_by_id(world.x.id)
        assert user_view(on_x, variant=2)['is_admin'] is True
        assert on_x.id in [admin['person']['id'] for admin in site_view(None)['admins']]
    with app.test_request_context('/', base_url='https://y.shown.example/'):
        g.site, g.admin_ids, g.storefront = db.session.get(Site, 1), [1], domain_by_id(world.y.id)
        assert user_view(on_x, variant=2)['is_admin'] is False
        assert on_x.id not in [admin['person']['id'] for admin in site_view(None)['admins']]


def test_about_names_them_on_that_domain(app, world):
    """/about follows the same rule as the API's site admins (RB12, O1)."""
    on_x = shown(world, 'onx', world.x)
    def about(host):
        page = app.test_client().get('/about', base_url=f'https://{host}/').get_data(as_text=True)
        return next((line for line in page.splitlines() if 'administered by' in line), '')
    assert on_x.user_name in about('x.shown.example')
    assert on_x.user_name not in about('y.shown.example')


def test_a_role_change_empties_the_cached_user_views(world):
    """user_view is cached for ten minutes with its is_admin in it: RB15 wants the next request to see a change."""
    actor = make_user(world.local, 'boss', local=True)
    grant_role(actor, admin_role())
    role = role_with('Shown', 'shown as admin')
    with patch('app.roles.cache') as cache_:
        grant = grant_through_ui(actor, world.holder, role)
        update_role(actor, role, 'Shown', '', False, ['shown as admin'])
        revoke_grant(actor, grant)
    assert cache_.delete_memoized.call_count == 3

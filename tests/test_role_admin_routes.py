"""RBAC build step 7: the /admin/roles pages (spec 2026-11-03-rbac-design.md, Admin UI; RB5, RB7, RB15, RB17).

The rules behind them are tested in tests/test_role_admin.py; here, that each page reaches them, shows what the spec
says it shows, and is Admin's alone.
"""
from datetime import date, timedelta

import pytest
from markupsafe import escape

from app import db
from app.models import Role, RoleGrant, RolePermission, SiteDomain, utcnow
from app.permissions import CATALOGUE, ROLE_PERMISSIONS, user_access
from app.storefront import clear_domain_cache
from tests.factories import admin_role, grant_permissions, grant_role, make_instance, make_user, staff_role

pytestmark = pytest.mark.usefixtures('site')


def login(client, user):
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True


def csrf(app, client):
    """Fact 355."""
    from flask import session as flask_session
    from flask_wtf.csrf import generate_csrf

    with app.test_request_context():
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with client.session_transaction() as session:
        session['csrf_token'] = raw
    return token


@pytest.fixture
def env(app, db_session):
    instance = make_instance('test.piefed.local', software='piefed')
    founder = make_user(instance, 'founder', local=True)
    assert founder.id == 1
    admin = make_user(instance, 'boss', local=True)
    holder = make_user(instance, 'holder', local=True)
    for user in (admin, holder):
        user.verified = True
    domain = SiteDomain(domain='x.roles.example', scheme='https', federates=False)
    db.session.add(domain)
    db.session.commit()
    clear_domain_cache()
    grant_role(admin, admin_role())
    client = app.test_client()
    login(client, admin)
    token = csrf(app, client)
    yield type('Env', (), dict(app=app, client=client, token=token, admin=admin, holder=holder, domain=domain,
                               founder=founder, instance=instance))
    clear_domain_cache()


def page(env, path):
    response = env.client.get(path)
    assert response.status_code == 200, path
    return response.get_data(as_text=True)


def post(env, path, **data):
    return env.client.post(path, data={'csrf_token': env.token, **data})


def a_role(name='Helpers', *permissions, listed=False):
    role = Role(name=name, listed_publicly=listed)
    db.session.add(role)
    db.session.commit()
    for permission in permissions:
        db.session.add(RolePermission(role_id=role.id, permission=permission))
    db.session.commit()
    return role


# --------------------------------------------------------------------------
# Admin's alone
# --------------------------------------------------------------------------

def test_every_page_is_refused_to_anyone_but_an_admin(env):
    """RB5. The holder holds every permission there is, and is still not an admin."""
    grant_permissions(env.holder, *ROLE_PERMISSIONS)
    role = a_role()
    grant = RoleGrant(user_id=env.founder.id, role_id=role.id)
    db.session.add(grant)
    db.session.commit()
    login(env.client, env.holder)
    for path in ('/admin/roles', f'/admin/roles/{role.id}', '/admin/roles/new', f'/admin/roles/{role.id}/edit',
                 f'/admin/roles/{role.id}/delete', '/admin/permissions'):
        response = env.client.get(path)
        assert response.status_code == 302 and 'permission_denied' in response.headers['Location'], path
    for path in (f'/admin/roles/{role.id}/edit', f'/admin/roles/{role.id}/delete', f'/admin/roles/{role.id}/grants',
                 f'/admin/user/{env.holder.id}/grants', f'/admin/grants/{grant.id}/expiry',
                 f'/admin/grants/{grant.id}/revoke', '/admin/roles/new'):
        response = post(env, path, name='Mine', revoke_all='1', **{'grant-user': 'holder', 'grant-role': role.id})
        assert response.status_code == 302 and 'permission_denied' in response.headers['Location'], path
    assert db.session.get(Role, role.id) is not None and db.session.get(RoleGrant, grant.id) is not None
    assert Role.query.filter_by(name='Mine').first() is None


def test_the_old_permissions_page_sends_an_admin_to_the_roles(env):
    response = env.client.get('/admin/permissions')
    assert response.status_code == 302 and response.headers['Location'].endswith('/admin/roles')


# --------------------------------------------------------------------------
# List and view
# --------------------------------------------------------------------------

def test_the_list_pins_admin_first_with_only_a_view_action(env):
    staff = staff_role()
    grant_role(env.holder, staff)
    body = page(env, '/admin/roles')
    admin_row, staff_row = body.split('<tr>')[2:4]
    assert 'Built-in' in admin_row and 'Edit' not in admin_row and 'Delete' not in admin_row
    assert 'Staff' in staff_row and 'Edit' in staff_row and 'Duplicate' in staff_row and 'Delete' in staff_row
    cells = [cell.strip() for cell in staff_row.split('<td class="text-center">')[1:5]]
    assert [c.split('<')[0] for c in cells] == [str(len(staff.permissions)), 'No', 'Yes', '1']   # Staff holds instance-only ones


def test_a_role_with_an_instance_only_permission_cannot_be_granted_on_a_domain(env):
    a_role('Mixed', 'ban users', 'masquerade')
    row = next(r for r in page(env, '/admin/roles').split('<tr>') if 'Mixed' in r)
    assert row.split('<td class="text-center">')[2].startswith('No')


def test_the_view_page_names_the_permissions_and_the_grants(env):
    role = a_role('Helpers', 'ban users')
    grant_role(env.holder, role, site_domain_id=env.domain.id)
    body = page(env, f'/admin/roles/{role.id}')
    assert 'ban users' in body and 'masquerade' not in body
    assert 'holder' in body and 'on x.roles.example' in body


def test_admins_view_page_says_it_has_every_permission(env):
    body = page(env, f'/admin/roles/{admin_role().id}')
    assert 'Has every permission.' in body and 'boss' in body
    assert f'/admin/roles/{admin_role().id}/edit' not in body


def test_an_unknown_role_is_a_404(env):
    assert env.client.get('/admin/roles/999999').status_code == 404


# --------------------------------------------------------------------------
# Create, duplicate, edit
# --------------------------------------------------------------------------

def test_the_create_form_offers_every_permission_grouped_with_its_guard(env):
    body = page(env, '/admin/roles/new')
    for permission in CATALOGUE:
        assert f'value="{permission.name}"' in body
        assert str(escape(permission.guards)) in body
    assert body.index('<legend class="h5">Site</legend>') < body.index('<legend class="h5">Display</legend>')


def test_a_role_is_created_from_the_form(env):
    response = post(env, '/admin/roles/new', name='Helpers', description='They help', listed_publicly='y',
                    permissions=['ban users', 'view modlog'])
    role = Role.query.filter_by(name='Helpers').one()
    assert response.headers['Location'].endswith(f'/admin/roles/{role.id}')
    assert sorted(p.permission for p in role.permissions) == ['ban users', 'view modlog']
    assert role.listed_publicly is True


def test_a_refused_name_is_a_form_error(env):
    response = post(env, '/admin/roles/new', name='admin', permissions=['ban users'])
    assert response.status_code == 200 and "built-in Admin role" in response.get_data(as_text=True)
    assert Role.query.count() == 1


def test_duplicate_prefills_the_form_but_copies_no_grants(env):
    role = a_role('Helpers', 'ban users', listed=True)
    grant_role(env.holder, role)
    body = page(env, f'/admin/roles/new?from={role.id}')
    assert 'value="Copy of Helpers"' in body
    assert 'value="ban users"\n                           id' in body or 'value="ban users"' in body
    assert body.count(' checked') == 2   # ban users and "listed publicly"
    post(env, '/admin/roles/new', name='Copy of Helpers', permissions=['ban users'])
    copy = Role.query.filter_by(name='Copy of Helpers').one()
    assert RoleGrant.query.filter_by(role_id=copy.id).count() == 0


def test_duplicating_admin_ticks_every_permission(env):
    body = page(env, f'/admin/roles/new?from={admin_role().id}')
    assert body.count('name="permissions"') == body.count('name="permissions" value') == len(CATALOGUE)
    assert body.count(' checked>') == len(CATALOGUE)


def test_an_edit_of_a_role_nobody_holds_saves_at_once(env):
    role = a_role('Helpers', 'ban users')
    response = post(env, f'/admin/roles/{role.id}/edit', name='Helpers', permissions=['view modlog'])
    assert response.status_code == 302
    assert [p.permission for p in RolePermission.query.filter_by(role_id=role.id)] == ['view modlog']


def test_an_edit_that_changes_access_asks_first_then_takes_effect_live(env):
    """RB15: the next request sees it, with no restart."""
    role = a_role('Helpers', 'ban users')
    grant_role(env.holder, role)
    response = post(env, f'/admin/roles/{role.id}/edit', name='Helpers', permissions=['view modlog'])
    assert response.status_code == 200 and 'This changes access for 1 user.' in response.get_data(as_text=True)
    assert [p.permission for p in RolePermission.query.filter_by(role_id=role.id)] == ['ban users']
    post(env, f'/admin/roles/{role.id}/edit', name='Helpers', permissions=['view modlog'], confirmed='1')
    assert user_access('ban users', env.holder.id) is False and user_access('view modlog', env.holder.id) is True


def test_a_rename_alone_needs_no_confirmation(env):
    role = a_role('Helpers', 'ban users')
    grant_role(env.holder, role)
    assert post(env, f'/admin/roles/{role.id}/edit', name='Assistants', permissions=['ban users']).status_code == 302
    assert role.name == 'Assistants'


def test_an_edit_refused_by_rb7_names_the_grants(env):
    role = a_role('Domain helpers', 'ban users')
    grant_role(env.holder, role, site_domain_id=env.domain.id)
    response = post(env, f'/admin/roles/{role.id}/edit', name='Domain helpers',
                    permissions=['ban users', 'masquerade'], confirmed='1')
    body = response.get_data(as_text=True)
    assert response.status_code == 200 and 'masquerade' in body and 'holder on x.roles.example' in body
    assert [p.permission for p in RolePermission.query.filter_by(role_id=role.id)] == ['ban users']


def test_admin_has_no_edit_form(env):
    response = env.client.get(f'/admin/roles/{admin_role().id}/edit')
    assert response.status_code == 302 and response.headers['Location'].endswith(f'/admin/roles/{admin_role().id}')


# --------------------------------------------------------------------------
# Delete
# --------------------------------------------------------------------------

def test_the_delete_page_lists_the_grants_and_needs_the_tick(env):
    role = a_role('Helpers', 'ban users')
    grant_role(env.holder, role)
    body = page(env, f'/admin/roles/{role.id}/delete')
    assert 'holder, instance-wide' in body and 'Revoke 1 grant' in body
    post(env, f'/admin/roles/{role.id}/delete')
    assert db.session.get(Role, role.id) is not None
    response = post(env, f'/admin/roles/{role.id}/delete', revoke_all='1')
    assert response.headers['Location'].endswith('/admin/roles')
    assert Role.query.filter_by(name='Helpers').first() is None


def test_admin_cannot_be_deleted_from_the_page(env):
    post(env, f'/admin/roles/{admin_role().id}/delete', revoke_all='1')
    assert Role.admin() is not None


# --------------------------------------------------------------------------
# Grants
# --------------------------------------------------------------------------

def test_a_grant_is_added_on_the_role_page_by_name_with_scope_and_expiry(env):
    role = a_role('Helpers', 'ban users')
    day = date.today() + timedelta(days=5)
    response = post(env, f'/admin/roles/{role.id}/grants', **{'grant-user': 'holder', 'grant-scope': env.domain.id,
                                                              'grant-expires': day.isoformat()})
    assert response.headers['Location'].endswith(f'/admin/roles/{role.id}')
    grant = RoleGrant.query.filter_by(role_id=role.id).one()
    assert (grant.user_id, grant.site_domain_id, grant.expires_at.date(), grant.granted_by) == \
        (env.holder.id, env.domain.id, day, env.admin.id)


def test_a_grant_to_nobody_or_a_refused_one_is_reported(env):
    role = a_role('Mixed', 'masquerade')
    with env.client:
        post(env, f'/admin/roles/{role.id}/grants', **{'grant-user': 'nobody-here', 'grant-scope': 0})
        post(env, f'/admin/roles/{role.id}/grants', **{'grant-user': 'holder', 'grant-scope': env.domain.id})
        flashes = env.client.get(f'/admin/roles/{role.id}').get_data(as_text=True)
    assert 'There is no local account called' in flashes
    assert 'cannot be granted on a domain' in flashes
    assert RoleGrant.query.filter_by(role_id=role.id).count() == 0


def test_a_grant_is_added_from_the_user_page(env):
    role = a_role('Helpers', 'ban users')
    response = post(env, f'/admin/user/{env.holder.id}/grants', **{'grant-role': role.id, 'grant-scope': 0})
    assert response.headers['Location'].endswith(f'/admin/user/{env.holder.id}/edit')
    assert RoleGrant.query.filter_by(role_id=role.id, user_id=env.holder.id).count() == 1


def test_an_expiry_is_changed_and_cleared(env):
    role = a_role('Helpers', 'ban users')
    grant_role(env.holder, role)
    grant = RoleGrant.query.filter_by(role_id=role.id).one()
    day = date.today() + timedelta(days=9)
    post(env, f'/admin/grants/{grant.id}/expiry', **{f'grant{grant.id}-expires': day.isoformat()})
    db.session.refresh(grant)
    assert grant.expires_at.date() == day
    post(env, f'/admin/grants/{grant.id}/expiry', **{f'grant{grant.id}-expires': ''})
    db.session.refresh(grant)
    assert grant.expires_at is None


def test_a_revoke_takes_effect_on_the_next_request(env):
    role = a_role('Helpers', 'ban users')
    grant_role(env.holder, role)
    grant = RoleGrant.query.filter_by(role_id=role.id).one()
    assert user_access('ban users', env.holder.id) is True
    response = post(env, f'/admin/grants/{grant.id}/revoke', next=f'/admin/user/{env.holder.id}/edit')
    assert response.headers['Location'].endswith(f'/admin/user/{env.holder.id}/edit')
    assert RoleGrant.query.filter_by(role_id=role.id).count() == 0
    assert user_access('ban users', env.holder.id) is False


def test_an_admin_cannot_revoke_their_own_admin_grant_from_the_page(env):
    own = RoleGrant.query.filter_by(user_id=env.admin.id, role_id=admin_role().id).one()
    post(env, f'/admin/grants/{own.id}/revoke')
    assert db.session.get(RoleGrant, own.id) is not None


def test_an_unknown_grant_is_a_404(env):
    assert post(env, '/admin/grants/999999/revoke').status_code == 404
    assert post(env, '/admin/grants/999999/expiry').status_code == 404


def test_an_expired_grant_on_the_page_reads_as_its_date(env):
    role = a_role('Helpers')
    db.session.add(RoleGrant(user_id=env.holder.id, role_id=role.id, expires_at=utcnow() + timedelta(days=2)))
    db.session.commit()
    assert (date.today() + timedelta(days=2)).isoformat() in page(env, f'/admin/roles/{role.id}') or \
        (utcnow() + timedelta(days=2)).date().isoformat() in page(env, f'/admin/roles/{role.id}')


# --------------------------------------------------------------------------
# Forms that cannot be read, and refusals the pages pass on
# --------------------------------------------------------------------------

def flashed_after(env, path):
    with env.client.session_transaction() as session:
        return [message for _, message in session.get('_flashes', [])]


def test_a_create_form_with_no_name_is_shown_again(env):
    response = post(env, '/admin/roles/new', name='', permissions=['ban users'])
    assert response.status_code == 200 and 'id="role-form"' in response.get_data(as_text=True)
    assert Role.query.count() == 1


def test_an_edit_form_with_no_name_is_shown_again(env):
    role = a_role('Helpers', 'ban users')
    response = post(env, f'/admin/roles/{role.id}/edit', name='', permissions=[])
    assert response.status_code == 200 and role.name == 'Helpers'


def test_a_grant_form_that_cannot_be_read_is_reported(env):
    role = a_role('Helpers')
    post(env, f'/admin/roles/{role.id}/grants', **{'grant-user': 'holder', 'grant-scope': '987654'})
    assert any('could not be read' in message for message in flashed_after(env, ''))
    assert RoleGrant.query.filter_by(role_id=role.id).count() == 0


def test_a_user_grant_with_no_role_is_reported(env):
    post(env, f'/admin/user/{env.holder.id}/grants', **{'grant-role': '987654', 'grant-scope': 0})
    assert 'Choose a role to grant.' in flashed_after(env, '')


def test_an_expiry_in_the_past_or_unreadable_is_reported(env):
    role = a_role('Helpers')
    grant_role(env.holder, role)
    grant = RoleGrant.query.filter_by(role_id=role.id).one()
    post(env, f'/admin/grants/{grant.id}/expiry', **{f'grant{grant.id}-expires': '2001-01-01'})
    assert 'An expiry must be in the future.' in flashed_after(env, '')
    post(env, f'/admin/grants/{grant.id}/expiry', **{f'grant{grant.id}-expires': 'not a date'})
    assert 'The expiry could not be read.' in flashed_after(env, '')
    assert RoleGrant.query.filter_by(role_id=role.id).one().expires_at is None


def test_a_revoke_without_its_csrf_token_changes_nothing(env):
    role = a_role('Helpers')
    grant_role(env.holder, role)
    grant = RoleGrant.query.filter_by(role_id=role.id).one()
    env.app.config['WTF_CSRF_ENABLED'] = True
    try:
        env.client.post(f'/admin/grants/{grant.id}/revoke', data={})
    finally:
        env.app.config['WTF_CSRF_ENABLED'] = False
    assert db.session.get(RoleGrant, grant.id) is not None

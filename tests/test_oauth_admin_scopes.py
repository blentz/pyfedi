"""An app's admin scopes (owner ruling 2026-11-09): accepted, granted only to a person holding the RBAC role an admin
maps them to (Staff unless chosen; Admin too), and reaching no further than that role's permissions the person holds."""
from unittest.mock import patch

import pytest

from app import db
from app.models import OAuthCode, Role, RolePermission
from app.oauth_scopes import covers, parse_scope
from app.oauth_server import admin_scopes, tokens
from app.utils import set_setting
from tests.factories import grant_role, make_admin, make_user
from tests.test_admin_routes_entry import _seed, _settings_admin, login

CALLBACK = 'https://app.example/cb'


@pytest.fixture
def world(app, api_baseline):
    staff = Role(name='Staff')
    staff.permissions = [RolePermission(permission='manage users'), RolePermission(permission='moderate all communities')]
    other = Role(name='Helpers')
    other.permissions = [RolePermission(permission='manage users')]
    db.session.add_all([staff, other])
    db.session.commit()
    return {'staff': staff, 'other': other, 'nobody': make_user(None, 'plainperson', local=True),
            'staffer': make_user(None, 'staffer', local=True)}


def test_the_admin_scopes_are_accepted_and_none_of_reads_or_writes(world):
    assert parse_scope('read admin:read admin:write:reports') == 'read admin:read admin:write:reports'
    assert covers('admin:read', 'admin:read:accounts') and covers('admin:write', 'admin:write:reports')
    assert not covers('read', 'admin:read') and not covers('write', 'admin:write:accounts')
    assert not covers('admin:read', 'admin:write')


def test_the_role_is_staff_until_one_is_chosen(world):
    assert admin_scopes.admin_role() == world['staff']
    set_setting(admin_scopes.SETTING, world['other'].id)
    assert admin_scopes.admin_role() == world['other']
    set_setting(admin_scopes.SETTING, 2 ** 31 - 1)   # a role since deleted: back to Staff
    assert admin_scopes.admin_role() == world['staff']


def test_no_such_role_grants_them_to_nobody(world):
    world['staff'].name = 'Crew'
    db.session.commit()
    assert admin_scopes.admin_role() is None
    assert admin_scopes.granted('read admin:read', world['staffer']) == 'read'


def test_granted_only_to_a_holder_of_the_role_or_admin(world):
    grant_role(world['staffer'], world['staff'])
    assert admin_scopes.granted('read admin:read', world['staffer']) == 'read admin:read'
    assert admin_scopes.granted('read admin:read admin:write', world['nobody']) == 'read'
    assert admin_scopes.granted('read', None) == 'read'
    assert admin_scopes.granted('admin:read', make_admin(make_user(None, 'theadmin', local=True))) == 'admin:read'


def test_an_expired_grant_holds_no_role(world):
    grant_role(world['staffer'], world['staff'])
    db.session.execute(db.text('UPDATE role_grant SET expires_at = now() - interval \'1 day\''))
    db.session.commit()
    assert not admin_scopes.may_hold(world['staffer'])


def test_a_token_reaches_the_roles_permissions_the_person_holds(world):
    grant_role(world['staffer'], world['staff'])
    assert admin_scopes.admin_permissions('admin:read', world['staffer'], writing=False) == {
        'manage users', 'moderate all communities'}
    assert admin_scopes.admin_permissions('admin:read', world['staffer'], writing=True) == set()
    assert admin_scopes.admin_permissions('read write', world['staffer'], writing=False) == set()
    assert admin_scopes.admin_permissions('admin:read', world['nobody'], writing=False) == set()
    assert admin_scopes.admin_permissions('admin:read', None, writing=False) == set()
    set_setting(admin_scopes.SETTING, world['other'].id)   # a narrower role: narrower reach, though Staff is held
    grant_role(world['staffer'], world['other'])
    assert admin_scopes.admin_permissions('admin:write', world['staffer'], writing=True) == {'manage users'}


def test_admin_mapped_to_admin_reaches_every_permission_an_admin_holds(world):
    admin = make_admin(make_user(None, 'chief', local=True))
    set_setting(admin_scopes.SETTING, Role.admin().id)
    reach = admin_scopes.admin_permissions('admin:read', admin, writing=False)
    assert {'manage users', 'change instance settings'} <= reach


def _authorize(app, world, user, scope):
    registered, _secret = tokens.register_app('ModTool', [CALLBACK], 'read admin:read admin:write', None)
    client = app.test_client()
    login(client, user)
    query = {'client_id': registered.client_id, 'redirect_uri': CALLBACK, 'response_type': 'code', 'scope': scope}
    return client, query


def test_the_consent_page_says_what_is_withheld_and_the_code_leaves_it_out(app, world):
    client, query = _authorize(app, world, world['nobody'], 'read admin:read')
    page = client.get('/oauth/authorize', query_string=query).get_data(as_text=True)
    assert 'not granted: only for people with the Staff role' in page
    client.post('/oauth/authorize', query_string=query, data={'approve': 'y', 'csrf_token': _csrf(client, query)})
    assert OAuthCode.query.one().scope == 'read'


def test_a_holder_of_the_role_is_granted_them(app, world):
    grant_role(world['staffer'], world['staff'])
    client, query = _authorize(app, world, world['staffer'], 'read admin:read')
    assert 'not granted' not in client.get('/oauth/authorize', query_string=query).get_data(as_text=True)
    client.post('/oauth/authorize', query_string=query, data={'approve': 'y', 'csrf_token': _csrf(client, query)})
    assert OAuthCode.query.one().scope == 'read admin:read'


def _csrf(client, query) -> str:
    page = client.get('/oauth/authorize', query_string=query).get_data(as_text=True)
    return page.split('name="csrf_token" value="')[1].split('"')[0]


def test_the_admin_chooses_the_role_on_the_misc_page(app, db_session):
    """Its own seed (the admin pages' tests'), not the API baseline's."""
    client = app.test_client()
    login(client, _settings_admin(_seed()[0]))
    staff, helpers = Role(name='Staff'), Role(name='Helpers')
    db.session.add_all([staff, helpers])
    db.session.commit()
    with patch('app.admin.routes.render_template', return_value='rendered') as render:
        client.get('/admin/misc')
    form = render.call_args.kwargs['form']
    assert form.oauth_admin_role.data == staff.id
    assert (helpers.id, 'Helpers') in [(choice.value, choice.label) for choice in form.oauth_admin_role.choices]

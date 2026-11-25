"""RBAC build step 10: the acceptance test (spec 2026-11-03-rbac-design.md RB3, RB15).

An Admin composes "domain admin" through the /admin/roles pages, each permission limited to domain X, and grants it. No
such role is shipped (RB14). As the grantee, in the same running app, with no restart: allowed on X's objects; denied on
Y's, on remote ones and on instance-only pages. Then the Admin edits the role and revokes the grant, and each takes
effect on the next request. The test app's cache keeps nothing, so that a cached answer retires on a change is shown
by tests/test_permissions_scope.py's version tests rather than here.
"""
import io
from unittest.mock import patch

import pytest
from flask import g

from app import db
from app.models import Community, CustomTheme, Role, RoleGrant, SiteDomain, User, UserRegistration
from app.permissions import shown_as_admin_ids
from app.storefront import clear_domain_cache
from tests.factories import admin_role, grant_role, make_community, make_instance, make_post, make_user

pytestmark = pytest.mark.usefixtures('site')

# What RB3 asks a domain admin to hold. CMS pages and the newsletter are left out on purpose, as RB3 says they may be.
DOMAIN_ADMIN = ('edit domain branding', 'approve registrations', 'administer all users', 'ban users', 'manage users',
                'view user details', 'create local communities', 'moderate all communities',
                'administer all communities', 'administer communities pages', 'view modlog', 'shown as admin')


def login(client, user):
    g.pop('_login_user', None)
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True


def csrf(app, client):
    from flask import session as flask_session
    from flask_wtf.csrf import generate_csrf
    with app.test_request_context():
        g.pop('csrf_token', None)
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with client.session_transaction() as session:
        session['csrf_token'] = raw
    return token


@pytest.fixture
def world(app, db_session):
    local = make_instance('test.piefed.local', software='piefed')
    peer = make_instance('peer.example', software='piefed')
    make_user(local, 'founder', local=True)
    admin = make_user(local, 'boss', local=True)
    grantee = make_user(local, 'grantee', local=True)
    admin.verified = grantee.verified = True
    db.session.commit()
    grant_role(admin, admin_role())
    x = SiteDomain(domain='x.accept.example', scheme='https', federates=True, registration_mode='Closed')
    y = SiteDomain(domain='y.accept.example', scheme='https', federates=True)
    db.session.add_all([x, y])
    db.session.commit()
    clear_domain_cache()

    def homed(tag, domain):
        user = make_user(local, f'{tag}user', local=True)
        user.site_domain_id = domain.id
        community = make_community(f'{tag}comm')
        community.site_domain_id = domain.id
        db.session.commit()
        db.session.add(UserRegistration(user_id=user.id, answer=f'{tag} answer', status=0))
        db.session.commit()
        return type('Home', (), dict(user=user, community=community,
                                     post=make_post(community, user, f'https://test.piefed.local/post/{tag}')))

    on_x, on_y = homed('xx', x), homed('yy', y)
    remote = make_user(peer, 'remy')
    far = make_community('far', host='peer.example')
    far.ap_id, far.instance_id = 'far@peer.example', peer.id
    db.session.commit()
    yield type('World', (), dict(app=app, admin=admin, grantee=grantee, x=x, y=y, on_x=on_x, on_y=on_y, remote=remote,
                                 far=far))
    clear_domain_cache()


def form_for(domain):
    data = {'name': 'Domain admin', 'description': 'Runs one domain', 'permissions': list(DOMAIN_ADMIN)}
    for permission in DOMAIN_ADMIN:
        data[f'scope-{permission}'] = 'selected'
        data[f'domains-{permission}'] = str(domain.id)
    return data


def build_and_grant(world):
    """Through the pages, as an admin would."""
    client = world.app.test_client()
    login(client, world.admin)
    token = csrf(world.app, client)
    response = client.post('/admin/roles/new', data={'csrf_token': token, **form_for(world.x)})
    role = Role.query.filter_by(name='Domain admin').one()
    assert response.status_code == 302 and role.built_in is False
    response = client.post(f'/admin/roles/{role.id}/grants', data={'csrf_token': token, 'grant-user': 'grantee'})
    assert response.status_code == 302 and RoleGrant.query.filter_by(role_id=role.id).count() == 1
    return client, token, role


def as_grantee(world):
    client = world.app.test_client()
    login(client, world.grantee)
    return client


def opens(client, path):
    """Whether the page opens. Rendering is patched: some of these templates need a real CSRF field."""
    g.pop('_login_user', None)
    with patch('app.admin.routes.render_template', return_value='rendered'):
        response = client.get(path)
    assert response.status_code in (200, 302, 403), path
    return response.status_code == 200


def test_a_holder_runs_x_and_nothing_else(world):
    """RB3 and RB4, with the role made through the pages (RB15)."""
    build_and_grant(world)
    client = as_grantee(world)
    x, y = world.on_x, world.on_y

    for path in (f'/admin/site?domain={world.x.id}', '/admin/users', '/admin/communities', '/admin/content',
                 '/admin/reports', '/admin/referrals', '/admin/approve_registrations', '/admin/users/add',
                 f'/admin/user/{x.user.id}/edit', f'/admin/community/{x.community.id}/edit'):
        assert opens(client, path), f'should open: {path}'
    for path in (f'/admin/user/{y.user.id}/edit',
                 f'/admin/community/{y.community.id}/edit', f'/admin/user/{world.remote.id}/edit',
                 f'/admin/community/{world.far.id}/edit',
                 '/admin/misc', '/admin/pages', '/admin/newsletter', '/admin/messaging', '/admin/messaging/new',
                 '/admin/messaging/email/welcome', '/admin/roles', '/admin/federation',
                 '/admin/topics', '/admin/media', '/admin/instances'):
        assert not opens(client, path), f'should be refused: {path}'
    assert world.grantee.id in shown_as_admin_ids(world.x.id) and world.grantee.id not in shown_as_admin_ids(world.y.id)


def test_the_holder_manages_xs_themes_and_not_ys(world):
    """Custom themes spec CT4: theme holders are `edit domain branding` holders, so the composed domain admin manages
    X's custom themes, and Y's and the first domain's stay out of reach."""
    build_and_grant(world)
    client = as_grantee(world)
    y_theme = CustomTheme(site_domain_id=world.y.id, name='Why', base_theme='forest')
    db.session.add(y_theme)
    db.session.commit()
    assert opens(client, f'/admin/domains/{world.x.id}/themes')
    first = SiteDomain.query.filter_by(is_default=True).first()
    for path in [f'/admin/domains/{world.y.id}/themes'] + ([f'/admin/domains/{first.id}/themes'] if first else []):
        assert not opens(client, path), f'should be refused: {path}'
    token = csrf(world.app, client)
    g.pop('_login_user', None)
    assert client.post(f'/admin/domains/{world.x.id}/themes/copy',
                       data={'csrf_token': token, 'source': 'forest'}).status_code == 302
    assert CustomTheme.query.filter_by(site_domain_id=world.x.id).count() == 1
    g.pop('_login_user', None)
    assert client.post(f'/admin/domains/{world.y.id}/themes/{y_theme.id}/retire',
                       data={'csrf_token': token}).status_code == 403
    assert db.session.get(CustomTheme, y_theme.id).retired is False


def test_theme_code_needs_its_own_permission_added_to_the_role(world):
    """Theme code spec §3.1: `manage theme code` is never implied by `edit domain branding`. The composed domain admin
    cannot write a theme's script on X until the Admin adds the permission, limited to X, through the role editor;
    then they can on X and still not on Y."""
    admin_client, _token, role = build_and_grant(world)
    x_theme = CustomTheme(site_domain_id=world.x.id, name='Ex', base_theme='forest')
    y_theme = CustomTheme(site_domain_id=world.y.id, name='Why', base_theme='forest')
    db.session.add_all([x_theme, y_theme])
    db.session.commit()
    client = as_grantee(world)

    def write_script(theme, domain):
        g.pop('_login_user', None)
        return client.post(f'/admin/domains/{domain.id}/themes/{theme.id}/script',
                           data={'csrf_token': csrf(world.app, client), 'script': 'go();'}).status_code

    assert write_script(x_theme, world.x) == 403
    data = form_for(world.x)
    data['permissions'] = list(DOMAIN_ADMIN) + ['manage theme code']
    data['scope-manage theme code'], data['domains-manage theme code'] = 'selected', str(world.x.id)
    data['confirmed'] = '1'   # the role has a holder: the editor asks for a second look first
    g.pop('_login_user', None)
    login(admin_client, world.admin)
    fresh = csrf(world.app, admin_client)   # the grantee's requests replaced the token in the shared context
    g.pop('_login_user', None)
    assert admin_client.post(f'/admin/roles/{role.id}/edit', data={'csrf_token': fresh, **data}).status_code == 302
    assert write_script(x_theme, world.x) == 302
    assert db.session.get(CustomTheme, x_theme.id).script == 'go();'
    assert write_script(y_theme, world.y) == 403
    assert db.session.get(CustomTheme, y_theme.id).script is None


def test_the_holders_actions_land_on_x_and_are_refused_on_y(world):
    build_and_grant(world)
    client = as_grantee(world)
    token = csrf(world.app, client)
    edit = {'csrf_token': token, 'banned': 'y', 'hide_nsfw': '1', 'hide_nsfl': '1', 'admin_note': '', 'submit': 'Save'}
    with patch('app.admin.routes.render_template', return_value='rendered'):
        assert client.post(f'/admin/user/{world.on_x.user.id}/edit', data=edit).status_code == 302
        refused = client.post(f'/admin/user/{world.on_y.user.id}/edit', data=edit)
    assert 'permission_denied' in refused.headers['Location']
    db.session.expire_all()
    assert db.session.get(User, world.on_x.user.id).banned is True
    assert db.session.get(User, world.on_y.user.id).banned is not True

    client.post(f'/admin/approve_registrations/{world.on_x.user.id}/approve', data={'csrf_token': token})
    client.post(f'/admin/approve_registrations/{world.on_y.user.id}/approve', data={'csrf_token': token})
    assert UserRegistration.query.filter_by(user_id=world.on_x.user.id).one().status == 1
    assert UserRegistration.query.filter_by(user_id=world.on_y.user.id).one().status == 0

    with patch('app.admin.routes.finalize_user_setup'):
        client.post('/admin/users/add', content_type='multipart/form-data', data={
            'user_name': 'newbie', 'email': 'newbie@example.com', 'password': 'password123',
            'password2': 'password123', 'ignore_bots': '0', 'hide_nsfw': '1', 'hide_nsfl': '1',
            'site_domain': str(world.x.id), 'submit': 'Save', 'csrf_token': token,
            'profile_file': (io.BytesIO(b''), ''), 'banner_file': (io.BytesIO(b''), '')})
    assert User.query.filter_by(user_name='newbie').one().site_domain_id == world.x.id

    response = client.post(f'/admin/community/{world.on_y.community.id}/delete', data={'csrf_token': token})
    assert 'permission_denied' in response.headers['Location']
    assert db.session.get(Community, world.on_y.community.id).banned is not True


def test_a_holder_cannot_act_on_someone_who_holds_a_role(world):
    """RB6."""
    build_and_grant(world)
    other = world.on_x.user
    grant_role(other, Role.query.filter_by(name='Domain admin').one())
    assert not opens(as_grantee(world), f'/admin/user/{other.id}/edit')


def test_editing_the_role_and_revoking_the_grant_take_effect_on_the_next_request(world):
    """RB15: no deploy, migration, restart or cache flush between any of these."""
    admin_client, token, role = build_and_grant(world)
    client = as_grantee(world)
    assert opens(client, '/admin/users')

    without = form_for(world.x)
    without['permissions'] = [p for p in DOMAIN_ADMIN if p != 'administer all users']
    g.pop('_login_user', None)
    response = admin_client.post(f'/admin/roles/{role.id}/edit', data={'csrf_token': token, 'confirmed': '1', **without})
    assert response.status_code == 302
    assert not opens(client, '/admin/users')
    assert opens(client, '/admin/communities')

    # widened to Y on the role, which the holder has next too
    wider = form_for(world.x)
    wider['domains-administer communities pages'] = [str(world.x.id), str(world.y.id)]
    assert not opens(client, f'/admin/community/{world.on_y.community.id}/edit')
    g.pop('_login_user', None)
    admin_client.post(f'/admin/roles/{role.id}/edit', data={'csrf_token': token, 'confirmed': '1', **wider})
    assert opens(client, f'/admin/community/{world.on_y.community.id}/edit')

    grant = RoleGrant.query.filter_by(role_id=role.id, user_id=world.grantee.id).one()
    g.pop('_login_user', None)
    admin_client.post(f'/admin/grants/{grant.id}/revoke', data={'csrf_token': token})
    assert not opens(client, '/admin/communities')
    assert client.get('/admin/').status_code == 403


def test_no_domain_admin_role_ships(db_session):
    """RB14."""
    from app.roles import seed_default_roles
    assert set(seed_default_roles()) == {'Admin', 'Staff'}

"""RBAC build step 8: what a holder scoped to one domain sees and may do in /admin (spec 2026-11-03-rbac-design.md,
What scoped holders see; RB4, RB6, RB8).

Domains X and Y each home a user, a community, a post in it, a report on each and a registration. A holder of each
permission on X sees X's rows only, whatever the list, may act on X's objects and not Y's, and is offered no picker
with one domain. An instance-wide holder sees both, and the picker narrows to one.
"""
import re
from unittest.mock import patch

import pytest

from app import db
from app.constants import REPORT_STATE_NEW
from app.models import Referral, Report, Role, RolePermission, SiteDomain, UserRegistration, role_grant
from app.storefront import clear_domain_cache, default_domain, domain_by_id
from tests.factories import admin_role, grant_role, make_community, make_instance, make_post, make_user

pytestmark = pytest.mark.usefixtures('site')


def login(client, user):
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True


@pytest.fixture
def world(app, db_session):
    local = make_instance('test.piefed.local', software='piefed')
    founder = make_user(local, 'founder', local=True)
    assert founder.id == 1
    x = SiteDomain(domain='x.scoped.example', scheme='https', federates=True)
    y = SiteDomain(domain='y.scoped.example', scheme='https', federates=True)
    db.session.add_all([x, y])
    db.session.commit()
    clear_domain_cache()

    def homed(tag, domain):
        user = make_user(local, f'{tag}user', local=True)
        user.site_domain_id = domain.id
        community = make_community(f'{tag}comm')
        community.site_domain_id = domain.id
        db.session.commit()
        post = make_post(community, user, f'https://test.piefed.local/post/{tag}', title=f'{tag} post title')
        post.down_votes, post.score = 5, -5   # on the content page's "trash" list
        db.session.add_all([
            Report(reasons=f'{tag} user report', type=0, reporter_id=founder.id, suspect_user_id=user.id,
                   status=REPORT_STATE_NEW, source_instance_id=1),
            Report(reasons=f'{tag} post report', type=1, reporter_id=founder.id, suspect_post_id=post.id,
                   suspect_user_id=user.id, in_community_id=community.id, status=REPORT_STATE_NEW,
                   source_instance_id=1),
            UserRegistration(user_id=user.id, answer=f'{tag} answer', status=0),
            Referral(referrer_id=founder.id, referred_id=user.id, code=f'{tag}code'),
        ])
        db.session.commit()
        return user, community, post

    on_x = homed('xx', x)
    on_y = homed('yy', y)
    client = app.test_client()
    yield type('World', (), dict(app=app, client=client, x=x, y=y, local=local, founder=founder,
                                 on_x=on_x, on_y=on_y))
    clear_domain_cache()


def holder(world, name, *permissions, domain=None):
    """A holder of `permissions` on `domain` (instance-wide for None), signed in."""
    user = make_user(world.local, name, local=True)
    user.verified = True
    role = Role(name=f'role-{name}')
    db.session.add(role)
    db.session.commit()
    for permission in permissions:
        db.session.add(RolePermission(role_id=role.id, permission=permission))
    db.session.execute(role_grant.insert().values(user_id=user.id, role_id=role.id,
                                                  site_domain_id=domain.id if domain else None))
    db.session.commit()
    login(world.client, user)
    return user


def body(world, path):
    """The page, without the picker: its options name every domain the holder may pick."""
    response = world.client.get(path)
    assert response.status_code == 200, (path, response.status_code, response.headers.get('Location'))
    page = response.get_data(as_text=True)
    picker = re.search(r'<form method="get"[^>]*id="domain-picker">.*?</form>', page, re.S)
    return page.replace(picker.group(0), '<form id="domain-picker"></form>') if picker else page


def csrf(world):
    """Fact 355: `login_required(csrf=True)` checks the token itself. Made once a test: a second `generate_csrf`
    answers from `g` and puts nothing in the session."""
    if getattr(world, 'token', None):
        return world.token
    from flask import session as flask_session
    from flask_wtf.csrf import generate_csrf
    from flask import g
    with world.app.test_request_context():
        g.pop('csrf_token', None)   # a page rendered earlier in the test left one there, unsaved in this session
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with world.client.session_transaction() as session:
        session['csrf_token'] = raw
    world.token = token
    return token


def post(world, path, **kwargs):
    return world.client.post(path, data={'csrf_token': csrf(world), **kwargs.pop('data', {})}, **kwargs)


LISTS = [
    ('administer all users', '/admin/users?local_remote=local', 'xxuser', 'yyuser'),
    ('administer all users', '/admin/reports', 'xx user report', 'yy user report'),
    ('administer all users', '/admin/reports', 'xx post report', 'yy post report'),
    ('administer all users', '/admin/referrals', 'xxuser', 'yyuser'),
    ('administer communities pages', '/admin/communities', 'xxcomm', 'yycomm'),
    ('administer communities pages', '/admin/content?show=trash&days=0', 'xx post title', 'yy post title'),
    ('approve registrations', '/admin/approve_registrations', 'xx answer', 'yy answer'),
    ('edit domain branding', '/admin/domains', 'x.scoped.example', 'y.scoped.example'),
]


@pytest.mark.parametrize('permission,path,mine,theirs', LISTS, ids=[f'{p}:{path}:{m}' for p, path, m, _ in LISTS])
def test_a_holder_on_x_sees_only_xs_rows(world, permission, path, mine, theirs):
    holder(world, 'xholder', permission, domain=world.x)
    page = body(world, path)
    assert mine in page and theirs not in page


@pytest.mark.parametrize('permission,path,mine,theirs', LISTS, ids=[f'{p}:{path}:{m}' for p, path, m, _ in LISTS])
def test_an_instance_wide_holder_sees_both_and_the_picker_narrows(world, permission, path, mine, theirs):
    holder(world, 'wideholder', permission)
    page = body(world, path)
    assert mine in page and theirs in page
    assert 'id="domain-picker"' in page
    narrowed = body(world, f'{path}{"&" if "?" in path else "?"}domain={world.x.id}')
    assert mine in narrowed and theirs not in narrowed
    again = body(world, path)   # the pick is kept, so the next page is narrowed too
    assert theirs not in again
    assert theirs in body(world, f'{path}{"&" if "?" in path else "?"}domain=0')


def test_a_holder_on_one_domain_is_told_which_and_cannot_pick_another(world):
    holder(world, 'xholder', 'administer all users', domain=world.x)
    page = body(world, f'/admin/users?local_remote=local&domain={world.y.id}')
    assert 'Showing x.scoped.example.' in page and 'yyuser' not in page


def test_a_holder_on_both_domains_picks_between_them(world):
    user = holder(world, 'bothholder', 'administer all users', domain=world.x)
    role = Role.query.filter_by(name='role-bothholder').one()
    db.session.execute(role_grant.insert().values(user_id=user.id, role_id=role.id, site_domain_id=world.y.id))
    db.session.commit()
    page = body(world, '/admin/users?local_remote=local')
    assert 'xxuser' in page and 'yyuser' in page
    assert 'All of mine' in world.client.get('/admin/users').get_data(as_text=True)


def test_nobody_without_the_permission_anywhere_opens_a_list(world):
    holder(world, 'nobody', 'view modlog', domain=world.x)
    for _, path, _, _ in LISTS:
        response = world.client.get(path)
        assert response.status_code == 302 and 'permission_denied' in response.headers['Location'], path


def test_the_domains_page_hides_adding_and_deleting_from_a_branding_holder(world):
    holder(world, 'brander', 'edit domain branding', domain=world.x)
    page = body(world, '/admin/domains')
    assert 'Add a domain' not in page and 'admin/domains/' + str(world.x.id) + '/delete' not in page
    assert f'/admin/domains/{world.x.id}/branding' in page
    response = post(world, '/admin/domains', data={'domain': 'new.example', 'scheme': 'https'})
    assert response.status_code == 302 and 'permission_denied' in response.headers['Location']
    assert SiteDomain.query.filter_by(domain='new.example').first() is None


def test_an_admin_still_sees_every_domain_and_the_add_form(world):
    admin = make_user(world.local, 'boss', local=True)
    grant_role(admin, admin_role())
    login(world.client, admin)
    page = body(world, '/admin/domains')
    assert 'Add a domain' in page and 'y.scoped.example' in page


# --------------------------------------------------------------------------
# Acting on one object
# --------------------------------------------------------------------------

def test_a_holder_on_x_edits_xs_community_and_not_ys(world):
    holder(world, 'xcomms', 'administer communities pages', domain=world.x)
    with patch('app.admin.routes.render_template', return_value='rendered'):   # its template needs a real CSRF field
        assert world.client.get(f'/admin/community/{world.on_x[1].id}/edit').status_code == 200
    response = world.client.get(f'/admin/community/{world.on_y[1].id}/edit')
    assert response.status_code == 302 and 'permission_denied' in response.headers['Location']
    response = post(world, f'/admin/community/{world.on_y[1].id}/delete')
    assert response.status_code == 302 and 'permission_denied' in response.headers['Location']
    assert world.on_y[1].banned is not True


def test_a_holder_on_x_edits_xs_users_but_not_ys_nor_one_who_holds_a_grant(world):
    """RB4 and RB6."""
    me = holder(world, 'xusers', 'administer all users', domain=world.x)
    assert world.client.get(f'/admin/user/{world.on_x[0].id}/edit').status_code == 200
    for target in (world.on_y[0], me):
        response = world.client.get(f'/admin/user/{target.id}/edit')
        assert response.status_code == 302 and 'permission_denied' in response.headers['Location'], target.user_name
    response = post(world, f'/admin/user/{world.on_y[0].id}/delete')
    assert response.status_code == 302 and 'permission_denied' in response.headers['Location']
    assert world.on_y[0].banned is not True


def test_an_instance_wide_holder_who_is_not_admin_cannot_edit_or_delete_an_admin(world):
    """D1178, through may_act_on_user: the edit form can ban, so it is refused as a ban would be."""
    holder(world, 'wideusers', 'administer all users')
    admin = make_user(world.local, 'boss', local=True)
    grant_role(admin, admin_role())
    response = world.client.get(f'/admin/user/{admin.id}/edit')
    assert response.status_code == 302 and 'permission_denied' in response.headers['Location']
    response = post(world, f'/admin/user/{admin.id}/delete')
    assert response.status_code == 302 and 'permission_denied' in response.headers['Location']
    assert world.client.get(f'/admin/user/{world.on_y[0].id}/edit').status_code == 200


def test_a_holder_on_x_approves_xs_registrations_and_not_ys(world):
    holder(world, 'xapprover', 'approve registrations', domain=world.x)
    post(world, f'/admin/approve_registrations/{world.on_x[0].id}/approve')
    assert UserRegistration.query.filter_by(user_id=world.on_x[0].id).one().status == 1
    for action in ('approve', 'deny'):
        response = post(world, f'/admin/approve_registrations/{world.on_y[0].id}/{action}')
        assert response.status_code == 302 and 'permission_denied' in response.headers['Location']
    assert UserRegistration.query.filter_by(user_id=world.on_y[0].id).one().status == 0


def test_a_holder_on_x_may_resend_xs_verification_email_only(world):
    holder(world, 'xmailer', 'administer all users', domain=world.x)
    response = post(world, f'/admin/user/{world.on_y[0].id}/resend_email', headers={'HX-Request': 'true'})
    assert response.status_code == 302 and 'permission_denied' in response.headers['Location']


# --------------------------------------------------------------------------
# The modlog (RB9) and the API's lists
# --------------------------------------------------------------------------

def modlog_entries(world):
    from app.models import ModLog
    entries = {}
    for tag, (user, community, _) in (('xx', world.on_x), ('yy', world.on_y)):
        for public in (True, False):
            name = f'{tag}-{"public" if public else "private"}-reason'
            entry = ModLog(user_id=world.founder.id, type='admin', action='ban_user', target_user_id=user.id,
                           reason=name, link=f'u/{tag}{public}', link_text=f'{tag}{public}', public=public)
            db.session.add(entry)
            entries[name] = entry
    db.session.commit()
    return entries


def test_a_holder_on_x_sees_xs_whole_modlog_and_the_public_rest(world):
    modlog_entries(world)
    holder(world, 'xmodlog', 'view modlog', 'administer all communities', domain=world.x)
    page = world.client.get('/modlog').get_data(as_text=True)
    assert 'xxFalse' in page and 'xxTrue' in page and 'yyTrue' in page and 'yyFalse' not in page
    assert 'xx-private-reason' in page and 'yy-public-reason' not in page   # reasons for their own domain only


def test_an_instance_wide_holder_filters_the_modlog_by_domain(world):
    modlog_entries(world)
    holder(world, 'widemodlog', 'view modlog')
    page = world.client.get('/modlog').get_data(as_text=True)
    assert 'xxFalse' in page and 'yyFalse' in page and 'id="modlog-domain"' in page
    narrowed = world.client.get(f'/modlog?domain={world.x.id}').get_data(as_text=True)
    assert 'xxFalse' in narrowed and 'yyFalse' not in narrowed and 'yyTrue' not in narrowed


def test_a_grant_logged_on_a_domain_is_that_domains_entry(world):
    """A grant names the domain it was made on (ModLog.site_domain_id), so it is in that domain's log."""
    from app.visibility import modlog_domain_clause
    from app.models import ModLog
    entry = ModLog(user_id=world.founder.id, type='admin', action='grant_role', site_domain_id=world.x.id,
                   link_text='a grant', public=False)
    db.session.add(entry)
    db.session.commit()
    assert ModLog.query.filter(modlog_domain_clause(frozenset({world.x.id}))).all() == [entry]
    assert ModLog.query.filter(modlog_domain_clause(frozenset({world.y.id}))).count() == 0


def bearer(user):
    return f'Bearer {user.encode_jwt_token()}'


def test_the_apis_modlog_shows_a_holder_on_x_xs_private_entries_only(world):
    from flask import g
    from app.api.alpha.utils.misc import get_modlog
    from app.models import Site
    modlog_entries(world)
    user = holder(world, 'apimodlog', 'view modlog', domain=world.x)
    g.site = db.session.get(Site, 1)
    reasons = {entry['mod_ban']['reason'] for entry in get_modlog(bearer(user), {'type_': 'ModBan', 'limit': 50})['banned']}
    assert reasons == {'xx-public-reason', 'xx-private-reason', 'yy-public-reason'}


def test_the_apis_report_lists_name_a_holder_on_xs_reports_only(world):
    from flask import g
    from app.api.alpha.utils.post import get_post_report_list
    from app.models import Site
    user = holder(world, 'apireports', 'moderate all communities', domain=world.x)
    g.site, g.admin_ids = db.session.get(Site, 1), []
    reasons = {r['post_report']['reason'] for r in get_post_report_list(bearer(user), {})['post_reports']}
    assert reasons == {'xx post report'}


def test_the_apis_registrations_are_a_holder_on_xs_only(world):
    from flask import g
    from app.api.alpha.utils.admin import get_registration_list, put_registration_approve
    from app.models import Site
    user = holder(world, 'apiapprover', 'approve registrations', domain=world.x)
    g.site, g.admin_ids = db.session.get(Site, 1), []
    answers = {r['answer'] for r in get_registration_list(bearer(user), {})['registrations']}
    assert answers == {'xx answer'}
    with pytest.raises(Exception, match='Insufficient permissions'):
        put_registration_approve(bearer(user), {'user_id': world.on_y[0].id, 'approve': True})
    assert UserRegistration.query.filter_by(user_id=world.on_y[0].id).one().status == 0


def test_a_local_only_domain_is_offered_by_the_picker_on_its_own_storefront_alone(app, db_session):
    """Follow-up F6: it is never named off its own domain, and an admin there still needs to pick it."""
    from app.admin.scope import domain_choices
    make_instance('test.piefed.local', software='piefed')
    closed = SiteDomain(domain='closed.scoped.example', scheme='https', federates=False)
    db.session.add(closed)
    db.session.commit()
    clear_domain_cache()
    try:
        with patch('app.admin.scope.current_storefront', return_value=domain_by_id(closed.id)):
            assert (closed.id, closed.domain) in domain_choices(None)
        with patch('app.admin.scope.current_storefront', return_value=default_domain()):
            assert (closed.id, closed.domain) not in domain_choices(None)
    finally:
        clear_domain_cache()


# --------------------------------------------------------------------------
# RB13: creating accounts on a domain
# --------------------------------------------------------------------------

@pytest.fixture
def modes(app, db_session):
    """Domains in each mode, and what the add-user form needs. The default domain's own mode is the site's."""
    local = make_instance('test.piefed.local', software='piefed')
    founder = make_user(local, 'founder', local=True)
    assert founder.id == 1
    rows = {mode: SiteDomain(domain=f'{mode}.modes.example', scheme='https', federates=True, registration=mode)
            for mode in ('anyone', 'staff', 'admin')}
    db.session.add_all(rows.values())
    db.session.commit()
    clear_domain_cache()
    yield type('Modes', (), dict(app=app, client=app.test_client(), local=local, founder=founder, rows=rows))
    clear_domain_cache()


def add_user(world, domain, name='newbie'):
    data = {'user_name': name, 'email': f'{name}@example.com', 'password': 'password123',
            'password2': 'password123', 'ignore_bots': '0', 'hide_nsfw': '1', 'hide_nsfl': '1', 'grant_scope': '0',
            'site_domain': str(domain.id), 'submit': 'Save', 'csrf_token': csrf(world)}
    with patch('app.admin.routes.finalize_user_setup'):
        return world.client.post('/admin/users/add', data=data, content_type='multipart/form-data')


def grant_on(world, name, *domains):
    """A holder of `administer all users` on each of `domains`, signed in."""
    user = holder(world, name, 'administer all users', domain=domains[0])
    role = Role.query.filter_by(name=f'role-{name}').one()
    for domain in domains[1:]:
        db.session.execute(role_grant.insert().values(user_id=user.id, role_id=role.id, site_domain_id=domain.id))
    db.session.commit()
    return user


@pytest.mark.parametrize('mode,allowed', [('staff', True), ('admin', True), ('anyone', False)])
def test_a_holder_on_a_domain_creates_its_accounts_only_in_a_closed_mode(modes, mode, allowed):
    """RB13. Under 'anyone' the public signs up there, and a scoped holder is offered nothing."""
    from app.models import User
    grant_on(modes, 'scoped', modes.rows[mode])
    add_user(modes, modes.rows[mode])
    created = User.query.filter_by(user_name='newbie').first()
    assert (created is not None) is allowed
    if allowed:
        assert created.site_domain_id == modes.rows[mode].id


def test_a_holder_on_one_domain_cannot_create_an_account_on_another(modes):
    from app.models import User
    grant_on(modes, 'scoped', modes.rows['staff'])
    add_user(modes, modes.rows['admin'])
    assert User.query.filter_by(user_name='newbie').first() is None


def test_the_form_offers_a_scoped_holder_only_the_domains_they_may_create_on(modes):
    grant_on(modes, 'scoped', modes.rows['staff'], modes.rows['anyone'])
    with patch('app.admin.routes.render_template', return_value='rendered') as render:
        modes.client.get('/admin/users/add')
    assert [c.label for c in render.call_args.kwargs['form'].site_domain.choices] == ['staff.modes.example']


def test_a_holder_with_nowhere_to_create_is_refused_the_form_and_not_offered_the_button(modes):
    grant_on(modes, 'scoped', modes.rows['anyone'])
    response = modes.client.get('/admin/users/add')
    assert response.status_code == 302 and 'permission_denied' in response.headers['Location']
    assert 'Add local user' not in body(modes, '/admin/users?local_remote=local')


def test_a_holder_who_may_create_is_offered_the_button(modes):
    grant_on(modes, 'scoped', modes.rows['staff'])
    assert 'Add local user' in body(modes, '/admin/users?local_remote=local')


@pytest.mark.parametrize('mode', ['anyone', 'staff', 'admin'])
def test_an_instance_wide_holder_creates_accounts_on_any_domain(modes, mode):
    from app.models import User
    holder(modes, 'wide', 'administer all users')
    add_user(modes, modes.rows[mode])
    assert User.query.filter_by(user_name='newbie').first() is not None


def test_nobody_without_the_permission_opens_the_form(modes):
    holder(modes, 'other', 'ban users', domain=modes.rows['staff'])
    response = modes.client.get('/admin/users/add')
    assert response.status_code == 302 and 'permission_denied' in response.headers['Location']

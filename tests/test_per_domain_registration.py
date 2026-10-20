"""Each hosted domain's `registration` setting decides who may create its accounts (plan B task 4)."""
from unittest.mock import patch

import pytest
from flask import g

from app import db
from app.api.alpha.views import site_view
from app.models import Instance, Role, RolePermission, Site, SiteDomain, User, user_role
from app.storefront import clear_domain_cache, domain_for_host, public_registration_open
from tests.factories import make_instance, make_user
from tests.test_admin_user_administration import _add_payload
from tests.test_community_lifecycle import csrf, login
from tests.test_onion_guard_limits import FROM_CADDY, MARKER, ONION_URL, a_session, onion, onion_get, onion_post  # noqa: F401

pytestmark = pytest.mark.usefixtures('site')

CLOSED_TEXT = 'Registration is closed. Only admins can create accounts.'
FORM_TEXT = 'Create new account'


def _mode(mode):
    db.session.get(Site, 1).registration_mode = mode
    db.session.commit()


def _onion_row(registration):
    row = SiteDomain.query.filter_by(domain='abc.onion').one()
    row.registration = registration
    db.session.commit()
    clear_domain_cache()
    return row


def _register_payload(**overrides):
    data = {'user_name': 'newcomer', 'real_email': 'newcomer@example.com', 'password': 'password123',
            'password2': 'password123', 'timezone': 'UTC', 'submit': 'Register'}
    data.update(overrides)
    return data


def test_the_onion_register_page_refuses(app, onion):
    _mode('Open')
    _onion_row('admin')
    client = app.test_client()
    a_session(client)
    page = onion_get(client, '/auth/register')
    assert CLOSED_TEXT in page.get_data(as_text=True)
    assert FORM_TEXT not in page.get_data(as_text=True)
    posted = onion_post(client, '/auth/register', data=_register_payload())
    assert CLOSED_TEXT in posted.get_data(as_text=True)
    assert User.query.filter_by(user_name='newcomer').count() == 0


def test_a_staff_row_refuses_public_sign_up(app, onion):
    _mode('Open')
    _onion_row('staff')
    client = app.test_client()
    a_session(client)
    assert CLOSED_TEXT in onion_get(client, '/auth/register').get_data(as_text=True)
    onion_post(client, '/auth/register', data=_register_payload())
    assert User.query.filter_by(user_name='newcomer').count() == 0


@pytest.mark.parametrize('mode,closed', [('Open', False), ('RequireApplication', False), ('Closed', True)])
def test_an_anyone_row_follows_the_site_registration_mode(app, onion, mode, closed, monkeypatch):
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', True)   # the open form renders a csrf_token field
    monkeypatch.setattr('app.auth.routes.handle_abandoned_open_instance', lambda: None)   # no admin has logged in here
    _mode(mode)
    _onion_row('anyone')
    client = app.test_client()
    a_session(client)
    text = onion_get(client, '/auth/register').get_data(as_text=True)
    assert (CLOSED_TEXT in text) is closed
    assert (FORM_TEXT in text) is not closed


def test_public_registration_open_is_true_only_for_anyone(app, onion):
    for registration, expected in (('anyone', True), ('staff', False), ('admin', False)):
        _onion_row(registration)
        assert public_registration_open(domain_for_host('abc.onion')) is expected


def test_api_registration_follows_the_storefront(app, onion):
    """The API has no register endpoint yet (D1181); what it can do is advertise the mode, and a refused domain
    advertises Closed."""
    _mode('Open')
    for registration, advertised in (('anyone', 'Open'), ('admin', 'Closed'), ('staff', 'Closed')):
        _onion_row(registration)
        with app.test_request_context():
            g.site = db.session.get(Site, 1)
            g.storefront = domain_for_host('abc.onion')
            assert site_view(None)['site']['registration_mode'] == advertised


def _staff_and_admin(onion_row_registration):
    instance = db.session.get(Instance, 1) or make_instance('test.piefed.local')
    make_user(instance, 'founder', local=True)
    staff = make_user(instance, 'staffer', local=True)
    staff.verified = True
    admin = make_user(instance, 'useradmin', local=True)
    admin.verified = True
    db.session.commit()
    staff_role = Role(name='Staff', weight=2)
    admin_role = Role(name='Admin', weight=3)
    db.session.add_all([Role(name='Anonymous user', weight=0), Role(name='Authenticated user', weight=1),
                        staff_role, admin_role])
    db.session.commit()
    for role in (staff_role, admin_role):
        db.session.add(RolePermission(role_id=role.id, permission='administer all users'))
        db.session.add(RolePermission(role_id=role.id, permission='change user roles'))
    db.session.execute(user_role.insert().values(user_id=staff.id, role_id=staff_role.id))
    db.session.execute(user_role.insert().values(user_id=admin.id, role_id=admin_role.id))
    db.session.commit()
    return _onion_row(onion_row_registration), staff, admin


def _add_user(app, who, domain_id):
    client = app.test_client()
    g.pop('_login_user', None)   # g outlives a request here, and holds the previous request's user
    g.pop('csrf_token', None)    # generate_csrf caches in g
    login(client, who)
    token = csrf(app, client)
    with patch('app.admin.routes.render_template', return_value='rendered'):
        with patch('app.admin.routes.finalize_user_setup'):
            return client.post('/admin/users/add', data=_add_payload(token, site_domain=str(domain_id)),
                               content_type='multipart/form-data')


def test_adding_a_user_to_an_admin_row_needs_an_admin(app, onion):
    row, staff, admin = _staff_and_admin('admin')
    assert _add_user(app, staff, row.id).status_code == 403
    assert User.query.filter_by(user_name='newbie').count() == 0
    _add_user(app, admin, row.id)
    assert User.query.filter_by(user_name='newbie').one().site_domain_id == row.id


def test_adding_a_user_to_a_staff_row_allows_staff(app, onion):
    row, staff, admin = _staff_and_admin('staff')
    _add_user(app, staff, row.id)
    assert User.query.filter_by(user_name='newbie').one().site_domain_id == row.id


@pytest.mark.parametrize('row, mode, staff_may', [('admin', 'Open', True), ('admin', 'RequireApplication', True),
                                                  ('anyone', 'Closed', False)])
def test_adding_a_default_domain_user_follows_the_site_mode_not_its_row(app, onion, row, mode, staff_may):
    """F7: the default domain's own `registration` column is dead data; Site.registration_mode is its live rule,
    and a Closed site's accounts are made by an admin alone, as its register page says."""
    _, staff, admin = _staff_and_admin('anyone')
    _mode(mode)
    _default_row(row)
    default = SiteDomain.query.filter_by(is_default=True).one()
    assert (_add_user(app, staff, default.id).status_code != 403) is staff_may
    assert User.query.filter_by(user_name='newbie').count() == (1 if staff_may else 0)
    if not staff_may:
        _add_user(app, admin, default.id)
        assert User.query.filter_by(user_name='newbie').count() == 1


def _default_row(registration):
    row = SiteDomain.query.filter_by(is_default=True).one()
    row.registration = registration
    db.session.commit()
    clear_domain_cache()


@pytest.mark.parametrize('row,mode,closed', [('admin', 'Open', False), ('anyone', 'Closed', True)])
def test_the_default_domain_follows_the_site_mode_whatever_its_row_says(app, onion, monkeypatch, row, mode, closed):
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', True)
    monkeypatch.setattr('app.auth.routes.handle_abandoned_open_instance', lambda: None)
    _mode(mode)
    _default_row(row)
    text = app.test_client().get('/auth/register').get_data(as_text=True)
    assert (CLOSED_TEXT in text) is closed
    assert (FORM_TEXT in text) is not closed


def test_a_new_oauth_account_is_refused_off_the_default_domain(app, onion):
    from app.auth.oauth_util import refuse_new_oauth_account
    _mode('Open')
    _onion_row('anyone')
    for host, refused in (('abc.onion', True), ('test.piefed.local', False)):
        with app.test_request_context():
            g.site = db.session.get(Site, 1)
            g.storefront = domain_for_host(host)
            assert (refuse_new_oauth_account() is not None) is refused


def test_an_existing_oauth_user_logs_in_on_a_refused_storefront(app, onion):
    from app.auth.oauth_util import handle_oauth_authorize
    _mode('Open')
    _onion_row('admin')
    instance = db.session.get(Instance, 1) or make_instance('test.piefed.local')
    make_user(instance, 'founder', local=True)
    existing = make_user(instance, 'oauthy', local=True)
    existing.google_oauth_id = 'g-1'
    db.session.commit()
    with app.test_request_context():
        g.site = db.session.get(Site, 1)
        g.storefront = domain_for_host('abc.onion')
        with patch('app.auth.oauth_util.get_token_and_user_info', return_value=('t', {'id': 'g-1'})), \
                patch('app.auth.oauth_util.get_country', return_value=None), \
                patch('app.auth.oauth_util.finalize_user_login', return_value='logged in') as login_:
            assert handle_oauth_authorize('google', 'x', 'google_oauth_id') == 'logged in'
    login_.assert_called_once()
    assert User.query.filter_by(google_oauth_id='g-1').count() == 1

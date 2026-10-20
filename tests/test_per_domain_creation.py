"""New local accounts and communities are created on their domain, with ActivityPub URLs built from it (plan B task 2)."""
import io
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import g

from app import db
from app.auth.oauth_util import initialize_new_user
from app.auth.util import create_new_user, create_new_user_from_ldap
from app.activitypub.util import find_microblogging_community
from app.models import Community, Instance, Language, Role, RolePermission, Site, SiteDomain, User, user_role
from app.storefront import clear_domain_cache, default_domain, domain_base_url, domain_by_id
from app.utils import finalize_user_setup
from tests.factories import make_instance, make_user
from tests.test_admin_user_administration import _add_payload, _roles
from tests.test_community_lifecycle import _add_payload as _community_payload
from tests.test_community_lifecycle import able_to_create, csrf, login, url

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture
def domains(db_session):
    """The default clearnet row and an onion row, over the local instance row (id 1)."""
    make_instance('test.piefed.local')
    default = db.session.query(SiteDomain).filter_by(is_default=True).first()
    if default is None:
        default = SiteDomain(domain='default.example', is_default=True)
        db.session.add(default)
    onion = SiteDomain(domain='abc.onion', scheme='http', federates=False)
    db.session.add(onion)
    db.session.commit()
    clear_domain_cache()
    yield default, onion
    clear_domain_cache()


def _form(name):
    return SimpleNamespace(user_name=SimpleNamespace(data=name), real_email=SimpleNamespace(data=f'{name}@example.com'),
                           password=SimpleNamespace(data='password123'), timezone=SimpleNamespace(data='UTC'))


def test_the_default_base_url_is_server_url_exactly(app, db_session, domains):
    assert domain_base_url(default_domain()) == app.config['SERVER_URL']
    clear_domain_cache()
    db.session.query(SiteDomain).delete()
    db.session.commit()
    clear_domain_cache()
    assert domain_base_url(default_domain()) == app.config['SERVER_URL']


def test_an_onion_base_url_is_its_scheme_and_host(app, db_session, domains):
    default, onion = domains
    assert domain_base_url(default_domain()) == app.config['SERVER_URL']
    assert domain_base_url(domain_by_id(onion.id)) == 'http://abc.onion'


def test_registering_on_a_storefront_stamps_its_domain(app, db_session, domains):
    default, onion = domains
    with app.test_request_context(base_url='http://abc.onion'):
        app.preprocess_request()
        on_onion = create_new_user(_form('wakko'), '127.0.0.1', 'GB', 'token')
    with app.test_request_context(base_url=f'https://{default.domain}'):
        app.preprocess_request()
        on_default = create_new_user(_form('yakko'), '127.0.0.1', 'GB', 'token')
    assert on_onion.site_domain_id == onion.id
    assert on_default.site_domain_id == default.id


def test_a_default_domain_user_gets_exactly_todays_urls(app, db_session, domains):
    default, onion = domains
    user = make_user(None, 'Dot', local=True)
    user.site_domain_id = default.id
    db.session.commit()
    finalize_user_setup(user)
    base = app.config['SERVER_URL']
    assert user.ap_profile_id == f'{base}/u/dot'
    assert user.ap_public_url == f'{base}/u/Dot'
    assert user.ap_inbox_url == f'{base}/u/dot/inbox'


def test_an_onion_user_gets_onion_urls(app, db_session, domains):
    default, onion = domains
    user = make_user(None, 'Dot', local=True)
    user.site_domain_id = onion.id
    db.session.commit()
    finalize_user_setup(user)
    assert user.ap_profile_id == 'http://abc.onion/u/dot'
    assert user.ap_public_url == 'http://abc.onion/u/Dot'
    assert user.ap_inbox_url == 'http://abc.onion/u/dot/inbox'


def test_an_admin_added_onion_user_gets_onion_urls(app, db_session, domains):
    default, onion = domains
    instance = db.session.get(Instance, 1)
    make_user(instance, 'founder', local=True)
    admin = make_user(instance, 'useradmin', local=True)
    admin.verified = True
    db.session.commit()
    roles = _roles()
    db.session.add(RolePermission(role_id=roles['Admin'].id, permission='administer all users'))
    db.session.add(RolePermission(role_id=roles['Admin'].id, permission='change user roles'))
    db.session.execute(user_role.insert().values(user_id=admin.id, role_id=roles['Admin'].id))
    db.session.commit()
    client = app.test_client()
    login(client, admin)
    token = csrf(app, client)
    with patch('app.admin.routes.render_template', return_value='rendered'):
        client.post('/admin/users/add', data=_add_payload(token, site_domain=str(onion.id)),
                    content_type='multipart/form-data')
    user = User.query.filter_by(user_name='newbie').one()
    assert user.site_domain_id == onion.id
    assert user.ap_profile_id == 'http://abc.onion/u/newbie'
    assert user.ap_inbox_url == 'http://abc.onion/u/newbie/inbox'


def test_an_admin_added_user_defaults_to_the_default_domain(app, db_session, domains):
    default, onion = domains
    instance = db.session.get(Instance, 1)
    make_user(instance, 'founder', local=True)
    admin = make_user(instance, 'useradmin', local=True)
    admin.verified = True
    db.session.commit()
    roles = _roles()
    db.session.add(RolePermission(role_id=roles['Admin'].id, permission='administer all users'))
    db.session.add(RolePermission(role_id=roles['Admin'].id, permission='change user roles'))
    db.session.execute(user_role.insert().values(user_id=admin.id, role_id=roles['Admin'].id))
    db.session.commit()
    client = app.test_client()
    login(client, admin)
    token = csrf(app, client)
    with patch('app.admin.routes.render_template', return_value='rendered'):
        client.post('/admin/users/add', data=_add_payload(token), content_type='multipart/form-data')
    user = User.query.filter_by(user_name='newbie').one()
    assert user.site_domain_id == default.id
    assert user.ap_profile_id == f"{app.config['SERVER_URL']}/u/newbie"


def test_ldap_and_oauth_users_belong_to_the_default_domain(app, db_session, domains):
    default, onion = domains
    with app.test_request_context(base_url='http://abc.onion'):
        app.preprocess_request()
        ldap_user = create_new_user_from_ldap('ldapper', 'ldapper@example.com', 'password123', '127.0.0.1')
        with patch('app.auth.oauth_util.finalize_user_setup'):
            oauth_user = initialize_new_user('newcomer@example.com', 'New', 'google_oauth_id', {'id': 'r1'},
                                             '127.0.0.1', 'GB')
    assert ldap_user.site_domain_id == default.id
    assert oauth_user.site_domain_id == default.id


def test_the_microblogs_community_belongs_to_the_default_domain(app, db_session, domains):
    default, onion = domains
    make_user(db.session.get(Instance, 1), 'founder', local=True)
    community = find_microblogging_community()
    assert community.site_domain_id == default.id
    assert community.ap_profile_id == f"{app.config['SERVER_URL']}/c/microblogs"
    assert community.ap_domain == app.config['SERVER_NAME']


@pytest.fixture
def creators(app, db_session, domains):
    default, onion = domains
    instance = db.session.get(Instance, 1)
    founder = make_user(instance, 'founder', local=True)
    able_to_create(founder)
    founder.site_domain_id = default.id
    other = make_user(instance, 'onionite', local=True)
    able_to_create(other)
    other.site_domain_id = onion.id
    db.session.add(Language(code='und', name='Undetermined'))
    db.session.add(Language(code='en', name='English'))
    db.session.get(Site, 1).community_creation_admin_only = False
    db.session.commit()
    return founder, other


def _create(app, user, host, **overrides):
    """Create a community as `user`, over a request addressed to `host`."""
    from flask import session as flask_session
    from flask_wtf.csrf import generate_csrf
    base_url = f'http://{host}'
    client = app.test_client()
    g.pop('_login_user', None)   # g outlives a request here, and holds the previous request's user
    with app.test_request_context():
        g.pop('csrf_token', None)   # generate_csrf caches in g, which the app context keeps between calls
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with client.session_transaction(base_url=base_url) as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True
        session['csrf_token'] = raw
    with patch('app.community.routes.render_template', return_value='rendered'):
        with patch('app.community.routes.task_selector'):
            client.post(url(app, 'community.add_local'), data=_community_payload(token, **overrides),
                        content_type='multipart/form-data', base_url=base_url)
    return Community.query.filter_by(name=overrides['url']).one()


def test_a_community_belongs_to_its_creators_home_domain(app, creators, domains):
    default, onion = domains
    founder, other = creators
    base = app.config['SERVER_URL']

    made = _create(app, founder, 'abc.onion', url='clearnet')
    assert made.site_domain_id == default.id
    assert made.ap_profile_id == f'{base}/c/clearnet'
    assert made.ap_public_url == f'{base}/c/clearnet'
    assert made.ap_followers_url == f'{base}/c/clearnet/followers'
    assert made.ap_domain == app.config['SERVER_NAME']

    made = _create(app, other, 'abc.onion', url='hidden')
    assert made.site_domain_id == onion.id
    assert made.ap_profile_id == 'http://abc.onion/c/hidden'
    assert made.ap_public_url == 'http://abc.onion/c/hidden'
    assert made.ap_moderators_url == 'http://abc.onion/c/hidden/moderators'
    assert made.ap_domain == 'abc.onion'

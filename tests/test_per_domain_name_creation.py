"""A local name is unique per domain at creation, through every path (plan C1 task 5).

`bob` on the first domain and `bob` on the onion are two accounts: a name another domain holds no longer refuses a
creation, and a name the same domain holds still does. Feed names are per domain too (per-domain feeds, task 9).
Every test asserts the effect, the second row existing or the refusal happening, not a mock of the check."""
from unittest.mock import MagicMock, patch

import pytest
from flask import g
from flask_login import login_user
from werkzeug.datastructures import MultiDict
from wtforms.validators import ValidationError

from app import cli, db
from app.admin.forms import MoveCommunityForm
from app.auth.forms import RegistrationForm
from app.auth.oauth_util import find_new_username
from app.auth.util import can_be_a_local_user_name, create_new_user
from app.community.forms import AddCommunityForm
from app.constants import SRC_API
from app.feed.forms import AddCopyFeedForm
from app.models import Community, Feed, Instance, RolePermission, Site, User, user_role
from app.shared.community import make_community
from app.storefront import default_domain
from app.utils import user_name_is_taken
from tests.factories import bearer, make_user
from tests.test_admin_user_administration import _add_payload, _roles
from tests.test_community_lifecycle import csrf, login
from tests.test_per_domain_creation import _api_creator, _form, creators, domains  # noqa: F401  (fixtures)
from tests.test_shared_community_lifecycle import _api_input, _seed_und_language
from tests.two_domains import FIRST, ONION, default_request, onion_request, two_domains  # noqa: F401  (fixture)

pytestmark = pytest.mark.usefixtures('site')


def _admin(app, domains):
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
    return client, csrf(app, client)


def _admin_adds(client, token, name, **overrides):
    with patch('app.admin.routes.render_template', return_value='rendered'):
        with patch('app.admin.routes.finalize_user_setup'):
            return client.post('/admin/users/add', data=_add_payload(token, user_name=name, **overrides),
                               content_type='multipart/form-data')


def _count(name):
    return User.query.filter(db.func.lower(User.user_name) == name.lower(), User.ap_id == None).count()  # noqa: E711


def _registration_error(app, name):
    """The refusal `RegistrationForm.validate_user_name` raises in the current request, or None."""
    form = RegistrationForm(meta={'csrf': False})
    form.user_name.data = name
    try:
        form.validate_user_name(form.user_name)
    except ValidationError as error:
        return str(error)
    return None


# --------------------------------------------------------------------------
# accounts
# --------------------------------------------------------------------------

def test_an_admin_can_add_an_onion_bob_when_first_has_one(app, domains):
    default, onion = domains
    first_bob = make_user(None, 'bob', local=True)
    first_bob.site_domain_id = default.id
    db.session.commit()
    client, token = _admin(app, domains)

    response = _admin_adds(client, token, 'bob', site_domain=str(onion.id))

    assert response.status_code == 302
    assert sorted(u.site_domain_id for u in User.query.filter_by(user_name='bob')) == sorted([default.id, onion.id])


def test_registering_first_bob_works_when_only_the_onion_has_a_bob(app, two_domains):
    first, onion = two_domains.domains
    two_domains.local_user('bob', onion)
    with default_request(app):
        g.site = db.session.get(Site, 1)
        assert _registration_error(app, 'bob') is None
        made = create_new_user(_form('bob'), '127.0.0.1', 'GB', 'token')
    assert made.site_domain_id == first.id
    assert _count('bob') == 2


def test_an_admin_added_bob_is_refused_on_the_domain_that_has_one(app, domains):
    default, onion = domains
    first_bob = make_user(None, 'bob', local=True)
    first_bob.site_domain_id = default.id
    onion_alice = make_user(None, 'alice', local=True)
    onion_alice.site_domain_id = onion.id
    db.session.commit()
    client, token = _admin(app, domains)

    _admin_adds(client, token, 'BOB', site_domain=str(default.id))
    _admin_adds(client, token, 'alice', site_domain=str(onion.id))

    assert _count('bob') == 1
    assert _count('alice') == 1


def test_a_registration_name_is_refused_on_its_own_domain(app, two_domains):
    first, onion = two_domains.domains
    two_domains.local_user('bob', first)
    two_domains.local_user('alice', onion)
    two_domains.local_community('pets', onion)
    with default_request(app):
        assert _registration_error(app, 'Bob') == 'An account with this user name already exists.'
        assert _registration_error(app, 'alice') is None
        assert _registration_error(app, 'pets') is None
    with onion_request(app):
        assert _registration_error(app, 'ALICE') == 'An account with this user name already exists.'
        assert _registration_error(app, 'pets') == 'This name is in use already.'
        assert _registration_error(app, 'bob') is None


def test_user_name_is_taken_asks_about_one_domain(app, two_domains):
    first, onion = two_domains.domains
    two_domains.local_user('bob', onion)
    two_domains.local_community('pets', first)
    assert user_name_is_taken('bob', onion.id)
    assert not user_name_is_taken('bob', first.id)
    assert not user_name_is_taken('Bob', None)
    assert user_name_is_taken('PETS', None)
    assert not user_name_is_taken('pets', onion.id)


def test_user_name_is_taken_requires_the_domain():
    with pytest.raises(TypeError):
        user_name_is_taken('bob')


def test_oauth_and_ldap_names_are_checked_on_the_default_domain(app, two_domains):
    first, onion = two_domains.domains
    two_domains.local_user('wakko', onion)
    two_domains.local_user('yakko', first)
    assert find_new_username('wakko@example.com') == 'wakko'
    assert find_new_username('yakko@example.com') != 'yakko'
    assert can_be_a_local_user_name('wakko')
    assert not can_be_a_local_user_name('yakko')


# --------------------------------------------------------------------------
# communities
# --------------------------------------------------------------------------

def test_a_community_name_on_one_domain_does_not_block_the_other(app, db_session, domains):
    default, onion = domains
    make_user(db.session.get(Instance, 1), 'founder', local=True)
    _seed_und_language()
    clearnet = _api_creator('clearnetter', default)
    onionite = _api_creator('onionite', onion)
    make_community(_api_input(name='pets'), SRC_API, bearer(clearnet))
    make_community(_api_input(name='Pets'), SRC_API, bearer(onionite))
    assert sorted(c.site_domain_id for c in Community.query.filter_by(name='pets')) == sorted([default.id, onion.id])
    with pytest.raises(Exception, match='already exists'):
        make_community(_api_input(name='PETS'), SRC_API, bearer(onionite))
    assert Community.query.filter_by(name='pets').count() == 2


def test_a_user_name_on_one_domain_does_not_block_a_community_on_the_other(app, db_session, domains):
    default, onion = domains
    make_user(db.session.get(Instance, 1), 'founder', local=True)
    _seed_und_language()
    clearnet = _api_creator('clearnetter', default)
    onionite = _api_creator('onionite', onion)
    make_community(_api_input(name='ONIONITE'), SRC_API, bearer(clearnet))
    assert Community.query.filter_by(name='onionite', site_domain_id=default.id).count() == 1
    with pytest.raises(Exception, match='User with that name'):
        make_community(_api_input(name='onionite'), SRC_API, bearer(onionite))
    assert Community.query.filter_by(name='onionite').count() == 1


def _community_form(app, user, url):
    """The refusal messages `AddCommunityForm.validate` gives `user` for `url`, over a request on user's domain."""
    data = MultiDict({'community_name': 'A Name', 'url': url, 'description': '', 'posting_warning': '',
                      'theme': '', 'invitations': '0'})
    with app.test_request_context('/', method='POST', data=data):
        login_user(user)
        form = AddCommunityForm(formdata=data)
        form.theme.choices = [('', 'None')]
        form.languages.choices = []
        accepted = form.validate()
        return accepted, [str(message) for message in form.url.errors]


def test_the_add_community_form_checks_the_creators_home_domain(app, two_domains):
    first, onion = two_domains.domains
    two_domains.local_community('pets', first)
    two_domains.local_user('wakko', first)
    creator = two_domains.local_user('creator', onion)
    assert _community_form(app, creator, 'pets') == (True, [])
    assert _community_form(app, creator, 'wakko') == (True, [])
    two_domains.local_community('toys', onion)
    two_domains.local_user('yakko', onion)
    assert _community_form(app, creator, 'toys') == (False, ['A community with this url already exists.'])
    assert _community_form(app, creator, 'YAKKO') == (False, ['This name is in use already.'])


def test_a_moved_community_url_is_checked_on_the_default_domain(app, two_domains):
    first, onion = two_domains.domains
    two_domains.local_community('pets', onion)
    two_domains.local_community('toys', first)
    with app.test_request_context('/', method='POST'):
        form = MoveCommunityForm(meta={'csrf': False})
        form.new_url.data = 'Pets'
        form.validate_new_url(form.new_url)
        form.new_url.data = 'Toys'
        with pytest.raises(ValidationError, match='already exists'):
            form.validate_new_url(form.new_url)


# --------------------------------------------------------------------------
# feeds
# --------------------------------------------------------------------------

def _feed_form(app, user, url):
    data = MultiDict({'title': 'A feed', 'url': url, 'communities': 'pets@elsewhere.example', 'public': 'y'})
    with app.test_request_context('/', method='POST', data=data):
        login_user(user)
        form = AddCopyFeedForm(formdata=data, meta={'csrf': False})
        form.parent_feed_id.choices = [(0, '')]
        accepted = form.validate()
        return accepted, [str(message) for message in form.url.errors]


def test_a_feed_name_is_per_domain(app, two_domains):
    """Per-domain feeds (task 9) made feed names unique per domain, as user and community names already were."""
    first, onion = two_domains.domains
    owner = two_domains.local_user('feedowner', onion)
    # a row with no domain stamped on it is the default domain's
    db.session.add(Feed(user_id=owner.id, title='f', name='shared', public=True, instance_id=two_domains.instance.id))
    db.session.commit()
    first_user = two_domains.local_user('firstfeeder', first)
    assert _feed_form(app, first_user, 'shared') == (False, ['A Feed with this url already exists.'])
    assert _feed_form(app, owner, 'shared') == (True, [])
    assert _feed_form(app, owner, 'fresh') == (True, [])
    # a feed name blocks a user or community name on its own domain only
    assert user_name_is_taken('shared', first.id) and not user_name_is_taken('shared', onion.id)


def test_a_feed_url_is_checked_against_names_on_the_owners_home_domain(app, two_domains):
    first, onion = two_domains.domains
    owner = two_domains.local_user('feedowner', onion)
    two_domains.local_user('wakko', first)
    two_domains.local_community('pets', first)
    two_domains.local_user('yakko', onion)
    two_domains.local_community('toys', onion)
    assert _feed_form(app, owner, 'wakko') == (True, [])
    assert _feed_form(app, owner, 'pets') == (True, [])
    assert _feed_form(app, owner, 'yakko') == (False, ['This name is in use already.'])
    assert _feed_form(app, owner, 'toys') == (False, ['A community with this url already exists.'])


# --------------------------------------------------------------------------
# every new local row has a domain
# --------------------------------------------------------------------------

def test_a_new_local_row_never_has_a_null_domain(app, domains):
    default, onion = domains
    client, token = _admin(app, domains)
    _admin_adds(client, token, 'nodomain')
    assert User.query.filter_by(user_name='nodomain').one().site_domain_id == default.id


def test_the_cli_admin_name_must_be_free(app, db_session, domains):
    cli.register(app)
    default, onion = domains
    taken = make_user(None, 'taken', local=True)
    taken.site_domain_id = default.id
    db.session.commit()
    fake_db = MagicMock()
    fake_db.inspect.return_value.get_table_names.return_value = ['alembic_version']
    fake_db.session.execute.return_value.fetchone.return_value = ('UTF8',)
    answers = iter(['taken', 'a@example.com', 'freeone', 'password123'])
    with patch('app.cli.db', fake_db), patch('builtins.input', lambda prompt='': next(answers)), \
            patch('app.cli.retrieve_block_list', return_value=''), \
            patch('app.cli.retrieve_peertube_block_list', return_value=''):
        result = app.test_cli_runner().invoke(args=['init-db', '--interactive', 'yes'], input='n\nn\nn\nn\nn\n')
    assert result.exception is None, result.exception
    added = [call.args[0] for call in fake_db.session.add.call_args_list if isinstance(call.args[0], User)]
    assert [u.user_name for u in added] == ['freeone']

"""The LDAP directory: binding to it, reading from it, writing to it.

Sub-project 83, slice D -- `app/ldap_utils.py` and the LDAP arm of
`app/auth/util.py`. Six defects, all measured:

* the login name went into a search FILTER unescaped, so `*` became
  `(uid=*)` -- every entry in the directory -- and the code takes
  `entries[0]`, whose address the local account is then built around
  (D1145);
* and into a bind DN unescaped, so the caller chose which subtree the bind
  was attempted against (D1146);
* a bind with an EMPTY password is an unauthenticated bind, which a
  directory server treats as anonymous, and an anonymous bind succeeds
  (D1147);
* the LDAP arm made none of the checks the local arm makes, so an account
  this instance had BANNED logged in anyway (D1148);
* a deleted account's name got a SECOND row, because `find_user` filters
  deleted rows out and nothing looked again (D1149);
* and `create_new_user_from_ldap` wrote whatever name it was handed --
  `admin`, or `we ird/../x` (D1150).
"""
from unittest.mock import MagicMock, patch

import pytest
from ldap3.core.exceptions import LDAPBindError, LDAPException

from app import db
from app.auth.util import (can_be_a_local_user_name, validate_user_ldap_login,
                           validate_user_login)
from app.auth.forms import LoginForm
# `test_ldap_connection` is a production function whose name pytest collects
# as a test if it is imported under it -- see fact 539.
from app.ldap_utils import login_with_ldap, sync_user_to_ldap
from app.ldap_utils import test_ldap_connection as check_the_connection
from app.models import Instance, Role, Site, User
from tests.factories import make_community, make_instance, make_user

pytestmark = pytest.mark.usefixtures('site')


def instance(domain='test.piefed.local', software='piefed'):
    """Fact 394."""
    existing = Instance.query.filter_by(domain=domain).first()
    return existing if existing is not None else make_instance(domain,
                                                               software=software)


@pytest.fixture
def env(app, db_session):
    site = db.session.get(Site, 1)
    site.private_instance = False
    db.session.commit()
    local = instance()
    founder = make_user(local, 'founder', local=True)
    assert founder.id == 1  # fact 347
    person = make_user(local, 'person', local=True)
    person.email = 'person@example.com'
    person.set_password('a-good-password')
    person.verified = True
    db.session.commit()
    return app.test_client(), person


class Attribute(str):
    """What `ldap3` hands back for an attribute: something that compares
    equal to its string and also carries `.value`."""

    @property
    def value(self):
        return str(self)


def an_entry(email='someone@example.com', dn='uid=someone,dc=example,dc=com'):
    entry = MagicMock()
    entry.entry_dn = dn
    entry.mail = Attribute(email)
    return entry


def with_entries(*entries):
    """The connection `_bind_user` hands back, and the patch that installs
    it. Every network call `ldap3` would make is behind this one class."""
    conn = MagicMock()
    conn.entries = list(entries)
    return patch('app.ldap_utils.Connection', return_value=conn), conn


def ldap_config(app, **overrides):
    config = {'LDAP_SERVER': 'ldap.example.com', 'LDAP_PORT': 389,
              'LDAP_READ_ENABLE': True, 'LDAP_WRITE_ENABLE': False,
              'LDAP_USE_SSL': False, 'LDAP_USE_TLS': False,
              'LDAP_BASE_DN': 'dc=example,dc=com',
              'LDAP_WRITE_BIND_DN': 'cn=writer,dc=example,dc=com',
              'LDAP_WRITE_BIND_PASSWORD': 'a-write-password'}
    config.update(overrides)
    return patch.dict(app.config, config)


# --------------------------------------------------------------------------
# _bind_user
# --------------------------------------------------------------------------


def test_an_unconfigured_directory_is_skipped(app, env):
    """No LDAP_SERVER is not an error; it means this instance does not use a
    directory, and every caller treats None as "carry on without it"."""
    client, person = env

    with ldap_config(app, LDAP_SERVER=''):
        assert login_with_ldap('person', 'a-good-password') is False


def test_an_empty_password_never_reaches_the_directory(app, env):
    """D1147. A simple bind carrying an empty password is an UNAUTHENTICATED
    bind, which RFC 4513 says a server should treat as anonymous -- and an
    anonymous bind succeeds, so the search below it ran and answered with
    somebody's address. Measured: `PROBE ax3 result: 'someone@example.com' |
    bind password: ''`."""
    client, person = env
    patcher, conn = with_entries(an_entry())

    with ldap_config(app):
        with patch('app.ldap_utils.Server'):
            with patcher as connection:
                result = login_with_ldap('person', '')

    assert result is False
    assert connection.call_args is None


def test_a_password_of_only_spaces_is_an_empty_password(app, env):
    client, person = env
    patcher, conn = with_entries(an_entry())

    with ldap_config(app):
        with patch('app.ldap_utils.Server'):
            with patcher as connection:
                result = login_with_ldap('person', '   ')

    assert result is False
    assert connection.call_args is None


def test_the_connection_carries_the_configured_transport(app, env):
    client, person = env
    patcher, conn = with_entries(an_entry())

    with ldap_config(app, LDAP_PORT=636, LDAP_USE_SSL=True):
        with patch('app.ldap_utils.Server') as server:
            with patcher:
                login_with_ldap('person', 'a-good-password')

    assert server.call_args.kwargs['port'] == 636
    assert server.call_args.kwargs['use_ssl'] is True


def test_tls_is_started_when_it_is_configured(app, env):
    client, person = env
    patcher, conn = with_entries(an_entry())

    with ldap_config(app, LDAP_USE_TLS=True):
        with patch('app.ldap_utils.Server'):
            with patcher:
                login_with_ldap('person', 'a-good-password')

    conn.start_tls.assert_called_once()


@pytest.mark.parametrize('raised', [LDAPBindError('bind refused'),
                                    LDAPException('no route to host'),
                                    Exception('something else')])
def test_a_bind_that_fails_is_no_login(app, env, raised):
    """Three arms, one answer: whatever the directory or the library does,
    the caller gets False and the local password path runs instead."""
    client, person = env

    with ldap_config(app):
        with patch('app.ldap_utils.Server'):
            with patch('app.ldap_utils.Connection', side_effect=raised):
                assert login_with_ldap('person', 'a-good-password') is False


# --------------------------------------------------------------------------
# D1145, D1146 -- what goes into the filter and the DN
# --------------------------------------------------------------------------


def test_a_wildcard_name_does_not_widen_the_search(app, env):
    """D1145. `(uid=*)` matches every entry in the directory and the code
    takes `entries[0]`, so the address it answers with -- the address the
    local account is then built around -- belongs to whoever sorts first.
    Measured: `PROBE ax1 result: 'someone@example.com' | search:
    '(uid=*)'`."""
    client, person = env
    patcher, conn = with_entries(an_entry())

    with ldap_config(app):
        with patch('app.ldap_utils.Server'):
            with patcher:
                login_with_ldap('*', 'a-good-password')

    assert conn.search.call_args.kwargs['search_filter'] == r'(uid=\2a)'


def test_a_name_carrying_dn_syntax_does_not_move_the_bind(app, env):
    """D1146. Measured: `PROBE ax2 bind dn:
    'uid=bob,ou=admins,dc=example,dc=com'` -- the caller chose the subtree."""
    client, person = env
    patcher, conn = with_entries(an_entry())

    with ldap_config(app):
        with patch('app.ldap_utils.Server'):
            with patcher as connection:
                login_with_ldap('bob,ou=admins', 'a-good-password')

    assert connection.call_args.kwargs['user'] == \
        r'uid=bob\,ou\=admins,dc=example,dc=com'


# --------------------------------------------------------------------------
# login_with_ldap
# --------------------------------------------------------------------------


def test_reading_is_skipped_when_it_is_disabled(app, env):
    """Skipped, not merely unsuccessful: a directory this instance does not
    read from is never contacted."""
    client, person = env
    patcher, conn = with_entries(an_entry())

    with ldap_config(app, LDAP_READ_ENABLE=False):
        with patch('app.ldap_utils.Server'):
            with patcher as connection:
                assert login_with_ldap('person', 'a-good-password') is False

    assert connection.call_args is None


def test_a_bind_that_works_answers_with_the_address(app, env):
    client, person = env
    patcher, conn = with_entries(an_entry('someone@example.com'))

    with ldap_config(app):
        with patch('app.ldap_utils.Server'):
            with patcher:
                assert login_with_ldap('person', 'a-good-password') == \
                    'someone@example.com'
    conn.unbind.assert_called_once()


def test_a_directory_with_no_such_entry_is_no_login(app, env):
    """The bind succeeded and the search found nothing -- which a
    permissively configured directory can do."""
    client, person = env
    patcher, conn = with_entries()

    with ldap_config(app):
        with patch('app.ldap_utils.Server'):
            with patcher:
                assert login_with_ldap('person', 'a-good-password') is False


@pytest.mark.parametrize('raised', [LDAPException('search failed'),
                                    Exception('something else')])
def test_a_search_that_fails_is_no_login(app, env, raised):
    client, person = env
    patcher, conn = with_entries(an_entry())
    conn.search.side_effect = raised

    with ldap_config(app):
        with patch('app.ldap_utils.Server'):
            with patcher:
                assert login_with_ldap('person', 'a-good-password') is False
    conn.unbind.assert_called_once()


# --------------------------------------------------------------------------
# sync_user_to_ldap
# --------------------------------------------------------------------------


def test_writing_is_skipped_when_it_is_disabled(app, env):
    """"True if sync was successful **or skipped**" -- the caller cannot tell
    an instance with no directory from one that wrote."""
    client, person = env
    patcher, conn = with_entries(an_entry())

    with ldap_config(app, LDAP_WRITE_ENABLE=False):
        with patch('app.ldap_utils.Server'):
            with patcher as connection:
                assert sync_user_to_ldap('person', 'person@example.com',
                                         'pw') is True

    assert connection.call_args is None


def test_a_write_bind_that_fails_is_not_an_error(app, env):
    client, person = env

    with ldap_config(app, LDAP_WRITE_ENABLE=True, LDAP_SERVER=''):
        assert sync_user_to_ldap('person', 'person@example.com', 'pw') is True


def test_a_changed_address_is_written_to_the_entry(app, env):
    client, person = env
    patcher, conn = with_entries(an_entry('old@example.com'))
    conn.modify.return_value = True

    with ldap_config(app, LDAP_WRITE_ENABLE=True):
        with patch('app.ldap_utils.Server'):
            with patcher:
                assert sync_user_to_ldap('person', 'new@example.com',
                                         '') is True

    changes = conn.modify.call_args.args[1]
    assert changes['mail'][0][1] == ['new@example.com']
    assert 'userPassword' not in changes


def test_a_new_password_is_written_to_the_entry(app, env):
    client, person = env
    patcher, conn = with_entries(an_entry('same@example.com'))
    conn.modify.return_value = True

    with ldap_config(app, LDAP_WRITE_ENABLE=True):
        with patch('app.ldap_utils.Server'):
            with patcher:
                sync_user_to_ldap('person', 'new@example.com', 'a-password')

    assert 'userPassword' in conn.modify.call_args.args[1]


def test_an_entry_with_nothing_to_change_is_left_alone(app, env):
    client, person = env
    patcher, conn = with_entries(an_entry('same@example.com'))

    with ldap_config(app, LDAP_WRITE_ENABLE=True):
        with patch('app.ldap_utils.Server'):
            with patcher:
                assert sync_user_to_ldap('person', 'same@example.com',
                                         '') is True

    conn.modify.assert_not_called()


def test_a_modify_the_directory_refuses_is_reported(app, env):
    client, person = env
    patcher, conn = with_entries(an_entry('old@example.com'))
    conn.modify.return_value = False

    with ldap_config(app, LDAP_WRITE_ENABLE=True):
        with patch('app.ldap_utils.Server'):
            with patcher:
                assert sync_user_to_ldap('person', 'new@example.com',
                                         '') is False


def test_an_account_with_no_password_is_not_created(app, env):
    """A directory entry has to carry a password, and this path is reached
    on a profile edit that did not change one."""
    client, person = env
    patcher, conn = with_entries()

    with ldap_config(app, LDAP_WRITE_ENABLE=True):
        with patch('app.ldap_utils.Server'):
            with patcher:
                assert sync_user_to_ldap('person', 'person@example.com',
                                         '  ') is True

    conn.add.assert_not_called()


def test_an_account_the_directory_does_not_have_is_created(app, env):
    client, person = env
    patcher, conn = with_entries()
    conn.add.return_value = True

    with ldap_config(app, LDAP_WRITE_ENABLE=True):
        with patch('app.ldap_utils.Server'):
            with patcher:
                assert sync_user_to_ldap('Person', 'person@example.com',
                                         'a-password') is True

    assert conn.add.call_args.args[0] == 'uid=person,dc=example,dc=com'
    assert conn.add.call_args.kwargs['attributes']['objectClass'] == \
        ['inetOrgPerson']


def test_a_name_carrying_filter_syntax_is_escaped_on_the_write_side(app, env):
    """D1145's other end. Measured: `PROBE ax8 search: '(uid=bob)(uid=*)' |
    added: 'uid=bob)(uid=*,dc=example,dc=com'`."""
    client, person = env
    patcher, conn = with_entries()
    conn.add.return_value = True

    with ldap_config(app, LDAP_WRITE_ENABLE=True):
        with patch('app.ldap_utils.Server'):
            with patcher:
                sync_user_to_ldap('bob)(uid=*', 'bob@example.com', 'a-password')

    assert conn.search.call_args.kwargs['search_filter'] == \
        r'(uid=bob\29\28uid=\2a)'
    assert conn.add.call_args.args[0] == r'uid=bob)(uid\=*,dc=example,dc=com'


def test_an_add_the_directory_refuses_is_reported(app, env):
    client, person = env
    patcher, conn = with_entries()
    conn.add.return_value = False

    with ldap_config(app, LDAP_WRITE_ENABLE=True):
        with patch('app.ldap_utils.Server'):
            with patcher:
                assert sync_user_to_ldap('person', 'person@example.com',
                                         'a-password') is False


@pytest.mark.parametrize('raised', [LDAPException('search failed'),
                                    Exception('something else')])
def test_a_sync_that_fails_is_reported(app, env, raised):
    client, person = env
    patcher, conn = with_entries()
    conn.search.side_effect = raised

    with ldap_config(app, LDAP_WRITE_ENABLE=True):
        with patch('app.ldap_utils.Server'):
            with patcher:
                assert sync_user_to_ldap('person', 'person@example.com',
                                         'a-password') is False
    conn.unbind.assert_called_once()


# --------------------------------------------------------------------------
# test_ldap_connection
# --------------------------------------------------------------------------


def test_the_connection_test_binds_and_lets_go(app, env):
    client, person = env
    patcher, conn = with_entries()

    with ldap_config(app):
        with patch('app.ldap_utils.Server'):
            with patcher:
                assert check_the_connection() is True
    conn.unbind.assert_called_once()


def test_the_connection_test_reports_a_directory_it_cannot_reach(app, env):
    client, person = env

    with ldap_config(app, LDAP_SERVER=''):
        assert check_the_connection() is False


# --------------------------------------------------------------------------
# D1148, D1149, D1150 -- the account the directory resolves to
# --------------------------------------------------------------------------


def authenticated_as(email='person@example.com'):
    return patch('app.auth.util.login_with_ldap', return_value=email)


def test_a_directory_login_finds_the_local_account(app, env):
    """The feature, which every guard below must leave working."""
    client, person = env

    with app.test_request_context('/'):
        with authenticated_as():
            assert validate_user_ldap_login('person', 'pw', '127.0.0.1') == person


def test_a_banned_account_may_not_log_in_through_the_directory(app, env):
    """D1148. The LDAP arm made none of the checks the local arm makes -- a
    directory bind was the whole of it. Measured: `PROBE ax4 user: <User
    person_2> | banned: True`."""
    client, person = env
    person.banned = True
    db.session.commit()

    with app.test_request_context('/'):
        with authenticated_as():
            with patch('app.auth.util.flash'):
                assert validate_user_ldap_login('person', 'pw',
                                                '127.0.0.1') is False


def test_a_banned_ip_may_not_log_in_through_the_directory(app, env):
    client, person = env

    with app.test_request_context('/'):
        with authenticated_as():
            with patch('app.auth.util.user_ip_banned', return_value=True):
                with patch('app.auth.util.flash'):
                    assert validate_user_ldap_login('person', 'pw',
                                                    '127.0.0.1') is False


def test_a_deleted_account_gets_no_second_row(app, env):
    """D1149. `find_user` filters deleted rows out, so the name looked free
    and a SECOND `person` was written. Measured: `PROBE ax5 users before: 2 |
    after: 3 | names: ['founder', 'person', 'person']`."""
    client, person = env
    person.deleted = True
    db.session.commit()
    before = User.query.count()

    with app.test_request_context('/'):
        with authenticated_as():
            with patch('app.auth.util.flash'):
                result = validate_user_ldap_login('person', 'pw', '127.0.0.1')

    assert result is False
    assert User.query.count() == before


def test_a_reserved_name_cannot_arrive_from_the_directory(app, env):
    """D1150. Measured: `PROBE ax6 admin exists: True`."""
    client, person = env

    with app.test_request_context('/'):
        with authenticated_as('admin@example.com'):
            with patch('app.auth.util.flash'):
                assert validate_user_ldap_login('admin', 'pw',
                                                '127.0.0.1') is False
    assert User.query.filter_by(user_name='admin').first() is None


def test_a_name_outside_the_charset_cannot_arrive_from_the_directory(app, env):
    """D1150. Measured: `PROBE ax7 names: ['founder', 'person', 'we
    ird/../x']` -- and a local user name is interpolated into an actor URL, a
    webfinger answer and a feed regex."""
    client, person = env

    with app.test_request_context('/'):
        with authenticated_as('weird@example.com'):
            with patch('app.auth.util.flash'):
                assert validate_user_ldap_login('we ird/../x', 'pw',
                                                '127.0.0.1') is False
    assert User.query.count() == 2


def test_a_name_a_community_holds_cannot_arrive_from_the_directory(app, env):
    client, person = env
    make_community('general')
    db.session.commit()

    assert can_be_a_local_user_name('general') is False


def test_a_directory_account_nobody_has_yet_is_created(app, env):
    """The other half of the feature: a name that CAN be a local account
    gets one, and the same name resolves to it next time."""
    client, person = env

    with app.test_request_context('/'):
        from flask import g
        g.site = db.session.get(Site, 1)
        with authenticated_as('newcomer@example.com'):
            with patch('app.auth.util.finalize_user_setup'):
                user = validate_user_ldap_login('newcomer', 'pw', '127.0.0.1')

    assert user.user_name == 'newcomer'
    assert user.email == 'newcomer@example.com'
    assert user.verified is True


def test_the_first_account_on_an_empty_instance_is_an_admin(app, env):
    """`users_total() == 0`: whoever arrives first on an instance with nobody
    on it gets the admin role, or there is no way to administer it."""
    client, person = env
    db.session.add(Role(id=4, name='Admin'))
    db.session.commit()

    with app.test_request_context('/'):
        from flask import g
        g.site = db.session.get(Site, 1)
        with authenticated_as('newcomer@example.com'):
            with patch('app.auth.util.finalize_user_setup'):
                with patch('app.auth.util.users_total', return_value=0):
                    user = validate_user_ldap_login('newcomer', 'pw',
                                                    '127.0.0.1')

    assert db.session.get(Role, 4) in user.roles


def test_a_directory_that_refuses_falls_through_to_the_local_password(app, env):
    """None, not False: the caller then tries the local password, which is
    what an instance with both keeps working for."""
    client, person = env

    with app.test_request_context('/'):
        with patch('app.auth.util.login_with_ldap', return_value=False):
            with patch('app.auth.util.flash') as flashed:
                assert validate_user_ldap_login('person', 'pw',
                                                '127.0.0.1') is None
    assert 'Login failed' in str(flashed.call_args.args[0])


def test_a_local_login_still_refuses_a_banned_account(app, env):
    """The local arm's pin: the ban is tested AFTER the password, or the ban
    message tells a caller which accounts exist and which are banned."""
    client, person = env
    person.banned = True
    db.session.commit()

    with app.test_request_context('/'):
        with patch('app.auth.util.flash'):
            assert validate_user_login(person, 'a-good-password',
                                       '127.0.0.1') is False
            assert validate_user_login(person, 'the-wrong-password',
                                       '127.0.0.1') is False


def test_a_local_login_still_works(app, env):
    client, person = env

    with app.test_request_context('/'):
        assert validate_user_login(person, 'a-good-password',
                                   '127.0.0.1') is True


# --------------------------------------------------------------------------
# process_login, with a directory configured
# --------------------------------------------------------------------------


def login(client, **overrides):
    data = {'user_name': 'person', 'password': 'a-good-password',
            'timezone': 'UTC', 'submit': 'Log In'}
    data.update(overrides)
    return client.post('/auth/login', data=data)


def test_a_directory_login_signs_the_account_in(app, env):
    """The whole flow: the directory authenticates, the local account is
    resolved, and nothing is written back to the directory -- the password
    came from there in the first place."""
    client, person = env

    with ldap_config(app):
        with authenticated_as():
            with patch('app.auth.util.sync_user_to_ldap') as wrote:
                response = login(client)

    assert response.status_code == 302
    assert response.headers['Location'] != '/auth/login'
    wrote.assert_not_called()


def test_a_banned_account_cannot_use_the_directory_to_get_in(app, env):
    """D1148 through the route. A local password does not overrule the
    refusal either -- the request ends here."""
    client, person = env
    person.banned = True
    db.session.commit()

    with ldap_config(app):
        with authenticated_as():
            response = login(client)

    assert response.headers['Location'] == '/auth/login'


def test_a_directory_that_does_not_know_them_falls_back_to_the_password(app,
                                                                       env):
    """An instance with both keeps working for the accounts the directory
    has never heard of."""
    client, person = env

    with ldap_config(app):
        with patch('app.auth.util.login_with_ldap', return_value=False):
            with patch('app.auth.util.sync_user_to_ldap'):
                response = login(client)

    assert response.status_code == 302
    assert response.headers['Location'] != '/auth/login'


def test_the_founder_is_not_locked_out_by_an_ip_ban(app, env):
    """`user.id != 1`: whoever set the instance up keeps a way back in."""
    client, person = env
    founder = db.session.get(User, 1)
    founder.set_password('a-good-password')
    founder.verified = True
    db.session.commit()

    with app.test_request_context('/'):
        with patch('app.auth.util.user_ip_banned', return_value=True):
            assert validate_user_login(founder, 'a-good-password',
                                       '127.0.0.1') is True


def test_a_deleted_account_cannot_log_in_locally(app, env):
    """And is told what every other failure is told, because saying which
    accounts were deleted is the enumeration D1131 closed."""
    client, person = env
    person.deleted = True
    db.session.commit()

    with app.test_request_context('/'):
        with patch('app.auth.util.flash') as flashed:
            assert validate_user_login(person, 'a-good-password',
                                       '127.0.0.1') is False
    assert 'Invalid user name or password' in str(flashed.call_args.args[0])

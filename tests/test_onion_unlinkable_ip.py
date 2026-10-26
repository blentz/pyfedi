"""Stored IPs never reveal onion use (onion unlinkability spec 2026-10-26, goals 2 and 2a).

- An onion request never rewrites a clearnet account's `ip_address` or `ip_address_country`, nor an instance's address.
- An account whose home domain is the onion always stores `169.254.88.88` and no country, wherever and however it
  acts, and is never sent to ipinfo or to the ban-check servers.
"""
import importlib.util
import itertools
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from flask import g
from sqlalchemy import text

from app import db
from app.auth.oauth_util import finalize_user_login
from app.auth.util import get_country, update_user_session
from app.constants import POST_TYPE_ARTICLE, SRC_API
from app.models import Instance, Language, User
from app.onion import ONION_ACCOUNT_IP, apply_audit, audit_field
from app.onion_account import audit_country, audit_ip, is_onion_account
from app.shared.auth import api_log_user_in
from app.shared.post import make_post
from app.shared.reply import make_reply
from app.shared.tasks.users import check_user_application
from app.utils import set_setting
from tests.factories import bearer, make_post as make_post_row, make_site
from tests.test_onion_marker import REGISTRATION, ONION, PROXY, onion_context, proxy_ip, registration_env
from tests.test_shared_post_edit import _api_input
from tests.test_shared_tasks_users import _lowest_randint, _no_sleep, _recording_client, _seed
from tests.two_domains import default_request, two_domains  # noqa: F401

CLEARNET_IP = '203.0.113.7'
PASSWORD = 'correct horse battery'
FORM = SimpleNamespace(timezone=SimpleNamespace(data='UTC'))
MIGRATION = Path(__file__).resolve().parent.parent / 'migrations' / 'versions' / 'e7b3d9a4c2f8_onion_account_ips.py'


def account(two, domain, name, ip=CLEARNET_IP, country='NZ'):
    user = two.local_user(name, domain)
    user.set_password(PASSWORD)
    user.private_key = 'test-key-not-a-real-pem'
    user.ip_address = ip
    user.ip_address_country = country
    db.session.commit()
    return user


def web_login(user):
    update_user_session(user, FORM, '198.51.100.4', 'FR')


def oauth_login(user):
    finalize_user_login(user, None, '198.51.100.4', 'FR')


def api_login(user):
    with patch('app.shared.auth.sync_user_to_ldap'):
        api_log_user_in({'username': user.email, 'password': PASSWORD})


def a_post(user):
    make_post(_api_input(), WORLD['community'], POST_TYPE_ARTICLE, SRC_API, auth=bearer(user))


def a_reply(user):
    make_reply({'body': f'reply number {next(REPLY_NUMBERS)}', 'notify_author': False, 'language_id': None, 'distinguished': False},
               WORLD['post'], None, SRC_API, auth=bearer(user))


REPLY_NUMBERS = itertools.count()   # the same words twice is a duplicate reply
ACTIONS = [web_login, oauth_login, api_login, a_post, a_reply]
WORLD = {}


@pytest.fixture
def world(two_domains):
    """A community and a post on the first domain, a site row, and `g.site`."""
    first, onion = two_domains.domains
    WORLD['community'] = two_domains.local_community('talk', first)
    quiet = two_domains.local_community('quiet', first)
    quiet.private = True   # stops a reply's federation tasks at their first guard
    g.site = make_site()
    author = two_domains.local_user('author', first)
    WORLD['post'] = make_post_row(quiet, author, 'https://elsewhere.example/p/1')
    db.session.add(Language(code='en', name='English'))
    db.session.commit()
    return two_domains


def refreshed(user):
    db.session.expire_all()
    return db.session.get(User, user.id)


@pytest.mark.parametrize('action', ACTIONS, ids=lambda action: action.__name__)
def test_onion_login_keeps_a_clearnet_accounts_ip_and_country(app, world, action):
    user = account(world, world.domains[0], 'clearuser')
    with proxy_ip(app), onion_context(app):
        action(user)
    user = refreshed(user)
    assert (user.ip_address, user.ip_address_country) == (CLEARNET_IP, 'NZ')


@pytest.mark.parametrize('action', [web_login, oauth_login])
def test_clearnet_login_still_updates_the_ip(app, world, action):
    user = account(world, world.domains[0], 'clearuser')
    with default_request(app):
        action(user)
    user = refreshed(user)
    assert (user.ip_address, user.ip_address_country) == ('198.51.100.4', 'FR')


def test_a_clearnet_account_keeps_its_country_when_login_finds_none(app, world):
    user = account(world, world.domains[0], 'clearuser')
    with default_request(app):
        update_user_session(user, FORM, '198.51.100.4', '')
    assert refreshed(user).ip_address_country == 'NZ'


def test_clearnet_api_post_and_reply_still_update_the_ip(app, world):
    user = account(world, world.domains[0], 'clearuser')
    with default_request(app), patch('app.shared.post.ip_address', return_value='198.51.100.4'), \
            patch('app.shared.reply.ip_address', return_value='198.51.100.5'):
        a_post(user)
        assert refreshed(user).ip_address == '198.51.100.4'
        a_reply(user)
    assert refreshed(user).ip_address == '198.51.100.5'


def test_onion_registered_account_always_has_the_sentinel(app, world):
    registration_env()
    with proxy_ip(app), patch('app.auth.forms.get_setting', return_value=False), \
            patch('app.auth.util.send_email_verification'), patch('app.auth.util.sync_user_with_ldap'), \
            patch('app.auth.routes.onion_proof_refused', return_value=False), \
            patch('app.auth.util.get_country') as geolocated:
        client = app.test_client()
        assert client.get('/auth/register', headers=ONION, environ_base={'REMOTE_ADDR': PROXY}).status_code == 303
        response = client.post('/auth/register', data={**REGISTRATION, 'user_name': 'onionperson'},
                               headers={**ONION, 'CF-IPCountry': 'NZ'}, environ_base={'REMOTE_ADDR': PROXY})
    assert response.status_code == 302
    geolocated.assert_not_called()
    user = User.query.filter_by(user_name='onionperson').one()
    assert is_onion_account(user)
    user.set_password(PASSWORD)
    user.private_key = 'test-key-not-a-real-pem'
    user.verified = True
    db.session.commit()
    assert (user.ip_address, user.ip_address_country) == (ONION_ACCOUNT_IP, None)
    for action in ACTIONS:
        for context in (default_request, None):
            if context:
                with context(app), patch('app.shared.post.ip_address', return_value=CLEARNET_IP), \
                        patch('app.shared.reply.ip_address', return_value=CLEARNET_IP):
                    action(user)
            else:
                with proxy_ip(app), onion_context(app):
                    action(user)
            user = refreshed(user)
            assert (user.ip_address, user.ip_address_country) == (ONION_ACCOUNT_IP, None), action.__name__


def test_a_legacy_onion_account_is_overwritten_by_every_writer(app, world):
    user = account(world, world.domains[1], 'legacy')
    for action in ACTIONS:
        user.ip_address, user.ip_address_country = CLEARNET_IP, 'NZ'
        db.session.commit()
        with default_request(app):
            action(user)
        user = refreshed(user)
        assert user.ip_address == ONION_ACCOUNT_IP, action.__name__
        if action not in (a_post, a_reply):   # a post or reply never writes the country; the migration clears legacy ones
            assert user.ip_address_country is None, action.__name__


def test_no_ipinfo_lookup_for_an_onion_account(app, world):
    user = account(world, world.domains[1], 'onionuser')
    with default_request(app), patch('app.auth.util.get_request') as fetched, \
            patch.dict(app.config, {'IPINFO_TOKEN': 'token', 'COUNTRY_SOURCE_HEADER': ''}):
        assert get_country(CLEARNET_IP, 'unknown', user=user) == 'unknown'
        fetched.assert_not_called()
        clearnet = account(world, world.domains[0], 'clearuser')
        fetched.return_value = SimpleNamespace(status_code=200, json=lambda: {'country': 'NZ'})
        assert get_country('198.51.100.40', user=clearnet) == 'NZ'
        fetched.assert_called_once()


def test_ban_check_sends_no_ip_for_an_onion_account(app, world, monkeypatch):
    s = _seed(ip=ONION_ACCOUNT_IP)
    s.user.site_domain_id = world.domains[1].id
    db.session.commit()
    set_setting('ban_check_servers', 'one.example')
    _no_sleep(monkeypatch)
    _lowest_randint(monkeypatch)
    client = _recording_client(monkeypatch, (200, [False]))
    check_user_application(s.application.id)
    assert [url for url, _ in client.posts] == ['https://one.example/api/is_email_banned']


def test_ban_check_still_sends_a_clearnet_accounts_ip(app, world, monkeypatch):
    s = _seed()
    s.user.site_domain_id = world.domains[0].id
    db.session.commit()
    set_setting('ban_check_servers', 'one.example')
    _no_sleep(monkeypatch)
    _lowest_randint(monkeypatch)
    client = _recording_client(monkeypatch, (200, [False]), (200, [False]))
    check_user_application(s.application.id)
    assert [url for url, _ in client.posts] == ['https://one.example/api/is_ip_banned',
                                                'https://one.example/api/is_email_banned']


def test_instance_ip_unchanged_by_an_onion_request(app, db_session):
    instance = Instance(domain='remote.example', ip_address='198.51.100.9')
    db.session.add(instance)
    db.session.commit()
    with proxy_ip(app), onion_context(app):
        apply_audit(instance, 'ip_address', audit_field('192.0.2.1'))
    assert instance.ip_address == '198.51.100.9'
    with proxy_ip(app), app.test_request_context('/'):
        apply_audit(instance, 'ip_address', audit_field('192.0.2.1'))
    assert instance.ip_address == '192.0.2.1'


def test_the_audit_helpers_by_account_and_request(app, world):
    onion_user = account(world, world.domains[1], 'onionuser')
    clear_user = account(world, world.domains[0], 'clearuser')
    with proxy_ip(app), onion_context(app):
        assert audit_ip(onion_user, '1.2.3.4') == ONION_ACCOUNT_IP
        assert audit_country(onion_user, 'NZ') is None
        assert audit_ip(clear_user, '1.2.3.4') is audit_field('x') and audit_country(clear_user, 'NZ') is audit_field('x')
    with default_request(app):
        assert audit_ip(onion_user, '1.2.3.4') == ONION_ACCOUNT_IP
        assert audit_country(onion_user, 'NZ') is None
        assert audit_ip(clear_user, '1.2.3.4') == '1.2.3.4' and audit_country(clear_user, 'NZ') == 'NZ'


def load_migration():
    spec = importlib.util.spec_from_file_location('onion_account_ips_migration', MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_revision_chain():
    migration = load_migration()
    assert (migration.revision, migration.down_revision) == ('e7b3d9a4c2f8', 'c4e8a2f6d1b9')


def test_migration_sets_existing_onion_accounts_to_the_sentinel(app, world):
    onion = account(world, world.domains[1], 'onionuser')
    clear = account(world, world.domains[0], 'clearuser')
    remote = Instance.query.filter_by(domain='test.piefed.local').one()
    unhomed = User(user_name='unhomed', email='u@example.com', instance_id=remote.id, ip_address=CLEARNET_IP,
                   ip_address_country='NZ')
    db.session.add(unhomed)
    db.session.commit()
    ids = (onion.id, clear.id, unhomed.id)
    db.session.close()
    migration = load_migration()
    with db.engine.connect() as connection:
        transaction = connection.begin()
        try:
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                migration.downgrade()    # nothing to restore, and it must not fail
            rows = connection.execute(text('SELECT id, ip_address, ip_address_country FROM "user" WHERE id = ANY(:ids)'),
                                      {'ids': list(ids)}).all()
        finally:
            transaction.rollback()
    assert {row.id: (row.ip_address, row.ip_address_country) for row in rows} == {
        onion.id: (ONION_ACCOUNT_IP, None), clear.id: (CLEARNET_IP, 'NZ'), unhomed.id: (CLEARNET_IP, 'NZ')}

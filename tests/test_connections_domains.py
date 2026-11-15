"""Connected accounts: per-domain apps (spec 2026-11-13-connected-accounts-design.md §18, rulings C8-C12).

A second domain, shop.example, may have its own Meta app, Instagram app, Google client and Bluesky key; its members
use them, else the instance's, and sign in on their own domain. Its admins hold `manage domain connected accounts`
on it alone."""
import hashlib
import hmac
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from app import db, storefront
from app.connections import SERVICES, Permanent, QuotaWait, setting_name
from app.connections import bluesky, facebook, instagram, youtube
from app.connections.apps import (App, app_for, connections_offered, current, domain_app, domain_switch, enabled,
                                  enabled_services, for_domain, instance_app, remove_domain_app, save_domain_app,
                                  set_domain_switch)
from app.connections.dispatch import perform, reconnect_domain
from app.connections.tokens import seal_tokens
from app.models import ConnectionApp, SiteDomain, SocialConnection, User
from app.sealing import generate_key
from app.storefront import clear_domain_cache
from app.utils import set_setting
from tests.connections_fake import BASE, FakeService, fake_service  # noqa: F401 (fixture)
from tests.factories import grant_permission, limit_role, make_instance, make_user
from tests.test_community_add_post import csrf, login, poster  # noqa: F401 (poster is a fixture)
from tests.test_connections import SENT, a_post, a_row, fresh, world  # noqa: F401 (world is a fixture)

pytestmark = pytest.mark.usefixtures('site')

SHOP = 'shop.example'


@pytest.fixture
def shop(app, db_session, fake_service):
    if SiteDomain.query.filter_by(is_default=True).first() is None:
        db.session.add(SiteDomain(domain='test.piefed.local', scheme='https', is_default=True))
    row = SiteDomain(domain=SHOP, scheme='https')
    db.session.add(row)
    db.session.commit()
    clear_domain_cache()
    yield storefront.domain_by_id(row.id)
    clear_domain_cache()


@pytest.fixture
def meta_env(app, monkeypatch):
    monkeypatch.setitem(app.config, 'FACEBOOK_APP_ID', 'instance-meta')
    monkeypatch.setitem(app.config, 'FACEBOOK_APP_SECRET', 'instance-secret')


# --------------------------------------------------------------------------
# which app (C8)
# --------------------------------------------------------------------------

def test_the_instance_apps_come_from_the_environment(app, monkeypatch, meta_env):
    assert instance_app('facebook') == App('facebook', 'instance-meta', {'client_secret': 'instance-secret'}, None)
    assert instance_app('youtube') is None and instance_app('unknown') is None
    monkeypatch.setitem(app.config, 'BLUESKY_OAUTH_KEY', 'a-key')
    assert instance_app('bluesky').get('key') == 'a-key'


def test_a_domains_own_app_comes_first(app, shop, meta_env, monkeypatch):
    assert app_for('facebook', shop.id).client_id == 'instance-meta'
    assert save_domain_app(shop.id, 'facebook', 'shop-meta', {'client_secret': 'shop-secret'}) is False
    db.session.commit()
    assert app_for('facebook', shop.id) == App('facebook', 'shop-meta', {'client_secret': 'shop-secret'}, shop.id)
    assert app_for('facebook', None).client_id == 'instance-meta'
    assert save_domain_app(shop.id, 'facebook', 'shop-meta', {'client_secret': 'rotated'}) is False
    assert save_domain_app(shop.id, 'facebook', 'another-app', {'client_secret': 'x'}) is True
    db.session.commit()
    monkeypatch.setitem(app.config, 'CONNECTIONS_SECRETS_KEY', generate_key())   # a key that cannot open it
    assert domain_app('facebook', shop.id) is None and app_for('facebook', shop.id).client_id == 'instance-meta'
    assert remove_domain_app(shop.id, 'facebook') is True and remove_domain_app(shop.id, 'facebook') is False


def test_calls_run_as_one_domain(app, shop, meta_env):
    save_domain_app(shop.id, 'facebook', 'shop-meta', {'client_secret': 'shop-secret'})
    db.session.commit()
    assert current('facebook').client_id == 'instance-meta'
    with for_domain(shop.id):
        assert current('facebook').client_id == 'shop-meta'
        with for_domain(None):
            assert current('facebook').client_id == 'instance-meta'
        assert current('facebook').client_id == 'shop-meta'


# --------------------------------------------------------------------------
# switches (C12)
# --------------------------------------------------------------------------

def test_a_domain_follows_the_instances_switch_unless_it_has_its_own(app, shop, fake_service):
    assert enabled(fake_service, shop.id)
    set_domain_switch('fake', shop.id, 'off')
    assert not enabled(fake_service, shop.id) and enabled(fake_service, None)
    assert enabled_services(shop.id) == []
    set_setting(setting_name('fake'), False)
    set_domain_switch('fake', shop.id, 'on')
    assert enabled(fake_service, shop.id) and not enabled(fake_service, None)
    fake_service.has_credentials = False   # on, but no app: off (C12)
    assert not enabled(fake_service, shop.id)
    set_domain_switch('fake', shop.id, 'sideways')
    assert domain_switch('fake', shop.id) is None and domain_switch('fake', None) is None


def test_the_menu_offers_what_the_members_domain_has(app, shop, fake_service):
    member = make_user(make_instance(SHOP, software='piefed'), 'shopper', local=True)
    member.site_domain_id = shop.id
    db.session.commit()
    assert connections_offered(member)
    set_domain_switch('fake', shop.id, 'off')
    assert not connections_offered(member)
    assert not connections_offered(SimpleNamespace(is_authenticated=False))


# --------------------------------------------------------------------------
# the services read the domain's app
# --------------------------------------------------------------------------

def test_facebook_signs_in_and_proves_with_the_domains_app(app, shop, meta_env, monkeypatch):
    save_domain_app(shop.id, 'facebook', 'shop-meta', {'client_secret': 'shop-secret'})
    db.session.commit()
    with for_domain(shop.id):
        url = facebook.SERVICE.authorize_url(None, 's', 'https://shop.example/cb', {})
        assert parse_qs(urlsplit(url).query)['client_id'] == ['shop-meta']
        assert facebook.proof('tok') == hmac.new(b'shop-secret', b'tok', hashlib.sha256).hexdigest()
        assert facebook.meta_app_id() == 'shop-meta'
    with for_domain(shop.id), pytest.raises(Permanent):
        remove_domain_app(shop.id, 'facebook')
        db.session.commit()
        monkeypatch.setitem(app.config, 'FACEBOOK_APP_ID', '')
        facebook.meta_app()


def test_each_google_project_counts_its_own_uploads(app, shop, monkeypatch, redis_double):
    monkeypatch.setitem(app.config, 'YOUTUBE_OAUTH_CLIENT_ID', 'instance-google')
    monkeypatch.setitem(app.config, 'YOUTUBE_OAUTH_SECRET', 's')
    save_domain_app(shop.id, 'youtube', 'shop-google', {'client_secret': 's', 'daily_uploads': 1})
    db.session.commit()
    with for_domain(shop.id):
        youtube.take_upload()
        with pytest.raises(QuotaWait):
            youtube.take_upload()
    youtube.take_upload()   # the instance's project has its own hundred
    with for_domain(shop.id), pytest.raises(Permanent):
        remove_domain_app(shop.id, 'youtube')
        db.session.commit()
        monkeypatch.setitem(app.config, 'YOUTUBE_OAUTH_CLIENT_ID', '')
        youtube.google_app()


def test_each_domain_is_its_own_bluesky_client(app, shop, monkeypatch):
    monkeypatch.setitem(app.config, 'BLUESKY_OAUTH_KEY', bluesky.new_key_jwk())
    key = bluesky.new_key_jwk()
    save_domain_app(shop.id, 'bluesky', bluesky.key_id_of(key), {'key': key})
    db.session.commit()
    with for_domain(shop.id):
        assert bluesky.client_id() == f'https://{SHOP}/connections/bluesky/client-metadata.json'
        assert bluesky.redirect_uri() == f'https://{SHOP}/user/settings/connections/bluesky/callback'
        assert bluesky.client_metadata()['client_name'] == SHOP
        assert bluesky.jwks()['keys'][0]['kid'] == bluesky.key_id_of(key)
    assert bluesky.client_id() == 'https://test.piefed.local/connections/bluesky/client-metadata.json'
    set_setting(setting_name('bluesky'), True)
    monkeypatch.setattr('app.user.connections.current_storefront', lambda: shop)
    with app.test_client() as client:
        metadata = client.get('/connections/bluesky/client-metadata.json')
    assert metadata.json['client_id'] == f'https://{SHOP}/connections/bluesky/client-metadata.json'


# --------------------------------------------------------------------------
# a different app ends its connections (C11)
# --------------------------------------------------------------------------

def _youtube_like(monkeypatch, app):
    monkeypatch.setattr(FakeService, 'kind_of', lambda self, variant: 'youtube')
    monkeypatch.setitem(app.config, 'YOUTUBE_OAUTH_CLIENT_ID', 'instance-google')
    monkeypatch.setitem(app.config, 'YOUTUBE_OAUTH_SECRET', 's')


def test_a_connection_made_with_another_app_must_connect_again(world, monkeypatch, http_mock):
    _youtube_like(monkeypatch, world.app)
    world.connection.app_client_id = 'an-old-app'
    db.session.commit()
    row = a_row(world, a_post(world))
    perform(row.id)
    row = fresh(row)
    assert row.state == 'failed' and 'connect it again' in row.last_error
    assert db.session.get(SocialConnection, world.connection.id).state == 'reauth'


def test_a_connection_from_before_apps_were_recorded_adopts_its_app(world, monkeypatch, http_mock):
    _youtube_like(monkeypatch, world.app)
    http_mock.post(f'{BASE}/posts').respond(200, json=SENT)
    row = a_row(world, a_post(world))
    perform(row.id)
    assert fresh(row).state == 'sent'
    assert db.session.get(SocialConnection, world.connection.id).app_client_id == 'instance-google'


def test_a_new_domain_app_ends_the_connections_made_with_another(world, shop, monkeypatch):
    _youtube_like(monkeypatch, world.app)
    world.author.site_domain_id = shop.id
    world.connection.app_client_id = 'instance-google'
    other = SocialConnection(user_id=world.author.id, service='fake', remote_id='acct-2', remote_name='@two',
                             sealed_tokens=seal_tokens({}), app_client_id='shop-google')
    db.session.add(other)
    db.session.commit()
    save_domain_app(shop.id, 'youtube', 'shop-google', {'client_secret': 's'})
    db.session.commit()
    assert reconnect_domain(shop.id, 'youtube', app_for('youtube', shop.id)) == 1
    assert db.session.get(SocialConnection, world.connection.id).state == 'reauth'
    assert db.session.get(SocialConnection, other.id).state == 'ok'
    assert reconnect_domain(shop.id, 'youtube', app_for('youtube', shop.id)) == 0   # told once
    assert reconnect_domain(shop.id, 'facebook', None) == 0   # not its kind
    assert reconnect_domain(shop.id, 'bluesky', None) == 0    # a key is a secret: no client changes
    assert reconnect_domain(shop.id, 'youtube', None) == 1    # no app left at all


# --------------------------------------------------------------------------
# the member's own domain (C9, C10)
# --------------------------------------------------------------------------

def test_connect_runs_on_the_members_home_domain(world, shop, http_mock, monkeypatch):
    world.author.site_domain_id = shop.id
    db.session.commit()
    page = world.client.get('/user/settings/connections')
    assert b'Accounts are connected on shop.example' in page.data
    assert world.client.post('/user/settings/connections/fake/connect',
                             data={'csrf_token': world.token}).status_code == 404
    http_mock.get(f'{BASE}/ping').respond(200)
    monkeypatch.setattr('app.user.connections.current_storefront', lambda: shop)   # on shop.example
    response = world.client.post('/user/settings/connections/fake/connect', data={'csrf_token': world.token})
    assert response.status_code == 302


def test_a_connection_records_the_app_that_issued_it(world, shop, http_mock, monkeypatch):
    _youtube_like(monkeypatch, world.app)
    world.author.site_domain_id = shop.id
    db.session.commit()
    save_domain_app(shop.id, 'youtube', 'shop-google', {'client_secret': 's'})
    db.session.commit()
    http_mock.get(f'{BASE}/ping').respond(200)
    monkeypatch.setattr('app.user.connections.current_storefront', lambda: shop)   # on shop.example
    response = world.client.post('/user/settings/connections/fake/connect', data={'csrf_token': world.token})
    state = response.location.split('state=')[1]
    http_mock.post(f'{BASE}/token').respond(200, json={'accounts': [{'id': 'acct-9', 'name': '@nine', 'token': 't'}]})
    world.client.get(f'/user/settings/connections/fake/callback?state={state}&code=c')
    stored = SocialConnection.query.filter_by(remote_id='acct-9').one()
    assert (stored.app_domain_id, stored.app_client_id) == (shop.id, 'shop-google')


# --------------------------------------------------------------------------
# the admin pages
# --------------------------------------------------------------------------

def _admin_of(world, shop, scoped=True):
    keeper = make_user(make_instance(SHOP, software='piefed'), 'keeper', local=True)
    role = grant_permission(keeper, 'manage domain connected accounts')
    if scoped:
        limit_role(role, shop.id)
    login(world.client, keeper)
    return keeper


def test_the_instance_page_lists_what_to_register(world, shop, meta_env):
    login(world.client, db.session.get(User, 1))
    page = world.client.get('/admin/connections').data
    assert b'Addresses to register in the instance apps' in page
    assert f'https://{SHOP}/user/settings/connections/facebook/callback'.encode() in page
    assert f'https://{SHOP}/user/settings/connections/instagram/callback'.encode() in page
    save_domain_app(shop.id, 'facebook', 'shop-meta', {'client_secret': 's'})
    db.session.commit()
    page = world.client.get('/admin/connections').data
    assert f'https://{SHOP}/user/settings/connections/facebook/callback'.encode() not in page


def test_only_the_domains_admins_reach_its_page(world, shop):
    _admin_of(world, shop)
    assert world.client.get(f'/admin/domains/{shop.id}/connections').status_code == 200
    first = SiteDomain.query.filter_by(is_default=True).one()
    assert world.client.get(f'/admin/domains/{first.id}/connections').status_code == 403
    assert world.client.get('/admin/domains/999999/connections').status_code == 404


def test_the_default_domains_page_is_the_instances(world, shop):
    login(world.client, db.session.get(User, 1))
    first = SiteDomain.query.filter_by(is_default=True).one()
    assert world.client.get(f'/admin/domains/{first.id}/connections').location.endswith('/admin/connections')


def test_a_domain_admin_sets_its_apps(world, shop, monkeypatch, meta_env):
    _admin_of(world, shop)
    url = f'/admin/domains/{shop.id}/connections'
    page = world.client.get(url).data
    assert b'Meta app' in page and f'https://{SHOP}/user/settings/connections/youtube/callback'.encode() in page
    form = {'csrf_token': world.token, 'action': 'save', 'kind': 'youtube', 'client_id': 'shop-google',
            'client_secret': 'secret', 'daily_uploads': '20'}
    world.client.post(url, data=form)
    assert domain_app('youtube', shop.id) == App('youtube', 'shop-google',
                                                 {'client_secret': 'secret', 'daily_uploads': 20}, shop.id)
    world.client.post(url, data={**form, 'client_secret': '', 'daily_uploads': ''})   # blank keeps the secret
    assert domain_app('youtube', shop.id).get('client_secret') == 'secret'
    world.client.post(url, data={**form, 'client_id': 'other', 'client_secret': ''})   # a new app needs its secret
    assert domain_app('youtube', shop.id).client_id == 'shop-google'
    assert world.client.post(url, data={**form, 'daily_uploads': 'many'}).status_code == 400
    assert world.client.post(url, data={**form, 'client_id': ''}).status_code == 400
    world.client.post(url, data={'csrf_token': world.token, 'action': 'make-key', 'kind': 'bluesky'})
    assert domain_app('bluesky', shop.id).get('key')
    assert world.client.post(url, data={'csrf_token': world.token, 'action': 'save', 'kind': 'bluesky'}).status_code == 400
    assert world.client.post(url, data={'csrf_token': world.token, 'action': 'make-key', 'kind': 'youtube'}).status_code == 400
    world.client.post(url, data={'csrf_token': world.token, 'action': 'remove', 'kind': 'youtube'})
    assert domain_app('youtube', shop.id) is None
    assert world.client.post(url, data={'csrf_token': world.token, 'action': 'nonsense'}).status_code == 400
    page = world.client.get(url).data.decode()
    assert 'Its own' in page, page[page.find('app-bluesky'):][:600]
    assert 'The instance&#39;s' in page or "The instance's" in page


def test_a_new_app_tells_how_many_must_reconnect(world, shop, monkeypatch):
    _youtube_like(monkeypatch, world.app)
    world.author.site_domain_id = shop.id
    world.connection.app_client_id = 'instance-google'
    db.session.commit()
    _admin_of(world, shop)
    response = world.client.post(f'/admin/domains/{shop.id}/connections', follow_redirects=True, data={
        'csrf_token': world.token, 'action': 'save', 'kind': 'youtube', 'client_id': 'shop-google',
        'client_secret': 's'})
    assert b'1 connections were made with another app' in response.data


def test_a_domain_admin_sets_its_switches(world, shop, fake_service):
    _admin_of(world, shop)
    url = f'/admin/domains/{shop.id}/connections'
    world.client.post(url, data={'csrf_token': world.token, 'action': 'switch', 'service': 'fake', 'value': 'off'})
    assert domain_switch('fake', shop.id) == 'off'
    world.client.post(url, data={'csrf_token': world.token, 'action': 'switch', 'service': 'fake', 'value': ''})
    assert domain_switch('fake', shop.id) is None
    fake_service.has_credentials = False
    assert world.client.post(url, data={'csrf_token': world.token, 'action': 'switch', 'service': 'fake',
                                        'value': 'on'}).status_code == 400
    assert world.client.post(url, data={'csrf_token': world.token, 'action': 'switch', 'service': 'nowhere',
                                        'value': 'off'}).status_code == 400


def test_nothing_is_set_without_the_token_key(world, shop, monkeypatch):
    _admin_of(world, shop)
    monkeypatch.setitem(world.app.config, 'CONNECTIONS_SECRETS_KEY', '')
    url = f'/admin/domains/{shop.id}/connections'
    assert b'no CONNECTIONS_SECRETS_KEY yet' in world.client.get(url).data
    assert world.client.post(url, data={'csrf_token': world.token, 'action': 'switch', 'service': 'fake',
                                        'value': 'off'}).status_code == 400


def test_instagram_reads_its_own_app_and_names_its_kind(app, shop, monkeypatch):
    service = instagram.SERVICE
    assert service.kind_of(instagram.VIA_FACEBOOK) == 'facebook' and service.kind_of(instagram.DIRECT) == 'instagram'
    with for_domain(shop.id), pytest.raises(Permanent):
        instagram.ig_app()
    save_domain_app(shop.id, 'instagram', 'shop-ig', {'client_secret': 's'})
    db.session.commit()
    with for_domain(shop.id):
        assert instagram.ig_app().client_id == 'shop-ig' and service.variants == (instagram.DIRECT,)


def test_a_domain_admin_switches_a_service_on_and_saves_a_meta_app(world, shop, fake_service):
    _admin_of(world, shop)
    url = f'/admin/domains/{shop.id}/connections'
    world.client.post(url, data={'csrf_token': world.token, 'action': 'switch', 'service': 'fake', 'value': 'on'})
    assert domain_switch('fake', shop.id) == 'on'
    world.client.post(url, data={'csrf_token': world.token, 'action': 'save', 'kind': 'facebook',
                                 'client_id': 'shop-meta', 'client_secret': 'shop-secret'})
    assert domain_app('facebook', shop.id) == App('facebook', 'shop-meta', {'client_secret': 'shop-secret'}, shop.id)

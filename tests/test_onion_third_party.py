"""OAuth login/connect and Stripe checkout are unavailable on the onion (onion storefront spec §3): their
redirect_uri / success_url would name the onion host to the provider. The buttons are hidden and the routes refuse
without building a provider URL. Clearnet is unchanged."""
import sys
from unittest.mock import MagicMock, patch

import pytest

from app import db
from app.models import Site, SiteDomain
from app.storefront import clear_domain_cache
from tests.factories import make_instance, make_user

pytestmark = pytest.mark.usefixtures('site')

PROXY = '10.89.0.2'
ONION_HEADERS = {'X-PieFed-Via': 'onion', 'X-Forwarded-For': '10.89.0.7'}
PROVIDERS = ['google', 'mastodon', 'discord']
OAUTH_CONFIG = {'GOOGLE_OAUTH_CLIENT_ID': 'g', 'MASTODON_OAUTH_CLIENT_ID': 'm', 'DISCORD_OAUTH_CLIENT_ID': 'd'}
STRIPE_CONFIG = {'STRIPE_SECRET_KEY': 'sk_test', 'STRIPE_PUBLISHABLE_KEY': 'pk_test',
                 'STRIPE_MONTHLY_SMALL': 'price_small', 'STRIPE_MONTHLY_BIG': 'price_big',
                 'STRIPE_MONTHLY_SMALL_TEXT': 'A small amount', 'STRIPE_MONTHLY_BIG_TEXT': 'A larger amount'}


@pytest.fixture
def configured(app, db_session, monkeypatch):
    if SiteDomain.query.filter_by(domain='test.piefed.local').first() is None:   # the canonical host stays served
        db.session.add(SiteDomain(domain='test.piefed.local', scheme='https', is_default=True))
    db.session.add(SiteDomain(domain='abc.onion', scheme='http', federates=False))
    db.session.commit()
    clear_domain_cache()
    site = db.session.get(Site, 1)
    site.registration_mode = 'Open'
    site.tos_url = None
    site.application_question = ''
    db.session.commit()
    for key, value in {**OAUTH_CONFIG, **STRIPE_CONFIG, 'ONION_PROXY_IP': PROXY}.items():
        monkeypatch.setitem(app.config, key, value)
    user = make_user(make_instance('test.piefed.local', software='piefed'), 'donor', local=True)
    user.email = 'donor@example.com'
    user.verified = True
    db.session.commit()
    yield user
    clear_domain_cache()


def visit(app, user, path, onion):
    client = app.test_client()
    if user is not None:
        with client.session_transaction(base_url='http://abc.onion' if onion else None) as session:
            session['_user_id'] = str(user.id)
            session['_fresh'] = True
    if not onion:
        return client.get(path)
    # a cookieless onion request is sent out and back to pick up its session cookie (app/onion_guard.py)
    response = None
    for _ in range(3):
        response = client.get(path, base_url='http://abc.onion', headers=ONION_HEADERS,
                              environ_base={'REMOTE_ADDR': PROXY})
        if response.status_code != 303:
            break
        path = response.headers['Location']
    return response


def rendered_flags(app, path, onion):
    """The OAuth flags the login or registration page is rendered with (render_template is patched: the suite
    runs without CSRF, so the real form template cannot render)."""
    with patch('app.auth.util.render_template', return_value='rendered') as render:
        visit(app, None, path, onion)
    return {key: bool(render.call_args.kwargs.get(key)) for key in ('google_oauth', 'mastodon_oauth', 'discord_oauth')}


@pytest.mark.parametrize('path', ['/auth/login', '/auth/register'])
def test_the_onion_pages_have_no_oauth_buttons(app, configured, path):
    assert rendered_flags(app, path, onion=True) == {'google_oauth': False, 'mastodon_oauth': False,
                                                     'discord_oauth': False}


@pytest.mark.parametrize('path, flags', [
    ('/auth/login', {'google_oauth': True, 'mastodon_oauth': True, 'discord_oauth': True}),
    ('/auth/register', {'google_oauth': False, 'mastodon_oauth': True, 'discord_oauth': True})])
def test_the_clearnet_pages_keep_their_oauth_buttons(app, configured, path, flags):
    assert rendered_flags(app, path, onion=False) == flags


@pytest.mark.parametrize('provider', PROVIDERS)
@pytest.mark.parametrize('route, signed_in, refused_to', [('login', False, '/auth/login'),
                                                          ('connect', True, '/user/connect_oauth')])
def test_the_onion_refuses_to_start_an_oauth_flow(app, configured, provider, route, signed_in, refused_to):
    with patch('app.auth.routes.oauth') as fake:
        response = visit(app, configured if signed_in else None, f'/auth/{provider}_{route}', onion=True)
    assert response.status_code == 302
    assert response.headers['Location'].endswith(refused_to)
    assert not getattr(fake, provider).authorize_redirect.called


@pytest.mark.parametrize('provider', PROVIDERS)
def test_the_clearnet_still_redirects_to_the_provider(app, configured, provider):
    with patch('app.auth.routes.oauth') as fake:
        getattr(fake, provider).authorize_redirect.return_value = 'redirected'
        response = visit(app, None, f'/auth/{provider}_login', onion=False)
    assert response.data == b'redirected'


@pytest.mark.parametrize('provider', PROVIDERS)
@pytest.mark.parametrize('route, signed_in, refused_to', [('authorize', False, '/auth/login'),
                                                          ('connect_callback', True, '/user/connect_oauth')])
def test_the_onion_refuses_an_oauth_callback(app, configured, provider, route, signed_in, refused_to):
    with patch('app.auth.routes.oauth') as fake, patch('app.auth.routes.handle_oauth_authorize') as handle:
        response = visit(app, configured if signed_in else None, f'/auth/{provider}_{route}', onion=True)
    assert response.status_code == 302
    assert response.headers['Location'].endswith(refused_to)
    assert not handle.called and not getattr(fake, provider).authorize_access_token.called


def test_the_onion_connect_page_offers_no_providers(app, configured):
    with patch('app.user.routes.render_template', return_value='rendered') as render:
        visit(app, configured, '/user/connect_oauth', onion=True)
    assert render.call_args.kwargs['oauth_providers'] == {'google': False, 'mastodon': False, 'discord': False}


def test_the_onion_donate_page_offers_no_plans(app, configured):
    body = visit(app, configured, '/donate', onion=True).get_data(as_text=True)
    assert 'A small amount' not in body
    assert 'stripe_redirect' not in body


def test_the_clearnet_donate_page_offers_the_plans(app, configured):
    assert 'A small amount' in visit(app, configured, '/donate', onion=False).get_data(as_text=True)


@pytest.mark.parametrize('plan', ['monthly_small', 'monthly_big', 'billing'])
def test_the_onion_refuses_a_checkout(app, configured, plan):
    configured.stripe_customer_id = 'cus_test'   # so the billing portal would be reached without the refusal
    db.session.commit()
    stripe = MagicMock()
    with patch.dict(sys.modules, {'stripe': stripe}), patch('app.user.subscription.httpx.Client') as client:
        response = visit(app, configured, f'/stripe_redirect/{plan}', onion=True)
    assert response.status_code == 302
    assert response.headers['Location'].endswith('/donate')
    assert not stripe.checkout.Session.create.called and not client.called


def test_the_onion_settings_nav_has_no_connect_oauth_link(app, configured):
    assert '/user/connect_oauth' not in visit(app, configured, '/user/passkeys', onion=True).get_data(as_text=True)


def test_the_clearnet_settings_nav_keeps_the_connect_oauth_link(app, configured):
    assert '/user/connect_oauth' in visit(app, configured, '/user/passkeys', onion=False).get_data(as_text=True)

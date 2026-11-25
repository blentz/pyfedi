"""Social logins are offered only on the domains they are set up for (`<PROVIDER>_OAUTH_FOR_DOMAINS`): a provider's
app lists the redirect URIs it accepts, and each names the domain it was asked from, so elsewhere the provider would
refuse it. There the buttons are hidden and the routes are not found. Empty means every domain."""
from unittest.mock import patch

import pytest

from app import db
from app.auth.util import oauth_on_this_domain
from app.models import Site, SiteDomain
from app.storefront import clear_domain_cache
from tests.factories import make_instance, make_user

pytestmark = pytest.mark.usefixtures('site')

PROVIDERS = ['google', 'mastodon', 'discord']
HOME, SISTER = 'test.piefed.local', 'sister.example'


@pytest.fixture
def domains(app, db_session, monkeypatch):
    if SiteDomain.query.filter_by(domain=HOME).first() is None:
        db.session.add(SiteDomain(domain=HOME, scheme='https', is_default=True))
    db.session.add(SiteDomain(domain=SISTER, scheme='https'))
    db.session.commit()
    clear_domain_cache()
    site = db.session.get(Site, 1)
    site.registration_mode = 'Open'
    site.tos_url = None
    site.application_question = ''
    db.session.commit()
    for provider in PROVIDERS:
        monkeypatch.setitem(app.config, f'{provider.upper()}_OAUTH_CLIENT_ID', provider[0])
        monkeypatch.setitem(app.config, f'{provider.upper()}_OAUTH_FOR_DOMAINS', f' {HOME.upper()} , ')
    user = make_user(make_instance(HOME, software='piefed'), 'member', local=True)
    user.verified = True
    db.session.commit()
    yield user
    clear_domain_cache()


def visit(app, host, path, user=None):
    client = app.test_client()
    if user is not None:
        with client.session_transaction(base_url=f'https://{host}') as session:
            session['_user_id'] = str(user.id)
            session['_fresh'] = True
    return client.get(path, base_url=f'https://{host}')


def on(app, host, provider):
    """oauth_on_this_domain as a request to `host` asks it (the request's set-up picks its storefront)."""
    with app.test_request_context(base_url=f'https://{host}'):
        app.preprocess_request()
        return oauth_on_this_domain(provider)


def login_flags(app, host):
    with patch('app.auth.util.render_template', return_value='rendered') as render:
        visit(app, host, '/auth/login')
    return {key: bool(render.call_args.kwargs.get(key)) for key in ('google_oauth', 'mastodon_oauth', 'discord_oauth')}


def test_a_listed_domain_matches_whatever_its_case_and_spacing(app, domains):
    assert all(on(app, HOME, provider) for provider in PROVIDERS)
    assert not any(on(app, SISTER, provider) for provider in PROVIDERS)


@pytest.mark.parametrize('listed', ['', ' , '])
def test_no_domains_listed_means_every_domain(app, domains, monkeypatch, listed):
    monkeypatch.setitem(app.config, 'GOOGLE_OAUTH_FOR_DOMAINS', listed)
    assert on(app, SISTER, 'google')


def test_a_provider_with_no_setting_at_all_is_on_every_domain(app, domains, monkeypatch):
    monkeypatch.delitem(app.config, 'GOOGLE_OAUTH_FOR_DOMAINS')
    assert on(app, SISTER, 'google')


def test_the_login_page_offers_only_the_providers_set_up_for_its_domain(app, domains, monkeypatch):
    assert login_flags(app, HOME) == {'google_oauth': True, 'mastodon_oauth': True, 'discord_oauth': True}
    assert login_flags(app, SISTER) == {'google_oauth': False, 'mastodon_oauth': False, 'discord_oauth': False}
    monkeypatch.setitem(app.config, 'DISCORD_OAUTH_FOR_DOMAINS', f'{HOME},{SISTER}')
    assert login_flags(app, SISTER) == {'google_oauth': False, 'mastodon_oauth': False, 'discord_oauth': True}


@pytest.mark.parametrize('provider', PROVIDERS)
@pytest.mark.parametrize('route, signed_in', [('login', False), ('connect', True), ('authorize', False),
                                              ('connect_callback', True)])
def test_a_domain_a_provider_is_not_set_up_for_has_none_of_its_routes(app, domains, provider, route, signed_in):
    with patch('app.auth.routes.oauth') as fake, patch('app.auth.routes.handle_oauth_authorize') as handle:
        response = visit(app, SISTER, f'/auth/{provider}_{route}', domains if signed_in else None)
    assert response.status_code == 404
    assert not getattr(fake, provider).authorize_redirect.called and not handle.called


@pytest.mark.parametrize('provider', PROVIDERS)
def test_a_domain_a_provider_is_set_up_for_redirects_to_it(app, domains, provider):
    with patch('app.auth.routes.oauth') as fake:
        getattr(fake, provider).authorize_redirect.return_value = 'redirected'
        response = visit(app, HOME, f'/auth/{provider}_login')
    assert response.data == b'redirected'
    assert getattr(fake, provider).authorize_redirect.call_args.kwargs['redirect_uri'] == \
        f'https://{HOME}/auth/{provider}_authorize'


def test_the_connect_page_offers_only_the_providers_set_up_for_its_domain(app, domains):
    with patch('app.user.routes.render_template', return_value='rendered') as render:
        visit(app, HOME, '/user/connect_oauth', domains)
        visit(app, SISTER, '/user/connect_oauth', domains)
    home, sister = (call.kwargs['oauth_providers'] for call in render.call_args_list)
    assert home == {'google': True, 'mastodon': True, 'discord': True}
    assert sister == {'google': False, 'mastodon': False, 'discord': False}

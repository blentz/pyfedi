"""On a storefront that is not the default domain, the login form says how an account from the default domain logs
in (final fix F3). A bare name there means that storefront's own account, so a hell.cloud member typing their bare
name on the onion gets "Login failed" with no clue why. The passkey button reads the same user name field."""
from html import unescape

import pytest

from app import db
from app.models import SiteDomain
from app.storefront import clear_domain_cache
from tests.test_onion_guard_limits import PROXY, a_session, onion_get

pytestmark = pytest.mark.usefixtures('site')

HINT = 'Account from another of our sites? Log in as name@test.piefed.local.'


@pytest.fixture
def onion(app, db_session, monkeypatch, redis_double):
    monkeypatch.setitem(app.config, 'ONION_PROXY_IP', PROXY)
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', True)   # the form renders form.csrf_token()
    if db.session.query(SiteDomain).filter_by(is_default=True).first() is None:
        db.session.add(SiteDomain(domain=app.config['SERVER_NAME'], scheme='https', is_default=True))
    db.session.add(SiteDomain(domain='abc.onion', scheme='http', federates=False))
    db.session.commit()
    clear_domain_cache()
    yield
    clear_domain_cache()


def test_the_onion_login_form_says_how_a_default_domain_account_logs_in(app, onion):
    client = app.test_client()
    a_session(client)
    response = onion_get(client, '/auth/login')
    assert response.status_code == 200
    page = unescape(response.get_data(as_text=True))
    assert HINT in page
    assert page.index('name="user_name"') < page.index(HINT) < page.index('log_in_with_passkey')


def test_the_default_domain_login_form_has_no_hint(app, onion):
    response = app.test_client().get('/auth/login')
    assert response.status_code == 200
    assert HINT not in unescape(response.get_data(as_text=True))

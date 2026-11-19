"""Fixtures from tests/test_onion_pow.py that other test modules request (registered in tests/conftest.py)."""
import pytest

from app import db
from app.models import SiteDomain
from app.storefront import clear_domain_cache
from tests.factories import make_instance, make_user
from tests.test_onion_pow import PASSWORD, PROXY


@pytest.fixture
def onion_pow_alice(onion_pow_onion):
    local = make_instance('test.piefed.local', software='piefed')
    make_user(local, 'founder', local=True)   # id 1 is exempt from every ban check
    user = make_user(local, 'alice', local=True)
    user.site_domain_id = SiteDomain.query.filter_by(domain='abc.onion').first().id   # her login name is the onion's
    user.email = 'alice@example.com'
    user.verified = True
    user.set_password(PASSWORD)
    db.session.commit()
    return user


@pytest.fixture
def onion_pow_dora(onion_pow_onion):
    """An account of the DEFAULT domain: on the onion she is reached as dora@test.piefed.local."""
    local = make_instance('test.piefed.local', software='piefed')
    make_user(local, 'founder', local=True)
    user = make_user(local, 'dora', local=True)
    user.site_domain_id = SiteDomain.query.filter_by(domain='test.piefed.local').first().id
    user.email = 'dora@example.com'
    user.verified = True
    user.set_password(PASSWORD)
    db.session.commit()
    return user


@pytest.fixture
def onion_pow_onion(app, site, monkeypatch, onion_redis):
    monkeypatch.setitem(app.config, 'ONION_PROXY_IP', PROXY)
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', True)   # the login and register forms render their token
    if not SiteDomain.query.filter_by(domain='test.piefed.local').first():
        db.session.add(SiteDomain(domain='test.piefed.local', scheme='https', is_default=True))
    db.session.add(SiteDomain(domain='abc.onion', scheme='http', federates=False))
    db.session.commit()
    clear_domain_cache()
    yield onion_redis
    clear_domain_cache()


@pytest.fixture
def onion_pow_open_registration(onion_pow_onion, site, monkeypatch):
    """Open registration, with no admin around to keep it open: the abandoned-instance safety is not under test."""
    monkeypatch.setattr('app.auth.routes.handle_abandoned_open_instance', lambda: None)
    site.registration_mode = 'Open'
    db.session.commit()

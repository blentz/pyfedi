"""Fixtures from tests/test_onion_guard_limits.py that other test modules request (registered in tests/conftest.py)."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app import create_app, db, limiter
from app.models import SiteDomain
from app.storefront import clear_domain_cache
from tests.factories import make_instance, make_user
from tests.test_onion_guard_limits import PASSWORD, PROXY, LimitedConfig


@pytest.fixture
def onion_guard_limits_alice(onion_guard_limits_onion):
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
def onion_guard_limits_clock(monkeypatch):
    now = SimpleNamespace(t=1_800_000_000.0)
    monkeypatch.setattr('app.onion_guard._now', lambda: now.t)
    return now


@pytest.fixture
def directory(app, monkeypatch):
    """LDAP on, as tests/test_ldap.py runs it: the bind succeeds only for alice's real password."""
    monkeypatch.setitem(app.config, 'LDAP_READ_ENABLE', True)
    with patch('app.auth.util.login_with_ldap',
               side_effect=lambda name, password: 'alice@example.com' if password == PASSWORD else False) as bind, \
            patch('app.auth.util.sync_user_to_ldap'), patch('app.shared.auth.sync_user_to_ldap'):
        yield bind


@pytest.fixture
def ldap_off():
    with patch('app.auth.util.sync_user_to_ldap'), patch('app.shared.auth.sync_user_to_ldap'):
        yield


@pytest.fixture(scope='module')
def onion_guard_limits_limited_app():
    limited = create_app(LimitedConfig)

    @limited.route('/api/alpha/onion_probe')
    def onion_probe():
        with limiter.limit('2/minute'):
            return 'ok'
    onion_probe.has_own_login_gate = True   # the site row here says private, and this asks only about the limit

    limiter.enabled = False
    yield limited
    limiter.enabled = False


@pytest.fixture
def onion_guard_limits_onion(app, site, monkeypatch):
    monkeypatch.setitem(app.config, 'ONION_PROXY_IP', PROXY)
    if not SiteDomain.query.filter_by(domain='test.piefed.local').first():
        db.session.add(SiteDomain(domain='test.piefed.local', scheme='https', is_default=True))
    db.session.add(SiteDomain(domain='abc.onion', scheme='http', federates=False))
    db.session.commit()
    clear_domain_cache()
    yield
    clear_domain_cache()

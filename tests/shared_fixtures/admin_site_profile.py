"""Fixtures from tests/test_admin_site_profile.py that other test modules request (registered in tests/conftest.py)."""
import pytest

from tests.test_admin_site_profile import _seed, _settings_admin, csrf, login


@pytest.fixture
def admin_site_profile_admin_client(app, db_session):
    instance, ordinary = _seed()
    admin = _settings_admin(instance)
    client = app.test_client()
    login(client, admin)
    return client, csrf(app, client)

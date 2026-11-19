"""Fixtures from tests/discovery_fixtures.py that other test modules request (registered in tests/conftest.py)."""
import pytest

from app import db
from tests.factories import grant_permission, make_instance, make_user
from tests.test_admin_federation import csrf, login


@pytest.fixture
def discovery_admin(app, db_session):
    """A logged-in local user holding 'change instance settings': returns (client, csrf_token)."""
    instance = make_instance('test.piefed.local', software='piefed')
    make_user(instance, 'founder', local=True)
    user = make_user(instance, 'settingsadmin', local=True)
    user.verified = True
    db.session.commit()
    grant_permission(user, 'change instance settings')
    client = app.test_client()
    login(client, user)
    return client, csrf(app, client)

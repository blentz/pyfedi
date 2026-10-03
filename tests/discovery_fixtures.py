"""Helpers shared by the discovery tests (interop D24). Defined once; import them into each test file."""
import pytest

from app import cache, db
from tests.factories import grant_permission, make_instance, make_user
from tests.test_admin_federation import csrf, login


def nobody_excluded(host):
    """An `exclude` callback that lets every host through."""
    return False


@pytest.fixture
def fresh_cache():
    """instance_banned and peertube_isolated_hosts are memoized: start and finish each test with a cold cache.
    Import it into a test file, then request it by name or put it in `pytestmark = pytest.mark.usefixtures(...)`."""
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def admin(app, db_session):
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

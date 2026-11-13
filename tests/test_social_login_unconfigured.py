"""Google, Mastodon and Discord login (app/auth/routes.py) on a server that has not configured that provider: each
of its routes is a 404, never a 500 (fuzz-routes finding, 2026-11-13: AttributeError('No such client: google')).
The provider is registered only when its client id is set (app/__init__.py), and the test config sets none."""
import pytest

from app import db
from tests.factories import make_instance, make_user
from tests.test_admin_site_profile import login

pytestmark = pytest.mark.usefixtures('site')

ROUTES = [f'/auth/{provider}_{step}' for provider in ('google', 'mastodon', 'discord')
          for step in ('login', 'connect', 'authorize', 'connect_callback')]


@pytest.mark.parametrize('path', ROUTES, ids=[path.split('/')[-1] for path in ROUTES])
def test_an_unconfigured_provider_is_not_found(app, path):
    assert app.test_client().get(path).status_code == 404


@pytest.mark.parametrize('path', ROUTES, ids=[path.split('/')[-1] for path in ROUTES])
def test_an_unconfigured_provider_is_not_found_signed_in(app, db_session, path):
    user = make_user(make_instance('test.piefed.local', software='piefed'), 'socialuser', local=True)
    user.verified = True
    db.session.commit()
    client = app.test_client()
    login(client, user)
    assert client.get(path).status_code == 404

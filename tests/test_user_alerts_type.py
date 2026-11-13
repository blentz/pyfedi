"""The alerts page names the kind of alert in its path, and its template includes that kind's partial by name
(fuzz-routes finding, 2026-11-13): GET /alerts/remote.example/<x> raised TemplateNotFound('user/alerts/_remote.example
.html'), a 500 to any signed-in person, and the path chose which template was included. A kind with no partial names
no alerts: a 404."""
import pytest

from app import db
from tests.factories import make_instance, make_user
from tests.test_admin_site_profile import login

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture
def client(app, db_session):
    user = make_user(make_instance('test.piefed.local', software='piefed'), 'alertreader', local=True)
    user.verified = True
    db.session.commit()
    signed_in = app.test_client()
    login(signed_in, user)
    return signed_in


@pytest.mark.parametrize('kind', ['remote.example', 'nothing', 'type', 'filter'], ids=['host', 'word', 'type', 'filter'])
def test_a_kind_with_no_partial_is_not_found(client, kind):
    assert client.get(f'/alerts/{kind}/all').status_code == 404


@pytest.mark.parametrize('kind', ['posts', 'comments', 'communities', 'topics', 'feeds', 'users'])
def test_every_kind_with_a_partial_is_shown(client, kind):
    assert client.get(f'/alerts/{kind}/all').status_code == 200


def test_the_plain_alerts_page_is_the_posts(client):
    assert client.get('/alerts').status_code == 200

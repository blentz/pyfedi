"""A community wiki revision asked for by an id that is not a number (fuzz-routes finding, 2026-11-13): the
revision_id path segment had no int converter, so 'fuzzmember' reached the integer column and PostgreSQL refused it,
a 500. A revision id is a number; anything else is no revision, a 404."""
import pytest

from app import db
from tests.factories import make_community, make_instance, make_user
from tests.test_admin_site_profile import login

pytestmark = pytest.mark.usefixtures('site')


@pytest.mark.parametrize('suffix', ['', '/revert'], ids=['revision', 'revert'])
def test_a_revision_id_that_is_not_a_number_is_not_found(app, db_session, suffix):
    instance = make_instance('test.piefed.local', software='piefed')
    user = make_user(instance, 'wikirevisor', local=True)
    user.verified = True
    make_community('wikicomm')
    db.session.commit()
    client = app.test_client()
    login(client, user)
    assert client.get(f'/community/wikicomm/wiki/a-page/not-a-number{suffix}').status_code == 404

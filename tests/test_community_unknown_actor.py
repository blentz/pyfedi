"""A community's wiki pages and flair page asked about a community that does not exist (fuzz-routes finding,
2026-11-13): each view ran only `if community is not None:` and then returned nothing, which Flask makes a 500.
They are 404s, as the community's own page is."""
import pytest

from app import db
from tests.factories import make_instance, make_user
from tests.test_admin_site_profile import login

pytestmark = pytest.mark.usefixtures('site')

PATHS = ['/community/nosuchcomm/wiki/a-page', '/community/nosuchcomm/wiki/a-page/1',
         '/community/nosuchcomm/wiki/a-page/1/revert', '/community/nosuchcomm/my_flair']


@pytest.mark.parametrize('path', PATHS, ids=['wiki', 'revision', 'revert', 'flair'])
def test_an_unknown_community_is_not_found(app, db_session, path):
    user = make_user(make_instance('test.piefed.local', software='piefed'), 'wikireader', local=True)
    user.verified = True
    db.session.commit()
    client = app.test_client()
    login(client, user)
    assert client.get(path).status_code == 404


def test_an_unknown_community_is_not_found_signed_out(app, db_session):
    assert app.test_client().get(PATHS[0]).status_code == 404

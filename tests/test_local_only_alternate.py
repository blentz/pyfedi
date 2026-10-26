"""The profile and community pages of a local-only actor do not advertise an ActivityPub id (final fix F4).

The id 404s to every other server, so the alternate only tells a scraper the account or community is local-only,
and on the first domain names the onion. The post page already leaves it out for a local-only post.
"""
import pytest

from app import db
from app.models import Site
from tests.conftest import forget_login
from tests.test_ap_interop_html_discovery import ap_link_headers
from tests.test_local_only_invisible import world  # noqa: F401 -- world is a fixture
from tests.test_per_domain_ap_routes import onion_proxy  # noqa: F401 -- the onion guard's proxy, for the onion's pages
from tests.test_per_domain_html_routes import Visitor

pytestmark = pytest.mark.usefixtures('site')

HTML = {'Accept': 'text/html'}
AP_LINK = 'type="application/activity+json"'


@pytest.fixture
def public(app, world):
    db.session.get(Site, 1).private_instance = False
    db.session.commit()
    return world


@pytest.mark.parametrize('path', ['/u/onionite', '/c/hidden'])   # the onion's, on the onion
def test_a_local_only_actor_page_has_no_activitypub_alternate(app, public, path):
    """Signed in: a logged-out visitor is not shown the page at all (owner ruling 2026-10-21), and off the onion
    nobody is (follow-up F6), so the page is read on the onion."""
    client = app.test_client()
    with client.session_transaction() as session:
        session.update({'_user_id': str(public.alice.id), '_fresh': True})
    forget_login()
    assert client.get(f'{path}@abc.onion', headers=HTML).status_code == 404
    response = Visitor(app, public.alice, 'abc.onion').get(path, headers=HTML)
    assert response.status_code == 200
    assert ap_link_headers(response) == []
    assert AP_LINK not in response.get_data(as_text=True)


@pytest.mark.parametrize('path', ['/u/alice', '/c/clearnet'])
def test_a_federating_actor_page_keeps_its_alternate(app, public, path):
    response = app.test_client().get(path, headers=HTML)
    assert response.status_code == 200
    assert len(ap_link_headers(response)) == 1
    assert AP_LINK in response.get_data(as_text=True)

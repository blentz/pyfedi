"""One check of the whole of spec section 4 that no earlier task holds whole (plan F4, task 10).

Every other row of section 4 is pinned where its code lives (see the task report); this file holds the last: a feed
reaches communities of several domains and of remote servers, and still shows their posts, in its page and its RSS."""
import pytest

from app import db
from tests.factories import make_community, make_feed_item, make_post
from tests.three_domains import FIRST, get, three_domains, two_domains  # noqa: F401

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture(autouse=True)
def public_site(site):
    """Site.private_instance defaults true, which sends an anonymous reader of a feed page to the login page."""
    site.private_instance = False
    db.session.commit()
    return site


def test_a_feed_holds_communities_from_several_domains_and_remote_servers(app, three_domains):  # noqa: F811
    d = three_domains
    owner = d.account('owner', d.first)
    remote = make_community('remotegroup', host='remote.example')
    groups = {'first post': d.local_community('firstgroup', d.first), 'shop post': d.local_community('shopgroup', d.shop),
              'remote post': remote}
    feed = d.local_feed('mixed', d.first, owner)
    for number, (title, community) in enumerate(groups.items()):
        make_feed_item(feed, community)
        make_post(community, owner, f'https://{FIRST}/post/{number + 1}', title=title)

    for path in ('/f/mixed', '/f/mixed.rss'):
        response = get(app, path, host=FIRST)
        assert response.status_code == 200
        text = response.get_data(as_text=True)
        assert all(title in text for title in groups), path

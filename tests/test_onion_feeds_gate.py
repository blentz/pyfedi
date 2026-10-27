"""The one route gate knows feeds (plan F4, task 7, controller ruling 2): a route naming an onion feed by id, or by a
handle on the onion, answers off the onion exactly as one naming a feed nobody holds."""
import pytest

from tests.test_onion_feeds_hidden import answer, public_site, signed_get, world  # noqa: F401 -- fixtures
from tests.three_domains import FIRST, ONION, three_domains, two_domains  # noqa: F401

pytestmark = pytest.mark.usefixtures('site')


# --- the gate -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('path', ['/feed/{id}/edit', '/feed/{id}/copy', '/community/{community}/feed/{id}'])
@pytest.mark.parametrize('who', ['first_owner', 'onion_owner'])
def test_a_feed_id_route_answers_an_onion_feed_as_an_id_nobody_holds(app, world, path, who):
    community = world.d.local_community('somewhere', world.d.first)
    user = getattr(world, who)
    missing = signed_get(app, user, path.format(id=2147483000, community=community.id), FIRST)
    for feed in (world.onion_feed, world.legacy_feed):
        hidden = signed_get(app, user, path.format(id=feed.id, community=community.id), FIRST)
        assert hidden.status_code == 404, (path, feed.name)
        assert answer(hidden) == (missing.status_code, missing.get_data().replace(b'2147483000', str(feed.id).encode()))


@pytest.mark.parametrize('path', ['/f/{name}/submit', '/f/{name}.rss', '/f/parent/{name}.rss'])
def test_a_feed_name_route_answers_an_onion_handle_as_a_missing_one(app, world, path):
    hidden = signed_get(app, world.onion_owner, path.format(name=f'onionfeed@{ONION}'), FIRST)
    missing = signed_get(app, world.onion_owner, path.format(name=f'nosuchfeed@{ONION}'), FIRST)
    assert hidden.status_code == 404
    assert answer(hidden) == (missing.status_code, missing.get_data().replace(b'nosuchfeed', b'onionfeed'))

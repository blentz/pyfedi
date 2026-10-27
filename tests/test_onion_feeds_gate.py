"""The one route gate knows feeds (plan F4, task 7, controller ruling 2): a route naming an onion feed by id, or by a
handle on the onion, answers off the onion exactly as one naming a feed nobody holds."""
import pytest

from app import db
from app.models import CommunityMember, Feed, RssFeed
from tests.test_onion_feeds_hidden import answer, login, public_site, signed_get, world  # noqa: F401 -- fixtures
from tests.test_per_domain_feed_routes import csrf
from tests.three_domains import FIRST, ONION, three_domains, two_domains  # noqa: F401

pytestmark = pytest.mark.usefixtures('site')


# --- the gate -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('path', ['/feed/{id}/edit', '/feed/{id}/copy'])
@pytest.mark.parametrize('who', ['first_owner', 'onion_owner'])
def test_a_feed_id_route_answers_an_onion_feed_as_an_id_nobody_holds(app, world, path, who):
    user = getattr(world, who)
    missing = signed_get(app, user, path.format(id=2147483000), FIRST)
    for feed in (world.onion_feed, world.legacy_feed):
        hidden = signed_get(app, user, path.format(id=feed.id), FIRST)
        assert hidden.status_code == 404, (path, feed.name)
        assert answer(hidden) == (missing.status_code, missing.get_data().replace(b'2147483000', str(feed.id).encode()))


@pytest.mark.parametrize('path', ['/f/{name}/submit', '/f/{name}.rss', '/f/parent/{name}.rss'])
def test_a_feed_name_route_answers_an_onion_handle_as_a_missing_one(app, world, path):
    hidden = signed_get(app, world.onion_owner, path.format(name=f'onionfeed@{ONION}'), FIRST)
    missing = signed_get(app, world.onion_owner, path.format(name=f'nosuchfeed@{ONION}'), FIRST)
    assert hidden.status_code == 404
    assert answer(hidden) == (missing.status_code, missing.get_data().replace(b'nosuchfeed', b'onionfeed'))


def test_a_moderator_edits_and_deletes_an_rss_import_on_the_first_domain_while_an_onion_exists(app, world, monkeypatch):
    """Review I1: the RSS-import routes' id names an RssFeed, not a Feed, so the gate must not look it up as one (it
    answered 404 for every RSS import with no visible Feed of the same id)."""
    monkeypatch.setitem(app.config, 'RSS_FEEDS', True)
    community = world.d.local_community('somewhere', world.d.first)
    db.session.add(CommunityMember(user_id=world.first_owner.id, community_id=community.id, is_moderator=True))
    rss = RssFeed(id=900, title='an import', url='https://example.com/feed.xml', community_id=community.id,
                  check_frequency=60)
    db.session.add(rss)
    db.session.commit()
    assert db.session.get(Feed, rss.id) is None
    base = f'/community/community/{community.id}/feed/{rss.id}'
    assert signed_get(app, world.first_owner, base, FIRST).status_code == 200
    assert signed_get(app, world.first_owner, f'{base}/delete', FIRST).status_code == 200
    client = app.test_client()
    login(client, world.first_owner, FIRST)
    token = csrf(app, client)
    answer_ = client.post(f'{base}/delete', base_url=f'https://{FIRST}', data={'csrf_token': token, 'submit': 'Delete'})
    assert answer_.status_code == 302
    assert db.session.get(RssFeed, 900) is None

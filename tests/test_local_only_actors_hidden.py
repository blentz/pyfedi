"""A logged-out visitor never sees a local-only account or community, as plan B already does for local-only posts and
replies (owner ruling 2026-10-21, plan C2 task 6 fix round 1). They are left out of every listing and the modlog, and
their pages and feeds answer 404; a logged-in user still sees them. On hell.cloud their names read `bob@abc.onion`, so
the onion hostname is what each test looks for. hell.cloud's own accounts and communities are the control."""
from types import SimpleNamespace

import pytest

from app import db
from app.constants import ROLE_ADMIN_NAME
from app.models import CommunityMember, CommunityWikiPage, File, ModLog, Role, Topic, utcnow
from tests.conftest import forget_login
from tests.factories import make_feed_item, make_local_feed, make_post
from tests.test_instance_visible_here import HOSTS, get, scene  # noqa: F401 -- scene is a fixture
from tests.test_per_domain_ap_routes import onion_proxy, twins  # noqa: F401
from tests.two_domains import HELL, ONION, two_domains  # noqa: F401

pytestmark = pytest.mark.usefixtures('site')

ONION_BOB = f'bob@{ONION}'
ONION_ART = f'art@{ONION}'


@pytest.fixture
def world(app, scene, twins):  # noqa: F811
    hell_bob, hell_art, onion_bob, onion_art, *_ = twins
    avatar = File(file_path='app/static/media/avatar.png')
    db.session.add(avatar)
    db.session.flush()
    for user in (hell_bob, onion_bob):   # what /instance/people/interesting asks of an account
        user.post_count = user.post_reply_count = user.reputation = 5
        user.last_seen = utcnow()
        user.avatar_id = avatar.id
    db.session.commit()
    return SimpleNamespace(hell_bob=hell_bob, hell_art=hell_art, onion_bob=onion_bob, onion_art=onion_art)


def visit(app, path, signed_in, world):
    return get(app, path, user=world.hell_bob if signed_in else None)


# --- the modlog ---------------------------------------------------------------------------------------------------

@pytest.fixture
def entries(world):
    """Public `add_mod` entries, their link text a mark: one in the onion's community, one about an onion account in
    hell's community, and hell's own."""
    rows = [ModLog(community_id=world.onion_art.id, user_id=world.onion_bob.id, target_user_id=world.hell_bob.id,
                   action='add_mod', link='u/bob', link_text='MARK-ONION-COMMUNITY', public=True, type='mod'),
            ModLog(community_id=world.hell_art.id, user_id=world.hell_bob.id, target_user_id=world.onion_bob.id,
                   action='add_mod', link=f'u/{ONION_BOB}', link_text='MARK-ONION-TARGET', public=True, type='mod'),
            ModLog(community_id=world.hell_art.id, user_id=world.hell_bob.id, target_user_id=world.hell_bob.id,
                   action='add_mod', link='u/bob', link_text='MARK-HELL', public=True, type='mod')]
    db.session.add_all(rows)
    db.session.commit()
    return rows


def test_a_logged_out_modlog_lists_nothing_about_a_local_only_community_or_account(app, world, entries):
    body = visit(app, '/modlog', False, world).get_data(as_text=True)
    assert 'MARK-HELL' in body
    assert 'MARK-ONION-COMMUNITY' not in body and 'MARK-ONION-TARGET' not in body
    assert ONION not in body
    assert 'Onion art' not in body   # nor in the community chooser


def test_a_logged_in_modlog_still_lists_them(app, world, entries):
    body = visit(app, '/modlog', True, world).get_data(as_text=True)
    assert {'MARK-HELL', 'MARK-ONION-COMMUNITY', 'MARK-ONION-TARGET'} <= set(body.split())
    assert ONION_ART in body


@pytest.mark.parametrize('signed_in, listed', [(False, 0), (True, 1)])
def test_the_api_modlog_follows_the_same_rule(app, world, entries, monkeypatch, signed_in, listed):
    monkeypatch.setitem(app.config, 'ENABLE_ALPHA_API', 'true')
    forget_login()
    from_api = app.test_client().get('/api/alpha/modlog', base_url=HOSTS[HELL],
                                     headers={'Authorization': f'Bearer {world.hell_bob.encode_jwt_token()}'}
                                     if signed_in else {})
    assert from_api.status_code == 200
    body = from_api.get_data(as_text=True)
    assert f'https://{HELL}/c/art' in body
    assert (ONION in body) == bool(listed)


def test_the_modlog_name_suggestions_leave_out_local_only_accounts_when_logged_out(app, world):
    forget_login()
    logged_out = app.test_client().post('/modlog/search_suggestions', data={'user_name': 'bob'},
                                        base_url=HOSTS[HELL]).get_data(as_text=True)
    assert logged_out.count('<option') == 2   # hell's and the shop's bob, not the onion's


# --- people -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('path', ['/instance/all/people', '/instance/people/interesting'])
def test_logged_out_people_lists_leave_out_local_only_accounts(app, world, path):
    body = visit(app, path, False, world).get_data(as_text=True)
    assert 'href="/u/bob"' in body
    assert ONION not in body


@pytest.mark.parametrize('path', ['/instance/all/people', '/instance/people/interesting'])
def test_logged_in_people_lists_still_show_them(app, world, path):
    assert f'/u/{ONION_BOB}' in visit(app, path, True, world).get_data(as_text=True)


# --- communities --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('path', ['/communities', '/'])
def test_logged_out_community_lists_and_sidebars_leave_out_local_only_communities(app, world, path):
    body = visit(app, path, False, world).get_data(as_text=True)
    assert '/c/art"' in body or "/c/art'" in body   # hell's own
    assert ONION not in body


@pytest.mark.parametrize('path', ['/communities', '/'])
def test_logged_in_community_lists_and_sidebars_still_show_them(app, world, path):
    assert f'/c/{ONION_ART}' in visit(app, path, True, world).get_data(as_text=True)


# --- pages and feeds ----------------------------------------------------------------------------------------------

PAGES = [f'/c/{ONION_ART}', f'/community/{ONION_ART}/feed', f'/community/{ONION_ART}/ical', f'/u/{ONION_BOB}',
         f'/u/{ONION_BOB}/feed', f'/u/{ONION_BOB}/upvotes']


@pytest.mark.parametrize('path', PAGES)
def test_a_local_only_community_or_profile_page_is_not_found_when_logged_out(app, world, path):
    response = visit(app, path, False, world)
    assert response.status_code == 404
    assert ONION not in response.get_data(as_text=True)


@pytest.mark.parametrize('path', [p for p in PAGES if not p.endswith('/feed')])
def test_a_local_only_community_or_profile_page_renders_when_logged_in(app, world, path):
    assert visit(app, path, True, world).status_code == 200


@pytest.mark.parametrize('path', [f'/community/{ONION_ART}/feed', f'/u/{ONION_BOB}/feed'])
def test_a_local_only_feed_answers_an_rss_token(app, world, path):
    world.hell_bob.rss_token = 'hell-bob-rss-token'
    db.session.commit()
    assert visit(app, f'{path}?token=hell-bob-rss-token', False, world).status_code == 200


@pytest.mark.parametrize('path', [p.replace(f'@{ONION}', '') for p in PAGES])
def test_hells_own_community_and_profile_pages_are_unchanged_when_logged_out(app, world, path):
    assert visit(app, path, False, world).status_code == 200


# --- topic and feed pages (fix round 2) ---------------------------------------------------------------------------

@pytest.fixture
def grouped(world):
    """hell's and the onion's community, both in one topic and one public feed, each with a subscriber."""
    topic = Topic(machine_name='arts', name='Arts')
    db.session.add(topic)
    db.session.flush()
    feed = make_local_feed('artfeed', public=True)
    feed.user_id = world.hell_bob.id
    for community in (world.hell_art, world.onion_art):
        community.topic_id = topic.id
        community.total_subscriptions_count = 3
        make_feed_item(feed, community)
    db.session.commit()
    return world


@pytest.mark.parametrize('path', ['/topic/arts', '/f/artfeed'])
def test_logged_out_topic_and_feed_pages_list_no_local_only_community(app, grouped, path):
    response = visit(app, path, False, grouped)
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'Hell art' in body
    assert ONION not in body and 'Onion art' not in body


@pytest.mark.parametrize('path', ['/topic/arts', '/f/artfeed'])
def test_logged_in_topic_and_feed_pages_still_list_them(app, grouped, path):
    assert 'Onion art' in visit(app, path, True, grouped).get_data(as_text=True)


# --- share (fix round 2) -------------------------------------------------------------------------------

SHARED = 'https://example.com/shared-story'


@pytest.fixture
def shared(world):
    """A post of the same link in hell's community and in the onion's (one written before its domain stopped
    federating, so the post itself is not stamped local-only: only its community is)."""
    for community, author in ((world.hell_art, world.hell_bob), (world.onion_art, world.onion_bob)):
        post = make_post(community, author, f'https://{HELL}/post/{community.id}-shared', title='a shared story')
        post.url = SHARED
    db.session.commit()
    return world


def test_logged_out_share_lists_no_local_only_community(app, shared):
    response = visit(app, f'/share?url={SHARED}', False, shared)
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'Hell art' in body
    assert ONION not in body and 'Onion art' not in body


def test_logged_in_share_still_lists_them(app, shared):
    assert 'Onion art' in visit(app, f'/share?url={SHARED}', True, shared).get_data(as_text=True)

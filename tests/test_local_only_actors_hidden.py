"""A logged-out visitor never sees a local-only account or community, as plan B already does for local-only posts and
replies (owner ruling 2026-10-21, plan C2 task 6 fix round 1). They are left out of every listing and the modlog, and
their pages and feeds answer 404. Follow-up F6 ("yes, hide onion from logged-in clearnet viewers too", 2026-10-26)
superseded the C2 ruling that a logged-in user still sees them: off the onion nobody does, so a logged-in user sees them
on the onion only. On the first domain their names read `bob@abc.onion`, so the onion hostname is what each test looks
for. the first domain's own accounts and communities are the control."""
from types import SimpleNamespace
from urllib.parse import quote

import pytest

import app.api.alpha.utils.misc as misc_utils
from app import db
from app.constants import ROLE_ADMIN_NAME
from app.models import CommunityMember, CommunityWikiPage, File, ModLog, Role, Topic, utcnow
from tests.conftest import forget_login
from tests.factories import make_feed_item, make_local_feed, make_post, make_post_reply
from tests.test_instance_visible_here import HOSTS, get, scene  # noqa: F401 -- scene is a fixture
from tests.test_per_domain_ap_routes import onion_proxy, twins  # noqa: F401
from tests.two_domains import FIRST, ONION, two_domains  # noqa: F401

pytestmark = pytest.mark.usefixtures('site')

ONION_BOB = f'bob@{ONION}'
ONION_ART = f'art@{ONION}'


@pytest.fixture
def world(app, scene, twins):  # noqa: F811
    first_bob, first_art, onion_bob, onion_art, *_ = twins
    avatar = File(file_path='app/static/media/avatar.png')
    db.session.add(avatar)
    db.session.flush()
    for user in (first_bob, onion_bob):   # what /instance/people/interesting asks of an account
        user.post_count = user.post_reply_count = user.reputation = 5
        user.last_seen = utcnow()
        user.avatar_id = avatar.id
    db.session.commit()
    return SimpleNamespace(first_bob=first_bob, first_art=first_art, onion_bob=onion_bob, onion_art=onion_art)


def visit(app, path, signed_in, world, host=FIRST):
    return get(app, path, host=host, user=world.first_bob if signed_in else None)


def around(body: str, needle: str) -> str:
    """The text around `needle` in `body`, for a failed assertion's message."""
    at = body.find(needle)
    return body[max(0, at - 300):at + 100]


def on_onion(path: str) -> str:
    """An onion row's `path` as the onion names it: bare."""
    return path.replace(f'@{ONION}', '')


# --- the modlog ---------------------------------------------------------------------------------------------------

@pytest.fixture
def entries(world):
    """Public `add_mod` entries, their link text a mark: one in the onion's community, one about an onion account in
    The first domain's community, and the first domain's own."""
    rows = [ModLog(community_id=world.onion_art.id, user_id=world.onion_bob.id, target_user_id=world.first_bob.id,
                   action='add_mod', link='u/bob', link_text='MARK-ONION-COMMUNITY', public=True, type='mod'),
            ModLog(community_id=world.first_art.id, user_id=world.first_bob.id, target_user_id=world.onion_bob.id,
                   action='add_mod', link=f'u/{ONION_BOB}', link_text='MARK-ONION-TARGET', public=True, type='mod'),
            ModLog(community_id=world.first_art.id, user_id=world.first_bob.id, target_user_id=world.first_bob.id,
                   action='add_mod', link='u/bob', link_text='MARK-FIRST', public=True, type='mod')]
    db.session.add_all(rows)
    db.session.commit()
    return rows


def test_a_logged_out_modlog_lists_nothing_about_a_local_only_community_or_account(app, world, entries):
    body = visit(app, '/modlog', False, world).get_data(as_text=True)
    assert 'MARK-FIRST' in body
    assert 'MARK-ONION-COMMUNITY' not in body and 'MARK-ONION-TARGET' not in body
    assert ONION not in body
    assert 'Onion art' not in body   # nor in the community chooser


def test_a_logged_in_modlog_lists_them_on_the_onion_only(app, world, entries):
    """Follow-up F6: no longer on the first domain."""
    body = visit(app, '/modlog', True, world).get_data(as_text=True)
    assert 'MARK-FIRST' in body
    assert 'MARK-ONION-COMMUNITY' not in body and 'MARK-ONION-TARGET' not in body and ONION not in body
    body = visit(app, '/modlog', True, world, host=ONION).get_data(as_text=True)
    assert {'MARK-FIRST', 'MARK-ONION-COMMUNITY', 'MARK-ONION-TARGET'} <= set(body.split())


@pytest.mark.parametrize('signed_in, host, listed', [(False, FIRST, 1), (True, FIRST, 1), (False, ONION, 1),
                                                   (True, ONION, 3)])
def test_the_api_modlog_follows_the_same_rule(app, world, entries, monkeypatch, signed_in, host, listed):
    """Follow-up F6: a signed-in caller is shown them on the onion only."""
    monkeypatch.setitem(app.config, 'ENABLE_ALPHA_API', 'true')
    forget_login()
    from_api = app.test_client().get('/api/alpha/modlog', base_url=HOSTS[host],
                                     headers={'Authorization': f'Bearer {world.first_bob.encode_jwt_token()}'}
                                     if signed_in else {})
    assert from_api.status_code == 200
    assert len(from_api.get_json()['added_to_community']) == listed
    if host == FIRST:
        assert ONION not in from_api.get_data(as_text=True)


def test_the_modlog_name_suggestions_leave_out_local_only_accounts_when_logged_out(app, world):
    forget_login()
    logged_out = app.test_client().post('/modlog/search_suggestions', data={'user_name': 'bob'},
                                        base_url=HOSTS[FIRST]).get_data(as_text=True)
    assert logged_out.count('<option') == 2   # The first domain's and the shop's bob, not the onion's


# --- people -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('path', ['/instance/all/people', '/instance/people/interesting'])
def test_logged_out_people_lists_leave_out_local_only_accounts(app, world, path):
    body = visit(app, path, False, world).get_data(as_text=True)
    assert 'href="/u/bob"' in body
    assert ONION not in body


@pytest.mark.parametrize('path', ['/instance/all/people', '/instance/people/interesting'])
def test_logged_in_people_lists_show_them_on_the_onion_only(app, world, path):
    """Follow-up F6: no longer on the first domain."""
    assert ONION not in visit(app, path, True, world).get_data(as_text=True)
    assert 'href="/u/bob"' in visit(app, path, True, world, host=ONION).get_data(as_text=True)


# --- communities --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('path', ['/communities', '/'])
def test_logged_out_community_lists_and_sidebars_leave_out_local_only_communities(app, world, path):
    body = visit(app, path, False, world).get_data(as_text=True)
    assert '/c/art"' in body or "/c/art'" in body   # The first domain's own
    assert ONION not in body


@pytest.mark.parametrize('path', ['/communities', '/'])
def test_logged_in_community_lists_and_sidebars_show_them_on_the_onion_only(app, world, path):
    """Follow-up F6: no longer on the first domain."""
    body = visit(app, path, True, world).get_data(as_text=True)
    assert ONION not in body, around(body, ONION)
    body = visit(app, path, True, world, host=ONION).get_data(as_text=True)
    assert '/c/art"' in body or "/c/art'" in body


# --- pages and feeds ----------------------------------------------------------------------------------------------

PAGES = [f'/c/{ONION_ART}', f'/community/{ONION_ART}/feed', f'/community/{ONION_ART}/ical', f'/u/{ONION_BOB}',
         f'/u/{ONION_BOB}/feed', f'/u/{ONION_BOB}/upvotes']


@pytest.mark.parametrize('path', PAGES)
def test_a_local_only_community_or_profile_page_is_not_found_when_logged_out(app, world, path):
    response = visit(app, path, False, world)
    assert response.status_code == 404
    assert ONION not in response.get_data(as_text=True)


@pytest.mark.parametrize('path', [p for p in PAGES if not p.endswith('/feed')])
def test_a_local_only_community_or_profile_page_renders_when_logged_in_on_its_own_domain_only(app, world, path):
    """C2 let a logged-in user reach them on the first domain. Follow-up F6 ("yes, hide onion from logged-in clearnet
    viewers too", owner ruling 2026-10-26) supersedes that: off the onion they are 404 to everyone."""
    assert visit(app, path, True, world).status_code == 404
    assert visit(app, on_onion(path), True, world, host=ONION).status_code == 200


@pytest.mark.parametrize('path', [f'/community/{ONION_ART}/feed', f'/u/{ONION_BOB}/feed'])
def test_a_local_only_feed_answers_an_rss_token_on_its_own_domain_only(app, world, path):
    """Follow-up F6: an RSS token is a signed-in reader, so it is answered on the onion and nowhere else."""
    world.first_bob.rss_token = 'first-bob-rss-token'
    db.session.commit()
    assert visit(app, f'{path}?token=first-bob-rss-token', False, world).status_code == 404
    assert visit(app, f'{on_onion(path)}?token=first-bob-rss-token', False, world, host=ONION).status_code == 200


@pytest.mark.parametrize('path', [p.replace(f'@{ONION}', '') for p in PAGES])
def test_first_domains_own_community_and_profile_pages_are_unchanged_when_logged_out(app, world, path):
    assert visit(app, path, False, world).status_code == 200


# --- topic and feed pages (fix round 2) ---------------------------------------------------------------------------

@pytest.fixture
def grouped(world):
    """The first domain's and the onion's community, both in one topic and one public feed, each with a subscriber."""
    topic = Topic(machine_name='arts', name='Arts')
    db.session.add(topic)
    db.session.flush()
    feed = make_local_feed('artfeed', public=True)
    feed.user_id = world.first_bob.id
    for community in (world.first_art, world.onion_art):
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
    assert 'First art' in body
    assert ONION not in body and 'Onion art' not in body


@pytest.mark.parametrize('path', ['/topic/arts', '/f/artfeed'])
def test_logged_in_topic_and_feed_pages_list_them_on_the_onion_only(app, grouped, path):
    """Follow-up F6: no longer on the first domain."""
    assert 'Onion art' not in visit(app, path, True, grouped).get_data(as_text=True)
    if path == '/topic/arts':   # a feed is its home domain's: the first domain's renders there only (F4)
        assert 'Onion art' in visit(app, path, True, grouped, host=ONION).get_data(as_text=True)


# --- share (fix round 2) -------------------------------------------------------------------------------

SHARED = 'https://example.com/shared-story'


@pytest.fixture
def shared(world):
    """A post of the same link in the first domain's community and in the onion's (one written before its domain stopped
    federating, so the post itself is not stamped local-only: only its community is)."""
    for community, author in ((world.first_art, world.first_bob), (world.onion_art, world.onion_bob)):
        post = make_post(community, author, f'https://{FIRST}/post/{community.id}-shared', title='a shared story')
        post.url = SHARED
    db.session.commit()
    return world


def test_logged_out_share_lists_no_local_only_community(app, shared):
    response = visit(app, f'/share?url={SHARED}', False, shared)
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'First art' in body
    assert ONION not in body and 'Onion art' not in body


def test_logged_in_share_lists_them_on_the_onion_only(app, shared):
    """Follow-up F6: no longer on the first domain."""
    assert 'Onion art' not in visit(app, f'/share?url={SHARED}', True, shared).get_data(as_text=True)
    assert 'Onion art' in visit(app, f'/share?url={SHARED}', True, shared, host=ONION).get_data(as_text=True)


# --- the alpha API (fix round 2) ----------------------------------------------------------------------------------

API_LISTS = ['/api/alpha/community/list', '/api/alpha/search?q=art&type_=Communities',
             '/api/alpha/search?q=bob&type_=Users', '/api/alpha/suggest_completion?q=!art',
             '/api/alpha/suggest_completion?q=@bob', '/api/alpha/site']
API_GROUPED = ['/api/alpha/topic/list', f'/api/alpha/feed?name=artfeed@{FIRST}']
API_ONE = [f'/api/alpha/community?name={ONION_ART}', f'/api/alpha/user?username={ONION_BOB}']


def api_get(app, path, world, signed_in, host=FIRST):
    forget_login()
    headers = {'Authorization': f'Bearer {world.first_bob.encode_jwt_token()}'} if signed_in else {}
    return app.test_client().get(path, base_url=HOSTS[host], headers=headers)


@pytest.fixture
def api(app, world, monkeypatch):
    monkeypatch.setitem(app.config, 'ENABLE_ALPHA_API', 'true')
    world.onion_bob.roles.append(Role.query.filter_by(name=ROLE_ADMIN_NAME).first() or Role(name=ROLE_ADMIN_NAME))
    db.session.commit()   # an onion admin, for the site view's admin list
    return world


@pytest.mark.parametrize('path', API_LISTS)
def test_the_alpha_apis_lists_name_no_local_only_row_to_a_logged_out_caller(app, api, path):
    response = api_get(app, path, api, False)
    assert response.status_code == 200
    assert ONION not in response.get_data(as_text=True)


@pytest.mark.parametrize('path', API_LISTS)
def test_the_alpha_apis_lists_name_them_to_a_logged_in_caller_on_the_onion_only(app, api, path):
    """Follow-up F6: no longer on the first domain."""
    response = api_get(app, path, api, True)
    assert response.status_code == 200
    assert ONION not in response.get_data(as_text=True)
    response = api_get(app, path, api, True, host=ONION)
    assert response.status_code == 200
    assert ONION in response.get_data(as_text=True)


def test_get_suggestion_passes_on_a_refusal_that_is_not_a_failed_login(app, api, monkeypatch):
    """Only 'incorrect_login' means logged out; any other failure from authorise_api_user is not swallowed."""
    def broken(auth):
        raise RuntimeError('database gone')
    monkeypatch.setattr(misc_utils, 'authorise_api_user', broken)
    with pytest.raises(RuntimeError, match='database gone'):
        misc_utils.get_suggestion({'q': '!art'}, auth='Bearer x')


@pytest.mark.parametrize('token', ['Bearer garbage', 'Bearer ', 'Basic dXNlcjpwYXNz', 'expired'])
@pytest.mark.parametrize('query', ['!art', '@bob'])
def test_suggest_completion_answers_a_token_that_fails_to_authorise_as_logged_out(app, api, monkeypatch, token,
                                                                                    query):
    """As the first domain always did: the Authorization header is optional here, and a stale one is no error (final wave)."""
    if token == 'expired':
        monkeypatch.setitem(app.config, 'JWT_EXPIRY_DAYS', -1)
        token = f'Bearer {api.first_bob.encode_jwt_token()}'
    forget_login()
    response = app.test_client().get(f'/api/alpha/suggest_completion?q={quote(query)}', base_url=HOSTS[FIRST],
                                     headers={'Authorization': token})
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert FIRST in body and ONION not in body


@pytest.mark.parametrize('signed_in', [False, True])
@pytest.mark.parametrize('path', API_GROUPED)
def test_the_alpha_apis_topic_and_feed_views_list_no_local_only_community_on_the_first_domain(
        app, api, grouped, path, signed_in):
    """Follow-up F6: not to a logged-in caller either."""
    response = api_get(app, path, api, signed_in)
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert f'https://{FIRST}/c/art' in body
    assert ONION not in body


@pytest.mark.parametrize('path', API_ONE)
def test_the_alpha_apis_single_views_refuse_a_local_only_row_to_a_logged_out_caller(app, api, path):
    response = api_get(app, path, api, False)
    assert response.status_code == 400
    assert ONION not in response.get_data(as_text=True)
    assert api_get(app, path, api, True).status_code == 400   # follow-up F6: on the first domain, to anyone
    assert api_get(app, path, api, True, host=ONION).status_code == 200


# --- subpages (fix round 2) ---------------------------------------------------------------------------------------

@pytest.fixture
def subpages(world):
    db.session.add(CommunityWikiPage(community_id=world.onion_art.id, slug='rules', title='Rules', body='be kind',
                                     body_html='<p>be kind</p>', who_can_edit=0))
    db.session.add(CommunityWikiPage(community_id=world.first_art.id, slug='rules', title='Rules', body='be kind',
                                     body_html='<p>be kind</p>', who_can_edit=0))
    db.session.commit()
    return world


def subpage_paths(world, onion=True):
    community, user = (world.onion_art, world.onion_bob) if onion else (world.first_art, world.first_bob)
    name, handle = (ONION_ART, ONION_BOB) if onion else ('art', 'bob')
    return [f'/community/{name}/wiki/rules', f'/community/get_sidebar/{community.id}',
            f'/community/community_changed?communities={community.id}', f'/user/{user.id}/preview',
            f'/user/{user.id}', f'/u/{handle}/feeds', f'/u/{handle}/fediverse_redirect']


def test_a_local_only_community_or_accounts_subpages_are_not_found_when_logged_out(app, subpages):
    for path in subpage_paths(subpages):
        response = visit(app, path, False, subpages)
        assert response.status_code == 404, path
        assert ONION not in response.get_data(as_text=True), path


def test_they_render_when_logged_in_on_the_onion_only(app, subpages):
    """Follow-up F6: on the first domain they are 404 to a logged-in user too."""
    for path in subpage_paths(subpages):
        assert visit(app, path, True, subpages).status_code == 404, path
        assert visit(app, on_onion(path), True, subpages, host=ONION).status_code == 200, path


def test_first_domains_own_subpages_are_unchanged_when_logged_out(app, subpages):
    for path in subpage_paths(subpages, onion=False):
        assert visit(app, path, False, subpages).status_code == 200, path


@pytest.mark.parametrize('signed_in', [False, True])
def test_first_domains_community_page_names_no_local_only_moderator_or_related_community(app, grouped, signed_in):
    """Follow-up F6: not to a logged-in reader either; on the onion it does."""
    db.session.add(CommunityMember(user_id=grouped.onion_bob.id, community_id=grouped.first_art.id, is_moderator=True))
    db.session.commit()
    response = visit(app, '/c/art', signed_in, grouped)
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert f'/u/{ONION_BOB}' not in body   # the moderator list
    assert f'/c/{ONION_ART}' not in body   # the related communities
    if signed_in:
        body = visit(app, f'/c/art@{FIRST}', True, grouped, host=ONION).get_data(as_text=True)
        assert 'href="/u/bob"' in body and 'href="/c/art"' in body


# --- /about (fix round 2) -----------------------------------------------------------------------------------------

@pytest.mark.parametrize('signed_in', [False, True])
def test_the_about_page_names_a_local_only_admin_only_to_a_logged_in_reader_on_the_onion(app, api, signed_in):
    """Follow-up F6: on the first domain to nobody."""
    assert ONION not in visit(app, '/about', signed_in, api).get_data(as_text=True)
    assert ('href="/u/bob"' in visit(app, '/about', signed_in, api, host=ONION).get_data(as_text=True)) == signed_in


# --- moderator lists of the first domain's own rows (fix round 2) -------------------------------------------------------------

@pytest.fixture
def cross_moderated(api):
    """The onion's bob moderates the first domain's community, and the first domain's bob the onion's: the first domain's own rows then name them."""
    db.session.add(CommunityMember(user_id=api.onion_bob.id, community_id=api.first_art.id, is_moderator=True))
    db.session.add(CommunityMember(user_id=api.first_bob.id, community_id=api.onion_art.id, is_moderator=True))
    db.session.commit()
    api.first_post = make_post(api.first_art, api.first_bob, f'https://{FIRST}/post/firstpost', title='a first post')
    return api


@pytest.mark.parametrize('signed_in', [False, True])
def test_first_domains_own_api_views_name_no_local_only_moderator_or_community(app, cross_moderated, signed_in):
    """Follow-up F6: not to a logged-in caller either."""
    for path in ('/api/alpha/community?name=art', '/api/alpha/user?username=bob',
                 f'/api/alpha/post?id={cross_moderated.first_post.id}'):
        response = api_get(app, path, cross_moderated, signed_in)
        assert response.status_code == 200, path
        assert ONION not in response.get_data(as_text=True), path


@pytest.mark.parametrize('signed_in', [False, True])
def test_first_domains_own_profile_names_no_local_only_community_it_moderates(app, cross_moderated, signed_in):
    """Follow-up F6: not to a logged-in reader either."""
    body = visit(app, '/u/bob', signed_in, cross_moderated).get_data(as_text=True)
    assert f'/c/{ONION_ART}' not in body, around(body, f'/c/{ONION_ART}')


# --- post and reply pages (fix round 3) ---------------------------------------------------------------------------

@pytest.fixture
def first_thread(grouped):
    """A post and a reply in the first domain's community, which the onion's bob moderates and whose topic holds the onion's."""
    db.session.add(CommunityMember(user_id=grouped.onion_bob.id, community_id=grouped.first_art.id, is_moderator=True))
    db.session.commit()
    grouped.post = make_post(grouped.first_art, grouped.first_bob, f'https://{FIRST}/post/thread', title='a first thread')
    grouped.reply = make_post_reply(grouped.post, grouped.first_bob)
    grouped.reply.body_html = '<p>a reply</p>'
    db.session.commit()
    return grouped


def thread_paths(world):
    return [f'/post/{world.post.id}', f'/post/{world.post.id}/comment/{world.reply.id}']


@pytest.mark.parametrize('signed_in', [False, True])
def test_a_first_post_page_names_no_local_only_moderator_or_community(app, first_thread, monkeypatch, signed_in):
    """Follow-up F6: not to a logged-in reader either."""
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', True)   # the signed-in reply form renders its token
    for path in thread_paths(first_thread):
        response = visit(app, path, signed_in, first_thread)
        assert response.status_code == 200, path
        body = response.get_data(as_text=True)
        assert f'/u/{ONION_BOB}' not in body, path   # the moderator list
    body = visit(app, thread_paths(first_thread)[0], signed_in, first_thread).get_data(as_text=True)
    assert f'/c/{ONION_ART}' not in body, around(body, f'/c/{ONION_ART}')   # the same-topic communities

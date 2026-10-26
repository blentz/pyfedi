"""Off the onion, the onion is invisible to everyone (follow-up F6). Owner rulings 2026-10-26: "yes, hide onion from
logged-in clearnet viewers too", and for admins "No exception": on the clearnet, a report or notification shows an
onion target as plain text, with a note to open it on the onion.

On the first domain a signed-in user and an admin alike get the 404 a name nobody holds gets for the onion's accounts,
communities, posts and replies, and every list and search leaves them out. On the onion the same viewers see them all.
The first domain's own rows are the control."""

import pytest

from app import db
from app.constants import ROLE_ADMIN_NAME
from app.local_only import content_local_only
from app.models import CommunityMember, Post, PostVote, Role, Topic, utcnow
from tests.factories import grant_permission, make_post, make_post_reply
from tests.test_instance_visible_here import HOSTS, get, scene  # noqa: F401 -- scene is a fixture
from tests.test_local_only_actors_hidden import ONION_ART, ONION_BOB, world  # noqa: F401 -- world is a fixture
from tests.test_per_domain_ap_routes import onion_proxy, twins  # noqa: F401
from tests.two_domains import FIRST, ONION, two_domains  # noqa: F401
from tests.conftest import forget_login

pytestmark = pytest.mark.usefixtures('site')

TOKEN = 'f6-rss-token'


def near(body: str, *needles: str) -> list[str]:
    """The text around each needle found in `body`: what a failed assertion shows."""
    return [body[max(0, i - 150):i + 80] for needle in needles if (i := body.find(needle)) >= 0]


def stamped(post_or_reply, author, community, *parents):
    post_or_reply.local_only = content_local_only(author, community, *parents)
    db.session.commit()
    return post_or_reply


@pytest.fixture
def onion(app, world, scene, two_domains):  # noqa: F811
    """The onion's bob and art, his post ONIONPOST in art (from `scene`) with a reply, and his post ONIONINFIRST in the
    first domain's art; the first domain's bob, its admin, and the first domain's own post FIRSTPOST."""
    first = two_domains.domains[0]
    world.post = Post.query.filter_by(title='ONIONPOST').one()
    world.reply = stamped(make_post_reply(world.post, world.onion_bob), world.onion_bob, world.onion_art, None,
                          world.post)
    world.reply.body_html = '<p>ONIONREPLY</p>'
    world.in_first = stamped(make_post(world.first_art, world.onion_bob, f'http://{ONION}/post/in-first',
                                       title='ONIONINFIRST'), world.onion_bob, world.first_art)
    world.first_post = make_post(world.first_art, world.first_bob, f'https://{FIRST}/post/first', title='FIRSTPOST')
    world.admin = two_domains.local_user('boss', first)
    world.admin.private_key = 'a key'
    world.admin.roles.append(Role.query.filter_by(name=ROLE_ADMIN_NAME).first() or Role(name=ROLE_ADMIN_NAME))
    for user in (world.first_bob, world.admin):
        user.rss_token = f'{TOKEN}-{user.id}'
    db.session.commit()
    grant_permission(world.admin, 'administer all users')
    for post in (world.post, world.in_first, world.first_post):
        post.posted_at = post.created_at = post.last_active = utcnow()
        post.score = post.up_votes = 50
        post.ranking = post.ranking_scaled = 1000
    topic = Topic(machine_name='arts', name='Arts')
    db.session.add(topic)
    db.session.flush()
    for community in (world.first_art, world.onion_art):   # in every feed: all, popular, a topic, the subscribed
        community.show_all = community.show_popular = True
        community.topic_id = topic.id
        for user in (world.first_bob, world.admin):
            db.session.add(CommunityMember(user_id=user.id, community_id=community.id))
    db.session.commit()
    return world


def viewers(world):
    return {'user': world.first_bob, 'admin': world.admin}


def pages(world, on_onion=False):
    """The onion's pages and sub-pages, as each host names them."""
    bob, art = ('bob', 'art') if on_onion else (ONION_BOB, ONION_ART)
    return [f'/u/{bob}', f'/user/{world.onion_bob.id}', f'/u/{bob}/upvotes', f'/user/{world.onion_bob.id}/preview',
            f'/c/{art}', f'/community/{world.onion_art.id}', f'/community/{art}/moderate',
            f'/community/get_sidebar/{world.onion_art.id}', f'/post/{world.post.id}',
            f'/post/{world.post.id}/comment/{world.reply.id}', f'/post/{world.post.id}/embed',
            f'/post/{world.in_first.id}']


def feeds(world, viewer, on_onion=False):
    bob, art = ('bob', 'art') if on_onion else (ONION_BOB, ONION_ART)
    return [f'/u/{bob}/feed?token={viewer.rss_token}', f'/community/{art}/feed?token={viewer.rss_token}']


@pytest.mark.parametrize('who', ['user', 'admin'])
def test_on_the_first_domain_every_onion_page_is_not_found_for_a_signed_in_user_or_an_admin(app, onion, who):
    viewer = viewers(onion)[who]
    nobody = get(app, '/u/nobody', user=viewer)
    assert nobody.status_code == 404
    for path in pages(onion) + feeds(onion, viewer):
        response = get(app, path, user=viewer)
        assert response.status_code == 404, path
        body = response.get_data(as_text=True)
        assert ONION not in body and 'ONIONPOST' not in body and 'Onion art' not in body, path


@pytest.mark.parametrize('who', ['user', 'admin'])
def test_on_the_onion_the_same_viewers_see_them_all(app, onion, monkeypatch, who):
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', True)   # the signed-in reply form renders its token
    viewer = viewers(onion)[who]
    for path in pages(onion, on_onion=True):
        if path.endswith('/moderate') and who == 'user':
            continue   # a moderator's page: the user moderates nothing there
        assert get(app, path, host=ONION, user=viewer).status_code == 200, path
    for path in feeds(onion, viewer, on_onion=True):
        assert get(app, path, host=ONION, user=viewer).status_code == 200, path   # the token is the reader


@pytest.mark.parametrize('who', ['user', 'admin'])
@pytest.mark.parametrize('path', ['/c/art', '/', '/home/new/popular', '/home/new/all', '/home/new/subscribed',
                                  '/community/art/feed', '/topic/arts',
                                  '/topic/arts.rss'])
def test_an_onion_accounts_post_in_a_first_domain_community_is_absent_there(app, onion, who, path):
    viewer = viewers(onion)[who]
    if path.endswith('/feed'):
        path = f'{path}?token={viewer.rss_token}'
    body = get(app, path, user=viewer).get_data(as_text=True)
    assert 'ONIONINFIRST' not in body and 'ONIONPOST' not in body, path
    assert 'FIRSTPOST' in body, path   # the control: the list does show posts


def test_on_the_onion_it_is_present_in_the_first_domains_community(app, onion):
    body = get(app, f'/c/art@{FIRST}', host=ONION, user=onion.first_bob).get_data(as_text=True)
    assert 'ONIONINFIRST' in body and 'FIRSTPOST' in body


@pytest.mark.parametrize('who', ['user', 'admin'])
@pytest.mark.parametrize('query', ['ONIONPOST', 'ONIONINFIRST', 'ONIONREPLY', 'bob', 'art'])
def test_search_on_the_first_domain_finds_none_of_them(app, onion, who, query):
    viewer = viewers(onion)[who]
    hidden = (ONION, f'/post/{onion.post.id}"', f'/post/{onion.in_first.id}"', 'ONIONREPLY</p>')
    for kind in ('', '&type=communities', '&type=comments'):
        body = get(app, f'/search?q={query}{kind}', user=viewer).get_data(as_text=True)
        assert not near(body, *hidden), (query, kind, near(body, *hidden))


def api(app, path, viewer, host=FIRST):
    forget_login()
    return app.test_client().get(path, base_url=HOSTS[host],
                                 headers={'Authorization': f'Bearer {viewer.encode_jwt_token()}'})


@pytest.fixture
def alpha(app, onion, monkeypatch):
    monkeypatch.setitem(app.config, 'ENABLE_ALPHA_API', 'true')
    return onion


@pytest.mark.parametrize('who', ['user', 'admin'])
def test_the_api_on_the_first_domain_lists_none_of_them(app, alpha, who):
    viewer = viewers(alpha)[who]
    for path in ('/api/alpha/post/list?sort=New', f'/api/alpha/post/list?community_id={alpha.first_art.id}',
                 '/api/alpha/search?q=ONION&type_=Posts', '/api/alpha/search?q=ONIONREPLY&type_=Comments',
                 '/api/alpha/search?q=bob&type_=Users',
                 '/api/alpha/search?q=art&type_=Communities', '/api/alpha/community/list',
                 f'/api/alpha/comment/list?post_id={alpha.first_post.id}'):
        response = api(app, path, viewer)
        assert response.status_code == 200, path
        body = response.get_data(as_text=True)
        assert not near(body, ONION, 'ONIONPOST', 'ONIONINFIRST'), (path, near(body, ONION, 'ONIONPOST', 'ONIONINFIRST'))


@pytest.mark.parametrize('who', ['user', 'admin'])
def test_the_apis_not_found_for_a_hidden_object_is_the_one_for_an_id_nobody_holds(app, alpha, who):
    viewer = viewers(alpha)[who]
    nobody = 2 ** 30
    pairs = [(f'/api/alpha/post?id={alpha.post.id}', f'/api/alpha/post?id={nobody}'),
             (f'/api/alpha/post?id={alpha.in_first.id}', f'/api/alpha/post?id={nobody}'),
             (f'/api/alpha/comment?id={alpha.reply.id}', f'/api/alpha/comment?id={nobody}'),
             (f'/api/alpha/community?id={alpha.onion_art.id}', f'/api/alpha/community?id={nobody}'),
             (f'/api/alpha/user?person_id={alpha.onion_bob.id}', f'/api/alpha/user?person_id={nobody}'),
             (f'/api/alpha/resolve_object?q=http://{ONION}/post/{alpha.post.id}',
              f'/api/alpha/resolve_object?q=http://{ONION}/post/{nobody}'),
             (f'/api/alpha/resolve_object?q=http://{ONION}/u/bob', f'/api/alpha/resolve_object?q=http://{ONION}/u/nobody')]
    for hidden, missing in pairs:
        found, absent = api(app, hidden, viewer), api(app, missing, viewer)
        assert (found.status_code, found.get_data()) == (absent.status_code, absent.get_data()), hidden
        assert found.status_code in (400, 404), hidden


def test_the_api_on_the_onion_still_shows_them(app, alpha):
    for path in (f'/api/alpha/post?id={alpha.post.id}', f'/api/alpha/community?id={alpha.onion_art.id}',
                 f'/api/alpha/user?person_id={alpha.onion_bob.id}', f'/api/alpha/comment?id={alpha.reply.id}'):
        assert api(app, path, alpha.first_bob, host=ONION).status_code == 200, path


# --- lists of people ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize('who', ['user', 'admin'])
@pytest.mark.parametrize('path', ['/instance/all/people', '/instance/people/interesting', '/communities', '/modlog',
                                  '/about'])
def test_first_domain_lists_name_no_onion_row_for_any_viewer(app, onion, who, path):
    body = get(app, path, user=viewers(onion)[who]).get_data(as_text=True)
    assert not near(body, ONION, 'Onion art'), (path, near(body, ONION, 'Onion art'))


def test_a_first_domain_posts_voters_leave_out_an_onion_voter(app, onion):
    db.session.add(PostVote(user_id=onion.onion_bob.id, author_id=onion.first_bob.id, post_id=onion.first_post.id,
                            effect=1.0))
    db.session.commit()
    body = get(app, f'/post/{onion.first_post.id}/voting_activity', user=onion.admin).get_data(as_text=True)
    assert ONION not in body
    on_onion = get(app, f'/post/{onion.first_post.id}/voting_activity', host=ONION, user=onion.admin)
    assert on_onion.status_code == 200 and 'bob' in on_onion.get_data(as_text=True)


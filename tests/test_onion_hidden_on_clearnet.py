"""Off the onion, the onion is invisible to everyone (follow-up F6). Owner rulings 2026-10-26: "yes, hide onion from
logged-in clearnet viewers too", and for admins "No exception": on the clearnet, a report or notification shows an
onion target as plain text, with a note to open it on the onion.

On the first domain a signed-in user and an admin alike get the 404 a name nobody holds gets for the onion's accounts,
communities, posts and replies, and every list and search leaves them out. On the onion the same viewers see them all.
The first domain's own rows are the control."""
import re
from datetime import datetime
from types import SimpleNamespace

import pytest
from flask import render_template

from app import db
from app.constants import ROLE_ADMIN_NAME
from app.local_only import content_local_only
from app.models import CommunityMember, Domain, File, Notification, Post, PostBookmark, PostReplyBookmark, PostVote, \
    Report, Role, Topic, UserRegistration, read_posts, user_file, utcnow
from tests.factories import grant_permission, make_post, make_post_reply
from tests.test_hidden_here import reply_chain
from tests.test_per_domain_html_routes import Visitor
from tests.test_instance_visible_here import HOSTS, get, scene  # noqa: F401 -- scene is a fixture
from tests.test_local_only_actors_hidden import ONION_ART, ONION_BOB, world  # noqa: F401 -- world is a fixture
from tests.test_per_domain_ap_routes import onion_proxy, twins  # noqa: F401
from tests.two_domains import FIRST, ONION, onion_request, default_request, two_domains  # noqa: F401
from tests.conftest import forget_login

pytestmark = pytest.mark.usefixtures('site')

TOKEN = 'f6-rss-token'
NOTE = 'Open this on the onion to see it.'


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


# --- reports and notifications -----------------------------------------------------------------------------------

@pytest.fixture
def reported(onion):
    onion.report = Report(reasons='spam', description='about onion bob', type=0, reporter_id=onion.first_bob.id,
                          suspect_user_id=onion.onion_bob.id, source_instance_id=1,
                          targets={'suspect_user_user_name': ONION_BOB, 'suspect_user_id': onion.onion_bob.id,
                                   'reporter_user_name': 'bob', 'reporter_id': onion.first_bob.id})
    onion.post_report = Report(reasons='spam', description='about ONIONPOST', type=1, reporter_id=onion.first_bob.id,
                               suspect_post_id=onion.post.id, source_instance_id=1,
                               targets={'suspect_post_id': onion.post.id, 'orig_post_title': 'the onion post',
                                        'suspect_user_user_name': ONION_BOB, 'suspect_user_id': onion.onion_bob.id})
    db.session.add_all([onion.report, onion.post_report])
    db.session.commit()
    return onion


def test_a_first_domain_admins_report_page_names_onion_bob_as_text_with_the_note(app, reported):
    response = get(app, '/admin/reports', user=reported.admin)
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert ONION_BOB in body and NOTE in body
    assert f'/user/{reported.onion_bob.id}"' not in body and f'/post/{reported.post.id}"' not in body
    assert 'the onion post' in body
    assert f'href="/user/{reported.first_bob.id}"' in body   # the first domain's reporter is still a link


def test_on_the_onion_the_report_links_them(app, reported):
    with onion_request(app):
        html = render_template('admin/reports/post_report.html', report=reported.post_report)
    assert f'href="/post/{reported.post.id}"' in html and f'href="/user/{reported.onion_bob.id}"' in html
    assert NOTE not in html


def test_a_notification_on_the_first_domain_names_an_onion_author_as_text_with_the_note(app, onion):
    notification = SimpleNamespace(
        id=1, read=False, created_at=None, subtype=None,
        targets={'author_user_name': ONION_BOB, 'author_id': onion.onion_bob.id, 'community_name': 'art',
                 'community_id': onion.first_art.id, 'post_id': onion.first_post.id, 'post_title': 'a post'})
    with default_request(app):
        html = render_template('user/notifs/0.html', notification=notification, localize_datetime=lambda *a: '',
                               locale='en')
    assert NOTE in html and f'/user/{onion.onion_bob.id}"' not in html
    assert f'href="/community/{onion.first_art.id}"' in html


def test_a_report_with_no_usable_post_id_shows_its_text(app, onion):
    report = SimpleNamespace(id=1, reasons='spam', description='', is_local=lambda *a, **k: True,
                             created_at=datetime(2026, 1, 1), updated=datetime(2026, 1, 1), targets={'orig_post_title': 'a title'})
    with default_request(app):
        html = render_template('admin/reports/post_report.html', report=report)
    assert 'a title' in html and 'href="/post/' not in html


def test_a_first_domain_post_in_a_report_is_still_a_link(app, onion):
    report = SimpleNamespace(id=1, reasons='spam', description='', is_local=lambda *a, **k: True,
                             created_at=datetime(2026, 1, 1), updated=datetime(2026, 1, 1),
                             targets={'suspect_comment_id': onion.first_post.id, 'orig_comment_body': 'body'})
    with default_request(app):
        html = render_template('admin/reports/comment_report.html', report=report)
    assert NOTE not in html


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


# --- a reply further down an onion reply (review I6) ---------------------------------------------------------------

@pytest.fixture
def deep(app, onion):
    """The onion's bob replies to the first domain's FIRSTPOST; the first domain's bob answers him, on the onion, the
    only place the onion's reply can be seen. That answer must not show on the first domain either: listed on the first
    bob's profile it would tell that he uses the onion."""
    onion.onion_under, onion.first_under = reply_chain(onion.first_post, onion.onion_bob, onion.first_bob)
    onion.onion_under.body, onion.onion_under.body_html = 'ONIONUNDER', '<p>ONIONUNDER</p>'
    onion.first_under.body, onion.first_under.body_html = 'FIRSTUNDERONION', '<p>FIRSTUNDERONION</p>'
    db.session.commit()
    return onion


@pytest.mark.parametrize('who', ['user', 'admin'])
def test_a_reply_to_an_onion_reply_is_hidden_on_the_first_domain_everywhere(app, deep, monkeypatch, who):
    monkeypatch.setitem(app.config, 'ENABLE_ALPHA_API', 'true')
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', True)   # the signed-in reply form renders its token
    viewer = viewers(deep)[who]
    reply, nobody = deep.first_under.id, 2 ** 30
    for hidden, missing in ((f'/comment/{reply}', f'/comment/{nobody}'),
                            (f'/post/{deep.first_post.id}/comment/{reply}', f'/post/{deep.first_post.id}/comment/{nobody}')):
        found, absent = get(app, hidden, user=viewer), get(app, missing, user=viewer)
        assert (found.status_code, found.get_data()) == (absent.status_code, absent.get_data()), hidden
        assert found.status_code == 404, hidden
    for path in (f'/post/{deep.first_post.id}', '/u/bob', f'/user/{deep.first_bob.id}', '/search?q=FIRSTUNDERONION&search_for=comments'):
        body = get(app, path, user=viewer).get_data(as_text=True)   # the search page echoes its query
        assert 'FIRSTUNDERONION</p>' not in body and 'ONIONUNDER</p>' not in body, (path, near(body, 'FIRSTUNDERONION'))
        assert f'comment_{reply}' not in body and f'/comment/{reply}' not in body, path
    for path in (f'/api/alpha/comment/list?post_id={deep.first_post.id}', f'/api/alpha/user?person_id={deep.first_bob.id}',
                 '/api/alpha/search?q=FIRSTUNDERONION&type_=Comments'):
        body = api(app, path, viewer).get_data(as_text=True)
        assert 'FIRSTUNDERONION' not in body, (path, near(body, 'FIRSTUNDERONION'))
    found, absent = api(app, f'/api/alpha/comment?id={reply}', viewer), api(app, f'/api/alpha/comment?id={nobody}', viewer)
    assert (found.status_code, found.get_data()) == (absent.status_code, absent.get_data())


def test_on_the_onion_the_reply_to_an_onion_reply_is_shown(app, deep, monkeypatch):
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', True)   # the signed-in reply form renders its token
    body = get(app, f'/post/{deep.first_post.id}', host=ONION, user=deep.first_bob).get_data(as_text=True)
    assert 'FIRSTUNDERONION' in body and 'ONIONUNDER' in body
    assert get(app, f'/comment/{deep.first_under.id}', host=ONION, user=deep.first_bob).status_code in (200, 302)


# --- the link-domain and instance post lists (review C1) -------------------------------------------------------------

@pytest.fixture
def linked(onion):
    """FIRSTPOST and the onion's ONIONINFIRST link to example.com; the first's bob also posts FIRSTINONION in the onion's
    art, so it is on the first domain's instance row and hidden off the onion all the same."""
    domain = Domain(name='example.com')
    db.session.add(domain)
    db.session.flush()
    onion.first_in_onion = stamped(make_post(onion.onion_art, onion.first_bob, f'https://{FIRST}/post/in-onion',
                                             title='FIRSTINONION'), onion.first_bob, onion.onion_art)
    for post in (onion.in_first, onion.first_post, onion.first_in_onion):
        post.domain_id = domain.id
        post.url = 'https://example.com/a'
    db.session.commit()
    onion.domain = domain
    return onion


@pytest.mark.parametrize('who', ['user', 'admin'])
def test_the_link_domain_and_instance_post_lists_leave_out_onion_posts(app, linked, who):
    viewer = viewers(linked)[who]
    viewer.ignore_bots = 0   # the default: the signed-in branch of the domain page
    db.session.commit()
    for path in ('/d/example.com', f'/d/{linked.domain.id}', f'/instance/{FIRST}/posts'):
        body = get(app, path, user=viewer).get_data(as_text=True)
        assert 'ONIONINFIRST' not in body and 'FIRSTINONION' not in body, (path, near(body, 'ONIONINFIRST', 'FIRSTINONION'))
        assert 'FIRSTPOST' in body, path   # the control: the list does show posts


def test_on_the_onion_the_link_domain_lists_them(app, linked):
    body = get(app, '/d/example.com', host=ONION, user=linked.first_bob).get_data(as_text=True)
    assert 'ONIONINFIRST' in body and 'FIRSTINONION' in body and 'FIRSTPOST' in body


# --- an account's own activity lists (review I5) -----------------------------------------------------------------------

@pytest.fixture
def active(onion):
    """The first domain's bob, signed in on the onion, upvoted, bookmarked and read the onion's posts and its reply, and
    FIRSTPOST, the control."""
    bob = onion.first_bob
    for post in (onion.post, onion.in_first, onion.first_post):
        db.session.add(PostVote(user_id=bob.id, author_id=post.user_id, post_id=post.id, effect=1.0))
        db.session.add(PostBookmark(user_id=bob.id, post_id=post.id))
        db.session.execute(read_posts.insert().values(user_id=bob.id, read_post_id=post.id))
    onion.first_reply = make_post_reply(onion.first_post, bob, 'FIRSTREPLY')
    for reply in (onion.reply, onion.first_reply):
        db.session.add(PostReplyBookmark(user_id=bob.id, post_reply_id=reply.id))
    db.session.commit()
    return onion


def test_upvotes_bookmarks_and_read_posts_leave_out_onion_content_on_the_first_domain(app, active):
    pages = {'/u/bob/upvotes': active.admin, '/bookmarks': active.first_bob, '/read-posts': active.first_bob}
    for path, viewer in pages.items():
        body = get(app, path, user=viewer).get_data(as_text=True)
        assert 'ONIONPOST' not in body and 'ONIONINFIRST' not in body, (path, near(body, 'ONIONPOST', 'ONIONINFIRST'))
        assert 'FIRSTPOST' in body, path   # the control
    body = get(app, '/bookmarks/comments', user=active.first_bob).get_data(as_text=True)
    assert 'ONIONREPLY' not in body and 'FIRSTREPLY' in body


def test_on_the_onion_they_list_it(app, active):
    for path in ('/u/bob@test.piefed.local/upvotes', '/bookmarks', '/read-posts'):
        body = get(app, path, host=ONION, user=active.first_bob).get_data(as_text=True)
        assert 'ONIONPOST' in body, path
    assert 'ONIONREPLY' in get(app, '/bookmarks/comments', host=ONION, user=active.first_bob).get_data(as_text=True)


# --- the admin panel (review I4) ----------------------------------------------------------------------------------

@pytest.fixture
def panel(onion):
    for user in (onion.onion_bob, onion.first_bob):
        db.session.add(UserRegistration(user_id=user.id, answer=f'ANSWER-{user.id}', status=0))
    for post in (onion.post, onion.in_first, onion.first_post):
        post.deleted = True
    onion.reply.deleted = True
    for user in (onion.onion_bob, onion.first_bob):
        file = File(file_path=f'upload-{user.id}.png')
        db.session.add(file)
        db.session.flush()
        db.session.execute(user_file.insert().values(user_id=user.id, file_id=file.id))
    db.session.commit()
    grant_permission(onion.admin, 'administer all communities')
    grant_permission(onion.admin, 'approve registrations')
    grant_permission(onion.admin, 'change instance settings')
    return onion


def test_the_admin_panel_lists_no_onion_row_on_the_first_domain(app, panel):
    onion_bob, first_bob, onion_art, first_art = panel.onion_bob.id, panel.first_bob.id, panel.onion_art.id, \
        panel.first_art.id
    marks = (ONION, 'Onion art', 'ONIONPOST', 'ONIONINFIRST', f'ANSWER-{onion_bob}', f'/admin/user/{onion_bob}/edit"',
             f'/admin/community/{onion_art}/edit"', f'?user_id={onion_bob}"')
    for path, control in (('/admin/users', f'/admin/user/{first_bob}/edit"'),
                          ('/admin/users?search=bob', f'/admin/user/{first_bob}/edit"'),
                          ('/admin/communities', f'/admin/community/{first_art}/edit"'),
                          ('/admin/communities/no-topic', None),
                          ('/admin/content?show=deleted&days=0', 'FIRSTPOST'),
                          ('/admin/approve_registrations', f'ANSWER-{first_bob}'),
                          ('/admin/media', f'?user_id={first_bob}"'), ('/admin/instances', FIRST)):
        response = get(app, path, user=panel.admin)
        assert response.status_code == 200, path
        body = response.get_data(as_text=True)
        assert not near(body, *marks), (path, near(body, *marks))
        assert control is None or control in body, path


def test_the_admin_edit_pages_answer_an_onion_row_as_one_nobody_holds(app, panel):
    nobody = 2 ** 30
    for hidden, missing in ((f'/admin/user/{panel.onion_bob.id}/edit', f'/admin/user/{nobody}/edit'),
                            (f'/admin/community/{panel.onion_art.id}/edit', f'/admin/community/{nobody}/edit'),
                            (f'/admin/masquerade/{panel.onion_bob.id}', f'/admin/masquerade/{nobody}')):
        found, absent = get(app, hidden, user=panel.admin), get(app, missing, user=panel.admin)
        assert (found.status_code, found.get_data()) == (absent.status_code, absent.get_data()), hidden
        assert found.status_code == 404, hidden


def test_on_the_onion_the_admin_panel_lists_and_edits_them(app, panel, monkeypatch):
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', True)   # the edit form renders its token
    assert ONION_BOB.split('@')[0] in get(app, '/admin/users?search=bob', host=ONION, user=panel.admin).get_data(as_text=True)
    assert 'Onion art' in get(app, '/admin/communities', host=ONION, user=panel.admin).get_data(as_text=True)
    assert f'ANSWER-{panel.onion_bob.id}' in get(app, '/admin/approve_registrations', host=ONION,
                                                  user=panel.admin).get_data(as_text=True)
    assert get(app, f'/admin/user/{panel.onion_bob.id}/edit', host=ONION, user=panel.admin).status_code == 200
    assert ONION in get(app, '/admin/instances', host=ONION, user=panel.admin).get_data(as_text=True)


# --- notifications of a hidden post or reply (review I3, M3) ---------------------------------------------------------

def notice(user, notif_type, url, subtype=None, **targets):
    notification = Notification(user_id=user.id, title='ONIONTITLE', url=url, notif_type=notif_type, subtype=subtype,
                                author_id=user.id, targets={'gen': '0', **targets})
    db.session.add(notification)
    db.session.commit()
    return notification


@pytest.fixture
def notified(onion):
    """One notification of each kind that names a post or reply, about the onion's ONIONPOST and its reply, for the first
    domain's bob, who took part on the onion; and one about FIRSTPOST, the control."""
    bob, post, reply = onion.first_bob, onion.post, onion.reply
    about = dict(post_id=post.id, post_title='ONIONTITLE', comment_id=reply.id, comment_body='ONIONBODY',
                 parent_reply_body='ONIONBODY', author_user_name=ONION_BOB, author_id=onion.onion_bob.id,
                 community_name=ONION_ART, community_id=onion.onion_art.id)
    onion.notices = [notice(bob, kind, f'/post/{post.id}', **about) for kind in (0, 1, 2, 5, 10, 11, 41, 999)]
    onion.notices += [notice(bob, kind, f'/post/{post.id}/comment/{reply.id}#comment_{reply.id}', **about)
                      for kind in (3, 4, 12)]
    onion.notices.append(notice(bob, 6, f'/comment/{reply.id}', subtype='comment_mention', **about))
    onion.notices.append(notice(bob, 20, '/admin/reports', subtype='post_reported', suspect_post_id=post.id,
                                orig_post_title='ONIONTITLE', orig_post_body='ONIONBODY',
                                suspect_user_user_name=ONION_BOB, suspect_user_id=onion.onion_bob.id))
    onion.notices.append(notice(bob, 20, '/admin/reports', subtype='comment_reported', suspect_comment_id=reply.id,
                                orig_comment_body='ONIONBODY', suspect_user_user_name=ONION_BOB,
                                suspect_user_id=onion.onion_bob.id))
    onion.control = notice(bob, 0, f'/post/{onion.first_post.id}', post_id=onion.first_post.id, post_title='FIRSTPOST',
                           author_user_name='bob', author_id=bob.id, community_name='art',
                           community_id=onion.first_art.id)
    onion.control.title = 'FIRSTPOST'
    db.session.commit()
    return onion


def test_a_notification_of_a_hidden_post_or_reply_shows_only_the_note_on_the_first_domain(app, notified):
    body = get(app, '/notifications', user=notified.first_bob).get_data(as_text=True)
    hidden = ('ONIONTITLE', 'ONIONBODY', f'/post/{notified.post.id}', f'/comment/{notified.reply.id}', ONION)
    assert not near(body, *hidden), near(body, *hidden)
    assert body.count(NOTE) == len(notified.notices)
    assert 'FIRSTPOST' in body   # the control
    for notification in notified.notices:   # each one's own row, when it is marked read or unread
        for action in ('read', 'unread'):
            row = Visitor(app, notified.first_bob, FIRST).post(f'/notification/{notification.id}/{action}')
            text = row.get_data(as_text=True)
            assert NOTE in text and not near(text, *hidden), (notification.notif_type, near(text, *hidden))


def test_a_report_notification_about_an_onion_account_names_it_as_text_with_the_note(app, onion):
    about = notice(onion.first_bob, 20, '/admin/reports', subtype='user_reported', suspect_user_user_name=ONION_BOB,
                   suspect_user_id=onion.onion_bob.id)
    control = notice(onion.first_bob, 20, '/admin/reports', subtype='user_reported', suspect_user_user_name='bob',
                     suspect_user_id=onion.first_bob.id)
    body = get(app, '/notifications', user=onion.first_bob).get_data(as_text=True)
    assert body.count(NOTE) == 1 and ONION_BOB in body
    assert f'/notification/{about.id}/goto' not in body and f'/notification/{control.id}/goto' in body


def test_following_a_notification_of_a_hidden_post_answers_404_and_spells_no_onion_name(app, notified):
    viewer = Visitor(app, notified.first_bob, FIRST)
    response = viewer.get(f'/notification/{notified.notices[0].id}/goto')
    assert response.status_code == 404 and 'Location' not in response.headers
    response = viewer.get(f'/notification/{notified.control.id}/goto')
    assert response.status_code == 302 and response.headers['Location'].endswith(f'/post/{notified.first_post.id}')


def test_a_notification_naming_an_onion_account_by_its_handle_is_followed_nowhere(app, onion):
    notification = notice(onion.first_bob, 13, f'/u/{ONION_BOB}', author_user_name=ONION_BOB,
                          author_id=onion.onion_bob.id)
    response = Visitor(app, onion.first_bob, FIRST).get(f'/notification/{notification.id}/goto')
    assert response.status_code == 404 and 'Location' not in response.headers


def test_a_notification_whose_link_is_no_page_or_none_is_shown_as_it_was(app, onion):
    for url in ('/nowhere/at/all', None, f'/post/{2 ** 30}'):
        notice(onion.first_bob, 999, url)
    body = get(app, '/notifications', user=onion.first_bob).get_data(as_text=True)
    assert NOTE not in body and body.count('ONIONTITLE') == 3   # the title these test notifications all carry


def test_on_the_onion_the_notifications_show_them(app, notified):
    body = get(app, '/notifications', host=ONION, user=notified.first_bob).get_data(as_text=True)
    assert 'ONIONTITLE' in body and NOTE not in body
    response = Visitor(app, notified.first_bob, ONION).get(f'/notification/{notified.notices[0].id}/goto')
    assert response.status_code == 302


def test_the_apis_notifications_leave_them_out_on_the_first_domain(app, notified, monkeypatch):
    monkeypatch.setitem(app.config, 'ENABLE_ALPHA_API', 'true')
    for notification in [*notified.notices, notified.control]:
        notification.subtype = notification.subtype or 'new_post'
    db.session.commit()
    body = api(app, '/api/alpha/user/notifications?status=All&limit=50', notified.first_bob).get_data(as_text=True)
    assert 'ONIONTITLE' not in body and 'ONIONPOST' not in body and 'FIRSTPOST' in body
    on_onion = api(app, '/api/alpha/user/notifications?status=All&limit=50', notified.first_bob, host=ONION)
    assert 'ONIONPOST' in on_onion.get_data(as_text=True)

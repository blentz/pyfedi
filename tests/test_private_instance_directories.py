"""D1413: the sweep was not exhausted -- a whole blueprint had been off the listing.

D1410, D1411 and D1412 read "the GET routes carrying neither privacy decorator nor
`login_required`" and fixed sixteen. D1412's ledger entry then said the sweep was
exhausted. It was not: the listing that drove those rounds was printed through `| head -45`
and `| tail -45`, and the first pass also filtered out every route declaring
`methods=['GET', 'POST']`. Fact 820 -- never `| head` a sweep -- broken by the sweep that
was checking for missing guards.

Re-run with both corrections, this is what was behind the cut. Measured, anonymous, with
`private_instance` on, one post titled SECRET TITLE and one wiki page:

    /instances                      200  the domains this instance federates with
    /instance/peer.example          200  that peer's overview
    /instance/peer.example/people   200  its users, by name
    /instance/peer.example/posts    200  THE POST TITLE, its author, its community
    /community/general/wiki/rules   200  THE WIKI PAGE BODY
    /domains                        200  the domains posts here link to
    /community/general/sidebar      404  (needs a fragment request)
    /sitemap.xml                    200  461 bytes for this fixture

`/instance/<domain>/posts` is the same leak D1410 closed for RSS, through a door nobody
had looked at: a listing of this instance's posts, filtered by which remote instance they
came from. `/community/<name>/wiki/<slug>` serves a community's wiki page -- prose written
by members, for members -- and carries `methods=['GET', 'POST']`, which is exactly why the
first pass never printed it.

All seven get `login_required_if_private_instance`, the decorator D1411 and D1412 used:
they are pages a person opens in a browser, and a member may open them.

`/sitemap.xml` was left alone here deliberately; R222 later settled it the way `index_rss`
settles feeds -- a private instance publishes no sitemap (owner ruling 2026-09-30).
"""
import pytest

from app import db
from app.models import CommunityWikiPage, Site
from tests.factories import make_community, make_instance, make_post, make_site, make_user

HOST = 'test.piefed.local'
PEER = 'peer.example'
SECRET = 'SECRET TITLE'
WIKI = 'SECRET WIKI BODY'


@pytest.fixture
def seeded(app, db_session):
    from types import SimpleNamespace

    make_site()
    site = db.session.get(Site, 1)
    make_instance(HOST, software='piefed')
    peer = make_instance(PEER, software='lemmy')
    author = make_user(peer, 'author')
    author.ap_profile_id = f'https://{PEER}/u/author'
    author.ap_domain = PEER
    community = make_community('general')
    community.private = False
    post = make_post(community, author, f'https://{PEER}/p/1', title=SECRET)
    page = CommunityWikiPage(community_id=community.id, slug='rules', title='Rules',
                             body=WIKI, body_html=f'<p>{WIKI}</p>')
    db.session.add(page)
    db.session.commit()
    return SimpleNamespace(site=site, community=community, author=author, post=post,
                           peer=peer)


URLS = {
    'the instance list': '/instances',
    "a peer's overview": f'/instance/{PEER}',
    "a peer's people": f'/instance/{PEER}/people',
    'the interesting people list': '/instance/people/interesting',
    "a peer's posts": f'/instance/{PEER}/posts',
    "a community's wiki page": '/community/general/wiki/rules',
    'the domain list': '/domains',
}
NAMES = list(URLS)


class TestAPrivateInstance:
    @pytest.mark.parametrize('name', NAMES)
    def test_an_anonymous_visitor_is_sent_to_the_login_page(self, app, seeded, name):
        seeded.site.private_instance = True
        db.session.commit()

        response = app.test_client().get(URLS[name])

        assert response.status_code == 302
        assert '/auth/login' in response.headers['Location']

    def test_the_peer_post_list_no_longer_serves_a_post(self, app, seeded):
        """The worst of the four rounds' findings, and the one that says the sweep had to
        be re-run: a listing of this instance's posts, by originating instance, with no
        gate at all."""
        seeded.site.private_instance = True
        db.session.commit()

        body = app.test_client().get(f'/instance/{PEER}/posts').get_data(as_text=True)

        assert SECRET not in body

    def test_the_wiki_page_no_longer_serves_its_body(self, app, seeded):
        """Prose written by members for members. This route declares
        `methods=['GET', 'POST']`, which is why the first pass of the sweep filtered it
        out before printing anything."""
        seeded.site.private_instance = True
        db.session.commit()

        body = app.test_client().get(
            '/community/general/wiki/rules').get_data(as_text=True)

        assert WIKI not in body

    def test_a_logged_in_member_still_gets_all_seven(self, app, seeded):
        seeded.site.private_instance = True
        db.session.commit()
        client = app.test_client()
        with client.session_transaction() as session:
            session['_user_id'] = str(seeded.author.id)
            session['_fresh'] = True

        for name in NAMES:
            assert client.get(URLS[name]).status_code == 200, name


class TestAPublicInstance:
    @pytest.mark.parametrize('name', NAMES)
    def test_every_one_of_them_still_answers(self, app, seeded, name):
        seeded.site.private_instance = False
        db.session.commit()

        assert app.test_client().get(URLS[name]).status_code == 200

    def test_the_post_list_and_the_wiki_still_serve_their_content(self, app, seeded):
        """The control that makes the two rows above mean something."""
        seeded.site.private_instance = False
        db.session.commit()
        client = app.test_client()

        assert SECRET in client.get(f'/instance/{PEER}/posts').get_data(as_text=True)
        assert WIKI in client.get(
            '/community/general/wiki/rules').get_data(as_text=True)


def test_a_private_instance_publishes_no_sitemap(app, seeded):
    """R222, fixed. The sitemap was left public on a private instance and listed its post
    slugs. It now 404s, the answer `index_rss` gives for feeds (owner ruling 2026-09-30).

    Fetched once while public first: the route is `@cache.cached`, and the refusal must
    run before the cache replays the public answer.
    """
    from app import cache

    cache.clear()
    seeded.site.private_instance = False
    db.session.commit()
    client = app.test_client()
    assert client.get('/sitemap.xml').status_code == 200

    seeded.site.private_instance = True
    db.session.commit()

    assert client.get('/sitemap.xml').status_code == 404


def test_the_seven_routes_carry_the_decorator():
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    expected = {('app/instance/routes.py', 'list_instances'),
                ('app/instance/routes.py', 'instance_overview'),
                ('app/instance/routes.py', 'instance_people'),
                ('app/instance/routes.py', 'instance_people_top'),
                ('app/instance/routes.py', 'instance_posts'),
                ('app/community/routes.py', 'community_wiki_view'),
                ('app/domain/routes.py', 'domains'),
                # Found by the pinned sweep below on its first run, not by the hand-run
                # listing: it serves a post's whole comment thread past a hundred
                # comments. D1106 already gave it `refuse_private_community` and
                # `refuse_unpublished_post` -- per-COMMUNITY checks, which say nothing
                # about whether the instance is private.
                ('app/post/routes.py', 'post_lazy_replies')}

    for path, name in expected:
        tree = ast.parse((root / path).read_text())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == name)
        decorators = {d.id for d in fn.decorator_list if isinstance(d, ast.Name)}

        assert 'login_required_if_private_instance' in decorators, f'{path}:{name}'


def test_no_get_route_outside_the_known_list_is_ungated():
    """THE SWEEP ITSELF, pinned -- because running it by hand is what let a blueprint hide
    behind `| head`.

    Every GET-capable route in `app/*/routes.py` that renders a template or returns JSON
    must carry a gate, or be named here with a reason. Routes declaring POST as well are
    INCLUDED, which the hand-run version filtered out and which is how the wiki page was
    missed.
    """
    import ast
    import pathlib

    GATES = {'login_required', 'permission_required', 'admin_required', 'staff_required',
             'login_required_if_private_instance', 'refuse_if_private_instance',
             'jwt_required'}
    # Public by design. Each entry says why, so the exemption is an argument.
    PUBLIC = {
        # served to anyone, by definition: the instance's own public documents
        ('main', 'about_page'), ('main', 'privacy'), ('main', 'robots'),
        ('main', 'security'), ('main', 'rsl'),
        ('main', 'keyboard_shortcuts'), ('main', 'content_warning'),
        ('main', 'anoobis'), ('main', 'bot_challenge_result'),
        # operational, no instance content
        ('main', 'health'), ('main', 'health2'), ('main', 'service_worker'),
        ('main', 'static_manifest'), ('main', 'instance_actor'), ('main', 'honey_pot'),
        ('main', 'share'), ('main', 'test'), ('main', 'test_email'),
        ('main', 'test_redis'), ('main', 'test_ip'), ('main', 'test_s3'),
        ('main', 'test_hashing'), ('main', 'test_ldap'), ('main', 'test_ldap_login'),
        ('main', 'test_libretranslate'),
        # menu fragments: they render the viewer's OWN menu, and are behind the pages
        # that use them
        ('main', 'communities_menu'), ('main', 'explore_menu'), ('main', 'topics_menu'),
        ('main', 'feeds_menu'),
        # unsubscribe links arrive by email and carry their own token
        ('user', 'user_newsletter_unsubscribe'),
        ('user', 'user_email_notifs_unsubscribe'),
        # redirects and lookups that resolve an actor to a url, no content rendered
        ('user', 'fediverse_redirect'), ('user', 'lookup'), ('community', 'lookup'),
        ('feed', 'lookup'), ('community', 'community_changed'),
        ('community', 'get_sidebar'),
        # ActivityPub and auth blueprints answer machines and anonymous visitors by design
    }
    SKIP_BLUEPRINTS = {'activitypub', 'auth', 'api', 'admin'}

    root = pathlib.Path(__file__).resolve().parent.parent
    ungated = []
    for path in sorted((root / 'app').glob('*/routes.py')):
        blueprint = path.parent.name
        if blueprint in SKIP_BLUEPRINTS:
            continue
        src = path.read_text()
        tree = ast.parse(src)
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            gets, names = False, []
            for d in fn.decorator_list:
                name = d.id if isinstance(d, ast.Name) else None
                if isinstance(d, ast.Call):
                    f = d.func
                    name = f.attr if isinstance(f, ast.Attribute) else getattr(f, 'id', None)
                    if name == 'route':
                        methods = ['GET']
                        for k in d.keywords:
                            if k.arg == 'methods' and isinstance(k.value, (ast.List, ast.Tuple)):
                                methods = [e.value for e in k.value.elts
                                           if isinstance(e, ast.Constant)]
                        if 'GET' in methods:
                            gets = True
                if name:
                    names.append(name)
            if not gets or (set(names) & GATES):
                continue
            body = ast.get_source_segment(src, fn) or ''
            if 'render_template' not in body and 'jsonify' not in body:
                continue
            if (blueprint, fn.name) in PUBLIC:
                continue
            ungated.append(f'{blueprint}/{fn.name}')

    assert ungated == [], (
        'GET routes that render content with no gate and no stated reason: '
        + ', '.join(ungated))

"""D1410: a private instance's posts, readable by anyone who asked for RSS.

`Site.private_instance` means "only members may read this instance". `index_rss`
(`app/main/routes.py:1348`) has said so since D1356, and says it first:

    if g.site.private_instance:
        abort(404)

Five sibling feeds did not. Measured, anonymous, with `private_instance` on and one post
titled SECRET TITLE:

    /community/general/feed  200  post title in body
    /u/author/feed           200  post title in body
    /tag/thetag/feed         200  post title in body
    /d/example.com/feed      200  post title in body
    /topic/thetopic.rss      200  post title in body
    /index/feed              404

So the instance-wide privacy setting was enforced on one of six RSS endpoints, and the
other five published titles, bodies and author names of every non-private post to anyone
who knew a community, user, tag, domain or topic name. The same shape as D1394, where a
private feed's membership was readable through thirteen readers of its id: one gate, many
doors, and the gate on only one of them.

THE GUARD IS UNCONDITIONAL, copied from `index_rss` rather than invented. An RSS reader
cannot log in -- it presents no session and follows no redirect to a login form -- so a
private instance has NO rss rather than rss-for-members. That is the decision `index_rss`
already encodes, and five endpoints disagreeing with it was the defect; making them agree
is the fix, and changing what that decision IS would be a separate argument.

IT IS ALSO FIRST IN EACH FUNCTION, ahead of the actor lookup, the rate limiter and the
`@cache.cached` body, because a check that runs after a lookup still tells the caller
whether the object exists, and one that runs after a cached response never runs at all.

`show_feed_rss` gets it too, though no probe row above names it: it is the sixth RSS
endpoint, D1394 already gave it `feed_readable_by` for per-feed visibility, and
instance-wide privacy is a different question from feed visibility.
"""
import pytest
from flask import g

from app import db
from app.models import Domain, Post, Site, Tag, Topic, User
from tests.factories import (make_community, make_feed, make_feed_item, make_instance,
                             make_post, make_site, make_user)

HOST = 'test.piefed.local'
SECRET = 'SECRET TITLE'


@pytest.fixture
def seeded(app, db_session):
    """One post, reachable by every RSS endpoint under test: it lives in a community, has
    an author, carries a tag, has a domain, and its community sits under a topic."""
    from types import SimpleNamespace

    make_site()
    site = db.session.get(Site, 1)
    instance = make_instance(HOST, software='piefed')
    author = make_user(instance, 'author', local=True)
    author.ap_profile_id = f'https://{HOST}/u/author'
    author.ap_public_url = f'https://{HOST}/u/author'
    author.ap_domain = HOST
    community = make_community('general')
    community.private = False
    post = make_post(community, author, f'https://{HOST}/p/1', title=SECRET)
    tag = Tag(name='thetag', display_as='thetag')
    topic = Topic(name='Topic', machine_name='thetopic')
    domain = Domain(name='example.com', banned=False)
    db.session.add_all([tag, topic, domain])
    db.session.commit()
    db.session.execute(db.text('INSERT INTO post_tag (post_id, tag_id) VALUES (:p, :t)'),
                       {'p': post.id, 't': tag.id})
    community.topic_id = topic.id
    post.domain_id = domain.id
    # The feed needs a MEMBER COMMUNITY, not just a row: `show_feed_rss` renders the
    # posts of the communities in the feed, and a feed with none serves 404 whatever the
    # privacy setting -- which is how the first version of this file left the feed guard
    # unobservable, and a mutation of it survived while every row passed.
    feed = make_feed(instance, name='thefeed', public=True, local=True)
    # `show_feed_rss` looks the feed up by MACHINE_NAME, which the factory does not set --
    # it sets `name` and `title` only. Without this the url resolves to no feed and the
    # route 404s whatever the privacy setting.
    feed.machine_name = 'thefeed'
    make_feed_item(feed, community)
    db.session.commit()
    return SimpleNamespace(site=site, community=community, author=author, post=post,
                           feed=feed)


# Every RSS endpoint, by the url an anonymous reader would use. `/index/feed` is the one
# that already refused, and it is in the table as the twin the other six now match.
FEEDS = {
    'a community': '/community/general/feed',
    'a user': '/u/author/feed',
    'a tag': '/tag/thetag/feed',
    'a domain': '/d/example.com/feed',
    'a topic': '/topic/thetopic.rss',
    'a feed': '/f/thefeed.rss',
    'the front page': '/index/feed',
}


class TestAPrivateInstance:
    @pytest.mark.parametrize('name', list(FEEDS))
    def test_no_rss_endpoint_serves_anything(self, app, seeded, name):
        seeded.site.private_instance = True
        db.session.commit()

        response = app.test_client().get(FEEDS[name])

        assert response.status_code == 404
        assert SECRET not in response.get_data(as_text=True)

    @pytest.mark.parametrize('name', list(FEEDS))
    def test_not_even_for_a_logged_in_member(self, app, seeded, name):
        """A session is not what opens a private instance's feeds: an RSS reader cannot
        present one. Since R219 a member's RSS token in the url does (TestAMembersRssToken)."""
        seeded.site.private_instance = True
        db.session.commit()
        client = app.test_client()
        with client.session_transaction() as session:
            session['_user_id'] = str(seeded.author.id)
            session['_fresh'] = True

        response = client.get(FEEDS[name])

        assert response.status_code == 404


class TestAMembersRssToken:
    """R219, fixed (owner ruling): an RSS reader presents no session, so on a
    private instance a member's own RSS token in the url is what opens a feed.
    Anonymous readers -- and a session alone, above -- still get 404."""

    @pytest.mark.parametrize('name', list(FEEDS))
    def test_a_valid_token_opens_every_feed(self, app, seeded, name):
        seeded.site.private_instance = True
        seeded.author.rss_token = 'a-members-rss-token'
        db.session.commit()

        response = app.test_client().get(FEEDS[name] + '?token=a-members-rss-token')

        assert response.status_code == 200

    @pytest.mark.parametrize('banned, deleted, token', [
        (False, False, 'not-anyones-token'),
        (True, False, 'a-members-rss-token'),
        (False, True, 'a-members-rss-token'),
    ])
    def test_a_wrong_token_or_one_from_a_banned_or_deleted_account_does_not(
            self, app, seeded, banned, deleted, token):
        seeded.site.private_instance = True
        seeded.author.rss_token = 'a-members-rss-token'
        seeded.author.banned = banned
        seeded.author.deleted = deleted
        db.session.commit()

        response = app.test_client().get('/community/general/feed?token=' + token)

        assert response.status_code == 404


PAGES = {
    'a community': ('/c/general', '/community/general/feed'),
    'a user': ('/u/author', '/u/author/feed'),
    'a tag': ('/tag/thetag', '/tag/thetag/feed'),
    'a domain': ('/d/example.com', '/d/{domain_id}/feed'),
    'a topic': ('/topic/thetopic', '/topic/thetopic.rss'),
}


class TestThePagesRssLinks:
    """R219 residue, fixed (owner ruling): on a private instance the RSS link a
    page renders for a logged-in member carries their RSS token, so the link
    works in a reader without hand-editing. A public instance's links stay bare."""

    def _page(self, app, seeded, name, private):
        seeded.site.private_instance = private
        seeded.author.rss_token = 'a-members-rss-token'
        seeded.author.post_count = 1     # the user and domain pages link a feed only when there are posts
        Domain.query.filter_by(name='example.com').one().post_count = 1
        db.session.commit()
        client = app.test_client()
        with client.session_transaction() as session:
            session['_user_id'] = str(seeded.author.id)
            session['_fresh'] = True
        response = client.get(PAGES[name][0])
        assert response.status_code == 200
        return response.get_data(as_text=True)

    @pytest.mark.parametrize('name', list(PAGES))
    def test_a_private_instances_rss_link_carries_the_members_token(self, app, seeded, name):
        html = self._page(app, seeded, name, private=True)

        domain_id = Domain.query.filter_by(name='example.com').one().id
        assert PAGES[name][1].format(domain_id=domain_id) + '?token=a-members-rss-token"' in html

    def test_a_member_with_no_token_yet_gets_one_on_the_page(self, app, seeded):
        """R219 residue. The token was only created on the front page, so a
        member who had not visited it got a bare link that 404s. It is now
        created wherever the link is rendered."""
        seeded.site.private_instance = True
        seeded.author.rss_token = None
        db.session.commit()
        client = app.test_client()
        with client.session_transaction() as session:
            session['_user_id'] = str(seeded.author.id)
            session['_fresh'] = True
        html = client.get('/c/general').get_data(as_text=True)

        token = db.session.get(User, seeded.author.id).rss_token
        assert token
        assert f'/community/general/feed?token={token}"' in html

    @pytest.mark.parametrize('name', list(PAGES))
    def test_a_public_instances_rss_link_does_not(self, app, seeded, name):
        html = self._page(app, seeded, name, private=False)

        assert 'a-members-rss-token' not in html


class TestAPublicInstance:
    @pytest.mark.parametrize('name', ['a community', 'a user', 'a tag', 'a domain',
                                      'a topic', 'a feed'])
    def test_every_feed_still_publishes(self, app, seeded, name):
        """The control, and the row that makes the table above mean something: with
        privacy off, each of these serves the post. Without it, a fix that 404'd
        unconditionally would pass every row above and break RSS for every public
        instance."""
        seeded.site.private_instance = False
        db.session.commit()

        response = app.test_client().get(FEEDS[name])

        assert response.status_code == 200
        assert SECRET in response.get_data(as_text=True)

    def test_the_front_page_feed_still_publishes(self, app, seeded):
        """`/index/feed` is the endpoint that already had the check, so this row says the
        round did not disturb it."""
        seeded.site.private_instance = False
        db.session.commit()

        response = app.test_client().get('/index/feed')

        assert response.status_code == 200


class TestTheCachedRoutes:
    """`show_community_rss`, `show_profile_rss` and `show_topic_rss` carry
    `@cache.cached`, and the first version of this fix put the privacy check INSIDE the
    function -- below the cache. The commit review named the hole: a body cached while the
    instance was public is replayed for up to 600 seconds after an admin makes it private,
    and an inline check cannot run at all, because `cache.cached` returns its stored
    response without calling the function.

    The rule is now `@refuse_if_private_instance`, listed directly under `@bp.route` and
    therefore the OUTERMOST wrapper, so it runs before the cache is consulted.

    TWO CACHES, AND ONLY ONE OF THEM IS THIS ROUND'S. Measuring the first needed a real
    cache backend, and with one the rows still served 200 after the toggle -- because
    `g.site` itself comes from `get_site_as_dict`, which is `@cache.memoize(timeout=60)`
    (app/utils.py:5995). So a privacy change takes up to a minute to be seen by ANY gate in
    this application, including `login_required_if_private_instance` and `index_rss`'s own
    check, and that window predates this round and belongs to a global caching decision
    rather than to these six views. The rows below drop that memo explicitly when they
    toggle, which is what isolates the response cache -- the thing the decorator placement
    actually fixes.
    """

    CACHED = {'a community': '/community/general/feed',
              'a user': '/u/author/feed',
              'a topic': '/topic/thetopic.rss'}

    @pytest.fixture(autouse=True)
    def a_real_cache(self, app):
        """The suite runs with `CACHE_TYPE = 'NullCache'` (tests/conftest.py:215), under
        which `@cache.cached` stores nothing and every row in this class would pass whether
        the guard were above the cache or below it -- the vacuous shape this campaign keeps
        meeting. These rows are ABOUT the cache, so they need one.

        Restored afterwards, because the cache object is shared by the app for the whole
        worker process and a SimpleCache left behind would let other modules' requests
        answer each other."""
        from app import cache

        cache.init_app(app, config={'CACHE_TYPE': 'SimpleCache',
                                    'CACHE_DEFAULT_TIMEOUT': 600})
        cache.clear()
        yield
        cache.clear()
        cache.init_app(app, config={'CACHE_TYPE': 'NullCache'})

    @staticmethod
    def _set_privacy(site, private):
        """Toggle the flag AND drop the memoized Site, so the next request sees it."""
        from app import cache
        from app.utils import get_site_as_dict

        site.private_instance = private
        db.session.commit()
        cache.delete_memoized(get_site_as_dict)

    @pytest.mark.parametrize('name', list(CACHED))
    def test_a_body_cached_while_public_is_not_replayed_once_private(self, app, seeded,
                                                                    name):
        """The review's scenario, in order: warm the cache on a public instance, make it
        private, ask again. With the check inside the function this returned the cached
        200; with the decorator above `cache.cached` the request never reaches the store."""
        url = self.CACHED[name]
        self._set_privacy(seeded.site, False)
        client = app.test_client()
        warmed = client.get(url)
        assert warmed.status_code == 200 and SECRET in warmed.get_data(as_text=True), \
            'the cache was not warmed, so this row would pass for the wrong reason'

        self._set_privacy(seeded.site, True)
        response = client.get(url)

        assert response.status_code == 404
        assert SECRET not in response.get_data(as_text=True)

    @pytest.mark.parametrize('name', list(CACHED))
    def test_nothing_is_cached_while_private_so_going_public_serves_at_once(self, app,
                                                                           seeded, name):
        """The same arithmetic backwards, and the half a fix could get wrong in the other
        direction: because the refusal happens above the cache, no 404 is ever stored, so an
        instance made public again starts serving immediately rather than answering from a
        cached refusal."""
        url = self.CACHED[name]
        self._set_privacy(seeded.site, True)
        client = app.test_client()
        assert client.get(url).status_code == 404

        self._set_privacy(seeded.site, False)
        response = client.get(url)

        assert response.status_code == 200
        assert SECRET in response.get_data(as_text=True)

    def test_the_site_row_itself_is_memoized_for_a_minute(self, app, seeded):
        """The window this round does NOT close, pinned so nobody mistakes it for one that
        is. `get_site_as_dict` is memoized for 60 seconds, so without dropping that memo a
        freshly privatised instance keeps serving -- through this guard and through every
        other one in the app. Fixing it means changing a global caching decision, which is
        a separate argument with its own evidence."""
        self._set_privacy(seeded.site, False)
        client = app.test_client()
        assert client.get('/community/general/feed').status_code == 200

        seeded.site.private_instance = True   # deliberately WITHOUT dropping the memo
        db.session.commit()

        assert client.get('/tag/thetag/feed').status_code == 200


def test_the_guard_runs_before_any_lookup_or_rate_limit():
    """Position, not just presence.

    A check after the actor lookup still answers "does this community exist"; a check
    after `@cache.cached` never runs for a url someone already fetched; and
    `show_domain_rss` opens `with limiter.limit('60/minute'):`, so a check inside that
    block spends the caller's rate-limit budget to tell them nothing. Each guard is the
    first statement of its function, which this row reads out of the source.
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    decorated = [('app/community/routes.py', 'show_community_rss'),
                 ('app/user/routes.py', 'show_profile_rss'),
                 ('app/tag/routes.py', 'show_tag_rss'),
                 ('app/topic/routes.py', 'show_topic_rss'),
                 ('app/domain/routes.py', 'show_domain_rss'),
                 ('app/feed/routes.py', 'show_feed_rss')]

    for path, name in decorated:
        tree = ast.parse((root / path).read_text())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == name)
        names = []
        for d in fn.decorator_list:
            if isinstance(d, ast.Name):
                names.append(d.id)
            elif isinstance(d, ast.Call):
                f = d.func
                names.append(f.attr if isinstance(f, ast.Attribute) else getattr(f, 'id', ''))

        assert 'refuse_if_private_instance' in names, f'{path}:{name} is not guarded'
        # Outermost after the route, so it runs before `cache.cached` reads its store and
        # before `limiter.limit` spends the caller's budget.
        assert names.index('refuse_if_private_instance') == 1, \
            f'{path}:{name} guards too late: {names}'
        if 'cached' in names:
            assert names.index('refuse_if_private_instance') < names.index('cached'), \
                f'{path}:{name} is cached above its guard: {names}'


def test_index_rss_still_checks_inline_and_says_why():
    """`index_rss` keeps its inline check rather than the decorator, and that is not an
    oversight: its own `@cache.cached` line is COMMENTED OUT (app/main/routes.py:1339), so
    nothing stands between the request and the function, and its comment argues the
    ordering against its own 304 handling instead. This row fails if someone re-enables
    that cache without moving the check."""
    import pathlib

    source = (pathlib.Path(__file__).resolve().parent.parent
              / 'app' / 'main' / 'routes.py').read_text()
    head = source[source.index("def index_rss("):]
    head = head[:head.index('current_etag')]

    assert 'if g.site.private_instance and user is None:' in head
    assert "#@cache.cached(timeout=600, query_string=True)" in source

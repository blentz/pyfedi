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
from app.models import Domain, Post, Site, Tag, Topic
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
        """The guard is unconditional, as `index_rss`'s always was. A row asserting only
        the anonymous case would pass for a fix that let members through, which is a
        different product decision and would need its own argument -- an RSS reader cannot
        present a session anyway."""
        seeded.site.private_instance = True
        db.session.commit()
        client = app.test_client()
        with client.session_transaction() as session:
            session['_user_id'] = str(seeded.author.id)
            session['_fresh'] = True

        response = client.get(FEEDS[name])

        assert response.status_code == 404


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
    sites = [('app/community/routes.py', 'show_community_rss'),
             ('app/user/routes.py', 'show_profile_rss'),
             ('app/tag/routes.py', 'show_tag_rss'),
             ('app/topic/routes.py', 'show_topic_rss'),
             ('app/domain/routes.py', 'show_domain_rss'),
             ('app/feed/routes.py', 'show_feed_rss'),
             ('app/main/routes.py', 'index_rss')]

    for path, name in sites:
        tree = ast.parse((root / path).read_text())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == name)
        first = fn.body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            first = fn.body[1]  # past the docstring
        source = ast.dump(first)

        assert 'private_instance' in source, f'{path}:{name} does not check it first'

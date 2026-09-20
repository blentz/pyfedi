"""app/tag/routes.py -- the hashtag page at /tag/<tag> and its RSS feed.

MEASUREMENT BASIS. The module stood at 11.517% on the full-suite --cov=app run
at cadf06a86, carrying 209 missing statements and 106 missing arcs. This round
takes the two reading surfaces -- `show_tag` and `show_tag_rss`, 90 of those
statements; sub-projects 67 and 68 take the rest and close the package.

Three defects are pinned here and repaired together:

  P1  both pagination links were built with `tag=tag`, and `tag` had been
      rebound to the Tag ROW at :32, so the URL builder wrote Flask-SQLAlchemy's
      repr into the path: /tag/%3CTag%201%3E?page=2. A tag with more than 100
      posts was readable only to its first page.
  P2  the RSS feed carried no `Post.private == False`, so it published the
      ingested microblogs its own HTML page hides.
  P3  `description` and `og_image` were assigned None and immediately tested,
      leaving four lines no test could reach -- the residue of the copy from
      show_community_rss, where both names have sources.

Fact 292 applies: render_template is patched for anything that renders, and the
site fixture supplies g.site. Both routes are GET-only, so fact 293's CSRF
token does not arise here.

Fact 322 was found by this round and governs two of its rows: conftest's `app`
fixture holds a session-scoped app context, so `g` outlives a request and
flask_login answers a SECOND request in the same test as the FIRST request's
user -- even from a new test_client with no cookies. The logged-in and
anonymous halves of the moderation-ids pair are therefore two tests, not one.
"""
import pytest
from unittest.mock import patch

from app import db
from app.models import Post, Site, Tag, post_tag
from tests.factories import make_community, make_instance, make_post, make_user

pytestmark = pytest.mark.usefixtures('site')


def login(client, user):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def _seed():
    """Two local users and an open instance.

    Site.private_instance defaults True and show_tag is decorated with
    login_required_if_private_instance, so an anonymous row would otherwise
    measure the decorator instead of the view.
    """
    instance = make_instance('test.piefed.local', software='piefed')
    burn = make_user(instance, 'burnseat', local=True)
    assert burn.id == 1
    alice = make_user(instance, 'alice', local=True)
    bob = make_user(instance, 'bob', local=True)
    site = db.session.get(Site, 1)
    site.private_instance = False
    db.session.commit()
    return instance, alice, bob


def _tag(name='solarstorm', banned=False):
    tag = Tag(name=name.lower(), display_as=name, banned=banned, post_count=0)
    db.session.add(tag)
    db.session.commit()
    return tag


def _tagged(tag, community, author, title, **kwargs):
    """A post carrying `tag`, published and past review.

    make_post leaves status at the column default, which is already past
    POST_STATUS_REVIEWING; anything else a row needs is set by keyword.
    """
    post = make_post(community, author,
                     ap_id=f'https://test.piefed.local/post/{title.replace(" ", "")}',
                     title=title)
    for key, value in kwargs.items():
        setattr(post, key, value)
    db.session.execute(post_tag.insert().values(post_id=post.id, tag_id=tag.id))
    db.session.commit()
    return post


def _titles(render):
    return [post.title for post in render.call_args.kwargs['posts'].items]


# --------------------------------------------------------------------------
# P1: the pagination links named the row, not the tag
# --------------------------------------------------------------------------


def test_the_next_link_addresses_the_tag_by_name(app, db_session):
    """Before the repair:

        PROBE k1 next_url: /tag/%3CTag%201%3E?page=2&category=

    `tag` is the Tag row from :32 by the time url_for sees it, and the URL
    builder has nothing but str() for a model. The link renders, resolves to no
    tag, and 404s -- so page 2 of a busy tag does not exist.
    """
    instance, alice, bob = _seed()
    tag = _tag()
    community = make_community('microblogs')
    for index in range(101):
        _tagged(tag, community, alice, f'post {index}')
    client = app.test_client()

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        response = client.get('/tag/solarstorm')

    assert response.status_code == 200
    assert render.call_args.kwargs['next_url'].startswith('/tag/solarstorm?')


def test_the_next_link_is_a_page_that_can_be_fetched(app, db_session):
    """The inversion of the row above: a repair that wrote any other string
    would pass an assertion about the prefix alone, so the link is followed.
    """
    instance, alice, bob = _seed()
    tag = _tag()
    community = make_community('microblogs')
    for index in range(101):
        _tagged(tag, community, alice, f'post {index}')
    client = app.test_client()

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        client.get('/tag/solarstorm')
        next_url = render.call_args.kwargs['next_url']
        response = client.get(next_url)

    assert response.status_code == 200
    assert len(_titles(render)) == 1


def test_the_previous_link_addresses_the_tag_by_name(app, db_session):
    instance, alice, bob = _seed()
    tag = _tag()
    community = make_community('microblogs')
    for index in range(101):
        _tagged(tag, community, alice, f'post {index}')
    client = app.test_client()

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        response = client.get('/tag/solarstorm?page=2')

    assert response.status_code == 200
    assert render.call_args.kwargs['prev_url'].startswith('/tag/solarstorm?')


# --------------------------------------------------------------------------
# P2: the feed published what the page hides
# --------------------------------------------------------------------------


def test_an_ingested_microblog_is_not_published_in_the_tags_feed(app, db_session):
    """Post.private is the microblog marker (tests/factories.py:294) and its
    job is to keep ingested microblogs off the discovery surfaces. show_tag
    filters it at :38; the feed of the same page did not:

        PROBE k5 show_tag posts: 0
        PROBE k8 body has entry: True

    show_domain_rss, the closest sibling, filters both halves
    (app/domain/routes.py:128-130).
    """
    instance, alice, bob = _seed()
    tag = _tag()
    community = make_community('microblogs')
    _tagged(tag, community, alice, 'a toot', private=True, microblog=True,
            body_html='<p>a short toot</p>')
    client = app.test_client()

    response = client.get('/tag/solarstorm/feed')

    assert response.status_code == 200
    assert b'<item>' not in response.data


def test_an_ordinary_post_is_still_published_in_the_tags_feed(app, db_session):
    """The inversion: a repair that filtered everything would pass the row
    above.
    """
    instance, alice, bob = _seed()
    tag = _tag()
    community = make_community('microblogs')
    _tagged(tag, community, alice, 'an article')
    client = app.test_client()

    response = client.get('/tag/solarstorm/feed')

    assert response.status_code == 200
    assert b'<title>an article</title>' in response.data


# --------------------------------------------------------------------------
# show_tag: the lookup and the gate
# --------------------------------------------------------------------------


def test_a_tag_nobody_has_used_is_a_404(app, db_session):
    instance, alice, bob = _seed()
    client = app.test_client()

    response = client.get('/tag/nosuchtag')

    assert response.status_code == 404


def test_the_tag_in_the_url_is_matched_without_regard_to_case(app, db_session):
    """Tag.name is the lowercase form and display_as keeps the capitals, so a
    link written #SolarStorm has to reach the same row as #solarstorm.
    """
    instance, alice, bob = _seed()
    tag = _tag('SolarStorm')
    client = app.test_client()

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        response = client.get('/tag/SolarStorm')

    assert response.status_code == 200
    assert render.call_args.kwargs['tag'].id == tag.id


def test_a_private_instance_refuses_an_anonymous_reader(app, db_session):
    """The decorator this module leans on, measured rather than assumed: it is
    the reason every other row here opens the instance first.
    """
    instance, alice, bob = _seed()
    site = db.session.get(Site, 1)
    site.private_instance = True
    db.session.commit()
    _tag()
    client = app.test_client()

    response = client.get('/tag/solarstorm')

    assert response.status_code == 302
    assert '/auth/login' in response.headers['Location']


# --------------------------------------------------------------------------
# show_tag: whose posts appear
# --------------------------------------------------------------------------


def test_an_anonymous_reader_is_not_shown_posts_from_bots(app, db_session):
    instance, alice, bob = _seed()
    tag = _tag()
    community = make_community('microblogs')
    _tagged(tag, community, alice, 'human post')
    _tagged(tag, community, alice, 'bot post', from_bot=True)
    client = app.test_client()

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        client.get('/tag/solarstorm')

    assert _titles(render) == ['human post']


@pytest.mark.parametrize('ignore_bots, expected', [
    (1, ['human post']),
    (0, ['bot post', 'human post']),
])
def test_a_reader_sees_bots_only_when_they_have_asked_to(app, db_session, ignore_bots, expected):
    instance, alice, bob = _seed()
    alice.ignore_bots = ignore_bots
    db.session.commit()
    tag = _tag()
    community = make_community('microblogs')
    _tagged(tag, community, alice, 'human post')
    _tagged(tag, community, alice, 'bot post', from_bot=True)
    client = app.test_client()
    login(client, alice)

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        client.get('/tag/solarstorm')

    assert sorted(_titles(render)) == expected


def test_a_banned_community_hides_its_posts_from_the_tag(app, db_session):
    instance, alice, bob = _seed()
    tag = _tag()
    community = make_community('microblogs')
    banned = make_community('bannedcomm')
    banned.banned = True
    db.session.commit()
    _tagged(tag, community, alice, 'open post')
    _tagged(tag, banned, alice, 'banned post')
    client = app.test_client()

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        client.get('/tag/solarstorm')

    assert _titles(render) == ['open post']


@pytest.mark.parametrize('column, value', [
    ('deleted', True),
    ('status', 0),
    ('private', True),
])
def test_a_post_that_is_not_publicly_readable_is_left_out(app, db_session, column, value):
    """The three columns in the base filter, one row each, with a readable post
    beside each one so a filter that stopped working returns the other rather
    than nothing.
    """
    instance, alice, bob = _seed()
    tag = _tag()
    community = make_community('microblogs')
    _tagged(tag, community, alice, 'readable post')
    _tagged(tag, community, alice, 'hidden post', **{column: value})
    client = app.test_client()

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        client.get('/tag/solarstorm')

    assert _titles(render) == ['readable post']


def test_a_logged_in_readers_own_blocks_apply(app, db_session):
    """Four reader-scoped filters in one row -- blocked domains, blocked
    instances, blocked communities and blocked users -- each with something to
    exclude and the good post left standing.
    """
    from app.models import Domain
    from tests.factories import (make_community_block, make_domain, make_domain_block,
                                 make_instance_block, make_user_block)
    instance, alice, bob = _seed()
    peer = make_instance('remote.example', software='lemmy')
    tag = _tag()
    community = make_community('microblogs')
    blocked_community = make_community('blockedcomm')
    domain = make_domain('blocked.test')
    _tagged(tag, community, alice, 'plain post')
    _tagged(tag, community, alice, 'domain post', domain_id=domain.id)
    _tagged(tag, community, alice, 'instance post', instance_id=peer.id)
    _tagged(tag, blocked_community, alice, 'community post')
    _tagged(tag, community, bob, 'blocked author post')
    make_domain_block(alice, domain)
    make_instance_block(alice, peer)
    make_community_block(alice, blocked_community)
    make_user_block(alice, bob)
    client = app.test_client()
    login(client, alice)

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        client.get('/tag/solarstorm')

    assert _titles(render) == ['plain post']


def test_a_post_with_no_instance_recorded_survives_an_instance_block(app, db_session):
    """`or_(Post.instance_id.not_in(instance_ids), Post.instance_id == None)` --
    the NULL half, which SQL's `NOT IN` cannot answer on its own: a row whose
    instance_id is NULL tests neither in nor not-in and disappears.

    The domain half of the same pair is pinned by the row above, where the good
    post has no domain_id at all. The instance half needs its own row because
    make_post always records one (`instance_id=user.instance_id`), and
    Post.instance_id is nullable -- so the mutant dropping this half survived a
    suite that never built a post without it.
    """
    from tests.factories import make_instance_block
    instance, alice, bob = _seed()
    peer = make_instance('remote.example', software='lemmy')
    tag = _tag()
    community = make_community('microblogs')
    _tagged(tag, community, alice, 'instance post', instance_id=peer.id)
    _tagged(tag, community, alice, 'instanceless post', instance_id=None)
    make_instance_block(alice, peer)
    client = app.test_client()
    login(client, alice)

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        client.get('/tag/solarstorm')

    assert _titles(render) == ['instanceless post']


def test_a_reader_who_has_blocked_nothing_sees_everything(app, db_session):
    """The other side of the four `if <ids>:` guards above: with no blocks at
    all, none of the four narrowing filters is applied.
    """
    from tests.factories import make_domain
    instance, alice, bob = _seed()
    peer = make_instance('remote.example', software='lemmy')
    tag = _tag()
    community = make_community('microblogs')
    domain = make_domain('open.test')
    _tagged(tag, community, alice, 'plain post')
    _tagged(tag, community, alice, 'domain post', domain_id=domain.id)
    _tagged(tag, community, alice, 'instance post', instance_id=peer.id)
    _tagged(tag, community, bob, 'other author post')
    client = app.test_client()
    login(client, alice)

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        client.get('/tag/solarstorm')

    assert sorted(_titles(render)) == ['domain post', 'instance post', 'other author post',
                                       'plain post']


@pytest.mark.parametrize('a_member, expected', [
    (True, ['open post', 'private post']),
    (False, ['open post']),
])
def test_a_private_communitys_posts_reach_its_members_only(app, db_session, a_member, expected):
    from tests.factories import make_community_member
    instance, alice, bob = _seed()
    tag = _tag()
    community = make_community('microblogs')
    private = make_community('privatecomm')
    private.private = True
    db.session.commit()
    _tagged(tag, community, alice, 'open post')
    _tagged(tag, private, alice, 'private post')
    if a_member:
        make_community_member(alice, private)
    client = app.test_client()
    login(client, alice)

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        client.get('/tag/solarstorm')

    assert sorted(_titles(render)) == expected


def test_an_anonymous_reader_never_sees_a_private_community(app, db_session):
    instance, alice, bob = _seed()
    tag = _tag()
    community = make_community('microblogs')
    private = make_community('privatecomm')
    private.private = True
    db.session.commit()
    _tagged(tag, community, alice, 'open post')
    _tagged(tag, private, alice, 'private post')
    client = app.test_client()

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        client.get('/tag/solarstorm')

    assert _titles(render) == ['open post']


@pytest.mark.parametrize('logged_in, expected', [
    (True, {'Spoilers': {'spoiler'}}),
    (False, {}),
])
def test_the_readers_content_filters_are_handed_to_the_template(app, db_session,
                                                                logged_in, expected):
    """content_filters is what the template dims a post with; an anonymous
    reader has none, and the empty dict is the branch that says so.
    """
    from app.models import Filter
    instance, alice, bob = _seed()
    db.session.add(Filter(title='Spoilers', user_id=alice.id, filter_posts=True,
                          hide_type=0, keywords='spoiler'))
    db.session.commit()
    tag = _tag()
    community = make_community('microblogs')
    _tagged(tag, community, alice, 'a post')
    client = app.test_client()
    if logged_in:
        login(client, alice)

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        client.get('/tag/solarstorm')

    assert dict(render.call_args.kwargs['content_filters']) == expected


# --------------------------------------------------------------------------
# show_tag: the three categories
# --------------------------------------------------------------------------


def test_a_community_category_narrows_the_tag_to_that_community(app, db_session):
    instance, alice, bob = _seed()
    tag = _tag()
    community = make_community('microblogs')
    other = make_community('othercomm')
    _tagged(tag, community, alice, 'wanted post')
    _tagged(tag, other, alice, 'other post')
    client = app.test_client()

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        client.get(f'/tag/solarstorm?category=community&category_id={community.id}')

    assert _titles(render) == ['wanted post']


def _topic(name='fediverse', parent=None, show_posts_in_children=False):
    from app.models import Topic
    topic = Topic(name=name.title(), machine_name=name, num_communities=0,
                  parent_id=parent.id if parent else None,
                  show_posts_in_children=show_posts_in_children)
    db.session.add(topic)
    db.session.commit()
    return topic


def _in_topic(topic, name):
    community = make_community(name)
    community.topic_id = topic.id
    db.session.commit()
    return community


@pytest.mark.parametrize('show_posts_in_children, expected', [
    (False, ['parent post']),
    (True, ['child post', 'parent post']),
])
def test_a_topic_category_follows_the_topic_tree_only_when_asked(app, db_session,
                                                                 show_posts_in_children,
                                                                 expected):
    instance, alice, bob = _seed()
    tag = _tag()
    parent = _topic('fediverse', show_posts_in_children=show_posts_in_children)
    child = _topic('microblogging', parent=parent)
    parent_community = _in_topic(parent, 'parentcomm')
    child_community = _in_topic(child, 'childcomm')
    elsewhere = make_community('elsewhere')
    _tagged(tag, parent_community, alice, 'parent post')
    _tagged(tag, child_community, alice, 'child post')
    _tagged(tag, elsewhere, alice, 'unrelated post')
    client = app.test_client()

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        client.get(f'/tag/solarstorm?category=topic&category_id={parent.id}')

    assert sorted(_titles(render)) == expected


def test_a_topic_category_naming_no_topic_is_a_404(app, db_session):
    instance, alice, bob = _seed()
    _tag()
    client = app.test_client()

    response = client.get('/tag/solarstorm?category=topic&category_id=9999')

    assert response.status_code == 404


@pytest.mark.parametrize('show_posts_in_children, expected', [
    (False, ['parent post']),
    (True, ['child post', 'parent post']),
])
def test_a_feed_category_follows_the_feed_tree_only_when_asked(app, db_session,
                                                               show_posts_in_children,
                                                               expected):
    from tests.factories import make_feed_item, make_local_feed
    instance, alice, bob = _seed()
    tag = _tag()
    parent_feed = make_local_feed('parentfeed')
    parent_feed.show_posts_in_children = show_posts_in_children
    child_feed = make_local_feed('childfeed')
    child_feed.parent_feed_id = parent_feed.id
    db.session.commit()
    parent_community = make_community('parentcomm')
    child_community = make_community('childcomm')
    elsewhere = make_community('elsewhere')
    make_feed_item(parent_feed, parent_community)
    make_feed_item(child_feed, child_community)
    _tagged(tag, parent_community, alice, 'parent post')
    _tagged(tag, child_community, alice, 'child post')
    _tagged(tag, elsewhere, alice, 'unrelated post')
    client = app.test_client()

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        client.get(f'/tag/solarstorm?category=feed&category_id={parent_feed.id}')

    assert sorted(_titles(render)) == expected


def test_a_feed_category_naming_no_feed_is_a_404(app, db_session):
    instance, alice, bob = _seed()
    _tag()
    client = app.test_client()

    response = client.get('/tag/solarstorm?category=feed&category_id=9999')

    assert response.status_code == 404


@pytest.mark.parametrize('query', [
    'category=community',
    'category=topic',
    'category=feed',
    'category_id=1',
    'category=nonsense&category_id=1',
])
def test_a_category_without_its_partner_narrows_nothing(app, db_session, query):
    """`category and category == '<name>' and category_id` is three operands,
    and a request carrying only one of them falls through every arm to the
    unfiltered page. Registered as R3: it is the answer a crafted query string
    gets, not one the UI can produce.
    """
    instance, alice, bob = _seed()
    tag = _tag()
    community = make_community('microblogs')
    other = make_community('othercomm')
    _tagged(tag, community, alice, 'first post')
    _tagged(tag, other, alice, 'second post')
    client = app.test_client()

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        response = client.get(f'/tag/solarstorm?{query}')

    assert response.status_code == 200
    assert sorted(_titles(render)) == ['first post', 'second post']


# --------------------------------------------------------------------------
# show_tag: what the template is handed
# --------------------------------------------------------------------------


def test_the_page_offers_no_links_when_everything_fits_on_it(app, db_session):
    instance, alice, bob = _seed()
    tag = _tag()
    community = make_community('microblogs')
    _tagged(tag, community, alice, 'only post')
    client = app.test_client()

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        client.get('/tag/solarstorm')

    assert render.call_args.kwargs['next_url'] is None
    assert render.call_args.kwargs['prev_url'] is None


def test_the_feed_address_and_its_name_name_the_tag_and_the_site(app, db_session):
    """rss_feed uses Tag.name, the lowercase form the route matches;
    rss_feed_name uses display_as, the form with the capitals in it.
    """
    instance, alice, bob = _seed()
    _tag('SolarStorm')
    client = app.test_client()

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        client.get('/tag/solarstorm')

    assert render.call_args.kwargs['rss_feed'].endswith('/tag/solarstorm/feed')
    assert render.call_args.kwargs['rss_feed_name'] == '#SolarStorm on Test Site'


@pytest.mark.parametrize('show_inoculation_block', [True, False])
def test_the_inoculation_block_appears_only_when_the_site_asks_for_it(app, db_session,
                                                                     show_inoculation_block):
    instance, alice, bob = _seed()
    site = db.session.get(Site, 1)
    site.show_inoculation_block = show_inoculation_block
    db.session.commit()
    _tag()
    client = app.test_client()

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        client.get('/tag/solarstorm')

    assert (render.call_args.kwargs['inoculation'] is not None) is show_inoculation_block


def test_a_moderators_communities_are_named_for_the_template(app, db_session):
    """moderated_community_ids is what the page uses to decide whether to offer
    moderation controls on a post.
    """
    from tests.factories import make_community_member
    instance, alice, bob = _seed()
    _tag()
    community = make_community('microblogs')
    make_community_member(alice, community, is_moderator=True)
    client = app.test_client()
    login(client, alice)

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        client.get('/tag/solarstorm')

    assert render.call_args.kwargs['moderated_community_ids'] == [community.id]


def test_an_anonymous_reader_moderates_nothing(app, db_session):
    """The other arm of moderating_communities_ids' own guard: get_id() answers
    None for an anonymous reader and the helper returns [] without a query.

    THIS IS A SEPARATE TEST ON PURPOSE. The two requests cannot share one,
    because tests/conftest.py's `app` fixture holds a session-scoped app
    context: Flask reuses an already-pushed context rather than making a new
    one per request, so `g` outlives the request, and flask_login caches the
    authenticated user in `g._login_user`. A second request in the same test --
    even from a brand new test_client with no cookies -- reads that cache and
    is answered as the user the FIRST request logged in. Measured: the
    anonymous half returned the moderator's [1] until `del g._login_user`
    preceded it. Fact 322.
    """
    from tests.factories import make_community_member
    instance, alice, bob = _seed()
    _tag()
    community = make_community('microblogs')
    make_community_member(alice, community, is_moderator=True)
    client = app.test_client()

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        client.get('/tag/solarstorm')

    assert render.call_args.kwargs['moderated_community_ids'] == []


# --------------------------------------------------------------------------
# show_tag_rss
# --------------------------------------------------------------------------


def test_the_feed_of_a_tag_nobody_has_used_is_a_404(app, db_session):
    instance, alice, bob = _seed()
    client = app.test_client()

    response = client.get('/tag/nosuchtag/feed')

    assert response.status_code == 404


def test_the_feed_is_served_as_rss_and_names_the_tag(app, db_session):
    instance, alice, bob = _seed()
    _tag('SolarStorm')
    client = app.test_client()

    # The mixed-case spelling is the one a link in a post carries, and the
    # lookup lowercases it -- as the HTML page's does.
    response = client.get('/tag/SolarStorm/feed')

    assert response.status_code == 200
    assert response.headers['Content-Type'] == 'application/rss+xml'
    assert b'<title>#SolarStorm on Test Site</title>' in response.data


def test_the_feed_leaves_out_bots_banned_communities_and_deleted_posts(app, db_session):
    instance, alice, bob = _seed()
    tag = _tag()
    community = make_community('microblogs')
    banned = make_community('bannedcomm')
    banned.banned = True
    private = make_community('privatecomm')
    private.private = True
    db.session.commit()
    _tagged(tag, community, alice, 'wanted post')
    _tagged(tag, community, alice, 'bot post', from_bot=True)
    _tagged(tag, community, alice, 'deleted post', deleted=True)
    _tagged(tag, community, alice, 'unreviewed post', status=0)
    _tagged(tag, banned, alice, 'banned post')
    _tagged(tag, private, alice, 'private community post')
    client = app.test_client()

    response = client.get('/tag/solarstorm/feed')

    assert b'<title>wanted post</title>' in response.data
    for unwanted in [b'bot post', b'deleted post', b'unreviewed post', b'banned post',
                     b'private community post']:
        assert unwanted not in response.data


def test_a_reader_who_reads_bots_is_served_them_in_the_feed(app, db_session):
    """The feed has no login gate, but it reads current_user all the same, so a
    logged-in subscriber's ignore_bots setting reaches it.
    """
    instance, alice, bob = _seed()
    alice.ignore_bots = 0
    db.session.commit()
    tag = _tag()
    community = make_community('microblogs')
    _tagged(tag, community, alice, 'bot post', from_bot=True)
    client = app.test_client()
    login(client, alice)

    response = client.get('/tag/solarstorm/feed')

    assert b'<title>bot post</title>' in response.data


@pytest.mark.parametrize('slug, expected', [
    ('/c/microblogs/p/1/a-post', b'/c/microblogs/p/1/a-post'),
    (None, b'/post/'),
])
def test_each_entry_links_to_the_posts_own_address(app, db_session, slug, expected):
    """Post.slug is set by Post.new() and is None for anything that predates it
    or whose title would not slugify, so both arms are reachable.
    """
    instance, alice, bob = _seed()
    tag = _tag()
    community = make_community('microblogs')
    _tagged(tag, community, alice, 'a post', slug=slug)
    client = app.test_client()

    response = client.get('/tag/solarstorm/feed')

    assert expected in response.data


@pytest.mark.parametrize('url, enclosed', [
    ('https://example.com/picture.jpg', True),
    ('https://example.com/article.html', False),
    ('https://example.com/page', False),
    (None, False),
])
def test_an_entry_encloses_a_file_but_not_a_web_page(app, db_session, url, enclosed):
    """`if type and not type.startswith('text/')` -- four rows, because a URL
    with no mimetype, a URL with no extension to guess one from, and a URL
    whose mimetype is text/html reach the same answer by different halves of
    the guard. The extensionless one is what makes `type and` load-bearing:
    without it, `None.startswith` is an AttributeError and a 500.
    """
    instance, alice, bob = _seed()
    tag = _tag()
    community = make_community('microblogs')
    _tagged(tag, community, alice, 'a post', url=url)
    client = app.test_client()

    response = client.get('/tag/solarstorm/feed')

    assert (b'<enclosure' in response.data) is enclosed


def test_the_feed_carries_at_most_twenty_entries(app, db_session):
    instance, alice, bob = _seed()
    tag = _tag()
    community = make_community('microblogs')
    for index in range(25):
        _tagged(tag, community, alice, f'post {index:02d}')
    client = app.test_client()

    response = client.get('/tag/solarstorm/feed')

    assert response.data.count(b'<item>') == 20


# --------------------------------------------------------------------------
# Ordering, and the values the two surfaces put in front of a reader
# --------------------------------------------------------------------------


def test_the_page_shows_the_newest_post_first(app, db_session):
    from app.models import utcnow
    from datetime import timedelta
    instance, alice, bob = _seed()
    tag = _tag()
    community = make_community('microblogs')
    now = utcnow()
    _tagged(tag, community, alice, 'older post', posted_at=now - timedelta(days=1))
    _tagged(tag, community, alice, 'newer post', posted_at=now)
    client = app.test_client()

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        client.get('/tag/solarstorm')

    assert _titles(render) == ['newer post', 'older post']


def test_the_feed_is_built_from_the_newest_posts_and_prints_them_oldest_first(app, db_session):
    """Two facts in one row, because they are the same mechanism seen twice.

    The QUERY takes the newest twenty (`desc(Post.posted_at)` then `limit(20)`),
    which is why post 00 through post 04 are absent from a tag with 25 posts.
    The OUTPUT then prints them oldest-first, because feedgen's add_entry()
    prepends by default, so the last entry added -- the oldest of the twenty --
    ends up at the top of the channel. Every RSS route in this repo shares the
    shape. Readers sort by pubDate, so it is registered as observed behaviour
    rather than repaired here.
    """
    instance, alice, bob = _seed()
    tag = _tag()
    community = make_community('microblogs')
    for index in range(25):
        _tagged(tag, community, alice, f'post {index:02d}')
    client = app.test_client()

    response = client.get('/tag/solarstorm/feed')

    assert b'<title>post 00</title>' not in response.data
    assert b'<title>post 24</title>' in response.data
    assert response.data.index(b'post 05') < response.data.index(b'post 24')


def test_the_page_is_titled_by_the_tag_and_asks_for_no_category(app, db_session):
    """`title` and `category` are what the template puts in the heading and in
    the category chooser's selected state; a request naming no category has to
    arrive with the empty string rather than a guess.
    """
    instance, alice, bob = _seed()
    _tag('SolarStorm')
    client = app.test_client()

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        client.get('/tag/solarstorm')

    assert render.call_args.kwargs['title'] == 'solarstorm'
    assert render.call_args.kwargs['category'] == ''
    assert render.call_args.kwargs['category_id'] is None


@pytest.mark.parametrize('logo_152, expected', [
    ('/static/images/logo152.png', b'/static/images/logo152.png'),
    (None, b'/static/images/apple-touch-icon.png'),
])
def test_the_feeds_logo_is_the_sites_own_when_it_has_one(app, db_session, logo_152, expected):
    """A conditional expression inside an f-string: both arms sit on one line,
    so branch coverage cannot tell them apart and only an assertion can.
    """
    instance, alice, bob = _seed()
    site = db.session.get(Site, 1)
    site.logo_152 = logo_152
    db.session.commit()
    _tag()
    client = app.test_client()

    response = client.get('/tag/solarstorm/feed')

    assert expected in response.data


def test_each_entry_carries_the_posts_body_author_and_address(app, db_session):
    """The two values an RSS reader renders as the article itself. `guid` is
    the post's ap_id, which is what makes an entry the same entry across polls.

    `fe.author(name=...)` is deliberately NOT asserted: RSS 2.0's <author> is an
    email address, so feedgen drops a name-only author from the output
    entirely. Nothing this route does with the author is observable to a
    subscriber, and the mutation pass records that rather than pretending an
    assertion exists.
    """
    instance, alice, bob = _seed()
    tag = _tag()
    community = make_community('microblogs')
    post = _tagged(tag, community, alice, 'an article',
                   body_html='<p>the body of the article</p>')
    client = app.test_client()

    response = client.get('/tag/solarstorm/feed')

    assert b'the body of the article' in response.data
    assert post.ap_id.encode() in response.data


def test_the_previous_link_is_absent_on_the_first_page_however_it_is_asked_for(app, db_session):
    """EQUIVALENCE PROOF for `posts.has_prev and page != 1`.

    The second conjunct cannot change the answer: `has_prev` is False exactly
    when the paginator is on its first page, and the paginator's page is the
    `page` argument. A request for page 0 or a negative page is clamped by
    paginate(error_out=False) to the first page, so `has_prev` is False there
    too, and the mutant that drops `and page != 1` answers None on every row
    this route can reach. Registered with the mutation pass rather than fixed.
    """
    instance, alice, bob = _seed()
    tag = _tag()
    community = make_community('microblogs')
    for index in range(101):
        _tagged(tag, community, alice, f'post {index}')
    client = app.test_client()

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        for query in ['', '?page=1', '?page=0', '?page=-3']:
            client.get(f'/tag/solarstorm{query}')
            assert render.call_args.kwargs['prev_url'] is None, query

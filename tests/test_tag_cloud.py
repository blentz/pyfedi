"""app/tag/routes.py -- the tag cloud, and the last function in the module.

MEASUREMENT BASIS. The module stood at 81.714% on the full-suite --cov=app run
at 3dab9dc41, with all 42 remaining missing statements in `tag_cloud`. This
round closes the package: app/tag/__init__.py is already at 100.

Five defects are pinned here and repaired together:

  P1  `int(request.args.get('page', 1))` -- a crafted page was a 500, the third
      time this shape has appeared in this file's neighbourhood.
  P2  the if/elif chain over `type` has no else, so a category the route does
      not understand rendered an empty cloud with a 200 instead of a 404.
  P3  the counting query filtered only Post.deleted, so the cloud offered tags
      whose posts the tag page will not show -- click one, get nothing.
  P4  the community branch appended the id with no check, so the tag cloud of a
      banned OR a private community was served to anyone holding the id.
  P5  the co-occurrence subquery had the same gaps, so relationship weights
      were computed over posts the cloud no longer counts.

Facts 292 and 322 apply: render_template is patched for anything that renders,
and one request per test.
"""
import pytest
from unittest.mock import patch

from app import db
from app.models import Site, Tag, post_tag
from tests.factories import make_community, make_instance, make_post, make_user

pytestmark = pytest.mark.usefixtures('site')


def login(client, user):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def _seed():
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
    """A post carrying `tag`. Several tags on one post is the shape the
    co-occurrence block exists for, so `tag` may be a list.
    """
    post = make_post(community, author,
                     ap_id=f'https://test.piefed.local/post/{title.replace(" ", "")}',
                     title=title)
    for key, value in kwargs.items():
        setattr(post, key, value)
    for one in (tag if isinstance(tag, list) else [tag]):
        db.session.execute(post_tag.insert().values(post_id=post.id, tag_id=one.id))
    db.session.commit()
    return post


def _names(render):
    return sorted(entry['text'] for entry in render.call_args.kwargs['tags_data'])


def _counts(render):
    return {entry['text']: entry['numPosts'] for entry in render.call_args.kwargs['tags_data']}


# --------------------------------------------------------------------------
# P1: a crafted page
# --------------------------------------------------------------------------


@pytest.mark.parametrize('page', ['abc', '', '1.5'])
def test_a_page_that_is_not_a_number_reads_as_the_first_page(app, db_session, page):
    """PROBE k6 exception: ValueError invalid literal for int() with base 10:
    'abc'. Every other function in this module reads page through type=int.
    """
    instance, alice, bob = _seed()
    tag = _tag()
    community = make_community('microblogs')
    _tagged(tag, community, alice, 'a post')
    client = app.test_client()

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        response = client.get(f'/tags/cloud/community/{community.id}?page={page}')

    assert response.status_code == 200
    assert _names(render) == ['solarstorm']


# --------------------------------------------------------------------------
# P2: a category the route does not understand
# --------------------------------------------------------------------------


def test_a_category_that_is_not_a_category_is_a_404(app, db_session):
    """Before the repair the chain simply fell through:

        PROBE k9 status: 200
        PROBE k9 title: Nonsense tags

    -- a page that looks like a real, empty cloud.
    """
    instance, alice, bob = _seed()
    community = make_community('microblogs')
    client = app.test_client()

    response = client.get(f'/tags/cloud/nonsense/{community.id}')

    assert response.status_code == 404


@pytest.mark.parametrize('category', ['community', 'topic', 'feed'])
def test_each_category_the_route_does_understand_still_renders(app, db_session, category):
    """The inversion: a repair that aborted on everything would pass the row
    above. Each of the three builds its own subject, so none of them is
    accidentally reaching the new else.
    """
    from tests.factories import make_feed_item, make_local_feed
    from app.models import Topic
    instance, alice, bob = _seed()
    community = make_community('microblogs')
    if category == 'community':
        category_id = community.id
    elif category == 'topic':
        topic = Topic(name='Fediverse', machine_name='fediverse', num_communities=0,
                      show_posts_in_children=False)
        db.session.add(topic)
        db.session.commit()
        community.topic_id = topic.id
        db.session.commit()
        category_id = topic.id
    else:
        feed = make_local_feed('localfeed', public=True)
        make_feed_item(feed, community)
        category_id = feed.id
    tag = _tag()
    _tagged(tag, community, alice, 'a post')
    client = app.test_client()

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        response = client.get(f'/tags/cloud/{category}/{category_id}')

    assert response.status_code == 200
    assert _names(render) == ['solarstorm']


@pytest.mark.parametrize('category', ['community', 'topic', 'feed'])
def test_a_category_id_naming_nothing_is_a_404(app, db_session, category):
    instance, alice, bob = _seed()
    client = app.test_client()

    response = client.get(f'/tags/cloud/{category}/9999')

    assert response.status_code == 404


# --------------------------------------------------------------------------
# P3: tags whose posts the tag page will not show
# --------------------------------------------------------------------------


@pytest.mark.parametrize('column, value', [
    ('status', 0),
    ('private', True),
    ('deleted', True),
])
def test_the_cloud_does_not_offer_a_tag_that_leads_nowhere(app, db_session, column, value):
    """PROBE p3 tags: ['microtag', 'reviewingtag'] -- both counted, and
    show_tag shows neither, so clicking either in the cloud reached an empty
    page. The cloud is a navigation surface; a tag it offers has to go
    somewhere.
    """
    instance, alice, bob = _seed()
    community = make_community('microblogs')
    shown = _tag('shown')
    hidden = _tag('hidden')
    _tagged(shown, community, alice, 'a readable post')
    _tagged(hidden, community, alice, 'a hidden post', **{column: value})
    client = app.test_client()

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        client.get(f'/tags/cloud/community/{community.id}')

    assert _names(render) == ['shown']


def test_the_count_beside_a_tag_counts_only_the_posts_it_can_reach(app, db_session):
    """numPosts is the size the word is drawn at, so a count inflated by posts
    the tag page hides is wrong even when the tag itself belongs in the cloud.
    """
    instance, alice, bob = _seed()
    community = make_community('microblogs')
    tag = _tag()
    _tagged(tag, community, alice, 'first readable')
    _tagged(tag, community, alice, 'second readable')
    _tagged(tag, community, alice, 'unreviewed', status=0)
    _tagged(tag, community, alice, 'a toot', private=True, microblog=True,
            body_html='<p>toot</p>')
    client = app.test_client()

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        client.get(f'/tags/cloud/community/{community.id}')

    assert _counts(render) == {'solarstorm': 2}


def test_a_banned_tag_is_not_in_the_cloud(app, db_session):
    instance, alice, bob = _seed()
    community = make_community('microblogs')
    good = _tag('goodtag')
    bad = _tag('badtag', banned=True)
    _tagged(good, community, alice, 'one post')
    _tagged(bad, community, alice, 'two post')
    client = app.test_client()

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        client.get(f'/tags/cloud/community/{community.id}')

    assert _names(render) == ['goodtag']


# --------------------------------------------------------------------------
# P4: whose cloud it is
# --------------------------------------------------------------------------


def test_a_banned_communitys_tag_cloud_is_empty(app, db_session):
    """PROBE p2 tags: ['solarstorm'] for a community with banned = True. The
    topic branch filters `banned is false` in its own SQL; the community branch
    appended the id with no check at all.
    """
    instance, alice, bob = _seed()
    tag = _tag()
    banned = make_community('bannedcomm')
    banned.banned = True
    db.session.commit()
    _tagged(tag, banned, alice, 'a post')
    client = app.test_client()

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        client.get(f'/tags/cloud/community/{banned.id}')

    assert _names(render) == []


def test_an_anonymous_reader_gets_no_cloud_for_a_private_community(app, db_session):
    """PROBE p1 tags: ['solarstorm'] for a community with private = True. A
    private community's tag cloud is a list of what its members are talking
    about, and it was served to anyone holding the id -- D828 one function
    along.
    """
    instance, alice, bob = _seed()
    tag = _tag()
    private = make_community('privatecomm')
    private.private = True
    db.session.commit()
    _tagged(tag, private, alice, 'a post')
    client = app.test_client()

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        client.get(f'/tags/cloud/community/{private.id}')

    assert _names(render) == []


@pytest.mark.parametrize('a_member, expected', [
    (True, ['solarstorm']),
    (False, []),
])
def test_a_private_communitys_cloud_belongs_to_its_members(app, db_session, a_member,
                                                           expected):
    from tests.factories import make_community_member
    instance, alice, bob = _seed()
    tag = _tag()
    private = make_community('privatecomm')
    private.private = True
    db.session.commit()
    _tagged(tag, private, alice, 'a post')
    if a_member:
        make_community_member(alice, private)
    client = app.test_client()
    login(client, alice)

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        client.get(f'/tags/cloud/community/{private.id}')

    assert _names(render) == expected


# --------------------------------------------------------------------------
# P5: the relationships
# --------------------------------------------------------------------------


def test_two_tags_on_one_post_are_related_in_both_directions(app, db_session):
    """PROBE p6 relationships: {2: {1: 1}, 1: {2: 1}}. The block is what the
    cloud draws its links from.
    """
    instance, alice, bob = _seed()
    community = make_community('microblogs')
    alpha = _tag('alpha')
    beta = _tag('beta')
    _tagged([alpha, beta], community, alice, 'shared post')
    client = app.test_client()

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        client.get(f'/tags/cloud/community/{community.id}')

    assert render.call_args.kwargs['tag_relationships'] == {alpha.id: {beta.id: 1},
                                                            beta.id: {alpha.id: 1}}


def test_tags_that_never_share_a_post_are_not_related(app, db_session):
    """`if cooccurrence_counts:` from the other side -- a tag with no
    neighbours is absent from the mapping rather than present with an empty
    one.
    """
    instance, alice, bob = _seed()
    community = make_community('microblogs')
    alpha = _tag('alpha')
    beta = _tag('beta')
    _tagged(alpha, community, alice, 'alpha post')
    _tagged(beta, community, alice, 'beta post')
    client = app.test_client()

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        client.get(f'/tags/cloud/community/{community.id}')

    assert render.call_args.kwargs['tag_relationships'] == {}


@pytest.mark.parametrize('column, value', [
    ('status', 0),
    ('private', True),
    ('deleted', True),
])
def test_a_post_the_cloud_does_not_count_does_not_relate_its_tags(app, db_session,
                                                                  column, value):
    """P5: the subquery answers the same question as the counting query, so it
    has to apply the same filters. Both tags are in the cloud on their own
    merits; only the post joining them is hidden.
    """
    instance, alice, bob = _seed()
    community = make_community('microblogs')
    alpha = _tag('alpha')
    beta = _tag('beta')
    _tagged(alpha, community, alice, 'alpha post')
    _tagged(beta, community, alice, 'beta post')
    _tagged([alpha, beta], community, alice, 'hidden shared post', **{column: value})
    client = app.test_client()

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        client.get(f'/tags/cloud/community/{community.id}')

    assert _names(render) == ['alpha', 'beta']
    assert render.call_args.kwargs['tag_relationships'] == {}


def test_the_empty_cloud_has_no_relationships_to_compute(app, db_session):
    """`if tag_ids:` -- the guard in front of the whole block."""
    instance, alice, bob = _seed()
    community = make_community('microblogs')
    client = app.test_client()

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        response = client.get(f'/tags/cloud/community/{community.id}')

    assert response.status_code == 200
    assert render.call_args.kwargs['tags_data'] == []
    assert render.call_args.kwargs['tag_relationships'] == {}


# --------------------------------------------------------------------------
# The three categories, the paging, and what the template is handed
# --------------------------------------------------------------------------


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
    (False, ['parenttag']),
    (True, ['childtag', 'parenttag']),
])
def test_a_topics_cloud_follows_the_tree_only_when_asked(app, db_session,
                                                         show_posts_in_children, expected):
    instance, alice, bob = _seed()
    parent = _topic('fediverse', show_posts_in_children=show_posts_in_children)
    child = _topic('microblogging', parent=parent)
    parent_community = _in_topic(parent, 'parentcomm')
    child_community = _in_topic(child, 'childcomm')
    _tagged(_tag('parenttag'), parent_community, alice, 'parent post')
    _tagged(_tag('childtag'), child_community, alice, 'child post')
    client = app.test_client()

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        client.get(f'/tags/cloud/topic/{parent.id}')

    assert _names(render) == expected


def test_a_topics_cloud_leaves_out_a_banned_community(app, db_session):
    """The topic branch's own SQL filters `banned is false`, and it is the
    reason the community branch's missing check was visible as an asymmetry."""
    instance, alice, bob = _seed()
    topic = _topic()
    good = _in_topic(topic, 'goodcomm')
    banned = _in_topic(topic, 'bannedcomm')
    banned.banned = True
    db.session.commit()
    _tagged(_tag('goodtag'), good, alice, 'good post')
    _tagged(_tag('bannedtag'), banned, alice, 'banned post')
    client = app.test_client()

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        client.get(f'/tags/cloud/topic/{topic.id}')

    assert _names(render) == ['goodtag']


@pytest.mark.parametrize('show_posts_in_children, expected', [
    (False, ['parenttag']),
    (True, ['childtag', 'parenttag']),
])
def test_a_feeds_cloud_follows_the_tree_only_when_asked(app, db_session,
                                                        show_posts_in_children, expected):
    from tests.factories import make_feed_item, make_local_feed
    instance, alice, bob = _seed()
    parent_feed = make_local_feed('parentfeed', public=True)
    parent_feed.show_posts_in_children = show_posts_in_children
    child_feed = make_local_feed('childfeed', public=True)
    child_feed.parent_feed_id = parent_feed.id
    db.session.commit()
    parent_community = make_community('parentcomm')
    child_community = make_community('childcomm')
    make_feed_item(parent_feed, parent_community)
    make_feed_item(child_feed, child_community)
    _tagged(_tag('parenttag'), parent_community, alice, 'parent post')
    _tagged(_tag('childtag'), child_community, alice, 'child post')
    client = app.test_client()

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        client.get(f'/tags/cloud/feed/{parent_feed.id}')

    assert _names(render) == expected


def test_the_cloud_shows_the_most_used_tag_first(app, db_session):
    instance, alice, bob = _seed()
    community = make_community('microblogs')
    busy = _tag('busytag')
    quiet = _tag('quiettag')
    _tagged(quiet, community, alice, 'one quiet post')
    for index in range(3):
        _tagged(busy, community, alice, f'busy post {index}')
    client = app.test_client()

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        client.get(f'/tags/cloud/community/{community.id}')

    order = [entry['text'] for entry in render.call_args.kwargs['tags_data']]
    assert order == ['busytag', 'quiettag']


def test_the_cloud_stops_at_fifty_tags(app, db_session):
    instance, alice, bob = _seed()
    community = make_community('microblogs')
    for index in range(51):
        _tagged(_tag(f'tag{index:03d}'), community, alice, f'post {index}')
    client = app.test_client()

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        client.get(f'/tags/cloud/community/{community.id}')

    assert len(render.call_args.kwargs['tags_data']) == 50


def test_the_list_view_is_paged_and_its_links_keep_the_category(app, db_session):
    """R1: the links deliberately carry view=list, because paging a word cloud
    is not a meaningful gesture. What they must not lose is which cloud this is.
    """
    instance, alice, bob = _seed()
    community = make_community('microblogs')
    for index in range(51):
        _tagged(_tag(f'tag{index:03d}'), community, alice, f'post {index}')
    client = app.test_client()

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        client.get(f'/tags/cloud/community/{community.id}?view=list')
        next_url = render.call_args.kwargs['next_url']
        assert next_url == f'/tags/cloud/community/{community.id}?view=list&page=2'
        assert render.call_args.kwargs['prev_url'] is None
        client.get(next_url)
        assert render.call_args.kwargs['prev_url'] == (
            f'/tags/cloud/community/{community.id}?view=list&page=1')
        assert render.call_args.kwargs['next_url'] is None
        assert len(render.call_args.kwargs['tag_list'].items) == 1


def test_a_cloud_that_fits_on_one_page_offers_no_links(app, db_session):
    instance, alice, bob = _seed()
    community = make_community('microblogs')
    _tagged(_tag(), community, alice, 'a post')
    client = app.test_client()

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        client.get(f'/tags/cloud/community/{community.id}')

    assert render.call_args.kwargs['next_url'] is None
    assert render.call_args.kwargs['prev_url'] is None


@pytest.mark.parametrize('view', ['cloud', 'list', None])
def test_the_requested_view_is_handed_to_the_template(app, db_session, view):
    """`view` decides which of the two renderings the template draws, and it
    defaults to the cloud.
    """
    instance, alice, bob = _seed()
    community = make_community('microblogs')
    _tagged(_tag(), community, alice, 'a post')
    client = app.test_client()
    query = f'?view={view}' if view else ''

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        client.get(f'/tags/cloud/community/{community.id}{query}')

    assert render.call_args.kwargs['view'] == (view or 'cloud')


@pytest.mark.parametrize('category', ['community', 'topic', 'feed'])
def test_the_subject_of_the_cloud_is_named_for_the_template(app, db_session, category):
    """community, topic and feed are three kwargs of which exactly one is ever
    set; the template uses whichever it finds to write the heading.
    """
    from tests.factories import make_feed_item, make_local_feed
    instance, alice, bob = _seed()
    community = make_community('microblogs')
    subject_ids = {'community': lambda: community.id}
    if category == 'topic':
        topic = _topic()
        community.topic_id = topic.id
        db.session.commit()
        subject_ids['topic'] = lambda: topic.id
    elif category == 'feed':
        feed = make_local_feed('localfeed', public=True)
        make_feed_item(feed, community)
        subject_ids['feed'] = lambda: feed.id
    _tagged(_tag(), community, alice, 'a post')
    client = app.test_client()

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        client.get(f'/tags/cloud/{category}/{subject_ids[category]()}')

    kwargs = render.call_args.kwargs
    assert kwargs[category] is not None
    assert [name for name in ('community', 'topic', 'feed') if kwargs[name] is not None] == [category]
    assert kwargs['category'] == category
    assert kwargs['title'] == f'{category.capitalize()} tags'


def test_a_private_instance_refuses_an_anonymous_reader_the_cloud(app, db_session):
    instance, alice, bob = _seed()
    site = db.session.get(Site, 1)
    site.private_instance = True
    db.session.commit()
    community = make_community('microblogs')
    client = app.test_client()

    response = client.get(f'/tags/cloud/community/{community.id}')

    assert response.status_code == 302
    assert '/auth/login' in response.headers['Location']


# --------------------------------------------------------------------------
# Three rows the mutation pass asked for
# --------------------------------------------------------------------------


def test_the_cloud_draws_the_tags_lowercase_name(app, db_session):
    """Tag.name is the lowercase form and Tag.display_as keeps the capitals.
    Every other row here uses a tag whose two forms are identical, so the
    mutant swapping them survived. The cloud currently draws `name`; whether a
    word cloud should show #SolarStorm rather than #solarstorm is a display
    decision, registered rather than changed under cover of a coverage round.
    """
    instance, alice, bob = _seed()
    community = make_community('microblogs')
    tag = _tag('SolarStorm')
    _tagged(tag, community, alice, 'a post')
    client = app.test_client()

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        client.get(f'/tags/cloud/community/{community.id}')

    assert tag.display_as == 'SolarStorm'
    assert _names(render) == ['solarstorm']


def test_a_private_community_in_a_topic_does_not_relate_its_tags(app, db_session):
    """The readability term inside the co-occurrence subquery, which needs a
    cloud spanning MORE THAN ONE community to be load-bearing: with a single
    private community the main query already returns nothing, tag_ids is empty,
    and the block never runs -- which is why the mutant dropping it survived.

    A topic's cloud spans every community in the topic, and the topic SQL
    filters only `banned is false`, so a private one is in community_ids. Both
    tags earn their place in the cloud from the public community; only the post
    joining them is out of reach.
    """
    instance, alice, bob = _seed()
    topic = _topic()
    public = _in_topic(topic, 'publiccomm')
    private = _in_topic(topic, 'privatecomm')
    private.private = True
    db.session.commit()
    alpha = _tag('alpha')
    beta = _tag('beta')
    _tagged(alpha, public, alice, 'alpha post')
    _tagged(beta, public, alice, 'beta post')
    _tagged([alpha, beta], private, alice, 'private shared post')
    client = app.test_client()

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        client.get(f'/tags/cloud/topic/{topic.id}')

    assert _names(render) == ['alpha', 'beta']
    assert render.call_args.kwargs['tag_relationships'] == {}


def test_a_tag_that_is_not_in_the_cloud_is_not_related_to_one_that_is(app, db_session):
    """`post_tag.c.tag_id.in_(tag_ids)` -- the co-occurrence query counts only
    the cloud's OWN tags, so a tag excluded from the cloud cannot appear in the
    relationship map and be drawn as a link to nothing. A banned tag is the
    reachable way to be excluded while still sharing a post.
    """
    instance, alice, bob = _seed()
    community = make_community('microblogs')
    shown = _tag('shown')
    banned = _tag('bannedtag', banned=True)
    _tagged([shown, banned], community, alice, 'shared post')
    client = app.test_client()

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        client.get(f'/tags/cloud/community/{community.id}')

    assert _names(render) == ['shown']
    assert render.call_args.kwargs['tag_relationships'] == {}


def test_a_topics_cloud_draws_relationships_at_all(app, db_session):
    """P6. `community_ids` for a topic came from `db.session.execute(...)
    .scalars()`, which is a ONE-SHOT cursor, and this function reads it twice:
    once for the counting query and again for the co-occurrence subquery. The
    second read got an exhausted iterator and rendered an empty IN, so a
    topic's cloud never drew a single link -- while the community and feed
    clouds, which build real lists, drew them correctly.

    Measured before the repair, same data, all three category types:

        DBG topic rel     {}
        DBG community rel {2: {1: 1}, 1: {2: 1}}
        DBG feed rel      {2: {1: 1}, 1: {2: 1}}

    Found because a mutant that dropped the readability term from the subquery
    would not die: the term could not matter in the only branch where the
    subquery could see more than one community.
    """
    instance, alice, bob = _seed()
    topic = _topic()
    community = _in_topic(topic, 'publiccomm')
    alpha = _tag('alpha')
    beta = _tag('beta')
    _tagged([alpha, beta], community, alice, 'shared post')
    client = app.test_client()

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        client.get(f'/tags/cloud/topic/{topic.id}')

    assert render.call_args.kwargs['tag_relationships'] == {alpha.id: {beta.id: 1},
                                                            beta.id: {alpha.id: 1}}

"""app/topic/routes.py -- the topic page, its feed, and the forms around it.

MEASUREMENT BASIS. The module stood at 48.197% on the full-suite --cov=app run
at c60264813, carrying 96 missing statements and 62 missing arcs.

Two defects are pinned here and repaired together:

  P1  `has_next_page = len(post_ids) > page + 1 * page_length` -- `*` binds
      tighter than `+`, so from page 1 on the reader is offered a next page
      that renders nothing. Page 0 agrees by arithmetic accident, which is why
      it survived. The same line is repaired in app/feed/routes.py.
  P2  a crafted community_id reached int() unguarded and was a 500.

Facts 292-294 apply: render_template is patched for anything that renders,
POST routes need a real CSRF token, and the site fixture supplies g.site.
"""
import pytest
from unittest.mock import patch

from flask import session
from flask_wtf.csrf import generate_csrf

from app import db
from app.models import Community, NotificationSubscription, Post, Site, Topic, User, utcnow
from tests.factories import make_community, make_instance, make_post, make_user

pytestmark = pytest.mark.usefixtures('site')


def login(client, user):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def csrf(app, client):
    with app.test_request_context():
        token = generate_csrf()
        raw = session['csrf_token']
    with client.session_transaction() as sess:
        sess['csrf_token'] = raw
    return token


def _seed():
    """Two local users and an open instance.

    Site.private_instance defaults True (app/models.py:4000) and show_topic is
    decorated with login_required_if_private_instance, so an anonymous test
    would otherwise measure the decorator.
    """
    instance = make_instance('test.piefed.local', software='piefed')
    burn = make_user(instance, 'burnseat', local=True)
    assert burn.id == 1
    alice = make_user(instance, 'alice', local=True)
    bob = make_user(instance, 'bob', local=True)
    site = Site.query.get(1)
    site.private_instance = False
    db.session.commit()
    return instance, alice, bob


def _topic(name='fediverse', parent=None, show_posts_in_children=False):
    topic = Topic(name=name.title(), machine_name=name, num_communities=0,
                  parent_id=parent.id if parent else None,
                  show_posts_in_children=show_posts_in_children)
    db.session.add(topic)
    db.session.commit()
    return topic


def _community_in(topic, name='microblogs', **kwargs):
    community = make_community(name)
    community.topic_id = topic.id
    community.total_subscriptions_count = 1
    for key, value in kwargs.items():
        setattr(community, key, value)
    db.session.commit()
    return community


def _posts(community, author, count, prefix='post'):
    made = []
    for index in range(count):
        made.append(make_post(community, author,
                              ap_id=f'https://test.piefed.local/post/{prefix}{index}',
                              title=f'{prefix} {index}'))
    return made


def _aged(user, days=30):
    """suggest_topics is gated on current_user.trustworthy(), which is false for
    an account created within 7 days whose reputation is under 100 (fact 307).
    """
    user.created = utcnow() - __import__('datetime').timedelta(days=days)
    db.session.commit()
    return user


# --------------------------------------------------------------------------
# P1: the next-page link
# --------------------------------------------------------------------------


@pytest.mark.parametrize('page, total, expects_next', [
    (0, 30, True),    # 30 posts, page 0 shows 20 -- there IS a second page
    (1, 30, False),   # page 1 shows the last 10 -- there is NOT a third
    (1, 45, True),    # page 1 shows 20 of 45 -- there IS a third
])
def test_the_next_link_appears_only_when_a_next_page_exists(app, db_session, page,
                                                            total, expects_next):
    """Before the repair, `page + 1 * page_length` read as `page +
    page_length`, so page 1 of 30 posts offered a page 2 that renders nothing:

        PROBE t1 page=1 total=30 actual_next=True intended_next=False

    Page 0 agrees under both readings, so the middle row is the one that
    pins the defect and the other two keep the repair honest.
    """
    instance, alice, bob = _seed()
    topic = _topic()
    community = _community_in(topic)
    _posts(community, alice, total)
    app.config['PAGE_LENGTH'] = 20
    client = app.test_client()

    with patch('app.topic.routes.render_template', return_value='rendered') as render:
        response = client.get(f'/topic/{topic.machine_name}?page={page}')

    assert response.status_code == 200
    assert (render.call_args.kwargs['next_url'] is not None) is expects_next


def test_the_previous_link_appears_on_every_page_but_the_first(app, db_session):
    instance, alice, bob = _seed()
    topic = _topic()
    community = _community_in(topic)
    _posts(community, alice, 45)
    app.config['PAGE_LENGTH'] = 20
    client = app.test_client()

    with patch('app.topic.routes.render_template', return_value='rendered') as render:
        client.get(f'/topic/{topic.machine_name}?page=0')
        assert render.call_args.kwargs['prev_url'] is None
        client.get(f'/topic/{topic.machine_name}?page=1')
        assert render.call_args.kwargs['prev_url'] is not None


# --------------------------------------------------------------------------
# P2: a crafted community_id
# --------------------------------------------------------------------------


def _submitter(user):
    """topic_create_post sits behind validation_required AND approval_required
    (fact 292's neighbourhood), so its user needs verified and a private_key.
    """
    user.verified = True
    user.private_key = 'a-private-key'
    db.session.commit()
    return user


def test_a_community_id_that_is_not_a_number_does_not_crash(app, db_session):
    """PROBE t3 exception: ValueError invalid literal for int() with base 10:
    'not-a-number'. A crafted request earns a page, not a traceback -- the
    value reads as absent and the route renders the chooser it already has.
    """
    instance, alice, bob = _seed()
    _submitter(alice)
    topic = _topic()
    client = app.test_client()
    login(client, alice)
    token = csrf(app, client)

    with patch('app.topic.routes.render_template', return_value='rendered') as render:
        response = client.post(f'/topic/{topic.machine_name}/submit',
                               data={'community_id': 'not-a-number', 'csrf_token': token})

    assert response.status_code == 200
    assert render.call_args.args[0] == 'topic/topic_create_post.html'


def test_a_real_community_id_still_redirects_to_the_join_page(app, db_session):
    """The other half of P2's inversion: a repair that dropped the branch would
    pass the test above.
    """
    instance, alice, bob = _seed()
    _submitter(alice)
    topic = _topic()
    community = _community_in(topic)
    client = app.test_client()
    login(client, alice)
    token = csrf(app, client)

    with patch('app.topic.routes.render_template', return_value='rendered'):
        response = client.post(f'/topic/{topic.machine_name}/submit',
                               data={'community_id': str(community.id), 'csrf_token': token})

    assert response.status_code == 302
    assert response.headers['Location'] == f'/community/{community.link()}/join_then_add'


# --------------------------------------------------------------------------
# show_topic: the topic tree and its breadcrumbs
# --------------------------------------------------------------------------


def test_a_nested_path_builds_a_breadcrumb_for_every_level(app, db_session):
    """The trail is built by walking the url's parts, and the LAST crumb gets
    an empty url because it is the page you are on. Two levels are what make
    that distinction visible.
    """
    instance, alice, bob = _seed()
    parent = _topic('technology')
    child = _topic('fediverse', parent=parent)
    client = app.test_client()

    with patch('app.topic.routes.render_template', return_value='rendered') as render:
        response = client.get('/topic/technology/fediverse')

    assert response.status_code == 200
    crumbs = render.call_args.kwargs['breadcrumbs']
    assert [crumb.text for crumb in crumbs] == ['Technology', 'Fediverse']
    assert crumbs[0].url == '/topic/technology'
    assert crumbs[1].url == ''
    assert render.call_args.kwargs['topic'].id == child.id


def test_a_path_naming_no_topic_is_a_404(app, db_session):
    instance, alice, bob = _seed()
    _topic('fediverse')
    client = app.test_client()

    assert client.get('/topic/nosuchtopic').status_code == 404
    assert client.get('/topic/fediverse/nosuchtopic').status_code == 404


def test_a_topic_showing_its_children_gathers_their_communities(app, db_session):
    """show_posts_in_children walks the whole subtree through
    get_all_child_topic_ids, so the fixture is three levels deep: a post in the
    GRANDCHILD is what proves the recursion rather than one hop.
    """
    instance, alice, bob = _seed()
    parent = _topic('technology', show_posts_in_children=True)
    child = _topic('fediverse', parent=parent)
    grandchild = _topic('activitypub', parent=child)
    deep_community = _community_in(grandchild, 'deep')
    _posts(deep_community, alice, 1, prefix='deep')
    client = app.test_client()

    with patch('app.topic.routes.render_template', return_value='rendered') as render:
        response = client.get('/topic/technology')

    assert response.status_code == 200
    assert [post.title for post in render.call_args.kwargs['posts']] == ['deep 0']


def test_a_topic_not_showing_children_ignores_their_posts(app, db_session):
    """The false arm of the same flag, and the row that keeps it load-bearing.
    """
    instance, alice, bob = _seed()
    parent = _topic('technology', show_posts_in_children=False)
    child = _topic('fediverse', parent=parent)
    child_community = _community_in(child, 'childcomm')
    _posts(child_community, alice, 1, prefix='child')
    client = app.test_client()

    with patch('app.topic.routes.render_template', return_value='rendered') as render:
        response = client.get('/topic/technology')

    assert response.status_code == 200
    assert list(render.call_args.kwargs['posts']) == []


def test_the_sub_topic_list_is_the_immediate_children_in_name_order(app, db_session):
    """sub_topics is ONE level, unlike the post gathering above, and it is
    ordered by name -- so the fixture creates them in the wrong order.
    """
    instance, alice, bob = _seed()
    parent = _topic('technology')
    _topic('zulu', parent=parent)
    _topic('alpha', parent=parent)
    grandchild_parent = _topic('fediverse', parent=parent)
    _topic('activitypub', parent=grandchild_parent)
    client = app.test_client()

    with patch('app.topic.routes.render_template', return_value='rendered') as render:
        client.get('/topic/technology')

    assert [t.name for t in render.call_args.kwargs['sub_topics']] == \
        ['Alpha', 'Fediverse', 'Zulu']


def test_get_all_child_topic_ids_returns_the_whole_subtree(app, db_session):
    from app.topic.routes import get_all_child_topic_ids
    instance, alice, bob = _seed()
    parent = _topic('technology')
    child = _topic('fediverse', parent=parent)
    grandchild = _topic('activitypub', parent=child)
    sibling = _topic('linux', parent=parent)
    unrelated = _topic('cooking')

    ids = get_all_child_topic_ids(parent)

    assert sorted(ids) == sorted([parent.id, child.id, grandchild.id, sibling.id])
    assert unrelated.id not in ids


# --------------------------------------------------------------------------
# show_topic: which communities and posts are visible
# --------------------------------------------------------------------------


def test_a_banned_community_contributes_nothing(app, db_session):
    instance, alice, bob = _seed()
    topic = _topic()
    visible = _community_in(topic, 'visible')
    banned = _community_in(topic, 'bannedcomm', banned=True)
    _posts(visible, alice, 1, prefix='ok')
    _posts(banned, alice, 1, prefix='banned')
    client = app.test_client()

    with patch('app.topic.routes.render_template', return_value='rendered') as render:
        client.get(f'/topic/{topic.machine_name}')

    assert [p.title for p in render.call_args.kwargs['posts']] == ['ok 0']
    assert [c.name for c in render.call_args.kwargs['topic_communities']] == ['visible']


def _private_community_fixture():
    """A private community holding one post, with alice a member of it."""
    from app.models import CommunityMember
    instance, alice, bob = _seed()
    topic = _topic()
    private = _community_in(topic, 'privatecomm', private=True)
    _posts(private, alice, 1, prefix='secret')
    # community_membership_private (app/utils.py:5359-5364) reads `is_banned is
    # false` in raw SQL, which does not match NULL, so the row has to say so
    db.session.add(CommunityMember(user_id=alice.id, community_id=private.id,
                                   is_banned=False))
    db.session.commit()
    return topic, alice


def test_a_private_community_is_hidden_from_a_stranger(app, db_session):
    """ONE AUTHORISATION PHASE PER TEST, AND THE REASON IS THE HARNESS.

    This pair used to be one test that read the page anonymously and then
    logged in. It cannot be: **a manual session login does not take on any
    client created after an anonymous request has already been served in the
    same test.** Measured -- two logged-in clients in one test are fine, and a
    login-first client is fine, but after one anonymous request the next
    client's `current_user.get_id()` reads None inside the route although the
    session it was given holds `_user_id`. Flask-Login's session protection is
    'basic' here, so that is not the cause and the cause is not established;
    what is established is the shape. Fact 314.
    """
    topic, alice = _private_community_fixture()

    with patch('app.topic.routes.render_template', return_value='rendered') as render:
        response = app.test_client().get(f'/topic/{topic.machine_name}')

    assert response.status_code == 200
    assert list(render.call_args.kwargs['posts']) == []
    assert list(render.call_args.kwargs['topic_communities']) == []


def test_a_private_community_is_shown_to_one_of_its_members(app, db_session):
    """The other half, in its own test for the reason above. The private
    community's ids are threaded into the post query as a bind parameter, so
    this is the row that proves the parameter carries the membership."""
    topic, alice = _private_community_fixture()
    client = app.test_client()
    login(client, alice)

    with patch('app.topic.routes.render_template', return_value='rendered') as render:
        response = client.get(f'/topic/{topic.machine_name}')

    assert response.status_code == 200
    assert [p.title for p in render.call_args.kwargs['posts']] == ['secret 0']
    assert [c.name for c in render.call_args.kwargs['topic_communities']] == ['privatecomm']


def test_a_community_with_no_subscribers_is_left_out_of_the_sidebar(app, db_session):
    """topic_communities filters on total_subscriptions_count > 0 while the
    post query does not, so a community with no subscribers still contributes
    posts -- which is what this row records.
    """
    instance, alice, bob = _seed()
    topic = _topic()
    quiet = _community_in(topic, 'quiet', total_subscriptions_count=0)
    _posts(quiet, alice, 1, prefix='quiet')
    client = app.test_client()

    with patch('app.topic.routes.render_template', return_value='rendered') as render:
        client.get(f'/topic/{topic.machine_name}')

    assert list(render.call_args.kwargs['topic_communities']) == []
    assert [p.title for p in render.call_args.kwargs['posts']] == ['quiet 0']


# --------------------------------------------------------------------------
# show_topic: the comments tab and the reader's own settings
# --------------------------------------------------------------------------


def _reply(community, author, post, body='a comment', **kwargs):
    from app.models import PostReply
    reply = PostReply(user_id=author.id, post_id=post.id, community_id=community.id,
                      body=body, body_html=f'<p>{body}</p>', from_bot=False,
                      nsfw=False, deleted=False, posted_at=utcnow(),
                      ap_id=f'https://test.piefed.local/comment/{body.replace(" ", "")}')
    for key, value in kwargs.items():
        setattr(reply, key, value)
    db.session.add(reply)
    db.session.commit()
    return reply


def test_the_comments_tab_lists_the_topics_comments(app, db_session):
    instance, alice, bob = _seed()
    topic = _topic()
    community = _community_in(topic)
    post = _posts(community, alice, 1)[0]
    _reply(community, bob, post, 'hello there')
    client = app.test_client()

    with patch('app.topic.routes.render_template', return_value='rendered') as render:
        response = client.get(f'/topic/{topic.machine_name}?content_type=comments')

    assert response.status_code == 200
    assert [c.body for c in render.call_args.kwargs['comments'].items] == ['hello there']
    assert render.call_args.kwargs['posts'] is None


def test_an_anonymous_reader_never_sees_bot_or_deleted_comments(app, db_session):
    """The anonymous comment filter is three predicates in one call, so the
    fixture supplies one comment of each kind plus a good one -- with a single
    comment, any subset of the filter would pass.
    """
    instance, alice, bob = _seed()
    topic = _topic()
    community = _community_in(topic)
    post = _posts(community, alice, 1)[0]
    _reply(community, bob, post, 'good one')
    _reply(community, bob, post, 'bot one', from_bot=True)
    _reply(community, bob, post, 'deleted one', deleted=True)
    _reply(community, bob, post, 'nsfw one', nsfw=True)
    client = app.test_client()

    with patch('app.topic.routes.render_template', return_value='rendered') as render:
        client.get(f'/topic/{topic.machine_name}?content_type=comments')

    assert [c.body for c in render.call_args.kwargs['comments'].items] == ['good one']


def test_a_reader_who_wants_bots_and_nsfw_sees_them(app, db_session):
    """The authenticated arm reads the reader's own preferences, so the two
    flags are turned OFF here -- the opposite of the anonymous defaults above.
    """
    instance, alice, bob = _seed()
    alice.ignore_bots = 0
    alice.hide_nsfw = 0
    db.session.commit()
    topic = _topic()
    community = _community_in(topic)
    post = _posts(community, alice, 1)[0]
    _reply(community, bob, post, 'bot one', from_bot=True)
    _reply(community, bob, post, 'nsfw one', nsfw=True)
    _reply(community, bob, post, 'deleted one', deleted=True)
    client = app.test_client()
    login(client, alice)

    with patch('app.topic.routes.render_template', return_value='rendered') as render:
        client.get(f'/topic/{topic.machine_name}?content_type=comments')

    bodies = [c.body for c in render.call_args.kwargs['comments'].items]
    assert sorted(bodies) == ['bot one', 'nsfw one']


def test_a_reader_who_hides_bots_and_nsfw_does_not_see_them(app, db_session):
    instance, alice, bob = _seed()
    alice.ignore_bots = 1
    alice.hide_nsfw = 1
    db.session.commit()
    topic = _topic()
    community = _community_in(topic)
    post = _posts(community, alice, 1)[0]
    _reply(community, bob, post, 'good one')
    _reply(community, bob, post, 'bot one', from_bot=True)
    _reply(community, bob, post, 'nsfw one', nsfw=True)
    client = app.test_client()
    login(client, alice)

    with patch('app.topic.routes.render_template', return_value='rendered') as render:
        client.get(f'/topic/{topic.machine_name}?content_type=comments')

    assert [c.body for c in render.call_args.kwargs['comments'].items] == ['good one']


def test_a_blocked_author_and_a_blocked_instance_are_filtered_from_comments(app, db_session):
    """Two filters that only run when the reader has something blocked, so the
    fixture blocks one user and one instance and leaves a third comment
    visible.
    """
    from tests.factories import make_instance_block, make_user_block
    instance, alice, bob = _seed()
    peer = make_instance('remote.example', software='lemmy')
    remote_author = make_user(peer, 'remote', local=False)
    topic = _topic()
    community = _community_in(topic)
    post = _posts(community, alice, 1)[0]
    _reply(community, bob, post, 'blocked author')
    _reply(community, remote_author, post, 'blocked instance', instance_id=peer.id)
    _reply(community, alice, post, 'visible one')
    make_user_block(alice, bob)
    make_instance_block(alice, peer)
    db.session.commit()
    client = app.test_client()
    login(client, alice)

    with patch('app.topic.routes.render_template', return_value='rendered') as render:
        client.get(f'/topic/{topic.machine_name}?content_type=comments')

    assert [c.body for c in render.call_args.kwargs['comments'].items] == ['visible one']


@pytest.mark.parametrize('sort', ['', 'hot', 'new', 'active', 'old', 'top', 'top_12h',
                                  'top_1w', 'top_1m', 'top_1y', 'top_all'])
def test_every_comment_sort_is_accepted(app, db_session, sort):
    """Eleven named sorts, each its own branch. The assertion is that the page
    renders and the query ran -- what each one orders by is the database's
    business, and asserting an order would need a fixture per sort without
    telling us anything the branch does not.
    """
    instance, alice, bob = _seed()
    topic = _topic()
    community = _community_in(topic)
    post = _posts(community, alice, 1)[0]
    _reply(community, bob, post, 'recent one')
    client = app.test_client()

    with patch('app.topic.routes.render_template', return_value='rendered') as render:
        response = client.get(f'/topic/{topic.machine_name}?content_type=comments&sort={sort}')

    assert response.status_code == 200
    assert render.call_args.kwargs['sort'] == sort


def test_a_time_limited_sort_drops_older_comments(app, db_session):
    """top_12h is the shortest window, so one comment inside it and one outside
    is what proves the filter rather than the ordering.
    """
    from datetime import timedelta
    instance, alice, bob = _seed()
    topic = _topic()
    community = _community_in(topic)
    post = _posts(community, alice, 1)[0]
    _reply(community, bob, post, 'fresh one')
    _reply(community, bob, post, 'stale one', posted_at=utcnow() - timedelta(days=2))
    client = app.test_client()

    with patch('app.topic.routes.render_template', return_value='rendered') as render:
        client.get(f'/topic/{topic.machine_name}?content_type=comments&sort=top_12h')

    assert [c.body for c in render.call_args.kwargs['comments'].items] == ['fresh one']


def test_an_unknown_content_type_is_a_400(app, db_session):
    instance, alice, bob = _seed()
    topic = _topic()
    _community_in(topic)
    client = app.test_client()

    assert client.get(f'/topic/{topic.machine_name}?content_type=nonsense').status_code == 400


@pytest.mark.parametrize('layout, expected_length', [
    ('masonry', 200),
    ('masonry_wide', 300),
])
def test_a_masonry_layout_asks_for_a_longer_page(app, db_session, layout, expected_length):
    """Both layout branches change page_length, which is visible through the
    next link: with fewer posts than the long page holds, there is no next
    page at all.
    """
    instance, alice, bob = _seed()
    topic = _topic()
    community = _community_in(topic)
    _posts(community, alice, 25)
    app.config['PAGE_LENGTH'] = 20
    client = app.test_client()

    with patch('app.topic.routes.render_template', return_value='rendered') as render:
        client.get(f'/topic/{topic.machine_name}?layout={layout}')

    assert render.call_args.kwargs['next_url'] is None
    assert render.call_args.kwargs['post_layout'] == layout


def test_a_low_bandwidth_reader_gets_a_short_page_and_no_layout(app, db_session):
    """The low_bandwidth cookie decides two things at once -- the page length
    and the default layout -- so both are asserted.
    """
    instance, alice, bob = _seed()
    topic = _topic()
    community = _community_in(topic)
    _posts(community, alice, 25)
    app.config['PAGE_LENGTH'] = 100
    client = app.test_client()
    # THE COOKIE'S DOMAIN HAS TO BE THE APP'S SERVER_NAME. Fact 300 records
    # that this client "delivers no cookies at all"; it does, but only when the
    # jar's domain matches the host the client requests, which is
    # config['SERVER_NAME'] -- test.piefed.local -- and not localhost. A
    # Cookie: header is dropped outright, so the jar is the only way in.
    client.set_cookie('low_bandwidth', '1', domain='test.piefed.local')

    with patch('app.topic.routes.render_template', return_value='rendered') as render:
        client.get(f'/topic/{topic.machine_name}')

    assert render.call_args.kwargs['post_layout'] is None
    assert render.call_args.kwargs['next_url'] is not None   # 25 posts, 20 per page


def test_a_readers_own_page_length_wins_when_it_is_shorter(app, db_session):
    """`if current_user.page_length and current_user.page_length < page_length`
    -- both operands matter, so the shorter preference is applied and a LONGER
    one is ignored.
    """
    instance, alice, bob = _seed()
    topic = _topic()
    community = _community_in(topic)
    # 25 posts, so a LONGER preference than the site's is visible: under the
    # repair the site's 20 wins and there is a next page, and a mutant that
    # took the preference unconditionally would show 50 per page and none
    _posts(community, alice, 25)
    app.config['PAGE_LENGTH'] = 20
    alice.page_length = 10
    db.session.commit()
    client = app.test_client()
    login(client, alice)

    with patch('app.topic.routes.render_template', return_value='rendered') as render:
        client.get(f'/topic/{topic.machine_name}?page=2')
        assert render.call_args.kwargs['next_url'] is None       # 25 posts, 10 per page

        # a LONGER preference is ignored, and page 0 is where that shows: the
        # site's 20 leaves a second page, while 50 would swallow all 25 posts
        alice.page_length = 50
        db.session.commit()
        client.get(f'/topic/{topic.machine_name}?page=0')
        assert render.call_args.kwargs['next_url'] is not None


def test_the_scaled_sort_is_read_as_the_default(app, db_session):
    """`if sort == 'scaled': sort = ''` -- the topic page has no scaled sort, so
    a reader whose default is scaled gets the default listing rather than an
    empty one.
    """
    instance, alice, bob = _seed()
    alice.default_sort = 'scaled'
    db.session.commit()
    topic = _topic()
    community = _community_in(topic)
    _posts(community, alice, 1)
    client = app.test_client()
    login(client, alice)

    with patch('app.topic.routes.render_template', return_value='rendered') as render:
        client.get(f'/topic/{topic.machine_name}')

    assert render.call_args.kwargs['sort'] == ''
    assert [p.title for p in render.call_args.kwargs['posts']] == ['post 0']


def test_a_logged_in_reader_gets_their_voting_history(app, db_session):
    """The authenticated block fills four values the template needs; anonymous
    readers get empty ones. Both rows are here because the else arm is what
    every anonymous test above exercises without asserting.
    """
    from tests.factories import make_post_vote
    instance, alice, bob = _seed()
    topic = _topic()
    community = _community_in(topic)
    post = _posts(community, alice, 1)[0]
    make_post_vote(alice, post, 1)
    client = app.test_client()
    login(client, alice)

    with patch('app.topic.routes.render_template', return_value='rendered') as render:
        client.get(f'/topic/{topic.machine_name}')

    assert post.id in render.call_args.kwargs['recently_upvoted']
    assert render.call_args.kwargs['recently_downvoted'] == []
    assert render.call_args.kwargs['communities_banned_from_list'] == []


def test_an_anonymous_reader_gets_no_voting_history(app, db_session):
    instance, alice, bob = _seed()
    topic = _topic()
    _community_in(topic)
    client = app.test_client()

    with patch('app.topic.routes.render_template', return_value='rendered') as render:
        client.get(f'/topic/{topic.machine_name}')

    assert render.call_args.kwargs['recently_upvoted'] == []
    assert render.call_args.kwargs['content_filters'] == {}


# --------------------------------------------------------------------------
# show_topic_rss
# --------------------------------------------------------------------------


def test_the_feed_carries_the_topics_posts(app, db_session):
    """The feed is built by hand, entry by entry, so the assertions read the
    XML rather than the route's arguments -- there is no render call to inspect.
    """
    instance, alice, bob = _seed()
    topic = _topic()
    community = _community_in(topic)
    _posts(community, alice, 2, prefix='news')
    client = app.test_client()

    response = client.get(f'/topic/{topic.machine_name}.rss')
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert response.headers['Content-Type'] == 'application/rss+xml'
    assert 'ETag' in response.headers
    assert response.headers['Cache-Control'] == 'no-cache, max-age=600, must-revalidate'
    assert '<title>news 0</title>' in body
    assert '<title>news 1</title>' in body
    assert f'{topic.name} on ' in body


def test_a_feed_entry_links_to_the_posts_slug_or_its_id(app, db_session):
    """Both arms of `if post.slug`. The two posts differ only in whether they
    carry one, and the urls they produce are different shapes.
    """
    instance, alice, bob = _seed()
    topic = _topic()
    community = _community_in(topic)
    with_slug, without = _posts(community, alice, 2, prefix='slugtest')
    with_slug.slug = '/post/slugged-one'
    without.slug = None
    db.session.commit()
    client = app.test_client()

    body = client.get(f'/topic/{topic.machine_name}.rss').get_data(as_text=True)

    assert 'https://test.piefed.local/post/slugged-one' in body
    assert f'https://test.piefed.local/post/{without.id}' in body


def test_a_feed_entry_encloses_a_media_url_but_not_a_web_page(app, db_session):
    """`if type and not type.startswith('text/')` -- three posts, one linking to
    an image, one to an HTML page and one with no url at all, so both operands
    of the guard are separated.
    """
    instance, alice, bob = _seed()
    topic = _topic()
    community = _community_in(topic)
    image_post, page_post, plain_post = _posts(community, alice, 3, prefix='enclosure')
    image_post.url = 'https://example.test/picture.jpg'
    page_post.url = 'https://example.test/article.html'
    plain_post.url = None
    db.session.commit()
    client = app.test_client()

    body = client.get(f'/topic/{topic.machine_name}.rss').get_data(as_text=True)

    assert 'https://example.test/picture.jpg' in body
    assert 'enclosure' in body
    assert 'article.html' not in body


def test_the_feed_gathers_child_topics_when_the_topic_says_so(app, db_session):
    instance, alice, bob = _seed()
    parent = _topic('technology', show_posts_in_children=True)
    child = _topic('fediverse', parent=parent)
    _posts(_community_in(child, 'childcomm'), alice, 1, prefix='child')
    client = app.test_client()

    body = client.get('/topic/technology.rss').get_data(as_text=True)

    assert '<title>child 0</title>' in body


def test_the_feed_of_a_topic_that_keeps_to_itself_omits_child_posts(app, db_session):
    instance, alice, bob = _seed()
    parent = _topic('technology', show_posts_in_children=False)
    child = _topic('fediverse', parent=parent)
    _posts(_community_in(child, 'childcomm'), alice, 1, prefix='child')
    client = app.test_client()

    body = client.get('/topic/technology.rss').get_data(as_text=True)

    assert '<title>child 0</title>' not in body


def test_the_feed_never_carries_a_private_communitys_posts(app, db_session):
    """The feed's own SQL has no membership parameter at all -- it is public by
    construction -- so a private community's post must not appear even for a
    member, and there is no logged-in variant of this route to try.
    """
    instance, alice, bob = _seed()
    topic = _topic()
    private = _community_in(topic, 'privatecomm', private=True)
    _posts(private, alice, 1, prefix='secret')
    client = app.test_client()

    body = client.get(f'/topic/{topic.machine_name}.rss').get_data(as_text=True)

    assert 'secret 0' not in body


def test_a_feed_for_no_topic_is_a_404(app, db_session):
    instance, alice, bob = _seed()
    client = app.test_client()

    assert client.get('/topic/nosuchtopic.rss').status_code == 404


# --------------------------------------------------------------------------
# topic_create_post, topic_notification, suggest_topics, suggestion_denied
# --------------------------------------------------------------------------


def test_the_submit_page_lists_the_topics_communities_and_its_childrens(app, db_session):
    """The two lists are built separately -- the topic's own communities and
    those of its children -- and each is ordered by title, so the fixture
    creates them in the wrong order.
    """
    instance, alice, bob = _seed()
    _submitter(alice)
    parent = _topic('technology')
    child = _topic('fediverse', parent=parent)
    _community_in(parent, 'zebra')
    _community_in(parent, 'antelope')
    _community_in(child, 'childcomm')
    _community_in(parent, 'bannedcomm', banned=True)
    client = app.test_client()
    login(client, alice)

    with patch('app.topic.routes.render_template', return_value='rendered') as render:
        response = client.get('/topic/technology/submit')

    assert response.status_code == 200
    assert [c.name for c in render.call_args.kwargs['communities']] == ['antelope', 'zebra']
    assert [c.name for c in render.call_args.kwargs['sub_communities']] == ['childcomm']


def test_submitting_to_an_unknown_topic_is_a_404(app, db_session):
    instance, alice, bob = _seed()
    _submitter(alice)
    client = app.test_client()
    login(client, alice)

    assert client.get('/topic/nosuchtopic/submit').status_code == 404


def test_submitting_to_an_unknown_community_is_a_404(app, db_session):
    instance, alice, bob = _seed()
    _submitter(alice)
    topic = _topic()
    client = app.test_client()
    login(client, alice)
    token = csrf(app, client)

    response = client.post(f'/topic/{topic.machine_name}/submit',
                           data={'community_id': '9999', 'csrf_token': token})

    assert response.status_code == 404


def test_the_notification_toggle_subscribes_and_unsubscribes(app, db_session):
    """The route is a toggle, so both directions are one test -- and the second
    POST proves the delete arm rather than a second row being created.
    """
    instance, alice, bob = _seed()
    topic = _topic()
    client = app.test_client()
    login(client, alice)
    token = csrf(app, client)

    with patch('app.topic.routes.render_template', return_value='rendered') as render:
        response = client.post(f'/topic/{topic.id}/notification',
                               data={'csrf_token': token})
        assert response.status_code == 200
        assert render.call_args.args[0] == 'topic/_notification_toggle.html'
        subscription = NotificationSubscription.query.one()
        assert subscription.user_id == alice.id
        assert subscription.entity_id == topic.id
        assert subscription.name == topic.name

        client.post(f'/topic/{topic.id}/notification', data={'csrf_token': token})
        assert NotificationSubscription.query.count() == 0


def test_the_notification_toggle_fires_on_a_GET_as_well(app, db_session):
    """D778: the route accepts GET and mutates on it, so any <img src> pointed
    at this url toggles a logged-in reader's subscription. The template's href
    is the no-JS fallback and hx-post is the path a browser with JS takes.
    Recorded as behaviour; the refusal is a product decision.
    """
    instance, alice, bob = _seed()
    topic = _topic()
    client = app.test_client()
    login(client, alice)

    with patch('app.topic.routes.render_template', return_value='rendered'):
        response = client.get(f'/topic/{topic.id}/notification')

    assert response.status_code == 200
    assert NotificationSubscription.query.count() == 1


def test_a_notification_for_an_unknown_topic_is_a_404(app, db_session):
    instance, alice, bob = _seed()
    client = app.test_client()
    login(client, alice)

    assert client.get('/topic/9999/notification').status_code == 404


def test_a_suggestion_is_sent_to_the_site_contact(app, db_session):
    instance, alice, bob = _seed()
    _aged(alice)
    site = Site.query.get(1)
    site.contact_email = 'admin@test.piefed.local'
    db.session.commit()
    client = app.test_client()
    login(client, alice)
    token = csrf(app, client)

    with patch('app.topic.routes.send_topic_suggestion') as sender, \
         patch('app.topic.routes.render_template', return_value='rendered'):
        response = client.post('/topics/new',
                               data={'topic_name': 'Gardening',
                                     'communities_for_topic': 'a\nb',
                                     'csrf_token': token})

    assert response.status_code == 302
    assert response.headers['Location'] == '/topics'
    assert sender.call_count == 1
    assert sender.call_args.args[0] == 'a\nb'
    assert sender.call_args.args[2] == 'admin@test.piefed.local'
    assert sender.call_args.args[4] == 'Gardening'


def test_the_suggestion_form_renders_for_a_trustworthy_reader(app, db_session):
    instance, alice, bob = _seed()
    _aged(alice)
    client = app.test_client()
    login(client, alice)

    with patch('app.topic.routes.render_template', return_value='rendered') as render:
        response = client.get('/topics/new')

    assert response.status_code == 200
    assert render.call_args.args[0] == 'topic/suggest_topics.html'


def test_a_new_account_cannot_suggest_topics(app, db_session):
    """trustworthy() is false for an account created within 7 days whose
    reputation is under 100 (fact 307), which is what every fresh fixture user
    is -- so this row needs no setup at all, and the row above needs ageing.
    """
    instance, alice, bob = _seed()
    client = app.test_client()
    login(client, alice)

    response = client.get('/topics/new')

    assert response.status_code == 302
    assert response.headers['Location'] == '/topic/suggestion-denied'


def test_a_suggestion_that_fails_validation_sends_nothing(app, db_session):
    instance, alice, bob = _seed()
    _aged(alice)
    client = app.test_client()
    login(client, alice)
    token = csrf(app, client)

    with patch('app.topic.routes.send_topic_suggestion') as sender, \
         patch('app.topic.routes.render_template', return_value='rendered') as render:
        response = client.post('/topics/new', data={'topic_name': '', 'csrf_token': token})

    assert response.status_code == 200
    assert render.call_args.args[0] == 'topic/suggest_topics.html'
    assert sender.call_count == 0


def test_the_denial_page_renders(app, db_session):
    instance, alice, bob = _seed()
    client = app.test_client()
    login(client, alice)

    with patch('app.topic.routes.render_template', return_value='rendered') as render:
        response = client.get('/topic/suggestion-denied')

    assert response.status_code == 200
    assert render.call_args.args[0] == 'topic/suggestion_denied.html'


def test_an_unrecognised_comment_sort_leaves_the_order_to_the_database(app, db_session):
    """The sort chain has no else, so a sort nobody defined falls through it
    and the query is paginated unordered. That fall-through is its own arc.
    """
    instance, alice, bob = _seed()
    topic = _topic()
    community = _community_in(topic)
    post = _posts(community, alice, 1)[0]
    _reply(community, bob, post, 'whatever')
    client = app.test_client()

    with patch('app.topic.routes.render_template', return_value='rendered') as render:
        response = client.get(f'/topic/{topic.machine_name}?content_type=comments&sort=nonsense')

    assert response.status_code == 200
    assert [c.body for c in render.call_args.kwargs['comments'].items] == ['whatever']


def test_a_content_warning_site_hides_only_bots_and_deletions_from_anonymous_readers(app, db_session):
    """The CONTENT_WARNING arm of the anonymous comment filter drops the nsfw
    predicate -- a site that shows a warning interstitial has already asked the
    reader about it. The config is patched at the MODULE's current_app, since
    setting it on the app would also arm whatever else reads it.
    """
    instance, alice, bob = _seed()
    topic = _topic()
    community = _community_in(topic)
    post = _posts(community, alice, 1)[0]
    _reply(community, bob, post, 'nsfw one', nsfw=True)
    _reply(community, bob, post, 'bot one', from_bot=True)
    _reply(community, bob, post, 'plain one')
    client = app.test_client()

    # Setting the config itself arms login_required_if_private_instance, which
    # reads the same key and redirects to /content_warning before the route runs
    # (fact 299's shape). Patching the MODULE's current_app leaves the
    # decorator's own view of the config alone and gives the route the value
    # under test.
    from unittest.mock import MagicMock
    module_app = MagicMock()
    module_app.config = dict(app.config, CONTENT_WARNING='this site contains adult content')
    with patch('app.topic.routes.render_template', return_value='rendered') as render, \
         patch('app.topic.routes.current_app', module_app):
        response = client.get(f'/topic/{topic.machine_name}?content_type=comments')

    assert response.status_code == 200
    bodies = sorted(c.body for c in render.call_args.kwargs['comments'].items)
    assert bodies == ['nsfw one', 'plain one']


def test_the_closing_abort_is_unreachable(app, db_session):
    """THE MODULE'S ONE RESIDUAL, PROVED.

    show_topic ends `if current_topic: ... else: abort(404)` at :211, and
    `current_topic` is the loop variable `topic` assigned at :66. The loop runs
    over `topic_path.split('/')`, and **str.split never returns an empty list**
    -- the emptiest answer is `['']`, one iteration. That iteration either
    finds a topic or aborts at :63. So reaching :211 with a falsy
    `current_topic` is impossible: the only path that leaves it falsy has
    already raised.

    Demonstrated on the value rather than argued: the empty path gives one
    part, and the route answers 404 from the loop's abort -- the flask 404, not
    the closing one, which nothing can distinguish from the outside, hence the
    split assertion below as the actual proof.
    """
    instance, alice, bob = _seed()
    client = app.test_client()

    assert ''.split('/') == ['']
    assert len(''.split('/')) == 1
    assert client.get('/topic/').status_code == 404


# --------------------------------------------------------------------------
# Rows added to close mutation survivors
# --------------------------------------------------------------------------


def test_a_path_in_capitals_with_padding_still_finds_its_topics(app, db_session):
    """`url_part.strip().lower()` -- urls arrive from links people type and
    from peers that capitalise, and every machine_name is stored lower case.
    """
    instance, alice, bob = _seed()
    parent = _topic('technology')
    _topic('fediverse', parent=parent)
    client = app.test_client()

    with patch('app.topic.routes.render_template', return_value='rendered') as render:
        response = client.get('/topic/Technology/%20FEDIVERSE%20')

    assert response.status_code == 200
    assert [crumb.text for crumb in render.call_args.kwargs['breadcrumbs']] == \
        ['Technology', 'Fediverse']


def test_a_three_level_breadcrumb_accumulates_the_path(app, db_session):
    """`existing_url = breadcrumb.url` is what makes the trail cumulative, and
    it only shows from the THIRD level: with two, the first crumb is the root
    and the second is the page itself, so nothing has accumulated yet.
    """
    instance, alice, bob = _seed()
    parent = _topic('technology')
    child = _topic('fediverse', parent=parent)
    _topic('activitypub', parent=child)
    client = app.test_client()

    with patch('app.topic.routes.render_template', return_value='rendered') as render:
        client.get('/topic/technology/fediverse/activitypub')

    crumbs = render.call_args.kwargs['breadcrumbs']
    assert [crumb.url for crumb in crumbs] == \
        ['/topic/technology', '/topic/technology/fediverse', '']


def test_the_sidebar_lists_the_busiest_community_first(app, db_session):
    """order_by(desc(total_subscriptions_count)) -- the fixture creates them in
    ascending order, so the arrival order cannot satisfy the assertion.
    """
    instance, alice, bob = _seed()
    topic = _topic()
    _community_in(topic, 'quiet', total_subscriptions_count=1)
    _community_in(topic, 'busy', total_subscriptions_count=50)
    client = app.test_client()

    with patch('app.topic.routes.render_template', return_value='rendered') as render:
        client.get(f'/topic/{topic.machine_name}')

    assert [c.name for c in render.call_args.kwargs['topic_communities']] == ['busy', 'quiet']


def test_the_comments_tab_shows_only_this_topics_comments(app, db_session):
    """The comment query is filtered to the topic's communities, and the only
    way to see that is a comment in a community belonging to ANOTHER topic.
    """
    instance, alice, bob = _seed()
    topic = _topic()
    other_topic = _topic('cooking')
    community = _community_in(topic)
    elsewhere = _community_in(other_topic, 'kitchen')
    post = _posts(community, alice, 1)[0]
    other_post = _posts(elsewhere, alice, 1, prefix='other')[0]
    _reply(community, bob, post, 'in this topic')
    _reply(elsewhere, bob, other_post, 'in another topic')
    client = app.test_client()

    with patch('app.topic.routes.render_template', return_value='rendered') as render:
        client.get(f'/topic/{topic.machine_name}?content_type=comments')

    assert [c.body for c in render.call_args.kwargs['comments'].items] == ['in this topic']


def test_a_url_with_no_recognisable_type_is_not_enclosed(app, db_session):
    """`if type and not type.startswith('text/')` -- the first operand is what
    keeps a url mimetype_from_url cannot classify from reaching .startswith on
    None, so this row is a crash test as much as a filter test.
    """
    instance, alice, bob = _seed()
    topic = _topic()
    community = _community_in(topic)
    post = _posts(community, alice, 1, prefix='untyped')[0]
    post.url = 'https://example.test/thing.unknownextension'
    db.session.commit()
    client = app.test_client()

    response = client.get(f'/topic/{topic.machine_name}.rss')

    assert response.status_code == 200
    assert 'enclosure' not in response.get_data(as_text=True)


def test_the_private_community_placeholder_and_the_banned_filter_are_redundant(app, db_session):
    """TWO OF THE ROUND'S THREE EQUIVALENT MUTANTS, PROVED.

    show_topic's own SQL asks for `banned is false` and `(private is false OR id
    IN :private_communities)`, and then hands the surviving ids to
    get_deduped_post_ids -- which asks BOTH again (app/utils.py: 'c.banned is
    false' unconditionally, and 'c.private is false' for an anonymous reader).
    So dropping either from the route's query changes nothing that reaches the
    page, and the placeholder `[0, 0]` for a reader with no private memberships
    is likewise unobservable: `IN (0)` and `IN (0, 0)` select the same nothing.

    This test asserts the OUTCOME the duplication guarantees -- a banned
    community and a private one both contribute nothing to an anonymous
    reader -- so it still fails if the day comes that the util's filters move.
    """
    instance, alice, bob = _seed()
    topic = _topic()
    banned = _community_in(topic, 'bannedcomm', banned=True)
    private = _community_in(topic, 'privatecomm', private=True)
    visible = _community_in(topic, 'visible')
    _posts(banned, alice, 1, prefix='banned')
    _posts(private, alice, 1, prefix='private')
    _posts(visible, alice, 1, prefix='visible')
    client = app.test_client()

    with patch('app.topic.routes.render_template', return_value='rendered') as render:
        client.get(f'/topic/{topic.machine_name}')

    assert [p.title for p in render.call_args.kwargs['posts']] == ['visible 0']

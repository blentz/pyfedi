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
    _posts(community, alice, 15)
    app.config['PAGE_LENGTH'] = 20
    alice.page_length = 10
    db.session.commit()
    client = app.test_client()
    login(client, alice)

    with patch('app.topic.routes.render_template', return_value='rendered') as render:
        client.get(f'/topic/{topic.machine_name}')
        assert render.call_args.kwargs['next_url'] is not None   # 15 posts, 10 per page

        alice.page_length = 50
        db.session.commit()
        client.get(f'/topic/{topic.machine_name}')
        assert render.call_args.kwargs['next_url'] is None       # 15 posts, 20 per page


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

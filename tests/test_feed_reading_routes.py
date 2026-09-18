"""app/feed/routes.py's reading routes: feed_list, show_feed,
get_all_child_feed_ids, feed_create_post and show_feed_rss.

MEASUREMENT BASIS. Before this file existed the five carried 74 missing
statements and 46 missing arcs on the full-suite --cov=app run at c8c85a0c.

feed_list is twelve statements nothing had ever executed, and all three of its
defects sit in its first five lines: it served any user's feeds to any
logged-in caller, a missing argument was a 500, and the feed title went into
returned HTML unescaped. The three are pinned below and repaired together.

Facts 292-294 apply: render_template is patched for anything that renders, POST
routes need a real CSRF token, and the site fixture supplies g.site.
"""
import pytest
from unittest.mock import patch

from flask import session
from flask_wtf.csrf import generate_csrf

from app import db
from app.models import Community, Feed, FeedItem, Site, User
from tests.factories import make_community, make_instance, make_user

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


def _seed(private_instance=False):
    """Two users, the id-1 seat burned, and the site opened by default.

    Site.private_instance defaults TRUE (app/models.py:4000), and
    login_required_if_private_instance -- which decorates show_feed -- redirects
    an anonymous reader to the login page on a private instance. Every
    anonymous test here would be measuring that decorator instead of the route,
    so the seed opens the instance and each test that cares says so.
    """
    instance = make_instance('test.piefed.local', software='piefed')
    burn = make_user(instance, 'burnseat', local=True)
    assert burn.id == 1
    owner = make_user(instance, 'feedowner', local=True)
    snooper = make_user(instance, 'snooper', local=True)
    site = Site.query.get(1)
    site.private_instance = private_instance
    db.session.commit()
    return instance, owner, snooper


def _feed(user, name, title=None, **kwargs):
    kwargs.setdefault('public', True)
    feed = Feed(user_id=user.id, title=title or name, name=name, machine_name=name,
                instance_id=1,
                ap_profile_id=f'https://test.piefed.local/f/{name}',
                ap_public_url=f'https://test.piefed.local/f/{name}', **kwargs)
    db.session.add(feed)
    db.session.commit()
    return feed


# --------------------------------------------------------------------------
# Task 1: feed_list's three defects.
# --------------------------------------------------------------------------


def test_the_feed_dropdown_lists_only_the_callers_own_feeds(app, db_session):
    """Was a PIN; INVERTED once the acting user came from the session.

    ORIGINAL PINNED CLAIM, now false: ":407 takes user_id from the QUERY STRING
    and :413 filters on it, with no `public` filter and no check that the
    caller is that user", so any logged-in account could read any other
    account's feed titles, private ones included.

    The snooper has a feed of their own, and it must appear: a repair that
    returned nothing at all would pass an assertion that only checked the
    owner's title was gone.
    """
    instance, owner, snooper = _seed()
    _feed(owner, 'secretfeed', title='Owner secret feed', public=False)
    _feed(snooper, 'snoopersfeed', title='Snoopers own feed')

    with app.test_client() as client:
        login(client, snooper)
        response = client.get(
            f'/feed/list?user_id={owner.id}&community_id=1&current_feed_id=0')

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'Owner secret feed' not in body
    assert 'Snoopers own feed' in body


def test_the_feed_dropdown_survives_a_request_without_arguments(app, db_session):
    """Was a PIN; INVERTED once the three reads gained defaults.

    ORIGINAL PINNED CLAIM, now false: ":407-411 call int() on three query
    parameters with no default", so a request without them raised TypeError
    before anything else ran.

    The caller's own feed still appears, so this asserts the route WORKS
    without its arguments rather than merely not raising -- and the generated
    link carries the zeros, which is what the defaults mean.
    """
    instance, owner, snooper = _seed()
    _feed(snooper, 'snoopersfeed', title='Snoopers own feed')

    with app.test_client() as client:
        login(client, snooper)
        response = client.get('/feed/list')

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'Snoopers own feed' in body
    assert 'current_feed_id=0' in body and 'community_id=0' in body


def test_the_feed_dropdown_escapes_the_title(app, db_session):
    """Was a PIN; INVERTED once the title was escaped.

    ORIGINAL PINNED CLAIM, now false: ":427 builds HTML in an f-string and
    drops feed.title into it verbatim", so a title carrying markup was returned
    as markup for the caller's page to splice into a dropdown.

    Both halves are asserted: the payload appears ESCAPED, and the surrounding
    anchor is still real markup -- a repair that escaped the whole line would
    pass the first assertion and break the feature.
    """
    instance, owner, snooper = _seed()
    _feed(owner, 'xssfeed', title='<img src=x onerror=alert(1)>')

    with app.test_client() as client:
        login(client, owner)
        response = client.get(
            f'/feed/list?user_id={owner.id}&community_id=1&current_feed_id=0')

    body = response.get_data(as_text=True)
    assert '<img src=x onerror=alert(1)>' not in body
    assert '&lt;img src=x onerror=alert(1)&gt;' in body
    assert body.startswith('<li><a class="dropdown-item"')


def test_the_feed_dropdown_offers_a_none_entry_when_the_community_is_in_a_feed(app, db_session):
    """:419-420's guard, and the loop's skip at :425-426, in one test because
    the skip's precondition is the guard's.

    Three feeds: the one the community is currently in (skipped), and two
    others (listed). The href is asserted as a WHOLE STRING rather than by
    fragment -- it carries four ids, and this campaign has watched adjacent ids
    swap without a test noticing (D653).
    """
    instance, owner, snooper = _seed()
    # Decoys first: Feed and Community have separate sequences, so without them
    # the feed ids, the community id and the user ids collide and an assertion
    # on a four-id href proves nothing (D653, fact 272).
    for n in range(3):
        _feed(owner, f'feediddecoy{n}')
    current = _feed(owner, 'currentfeed', title='Current feed')
    other = _feed(owner, 'otherfeed', title='Other feed')
    third = _feed(owner, 'thirdfeed', title='Third feed')
    # Community decoys AFTER the feeds, and counted: the three users take 1-3
    # and the six feeds take 1-6, so the community under test has to land above
    # 6 for the four-id href assertion to mean anything (D653, fact 272).
    for n in range(6):
        make_community(name=f'iddecoy{n}', host='remote.example')
    community = make_community(name='somecommunity', host='remote.example')
    assert len({current.id, other.id, third.id, community.id, owner.id}) == 5

    with app.test_client() as client:
        login(client, owner)
        response = client.get(f'/feed/list?user_id={owner.id}'
                              f'&community_id={community.id}&current_feed_id={current.id}')

    body = response.get_data(as_text=True)
    assert (f'<li><a class="dropdown-item" href="/feed/remove_community?user_id={owner.id}'
            f'&new_feed_id=0&current_feed_id={current.id}'
            f'&community_id={community.id}">None</li>') in body
    assert (f'<li><a class="dropdown-item" href="/feed/add_community?user_id={owner.id}'
            f'&new_feed_id={other.id}&current_feed_id={current.id}'
            f'&community_id={community.id}">Other feed</li>') in body
    assert 'Third feed' in body
    assert 'Current feed' not in body


def test_the_feed_dropdown_omits_the_none_entry_when_the_community_has_no_feed(app, db_session):
    """:419's False arm -- current_feed_id 0, which is what the caller passes
    for a community that is in no feed of theirs."""
    instance, owner, snooper = _seed()
    _feed(owner, 'otherfeed', title='Other feed')

    with app.test_client() as client:
        login(client, owner)
        response = client.get(f'/feed/list?user_id={owner.id}&community_id=7&current_feed_id=0')

    body = response.get_data(as_text=True)
    assert 'None</li>' not in body
    assert 'Other feed' in body


# --------------------------------------------------------------------------
# Task 3: show_feed, reached through /f/<name> (the route lives in
# app/activitypub/routes.py:2644 and calls this function for HTML requests).
# --------------------------------------------------------------------------


def _capture_render():
    captured = {}

    def fake_render(template, **kwargs):
        captured['template'] = template
        captured.update(kwargs)
        return 'rendered'

    return captured, fake_render


def test_showing_a_public_feed_renders_it(app, db_session):
    """The ordinary path, and the anonymous cache header at :578.

    The response is asserted for its Cache-Control as well as its status: an
    anonymous reader gets a 30-second public cache and a logged-in one does
    not, which is the difference the two arms exist for.
    """
    instance, owner, snooper = _seed()
    feed = _feed(owner, 'publicfeed')

    captured, fake_render = _capture_render()
    with app.test_client() as client:
        with patch('app.feed.routes.render_template', side_effect=fake_render):
            response = client.get('/f/publicfeed')

    assert response.status_code == 200
    assert captured['feed'].id == feed.id
    assert response.headers['Cache-Control'] == 'public, max-age=30'


def test_showing_a_public_feed_to_a_member_uses_a_private_cache(app, db_session):
    """:579-580, the logged-in arm of the same fork."""
    instance, owner, snooper = _seed()
    _feed(owner, 'publicfeed')

    with app.test_client() as client:
        login(client, snooper)
        with patch('app.feed.routes.render_template', return_value='rendered'):
            response = client.get('/f/publicfeed')

    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'private, max-age=15, must-revalidate'


def test_a_private_feed_is_hidden_from_a_stranger(app, db_session):
    """:438-445. A private feed redirects a stranger to the feed list with a
    flash; the owner and a subscriber fall through to the render.

    The three arms are separate tests rather than one parametrisation because
    the two `...` arms at :440 and :442 are no-ops -- their only observable
    effect is that the request does NOT redirect -- and folding them together
    would hide which one ran.
    """
    instance, owner, snooper = _seed()
    _feed(owner, 'privatefeed', public=False)

    with app.test_client() as client:
        login(client, snooper)
        with patch('app.feed.routes.flash') as flash_stub:
            response = client.get('/f/privatefeed')

    assert response.status_code == 302
    assert flash_stub.call_count == 1


def test_a_private_feed_is_visible_to_its_owner(app, db_session):
    """:439-440, the owner arm."""
    instance, owner, snooper = _seed()
    _feed(owner, 'privatefeed', public=False)

    with app.test_client() as client:
        login(client, owner)
        with patch('app.feed.routes.render_template', return_value='rendered'):
            response = client.get('/f/privatefeed')

    assert response.status_code == 200


def test_a_private_feed_is_visible_to_a_subscriber(app, db_session):
    """:441-442, the subscriber arm -- the one that needs a FeedMember row
    rather than ownership, and the only difference between it and the stranger
    test above."""
    from app.models import FeedMember
    instance, owner, snooper = _seed()
    feed = _feed(owner, 'privatefeed', public=False)
    db.session.add(FeedMember(feed_id=feed.id, user_id=snooper.id))
    db.session.commit()

    with app.test_client() as client:
        login(client, snooper)
        with patch('app.feed.routes.render_template', return_value='rendered'):
            response = client.get('/f/privatefeed')

    assert response.status_code == 200


@pytest.mark.parametrize('content_warning, nsfw, nsfl, expect_redirect', [
    (True, False, True, True),
    (True, True, False, False),
    (False, True, False, True),
    (False, False, True, True),
    (False, False, False, False),
])
def test_an_anonymous_reader_is_sent_to_log_in_for_adult_feeds(app, db_session,
                                                               content_warning, nsfw,
                                                               nsfl, expect_redirect):
    """:447-457, both CONTENT_WARNING arms.

    With CONTENT_WARNING on, only NSFL is hidden; with it off, either flag
    hides the feed. The (True, True, False) row is what separates the two arms:
    an NSFW feed is visible anonymously under CONTENT_WARNING and not without
    it.

    THE CONFIG IS PATCHED AT THE ROUTE'S OWN BINDING, not in app.config, and
    that is the only way to reach these branches at all: show_feed is decorated
    with login_required_if_private_instance, which reads the SAME
    CONTENT_WARNING setting and redirects an unwarned visitor to
    /content_warning first (app/utils.py:1930-1931). Setting it globally --
    through app.config, a cookie, or calling the function directly, all of
    which this test tried -- measures the decorator instead. Patching
    app.feed.routes.current_app leaves the decorator's view of the setting
    alone while giving the route the value under test. (The cookie route is
    closed anyway: this client delivers no cookies at all, by header or by
    set_cookie.)
    """
    from types import SimpleNamespace
    instance, owner, snooper = _seed()
    _feed(owner, 'adultfeed', nsfw=nsfw, nsfl=nsfl)
    stub_config = dict(app.config)
    stub_config['CONTENT_WARNING'] = content_warning
    stub_app = SimpleNamespace(config=stub_config, debug=False)

    with app.test_client() as client:
        with patch('app.feed.routes.current_app', stub_app), \
                patch('app.feed.routes.render_template', return_value='rendered'), \
                patch('app.feed.routes.flash') as flash_stub:
            result = client.get('/f/adultfeed')

    if expect_redirect:
        assert result.status_code == 302
        assert '/auth/login' in result.headers['Location']
        assert f'next=/f/adultfeed' in result.headers['Location']
        assert flash_stub.call_count == 1
    else:
        assert flash_stub.call_count == 0
        assert result.status_code == 200


def test_a_scaled_sort_collapses_to_the_default(app, db_session):
    """:467-468. 'scaled' is a post sort the feed page does not implement, and
    the route turns it into '' rather than passing it down -- asserted through
    the sort the template is handed."""
    instance, owner, snooper = _seed()
    _feed(owner, 'publicfeed')

    captured, fake_render = _capture_render()
    with app.test_client() as client:
        with patch('app.feed.routes.render_template', side_effect=fake_render):
            response = client.get('/f/publicfeed?sort=scaled')

    assert response.status_code == 200
    assert captured['sort'] == ''


@pytest.mark.parametrize('layout, user_page_length, expected', [
    (None, None, 100),
    (None, 5, 5),
    ('masonry', None, 200),
    ('masonry_wide', None, 300),
])
def test_the_page_length_ladder(app, db_session, layout, user_page_length, expected):
    """:472-479. Four rows, one per rung: the site default, a user whose own
    page_length is SMALLER (the guard is `<`, so an equal or larger one is
    ignored), and the two masonry layouts.

    The default row expects the site's PAGE_LENGTH (100), not the
    low-bandwidth 20: :472's ternary picks 20 only when the low_bandwidth
    cookie is set. The user's preference is 5 so it cannot be confused with
    either, and the two masonry values differ from each other so a swapped pair
    is visible.
    """
    instance, owner, snooper = _seed()
    _feed(owner, 'publicfeed')
    if user_page_length:
        snooper.page_length = user_page_length
        db.session.commit()

    captured, fake_render = _capture_render()
    with app.test_client() as client:
        login(client, snooper)
        with patch('app.feed.routes.render_template', side_effect=fake_render), \
                patch('app.feed.routes.paginate_post_ids',
                      side_effect=lambda ids, page, page_length: ids) as paginate:
            url = '/f/publicfeed' + (f'?layout={layout}' if layout else '')
            response = client.get(url)

    assert response.status_code == 200
    assert paginate.call_args.kwargs['page_length'] == expected


def test_a_nested_feed_gets_a_breadcrumb_for_every_ancestor(app, db_session):
    """:484-500. The trail is built by walking parent_feed_id UP and then
    reversing, so a grandchild's breadcrumbs read grandparent, parent, self.

    Three generations are the minimum that proves the reversal: with two, a
    trail built in the wrong order still ends with the feed itself.
    """
    instance, owner, snooper = _seed()
    grandparent = _feed(owner, 'grandparentfeed', title='Grandparent')
    parent = _feed(owner, 'parentfeed', title='Parent', parent_feed_id=grandparent.id)
    _feed(owner, 'childfeed', title='Child', parent_feed_id=parent.id)

    captured, fake_render = _capture_render()
    with app.test_client() as client:
        with patch('app.feed.routes.render_template', side_effect=fake_render):
            response = client.get('/f/childfeed')

    assert response.status_code == 200
    assert [crumb.text for crumb in captured['breadcrumbs']] == [
        'Grandparent', 'Parent', 'Child']
    assert captured['breadcrumbs'][-1].url == ''


@pytest.mark.parametrize('show_posts_in_children', [True, False])
def test_a_feed_includes_its_childrens_posts_only_when_asked(app, db_session,
                                                             show_posts_in_children):
    """:506-509, and with it get_all_child_feed_ids at :587-592.

    The child feed carries a community of its own, and the assertion is the
    community list the template is handed: with the flag off it holds only the
    parent's, with it on both. That also covers the recursion -- the child has a
    child of its own, so `extend` has to flatten more than one level.
    """
    instance, owner, snooper = _seed()
    parent = _feed(owner, 'parentfeed', show_posts_in_children=show_posts_in_children)
    child = _feed(owner, 'childfeed', parent_feed_id=parent.id)
    grandchild = _feed(owner, 'grandchildfeed', parent_feed_id=child.id)
    own = make_community(name='ownpostcommunity', host='remote.example')
    childs = make_community(name='childcommunity', host='remote.example')
    grandchilds = make_community(name='grandchildcommunity', host='remote.example')
    # :527 filters on total_subscriptions_count > 0, so a community with none
    # never reaches the template however its feed is wired -- the factory
    # leaves it at 0 and the first version of this test asserted against an
    # empty set for that reason alone.
    for community in (own, childs, grandchilds):
        community.total_subscriptions_count = 1
    db.session.commit()
    db.session.add_all([FeedItem(feed_id=parent.id, community_id=own.id),
                        FeedItem(feed_id=child.id, community_id=childs.id),
                        FeedItem(feed_id=grandchild.id, community_id=grandchilds.id)])
    db.session.commit()

    captured, fake_render = _capture_render()
    with app.test_client() as client:
        with patch('app.feed.routes.render_template', side_effect=fake_render):
            response = client.get('/f/parentfeed')

    assert response.status_code == 200
    names = {community.name for community in captured['feed_communities']}
    if show_posts_in_children:
        assert names == {'ownpostcommunity', 'childcommunity', 'grandchildcommunity'}
    else:
        assert names == {'ownpostcommunity'}


def test_show_feeds_final_abort_is_unreachable(app, db_session):
    """THE ROUND'S RESIDUAL IN show_feed: `:583-584`'s `else: abort(404)` can
    never run, and this test is the proof rather than an assertion about the
    route.

    `current_feed` is assigned `feed` at `:502` and never reassigned, so the
    `if current_feed:` at `:504` is false only when `feed` is falsy -- and a
    falsy `feed` has already raised at `:438`, `if not feed.public`, a hundred
    and fifty lines earlier. Demonstrated here by calling the function with
    None and watching it raise AttributeError at the FIRST access, not the
    last.

    Fact 75 CAUSE 9's shape -- a guard that cannot discriminate -- with the
    discrimination removed by an earlier statement rather than by a constant.
    """
    from app.feed.routes import show_feed
    instance, owner, snooper = _seed()

    with app.test_request_context('/f/nothing'):
        from flask import g
        g.site = Site.query.get(1)
        with pytest.raises(AttributeError, match='public'):
            show_feed(None)

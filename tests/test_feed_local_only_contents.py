"""A feed shows, announces and accepts only the communities its viewer may see (plan F4, task 8; spec goal 9).

A clearnet feed may hold an onion community. Off the onion the shared local-only rule (`app.local_only.hidden_here`)
hides that community, its posts and the posts an onion account wrote elsewhere, for every viewer: on the feed page, in
its RSS, in the API's `feed_view` and post list, and in the community pickers. Adding such a community is refused as
a missing one is, and no Add or Remove for a local-only community is sent to another server."""
from unittest.mock import patch

import pytest
from flask import g
from flask_login import login_user
from sqlalchemy import select

from app import db
from app.api.alpha.views import feed_view
from app.constants import SRC_API
from app.feed.util import feed_communities_for_edit
from app.feed_domains import community_shown_here, shown_community_count, shown_feed_communities
import app.shared.feed as shared_feed
from app.models import Community, Feed, FeedItem, FeedJoinRequest, FeedMember
from app.local_names import actor_handle
from app.shared.feed import announce_feed_add_remove_to_subscribers, edit_feed, form_communities_to_ids, join_feed
from app.utils import feed_open_to, feed_open_to_clause
from tests.factories import make_feed_item, make_feed_member, make_post, make_user
from tests.test_feed_copy_routes import _copy_payload, login
from tests.test_onion_feeds_hidden import signed_get
from tests.test_per_domain_feed_creation import api_input, api_site, token  # noqa: F401
from tests.test_per_domain_html_routes import Visitor
from tests.three_domains import FIRST, ONION, SHOP, get, peer, request_on, three_domains, two_domains  # noqa: F401

pytestmark = pytest.mark.usefixtures('site')

NOBODY = 2 ** 30


@pytest.fixture(autouse=True)
def public_site(site):
    """Site.private_instance defaults true, which sends an anonymous reader of a feed page to the login page."""
    site.private_instance = False
    db.session.commit()
    return site


class World:
    pass


@pytest.fixture
def world(app, three_domains):  # noqa: F811
    """A public first-domain feed `mixed` holding a first-domain and an onion community. Each community has one post,
    and an onion account has written one more in the first-domain community (stamped local-only, as it would be)."""
    d = three_domains
    w = World()
    w.d = d
    w.first_account, w.onion_account = d.account('fa', d.first), d.account('oa', d.onion)
    w.first_group, w.onion_group = d.local_community('firstgroup', d.first), d.local_community('oniongroup', d.onion)
    for community in (w.first_group, w.onion_group):
        community.total_subscriptions_count = 5
    w.feed = d.local_feed('mixed', d.first, w.first_account)
    for community in (w.first_group, w.onion_group):
        make_feed_item(w.feed, community)
    make_post(w.first_group, w.first_account, f'https://{FIRST}/post/1', title='first post')
    w.onion_post = make_post(w.onion_group, w.onion_account, f'http://{ONION}/post/2', title='onion post')
    w.guest_post = make_post(w.first_group, w.onion_account, f'http://{ONION}/post/3', title='onion author post')
    for post in (w.onion_post, w.guest_post):
        post.local_only = True
    db.session.commit()
    return w


def page(app, user, path, host=FIRST):
    return signed_get(app, user, path, host).get_data(as_text=True)


# --- the feed page -----------------------------------------------------------------------------------------------

def test_a_clearnet_feed_hides_its_onion_community_off_the_onion(app, world):
    html = page(app, world.first_account, '/f/mixed', FIRST)
    assert 'first post' in html
    assert 'onion post' not in html and 'oniongroup' not in html
    assert 'onion author post' not in html   # an onion account's post in a first-domain community goes with it


def test_the_onion_shows_it_to_a_signed_in_reader(app, world):
    html = page(app, world.onion_account, f'/f/mixed@{FIRST}', ONION)
    assert 'first post' in html and 'onion post' in html and 'oniongroup' in html


@pytest.mark.parametrize('host', [FIRST, SHOP, ONION])
def test_a_logged_out_viewer_never_sees_a_local_only_community_in_a_feed(app, world, host):
    path = '/f/mixed' if host == FIRST else f'/f/mixed@{FIRST}'
    response = signed_get(app, None, path, host)
    html = response.get_data(as_text=True)
    assert response.status_code != 200 or ('onion post' not in html and 'oniongroup' not in html)
    assert 'onion author post' not in html


def test_a_signed_in_reader_on_another_clearnet_domain_does_not_see_it_either(app, world):
    shop_account = world.d.account('sa', world.d.shop)
    html = page(app, shop_account, f'/f/mixed@{FIRST}', SHOP)
    assert 'first post' in html and 'onion post' not in html and 'oniongroup' not in html


def test_the_submit_picker_lists_no_hidden_community(app, world):
    html = page(app, world.first_account, '/f/mixed/submit', FIRST)
    assert 'firstgroup' in html and 'oniongroup' not in html


# --- RSS and the API -----------------------------------------------------------------------------------------------

def test_rss_omits_hidden_communities_and_posts(app, world):
    rss = get(app, '/f/mixed.rss', host=FIRST)
    assert rss.status_code == 200
    text = rss.get_data(as_text=True)
    assert 'first post' in text and 'onion post' not in text and 'onion author post' not in text


def test_the_api_view_omits_hidden_communities(app, world):
    def titles(host, user_id):
        with request_on(app, world.d.first if host == FIRST else world.d.onion):
            view = feed_view(world.feed, 1, user_id, [], True, [], [], [], [], [])
        return {c['name'].split('@')[0] for c in view['communities']}

    assert titles(FIRST, world.first_account.id) == {'firstgroup'}
    assert titles(FIRST, None) == {'firstgroup'}
    assert titles(ONION, world.onion_account.id) == {'firstgroup', 'oniongroup'}


@pytest.mark.parametrize('endpoint', ['list', 'list2'])
def test_the_api_post_list_omits_hidden_communities_and_posts(app, world, endpoint):
    from app.api.alpha.utils.post import get_post_list, get_post_list2
    function = {'list': get_post_list, 'list2': get_post_list2}[endpoint]
    with request_on(app, world.d.first):
        g.admin_ids = []
        titles = {p['post']['title'] for p in function(None, {'feed_id': world.feed.id, 'sort': 'New'}, world.first_account.id)['posts']}
    assert titles == {'first post'}


# --- adding a community -------------------------------------------------------------------------------------------

def add(app, user, host, feed, community_id):
    return Visitor(app, user, host).post('/feed/add_community',
                                         {'new_feed_id': str(feed.id), 'community_id': str(community_id)})


def test_adding_an_onion_community_off_the_onion_is_refused(app, world):
    mine = world.d.local_feed('mine', world.d.first, world.first_account)
    found, absent = (add(app, world.first_account, FIRST, mine, id_) for id_ in (world.onion_group.id, NOBODY))
    assert found.status_code == absent.status_code == 404
    assert found.get_data() == absent.get_data()
    assert FeedItem.query.filter_by(feed_id=mine.id).count() == 0


def test_adding_an_onion_community_on_the_onion_is_allowed(app, world):
    mine = world.d.local_feed('onionmine', world.d.onion, world.onion_account)
    assert add(app, world.onion_account, ONION, mine, world.onion_group.id).status_code in (200, 302)
    assert FeedItem.query.filter_by(feed_id=mine.id, community_id=world.onion_group.id).count() == 1


def test_a_form_names_a_hidden_community_and_it_is_skipped(app, world):
    text = f'!firstgroup@{FIRST}\n!oniongroup@{ONION}\n!nothing@{FIRST}'
    with request_on(app, world.d.first):
        assert form_communities_to_ids(text) == {world.first_group.id}
    with request_on(app, world.d.onion):
        login_user(world.onion_account)
        assert form_communities_to_ids(text) == {world.first_group.id, world.onion_group.id}


def test_an_edit_form_off_the_onion_does_not_list_the_onion_community(app, world):
    with request_on(app, world.d.first):
        text = feed_communities_for_edit(world.feed.id)
    assert 'firstgroup' in text and 'oniongroup' not in text


def test_an_edit_off_the_onion_removes_a_community_it_could_see_and_keeps_one_it_could_not(app, world, api_site):
    with request_on(app, world.d.first):
        edit_feed(api_input('mixed', communities=''), world.feed, SRC_API, token(world.first_account))
    kept = {item.community_id for item in FeedItem.query.filter_by(feed_id=world.feed.id)}
    assert kept == {world.onion_group.id}


# --- announcing -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('action', ['Add', 'Remove'])
@pytest.mark.parametrize('group, expected', [('onion_group', 0), ('first_group', 1)])
def test_an_onion_community_in_a_clearnet_feed_is_never_announced(app, world, peer, monkeypatch, action, group,
                                                                  expected):  # noqa: F811
    peer.inbox = f'https://{peer.domain}/inbox'
    db.session.add(FeedMember(feed_id=world.feed.id, user_id=make_user(peer, 'bob').id))
    db.session.commit()
    sends = []
    monkeypatch.setattr(shared_feed, 'send_post_request', lambda *args, **kwargs: sends.append(args))
    monkeypatch.setattr(shared_feed, 'instance_banned', lambda domain: False)

    announce_feed_add_remove_to_subscribers(action, world.feed.id, getattr(world, group).id)

    assert len(sends) == expected   # the first-domain community is announced, so the silence above is the rule's


# --- membership: a feed never joins a member to a community outside the member's domain -----------------------------

@pytest.fixture
def subscribed(monkeypatch):
    """The actors a join, an auto-follow or a copy would subscribe to; nothing is subscribed."""
    from unittest.mock import MagicMock
    recorder = MagicMock()
    monkeypatch.setattr('app.community.routes.do_subscribe', recorder)
    return lambda: {c.args[0] for c in recorder.call_args_list + recorder.delay.call_args_list}


def auto_follower(account):
    account.feed_auto_follow = True
    db.session.commit()
    return account


def test_joining_a_clearnet_feed_does_not_join_an_onion_community(app, world, subscribed):
    member = auto_follower(world.d.account('joiner', world.d.first))
    with request_on(app, world.d.first):
        join_feed('mixed', member.id)
    assert subscribed() == {actor_handle(world.first_group)}


def test_an_onion_account_joining_on_the_onion_gets_both(app, world, subscribed):
    member = auto_follower(world.d.account('onionjoiner', world.d.onion))
    with request_on(app, world.d.onion):
        join_feed(f'mixed@{FIRST}', member.id)
    assert subscribed() == {actor_handle(world.first_group), actor_handle(world.onion_group)}


@pytest.mark.parametrize('home, expected', [('first', ['first_group']), ('onion', ['first_group', 'onion_group'])])
def test_an_add_auto_follows_a_local_member_only_into_communities_of_their_domain(app, world, subscribed, home,
                                                                                  expected):
    member = auto_follower(world.d.account('follower', getattr(world.d, home)))
    db.session.add(FeedMember(feed_id=world.feed.id, user_id=member.id))
    db.session.commit()
    for group in (world.first_group, world.onion_group):
        announce_feed_add_remove_to_subscribers('Add', world.feed.id, group.id)
    assert subscribed() == {actor_handle(getattr(world, name)) for name in expected}


def test_an_onion_feed_does_not_join_a_clearnet_member_to_an_onion_community(app, world, subscribed):
    """The reverse case: an onion feed can hold a clearnet community and an onion one. A first-domain account that joins
    it on the onion gets the clearnet community, as it could by joining that directly, and not the onion one."""
    onion_feed = world.d.local_feed('onionmixed', world.d.onion, world.onion_account)
    for group in (world.first_group, world.onion_group):
        make_feed_item(onion_feed, group)
    member = auto_follower(world.d.account('visitor', world.d.first))
    with request_on(app, world.d.onion):
        join_feed('onionmixed', member.id)
    assert subscribed() == {actor_handle(world.first_group)}


def copy(app, user, source):
    client = app.test_client()
    login(client, user)
    with patch('app.feed.routes.render_template', return_value='rendered'):
        client.post(f'/feed/{source.id}/copy', data=_copy_payload(app, client))
    return Feed.query.filter_by(name='copiedfeed').first()


def test_copying_a_clearnet_feed_leaves_out_and_does_not_join_its_onion_community(app, world, subscribed):
    copier = auto_follower(world.d.account('copier', world.d.first))
    copied = copy(app, copier, world.feed)
    assert {item.community_id for item in FeedItem.query.filter_by(feed_id=copied.id)} == {world.first_group.id}
    assert copied.num_communities == 1
    assert subscribed() == {actor_handle(world.first_group)}


# --- counts --------------------------------------------------------------------------------------------------------------

def test_a_feeds_community_count_off_the_onion_leaves_out_hidden_communities(app, world):
    world.feed.num_communities = 2
    db.session.commit()
    with request_on(app, world.d.first):
        assert shown_community_count(world.feed, True) == 1
        g.admin_ids = []
        assert feed_view(world.feed, 1, None, [], False, [], [], [], [], [])['communities_count'] == 1
    with request_on(app, world.d.onion):
        login_user(world.onion_account)
        assert shown_community_count(world.feed) == 2


# --- parity -----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('where, signed_in', [(FIRST, True), (FIRST, False), (SHOP, True), (ONION, True), (ONION, False)])
def test_the_row_and_query_forms_agree_on_a_mixed_set_of_communities(app, world, where, signed_in):
    domain = {FIRST: world.d.first, SHOP: world.d.shop, ONION: world.d.onion}[where]
    remote = Community(name='far', title='far', ap_id='far@peer.example', ap_profile_id='https://peer.example/c/far')
    db.session.add(remote)
    db.session.commit()
    everything = Community.query.all()
    assert len(everything) >= 3
    with request_on(app, domain):
        by_row = {c.id for c in everything if community_shown_here(c, signed_in)}
        by_query = {c.id for c in shown_feed_communities(Community.query, signed_in)}
    assert by_row == by_query
    assert remote.id in by_row and world.first_group.id in by_row
    assert (world.onion_group.id in by_row) == (where == ONION and signed_in)


@pytest.mark.parametrize('who', ['public', 'owner', 'member', 'banned', 'pending', 'stranger', 'anonymous'])
def test_feed_open_to_and_its_clause_agree(app, world, who):
    d = world.d
    owner, member, banned, pending, stranger = (d.account(n, d.first) for n in ('o', 'm', 'b', 'p', 's'))
    feed = d.local_feed('closed' if who != 'public' else 'open', d.first, owner, public=who == 'public')
    FeedMember.query.filter_by(feed_id=feed.id, user_id=owner.id).delete()
    db.session.add_all([FeedMember(feed_id=feed.id, user_id=owner.id, is_owner=True),
                        FeedMember(feed_id=feed.id, user_id=member.id),
                        FeedMember(feed_id=feed.id, user_id=banned.id, is_banned=True)])
    db.session.add(FeedJoinRequest(feed_id=feed.id, user_id=pending.id))
    db.session.commit()
    viewer = {'public': stranger, 'owner': owner, 'member': member, 'banned': banned, 'pending': pending,
              'stranger': stranger, 'anonymous': None}[who]
    user_id = viewer.id if viewer else None
    by_clause = db.session.scalar(select(Feed.id).where(Feed.id == feed.id, feed_open_to_clause(user_id))) is not None
    assert feed_open_to(feed, user_id) == by_clause == (who in ('public', 'owner', 'member'))

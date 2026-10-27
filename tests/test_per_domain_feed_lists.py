"""Feed lists and menus show the viewer's domain, the onion falling back to the first domain's (plan F4, task 5).

A list on a storefront shows that domain's feeds and every remote feed. An onion feed is never listed off the onion
(spec anti-goal 2), whoever is reading and whatever was rendered first."""
import re

import pytest
from cachelib import SimpleCache
from flask import g
from sqlalchemy import select

from app import cache, db
from app.constants import ROLE_ADMIN_NAME
from app.feed_domains import feed_listed, feed_view_domain, listed_feeds_clause
from app.models import Feed, FeedItem, Role, user_role
from app.storefront import clear_domain_cache, domain_for_host
from app.utils import feed_tree_public, menu_instance_feeds
from tests.conftest import forget_login
from tests.factories import make_feed
from tests.three_domains import FIRST, ONION, SHOP, get, peer, request_on, three_domains, two_domains  # noqa: F401

pytestmark = pytest.mark.usefixtures('site')

HOSTS = (FIRST, SHOP, ONION)
VIEWERS = ('nobody', 'first', 'shop', 'onion', 'admin')


@pytest.fixture(autouse=True)
def public_site(site):
    """Site.private_instance defaults true, which sends an anonymous reader to the login page."""
    site.private_instance = False
    db.session.commit()
    return site


@pytest.fixture(autouse=True)
def fresh_caches(app, monkeypatch):
    """The suite's cache is a NullCache, which would let a cache bleed pass: use one that keeps."""
    monkeypatch.setitem(app.extensions['cache'], cache, SimpleCache())


@pytest.fixture
def world(app, three_domains, peer):  # noqa: F811
    """One public instance-menu feed on each domain and a remote one, and an account on each domain."""
    d = three_domains
    people = {'first': d.account('firstowner', d.first), 'shop': d.account('shopowner', d.shop),
              'onion': d.account('onionowner', d.onion)}
    feeds = {}
    for key, domain in (('first', d.first), ('shop', d.shop), ('onion', d.onion)):
        feed = d.local_feed(f'{key}news', domain, people[key])
        feed.title = f'{key.title()} headlines'
        feed.is_instance_feed = True
        feeds[key] = feed
    remote = make_feed(peer, 'faraway', public=True)
    remote.title = 'Faraway headlines'
    remote.user_id = people['first'].id   # whom the page names as its creator
    remote.is_instance_feed = True
    feeds['remote'] = remote
    admin = d.account('theadmin', d.first)
    role = Role(name=ROLE_ADMIN_NAME, weight=0)
    db.session.add(role)
    db.session.commit()
    db.session.execute(user_role.insert().values(user_id=admin.id, role_id=role.id))
    db.session.commit()
    people['admin'] = admin
    people['nobody'] = None
    return d, people, feeds


def login(client, user, host):
    forget_login()   # the app context outlives a request, and keeps the last caller
    scheme = 'http' if host == ONION else 'https'
    with client.session_transaction(base_url=f'{scheme}://{host}') as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def signed_get(app, user, path, host):
    """GET `path` on `host` as `user` (None: logged out); the onion goes through its guard."""
    if user is None:
        forget_login()
        return get(app, path, host=host)
    client = app.test_client()
    if host == ONION:
        from tests.test_onion_guard_limits import a_session, onion_get
        a_session(client)
        login(client, user, host)
        return onion_get(client, path)
    login(client, user, host)
    return client.get(path, base_url=f'https://{host}')


def as_viewer(app, host, user):
    """A request context on `host` whose reader is `user`; the caller leaves it to drop the login."""
    context = request_on(app, domain_for_host(host))
    context.__enter__()
    forget_login()
    if user is not None:
        g._login_user = user
    return context


def seen(page, feeds):
    """The keys of `feeds` whose list link `page` carries."""
    text = page.data.decode()
    return {key for key, feed in feeds.items() if re.search(rf'/f/{feed.name}(@[^"]*)?"', text)}


@pytest.mark.parametrize('who, host, expected', [
    (None, FIRST, 'first'), (None, SHOP, 'shop'),
    ('shop', FIRST, 'shop'),                       # an account sees its home domain's feeds anywhere
    ('first', ONION, 'first'),                     # a first-domain account on the onion
    ('onion', ONION, 'onion'), ('onion', SHOP, 'shop'),   # an onion account off the onion browses as logged out
    (None, ONION, 'first')])   # the onion's feeds are not for a logged-out reader (C2): the first domain's instead
def test_feed_view_domain(app, world, who, host, expected):
    d, people, _feeds = world
    context = as_viewer(app, host, people[who] if who else None)
    try:
        assert feed_view_domain() is not None and feed_view_domain().domain == getattr(d, expected).domain
    finally:
        context.__exit__(None, None, None)
        forget_login()


def test_an_api_account_is_the_account_the_view_follows(app, world):
    d, people, _feeds = world
    with request_on(app, d.first):
        forget_login()
        assert feed_view_domain(people['shop']).domain == SHOP
        assert feed_view_domain(people['onion']).domain == FIRST   # an onion account off the onion: as logged out


@pytest.fixture
def bare_onion(world):
    """The onion owns no public feed."""
    _d, _people, feeds = world
    feeds['onion'].public = False
    db.session.commit()
    return world


@pytest.mark.parametrize('who', ['onion', None])
def test_the_onion_falls_back_to_the_first_domain_when_it_has_no_feed(app, bare_onion, who):
    d, people, _feeds = bare_onion
    context = as_viewer(app, ONION, people[who] if who else None)
    try:
        assert feed_view_domain().domain == d.first.domain
    finally:
        context.__exit__(None, None, None)
        forget_login()


def test_a_banned_onion_feed_does_not_count_and_a_shop_without_feeds_does_not_fall_back(app, world):
    d, people, feeds = world
    feeds['onion'].banned = True
    feeds['shop'].public = False
    db.session.commit()
    with request_on(app, d.onion):
        forget_login()
        assert feed_view_domain().domain == FIRST
    with request_on(app, d.shop):   # only a local-only domain falls back
        forget_login()
        assert feed_view_domain().domain == SHOP


def test_the_onion_lists_the_first_domains_feeds_when_it_has_none_of_its_own(app, bare_onion):
    _d, people, feeds = bare_onion
    page = signed_get(app, people['onion'], '/feeds', ONION)
    assert seen(page, feeds) == {'first', 'remote'}


def test_each_storefront_lists_its_own_feeds_and_remote_ones(app, world):
    _d, _people, feeds = world
    assert seen(get(app, '/feeds', host=FIRST), feeds) == {'first', 'remote'}
    assert seen(get(app, '/feeds', host=SHOP), feeds) == {'shop', 'remote'}


def test_a_sister_account_on_the_first_domain_sees_qualified_links(app, world):
    _d, people, _feeds = world
    page = signed_get(app, people['shop'], '/feeds', FIRST)
    assert b'href="/f/shopnews@shop.example"' in page.data and b'href="/f/firstnews"' not in page.data


def test_the_search_narrows_the_domains_feeds_only(app, world):
    _d, _people, feeds = world
    assert seen(get(app, '/feeds?search=headlines', host=SHOP), feeds) == {'shop', 'remote'}
    assert seen(get(app, '/feeds?search=Faraway', host=SHOP), feeds) == {'remote'}


@pytest.mark.parametrize('order', [(SHOP, FIRST, SHOP), (FIRST, SHOP, FIRST)])
@pytest.mark.parametrize('path', ['/feeds', '/feeds_menu', '/explore_menu', '/explore'])
def test_menu_and_feeds_page_have_no_cache_bleed(app, world, order, path):
    _d, _people, feeds = world
    expected = {FIRST: {'first', 'remote'}, SHOP: {'shop', 'remote'}}
    for host in order:
        assert seen(get(app, path, host=host), feeds) == expected[host], (path, host)
    assert {f.name for f in menu_instance_feeds(domain_for_host(SHOP).id)} == {'shopnews', 'faraway'}
    assert {f.name for f in menu_instance_feeds(domain_for_host(FIRST).id)} == {'firstnews', 'faraway'}


def test_clearing_the_menu_cache_clears_every_domain(app, world):
    d, _people, _feeds = world
    first = {f.name for f in menu_instance_feeds(d.first.id)}
    menu_instance_feeds(d.shop.id)
    Feed.query.filter_by(name='firstnews').one().is_instance_feed = False
    db.session.commit()
    assert {f.name for f in menu_instance_feeds(d.first.id)} == first   # still cached
    cache.delete_memoized(menu_instance_feeds)
    assert {f.name for f in menu_instance_feeds(d.first.id)} == {'faraway'}


def test_the_communities_feed_dropdown_lists_only_the_view_domains_feeds(app, world):
    _d, _people, feeds = world
    for host, expected in ((FIRST, {'first', 'remote'}), (SHOP, {'shop', 'remote'})):
        page = get(app, '/communities', host=host)
        text = page.data.decode()
        assert page.status_code == 200
        assert {key for key, feed in feeds.items() if feed.title in text} == expected


def test_the_community_sidebar_lists_only_the_view_domains_feeds(app, world):
    d, _people, feeds = world
    community = d.local_community('shared', d.first)
    for feed in feeds.values():
        db.session.add(FeedItem(feed_id=feed.id, community_id=community.id))
    db.session.commit()
    page = get(app, '/c/shared', host=FIRST)
    assert page.status_code == 200
    text = page.data.decode()
    side = text[text.index('id="community_feeds"'):]
    assert {key for key, feed in feeds.items() if re.search(rf'/f/{feed.name}(@[^"]*)?"', side)} == {'first', 'remote'}


@pytest.mark.parametrize('host', [FIRST, SHOP])
@pytest.mark.parametrize('who', VIEWERS)
@pytest.mark.parametrize('primed_first', [True, False])
def test_no_clearnet_list_names_an_onion_feed(app, world, host, who, primed_first):
    d, people, _feeds = world
    if primed_first:   # the onion's own pages rendered first, by a signed-in onion reader
        assert 'onionnews' in {f.name for f in menu_instance_feeds(d.onion.id)}
        for path in ('/feeds_menu', '/explore_menu', '/explore', '/feeds'):
            assert b'Onion headlines' in signed_get(app, people['onion'], path, ONION).data, path   # shown there
    for path in ('/feeds', '/feeds_menu', '/explore_menu', '/explore', '/communities'):
        page = signed_get(app, people[who], path, host)
        assert page.status_code == 200
        assert b'onionnews' not in page.data and b'Onion headlines' not in page.data, (path, who, host)


@pytest.mark.parametrize('who', VIEWERS)
@pytest.mark.parametrize('host', [FIRST, SHOP])
def test_the_helpers_hide_an_onion_feed_off_the_onion(app, world, who, host):
    d, people, feeds = world
    context = as_viewer(app, host, people[who])
    try:
        names = {row['feed'].name for row in feed_tree_public()}
        assert 'onionnews' not in names and {'firstnews', 'shopnews', 'faraway'} <= names
        assert 'onionnews' not in {row['feed'].name for row in feed_tree_public(domain=d.onion)}
        for listed in (db.session.scalars(select(Feed).where(listed_feeds_clause(d.onion))),
                       db.session.scalars(select(Feed).where(listed_feeds_clause(d.first)))):
            assert 'onionnews' not in {f.name for f in listed}
    finally:
        context.__exit__(None, None, None)
        forget_login()


def test_an_onion_account_off_the_onion_is_listed_no_feed_of_its_own(app, world):
    _d, people, feeds = world
    for path in ('/feeds_menu', '/explore_menu', '/explore'):
        page = signed_get(app, people['onion'], path, SHOP)
        assert b'Onion headlines' not in page.data, path


def test_the_onion_lists_its_own_feeds_to_a_signed_in_reader_only(app, world):
    _d, people, feeds = world
    assert seen(signed_get(app, people['onion'], '/feeds', ONION), feeds) == {'onion', 'remote'}
    assert seen(signed_get(app, None, '/feeds', ONION), feeds) == {'first', 'remote'}   # the C2 rule stands


@pytest.mark.parametrize('path', ['/feeds', '/feeds_menu', '/explore_menu', '/explore'])
def test_a_logged_out_onion_visitor_is_shown_the_first_domains_feeds(app, world, path):
    _d, _people, feeds = world
    assert seen(signed_get(app, None, path, ONION), feeds) == {'first', 'remote'}, path


@pytest.mark.parametrize('signed_in', [False, True])
@pytest.mark.parametrize('host', HOSTS)
def test_the_list_clause_and_the_row_test_agree(app, world, host, signed_in):
    d, _people, feeds = world
    quiet = d.local_feed('quiet', d.shop, None, public=False)   # a private one, to be sure the mix is mixed
    with request_on(app, domain_for_host(host)):
        forget_login()
        for domain in (d.first, d.shop, d.onion):
            by_clause = set(db.session.scalars(select(Feed.id).where(listed_feeds_clause(domain, signed_in))))
            by_row = {feed.id for feed in Feed.query.all() if feed_listed(feed, domain, signed_in)}
            assert by_clause == by_row, (host, domain.domain, signed_in)
    assert quiet.id and len(feeds) == 4

"""`/f/name` opens this storefront's feed and `/f/name@domain` another hosted domain's, through one resolver,
`actor_to_feed` (plan F4, task 4)."""
import pytest
from flask import g, session
from flask_wtf.csrf import generate_csrf

from app import cache, db
from app.feed.util import actor_to_feed, search_for_feed
from app.models import Feed, FeedMember, User
from app.storefront import domain_for_host
from app.utils import menu_instance_feeds
from tests.conftest import forget_login
from tests.factories import make_feed, make_feed_item
from tests.three_domains import FIRST, ONION, SHOP, ap_get, get, no_http, peer, request_on, sent, three_domains, two_domains  # noqa: F401



@pytest.fixture(autouse=True)
def public_site(site):
    """Site.private_instance defaults true, which sends an anonymous reader of a feed page to the login page."""
    site.private_instance = False
    db.session.commit()
    return site

SHOP_URL = f'https://{SHOP}'


@pytest.fixture
def news(three_domains):  # noqa: F811
    """A public feed called `news` on the first domain and one on shop, with titles that tell them apart."""
    first = three_domains.local_feed('news', three_domains.first, three_domains.account('firstowner', three_domains.first))
    first.title = 'First news'
    shop = three_domains.local_feed('news', three_domains.shop, three_domains.account('shopowner', three_domains.shop))
    shop.title = 'Shop news'
    db.session.commit()
    return first, shop


def login(client, user, host=FIRST):
    forget_login()   # the app context outlives a request, and keeps the last caller
    with client.session_transaction(base_url=f'https://{host}') as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def csrf(app, client, host=FIRST):
    with app.test_request_context():
        g.pop('csrf_token', None)
        token = generate_csrf()
        raw = session['csrf_token']
    with client.session_transaction(base_url=f'https://{host}') as sess:
        sess['csrf_token'] = raw
    return token


def test_a_bare_name_opens_this_storefronts_feed(app, three_domains, news):  # noqa: F811
    assert b'First news' in get(app, '/f/news', host=FIRST).data
    assert b'Shop news' in get(app, '/f/news', host=SHOP).data


@pytest.mark.parametrize('path', ['/f/news@shop.example', '/f/NEWS@SHOP.EXAMPLE'])
def test_name_at_domain_opens_the_other_domains_feed(app, three_domains, news, path):  # noqa: F811
    page = get(app, path, host=FIRST)
    assert page.status_code == 200 and b'Shop news' in page.data


def test_a_first_domain_feed_named_at_its_own_host_opens_on_shop(app, three_domains, news):  # noqa: F811
    page = get(app, f'/f/news@{FIRST}', host=SHOP)
    assert page.status_code == 200 and b'First news' in page.data


def test_a_hosted_handle_with_no_feed_is_404_without_a_fetch(app, three_domains, no_http):  # noqa: F811
    assert get(app, '/f/nothing@shop.example', host=FIRST).status_code == 404
    assert no_http == []


def test_a_banned_feed_is_not_found(app, three_domains, news):  # noqa: F811
    news[1].banned = True
    db.session.commit()
    assert get(app, '/f/news@shop.example', host=FIRST).status_code == 404


@pytest.mark.parametrize('path', ['/f/a@b@c', '/f/%00', '/f/@', '/f/@shop.example', '/f/news@', '/f/' + 'x' * 400,
                                  '/f/' + 'x' * 400 + '@shop.example', '/f/a@b@c.rss', '/f/%00.rss', '/f/@.rss',
                                  '/f/' + 'x' * 400 + '.rss'])
def test_junk_names_are_refused_never_a_500(app, three_domains, news, path):  # noqa: F811
    for host in (FIRST, SHOP):
        assert get(app, path, host=host).status_code in (400, 404)
    assert ap_get(app, path).status_code in (400, 404)


def test_an_activitypub_request_for_an_actor_with_an_at_sign_is_400(app, three_domains, news):  # noqa: F811
    assert ap_get(app, '/f/news@shop.example', host=FIRST).status_code == 400
    assert ap_get(app, '/f/news@shop.example/outbox', host=FIRST).status_code == 400


def test_the_collections_find_this_storefronts_feed(app, three_domains):  # noqa: F811
    shop = three_domains.local_feed('news', three_domains.shop)
    first = three_domains.local_feed('news', three_domains.first)
    for feed in (shop, first):
        feed.public = True
    db.session.commit()
    shop.description = 'only shop'
    db.session.commit()
    for host, feed in ((FIRST, first), (SHOP, shop)):
        for collection in ('outbox', 'following', 'moderators'):
            assert ap_get(app, f'/f/news/{collection}', host=host).status_code == 200
    assert ap_get(app, '/f/news/followers', host=SHOP).status_code == 200
    assert ap_get(app, '/f/news/followers', host=FIRST).status_code == 200


def test_a_collection_of_a_feed_that_is_not_here_is_404(app, three_domains):  # noqa: F811
    three_domains.local_feed('onlyshop', three_domains.shop)
    for collection in ('outbox', 'following', 'moderators', 'followers'):
        assert ap_get(app, f'/f/onlyshop/{collection}', host=FIRST).status_code == 404


def test_a_private_feed_on_another_domain_opens_by_its_suffixed_path(app, three_domains):  # noqa: F811
    owner = three_domains.account('owner', three_domains.shop)
    other = three_domains.account('other', three_domains.shop)
    feed = three_domains.local_feed('slug/owner', three_domains.shop, owner=owner, public=False)
    feed.title = 'Secret shop feed'
    db.session.commit()
    owner.rss_token, other.rss_token = 'owner-rss-token', 'other-rss-token'
    db.session.commit()
    gone = get(app, '/f/nothing/owner@shop.example.rss', host=FIRST)
    with app.test_client() as client:
        login(client, owner)
        page = client.get('/f/slug/owner@shop.example', base_url=f'https://{FIRST}')
        assert page.status_code == 200 and b'Secret shop feed' in page.data
        rss = client.get('/f/slug/owner@shop.example.rss?token=owner-rss-token', base_url=f'https://{FIRST}')
        assert rss.status_code == 200 and b'Secret shop feed' in rss.data
    with app.test_client() as client:
        login(client, other)
        refused = client.get('/f/slug/owner@shop.example.rss?token=other-rss-token', base_url=f'https://{FIRST}')
    assert refused.status_code == 404 and refused.data == gone.data
    forget_login()
    anonymous = get(app, '/f/slug/owner@shop.example.rss', host=FIRST)
    assert anonymous.status_code == 404 and anonymous.data == gone.data


def test_rss_names_the_feed_of_the_storefront_or_the_named_domain(app, three_domains, news):  # noqa: F811
    assert b'Shop news' in get(app, '/f/news.rss', host=SHOP).data
    assert b'First news' in get(app, '/f/news.rss', host=FIRST).data
    other = get(app, '/f/news@shop.example.rss', host=FIRST)
    assert other.status_code == 200 and b'Shop news' in other.data
    assert b'/f/news@shop.example' in other.data


def test_rss_of_a_child_feed_finds_it_under_its_parent(app, three_domains):  # noqa: F811
    parent = three_domains.local_feed('parent', three_domains.shop)
    child = three_domains.local_feed('kid', three_domains.shop)
    child.parent_feed_id = parent.id
    child.title = 'Shop kid'
    db.session.commit()
    page = get(app, '/f/parent/kid@shop.example.rss', host=FIRST)
    assert page.status_code == 200 and b'Shop kid' in page.data
    assert get(app, '/f/parent/kid.rss', host=SHOP).status_code == 200


def test_submit_lists_the_named_domains_feed_communities(app, three_domains, news):  # noqa: F811
    _, shop_feed = news
    make_feed_item(shop_feed, three_domains.local_community('shopcommunity', three_domains.shop))
    user = three_domains.account('poster', three_domains.first)
    with app.test_client() as client:
        login(client, user)
        page = client.get('/f/news@shop.example/submit', base_url=f'https://{FIRST}')
        missing = client.get('/f/nothing@shop.example/submit', base_url=f'https://{FIRST}')
    assert page.status_code == 200 and b'shopcommunity' in page.data
    assert missing.status_code == 404


def test_subscribe_joins_the_named_domains_feed_without_a_request_out(app, three_domains, news, no_http, sent):  # noqa: F811
    first_id, shop_id = news[0].id, news[1].id
    user = three_domains.account('joiner', three_domains.first)
    with app.test_client() as client:
        login(client, user)
        token = csrf(app, client)
        client.post('/feed/news@shop.example/subscribe', data={'csrf_token': token}, base_url=f'https://{FIRST}')
    assert FeedMember.query.filter_by(user_id=user.id, feed_id=shop_id).count() == 1
    assert FeedMember.query.filter_by(user_id=user.id, feed_id=first_id).count() == 0
    assert no_http == [] and sent == []


def test_subscribe_by_a_bare_name_joins_this_storefronts_feed(app, three_domains, news):  # noqa: F811
    first_id, shop_id = news[0].id, news[1].id
    user = three_domains.account('joiner', three_domains.shop)
    with app.test_client() as client:
        login(client, user, SHOP)
        token = csrf(app, client, SHOP)
        client.post('/feed/news/subscribe', data={'csrf_token': token}, base_url=SHOP_URL)
    assert FeedMember.query.filter_by(user_id=user.id, feed_id=shop_id).count() == 1
    assert FeedMember.query.filter_by(user_id=user.id, feed_id=first_id).count() == 0


def test_unsubscribe_of_a_handle_on_a_domain_not_hosted_here_is_404(app, three_domains, news, no_http):  # noqa: F811
    user = three_domains.account('joiner', three_domains.shop)
    with app.test_client() as client:
        login(client, user, SHOP)
        token = csrf(app, client, SHOP)
        missing = client.post('/feed/news@nowhere.example/unsubscribe', data={'csrf_token': token}, base_url=SHOP_URL)
    assert missing.status_code == 404 and no_http == []


def test_unsubscribe_leaves_the_named_domains_feed(app, three_domains, news, no_http, sent):  # noqa: F811
    shop_id = news[1].id
    user = three_domains.account('joiner', three_domains.first)
    db.session.add(FeedMember(user_id=user.id, feed_id=shop_id))
    db.session.commit()
    with app.test_client() as client:
        login(client, user)
        token = csrf(app, client)
        client.post('/feed/news@shop.example/unsubscribe', data={'csrf_token': token}, base_url=f'https://{FIRST}')
    assert FeedMember.query.filter_by(user_id=user.id, feed_id=shop_id).count() == 0
    assert no_http == [] and sent == []


def test_lookup_redirects_a_hosted_feed_to_its_path_name(app, three_domains, news):  # noqa: F811
    answer = get(app, '/feed/lookup/news/shop.example', host=FIRST)
    assert answer.status_code == 302 and answer.headers['Location'].endswith('/f/news@shop.example')
    answer = get(app, '/feed/lookup/news/shop.example', host=SHOP)
    assert answer.status_code == 302 and answer.headers['Location'].endswith('/f/news')
    answer = get(app, f'/feed/lookup/news/{FIRST}', host=SHOP)
    assert answer.status_code == 302 and answer.headers['Location'].endswith(f'/f/news@{FIRST}')


def test_search_for_feed_finds_each_hosted_domains_own_row(app, three_domains, news, no_http):  # noqa: F811
    first_feed, shop_feed = news
    assert search_for_feed('~news@shop.example', signed_in=True).id == shop_feed.id
    assert search_for_feed(f'~news@{FIRST}', signed_in=True).id == first_feed.id
    assert search_for_feed('~nothing@shop.example', signed_in=True) is None
    assert no_http == []


def test_a_shop_feeds_page_on_the_first_domain_links_it_as_name_at_shop(app, three_domains, news):  # noqa: F811
    user = three_domains.account('viewer', three_domains.first)
    child = three_domains.local_feed('kid', three_domains.shop)
    child.parent_feed_id = news[1].id
    db.session.commit()
    with app.test_client() as client:
        login(client, user)
        html = client.get('/f/news@shop.example', base_url=f'https://{FIRST}').data.decode()
    assert '/feed/news@shop.example/subscribe' in html
    assert '/f/news@shop.example.rss' in html
    assert '/f/kid@shop.example' in html


def test_a_shop_feeds_menu_entries_name_it_at_shop_on_the_first_domain(app, three_domains, news):  # noqa: F811
    news[0].is_instance_feed = news[1].is_instance_feed = True
    db.session.commit()
    cache.delete_memoized(menu_instance_feeds)
    client = app.test_client()
    login(client, db.session.get(User, news[1].user_id))   # a shop account's menu is its home domain's (plan F4, task 5)
    html = client.get('/explore', base_url=f'https://{FIRST}').data.decode()
    assert 'href="/f/news@shop.example"' in html
    assert 'href="/f/news"' not in html
    forget_login()
    assert 'href="/f/news"' in get(app, '/explore', host=FIRST).data.decode()


def test_every_link_a_feed_renders_resolves_back_to_it(app, three_domains, news, peer):  # noqa: F811
    """A feed's `link()` is bare on its own storefront and `name@host` on another; each, on each storefront, names the
    same row through actor_to_feed (the first domain's feed on shop was `news@<first host>`, read as a remote one). The
    onion's feed is found only where it is shown, on the onion (task 7): elsewhere its link names nothing."""
    onion = three_domains.local_feed('onionfeed', three_domains.onion)
    remote = make_feed(peer, 'faraway')
    seen = set()
    for host in (FIRST, SHOP, ONION):
        with request_on(app, domain_for_host(host)):
            for feed in (*news, onion, remote):
                link = feed.link()
                seen.add(link)
                expected = feed if feed is not onion or host == ONION else None
                assert actor_to_feed(link, signed_in=True) == expected, (host, link)
                assert actor_to_feed(link.upper(), signed_in=True) == expected, (host, link)
    assert {'news', 'news@shop.example', f'news@{FIRST}', f'onionfeed@{ONION}'} <= seen


def test_the_resolver_is_the_one_place_a_name_becomes_a_feed(app, three_domains, news):  # noqa: F811
    first, shop = news
    with request_on(app, three_domains.shop):
        assert actor_to_feed(' News ') == shop
        assert actor_to_feed('news@' + FIRST) == first
        assert actor_to_feed('news@a@b') is None
        assert actor_to_feed('@') is None
        assert actor_to_feed('') is None
        assert actor_to_feed('nothing@shop.example') is None


def test_feed_path_names_the_last_segment_for_its_domain(app, three_domains):  # noqa: F811
    parent = three_domains.local_feed('parent', three_domains.shop)
    child = three_domains.local_feed('kid', three_domains.shop)
    child.parent_feed_id = parent.id
    db.session.commit()
    with request_on(app, three_domains.shop):
        assert child.path() == 'parent/kid'
    with request_on(app, three_domains.first):
        assert child.path() == 'parent/kid@shop.example'


def test_rss_of_a_public_child_is_not_hidden_by_a_private_feed_of_the_same_path(app, three_domains):  # noqa: F811
    """A private feed is named `parent/kid` (parent, owned by user kid); a public child `kid` under `parent` has the
    same path. A reader who may not read the private row gets the public child. Its owner gets the private feed: the
    path spells that feed's name exactly, and it is the first candidate the owner may read."""
    kid = three_domains.account('kid', three_domains.shop)
    kid.rss_token = 'kid-rss-token'
    parent = three_domains.local_feed('parent', three_domains.shop)
    child = three_domains.local_feed('kid', three_domains.shop)
    child.parent_feed_id, child.title = parent.id, 'Public kid'
    private = three_domains.local_feed('parent/kid', three_domains.shop, owner=kid, public=False)
    private.title = 'Private kid'
    db.session.commit()
    anonymous = get(app, '/f/parent/kid.rss', host=SHOP)
    assert anonymous.status_code == 200 and b'Public kid' in anonymous.data
    forget_login()
    owner = get(app, '/f/parent/kid.rss?token=kid-rss-token', host=SHOP)
    assert owner.status_code == 200 and b'Private kid' in owner.data


def test_the_edit_link_comes_back_to_the_feed_as_this_storefront_names_it(app, three_domains, news):  # noqa: F811
    owner = three_domains.account('shopowner2', three_domains.shop)
    news[1].user_id = owner.id
    db.session.add(FeedMember(feed_id=news[1].id, user_id=owner.id, is_owner=True))
    db.session.commit()
    with app.test_client() as client:
        login(client, owner)
        html = client.get('/f/news@shop.example', base_url=f'https://{FIRST}').data.decode()
    assert 'next=/f/news@shop.example' in html.replace('%40', '@').replace('%2F', '/')

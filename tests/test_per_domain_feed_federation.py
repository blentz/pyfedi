"""A sister domain's feed federates and answers WebFinger from its own domain (plan F4, task 6).

A shop feed's actor document, collections and activities name the shop, never SERVER_NAME; the shop answers WebFinger
for it and the first domain does not. The first domain's own feeds are unchanged (tests/test_c3_golden_documents.py
holds their bytes), and the onion's feeds are never federated."""
import pytest

import app.shared.feed as shared_feed
from app import db
from app.activitypub import routes as ap_routes
from app.activitypub.routes import process_inbox_request
from app.models import FeedMember, File
from app.utils import utcnow
from tests.factories import a_keypair, inbox_activity, make_user
from tests.three_domains import FIRST, ONION, SHOP, ap_get, peer, three_domains, two_domains, urls_in  # noqa: F401

pytestmark = pytest.mark.usefixtures('site')

SHOP_BASE = f'https://{SHOP}'


@pytest.fixture(autouse=True)
def public_site(site):
    site.private_instance = False
    db.session.commit()
    return site


@pytest.fixture
def shopnews(three_domains):  # noqa: F811
    owner = three_domains.account('shopowner', three_domains.shop)
    feed = three_domains.local_feed('shopnews', three_domains.shop, owner)
    feed.private_key, feed.public_key = a_keypair()
    feed.icon = File(file_path='app/static/media/feeds/sh/op/icon.webp')
    feed.image = File(file_path='app/static/media/feeds/sh/op/banner.webp')
    feed.description = 'Shop news'
    feed.description_html = '<p>Shop news</p>'
    child = three_domains.local_feed('shopchild', three_domains.shop, owner)
    child.parent_feed_id = feed.id
    db.session.commit()
    return feed


def test_every_url_in_a_shop_feeds_document_is_on_shop(app, three_domains, shopnews):
    document = ap_get(app, '/f/shopnews', host=SHOP).json
    assert document['id'] == f'{SHOP_BASE}/f/shopnews'
    urls = urls_in({key: value for key, value in document.items() if key != '@context'})
    for field in ('id', 'inbox', 'outbox', 'followers', 'following', 'moderators', 'attributedTo', 'url'):
        assert document[field].startswith(f'{SHOP_BASE}/f/shopnews')
    assert document['publicKey']['id'] == f'{SHOP_BASE}/f/shopnews#main-key'
    assert document['publicKey']['owner'] == f'{SHOP_BASE}/f/shopnews'
    assert document['endpoints']['sharedInbox'] == f'{SHOP_BASE}/inbox'
    assert document['icon']['url'] == f'{SHOP_BASE}/static/media/feeds/sh/op/icon.webp'
    assert document['image']['url'] == f'{SHOP_BASE}/static/media/feeds/sh/op/banner.webp'
    assert document['childFeeds'] == [f'{SHOP_BASE}/f/shopchild']
    for url in urls:
        assert url.startswith(f'{SHOP_BASE}/'), url
    assert FIRST not in str(document)


def test_the_link_header_of_a_shop_feed_names_the_shop(app, three_domains, shopnews):
    response = ap_get(app, '/f/shopnews', host=SHOP)
    assert response.headers['Link'] == f'<{SHOP_BASE}/f/shopnews>; rel="alternate"; type="text/html"'


def test_a_shop_feeds_document_is_404_on_the_first_domain(app, three_domains, shopnews):
    """A regression pin, not red-first: owned_elsewhere already answered 404 for it."""
    assert ap_get(app, '/f/shopnews', host=FIRST).status_code == 404
    assert ap_get(app, '/f/shopnews@shop.example', host=FIRST).status_code in (400, 404)


def test_a_first_domain_feed_document_is_unchanged_in_kind(app, three_domains):
    owner = three_domains.account('firstowner', three_domains.first)
    three_domains.local_feed('firstnews', three_domains.first, owner)
    assert ap_get(app, '/f/firstnews', host=FIRST).json['id'] == f'https://{FIRST}/f/firstnews'
    assert ap_get(app, '/f/firstnews', host=SHOP).status_code == 404


@pytest.mark.parametrize('resource', [f'acct:~shopnews@{SHOP}', 'acct:~ShopNews@Shop.Example'])
def test_webfinger_answers_a_shop_feed_on_shop_only(app, three_domains, shopnews, resource):
    response = ap_get(app, f'/.well-known/webfinger?resource={resource}', host=SHOP)
    assert response.status_code == 200
    link = [item for item in response.json['links'] if item['rel'] == 'self'][0]
    assert link['href'] == f'{SHOP_BASE}/f/shopnews'
    assert link['properties']['https://www.w3.org/ns/activitystreams#type'] == 'Feed'
    assert ap_get(app, f'/.well-known/webfinger?resource={resource}', host=FIRST).status_code == 404


def test_the_first_domain_no_longer_answers_webfinger_for_a_shop_feed(app, three_domains, shopnews):
    assert ap_get(app, f'/.well-known/webfinger?resource=acct:~shopnews@{FIRST}', host=FIRST).status_code == 404
    assert ap_get(app, f'/.well-known/webfinger?resource=acct:~shopnews@{SHOP}', host=FIRST).status_code == 404


def test_webfinger_without_the_tilde_finds_a_shop_feed_on_shop(app, three_domains, shopnews):
    assert ap_get(app, f'/.well-known/webfinger?resource=acct:shopnews@{SHOP}', host=SHOP).status_code == 200


def test_webfinger_still_answers_a_first_domain_feed(app, three_domains):
    owner = three_domains.account('firstowner', three_domains.first)
    three_domains.local_feed('firstnews', three_domains.first, owner)
    assert ap_get(app, f'/.well-known/webfinger?resource=acct:~firstnews@{FIRST}', host=FIRST).status_code == 200
    assert ap_get(app, f'/.well-known/webfinger?resource=acct:~firstnews@{SHOP}', host=SHOP).status_code == 404


def test_webfinger_does_not_answer_the_onions_feed_anywhere(app, three_domains):
    owner = three_domains.account('onionowner', three_domains.onion)
    three_domains.local_feed('onionnews', three_domains.onion, owner)
    for host in (FIRST, SHOP, ONION):
        for domain in (FIRST, SHOP, ONION):
            assert ap_get(app, f'/.well-known/webfinger?resource=acct:~onionnews@{domain}', host=host).status_code == 404


@pytest.mark.parametrize('resource', ['~shopnews@shop.example', 'acct:~', 'acct:~@', f'acct:~@{SHOP}', f'acct:~~@{SHOP}',
                                      f'acct:~shop@@{SHOP}', f'acct:~shopnews@{SHOP}@{SHOP}', f'acct:@{SHOP}',
                                      f'acct:~{"a" * 5000}@{SHOP}', f'acct:{"~" * 3000}', 'acct:~shopnews@',
                                      f'acct:~shopnews@{SHOP}/x'])
def test_a_junk_webfinger_resource_is_a_400_or_404(app, three_domains, shopnews, resource):
    response = ap_get(app, f'/.well-known/webfinger?resource={resource}', host=SHOP)
    assert response.status_code in (400, 404)


def test_a_shop_feeds_collections_are_on_shop(app, three_domains, shopnews):
    followers = ap_get(app, '/f/shopnews/followers', host=SHOP).json
    moderators = ap_get(app, '/f/shopnews/moderators', host=SHOP).json
    assert followers['id'] == f'{SHOP_BASE}/f/shopnews/followers'
    assert moderators['id'] == f'{SHOP_BASE}/f/shopnews/moderators'
    assert all(url.startswith(f'{SHOP_BASE}/') for url in moderators['orderedItems'])
    for path in ('followers', 'moderators', 'outbox', 'following'):
        assert ap_get(app, f'/f/shopnews/{path}', host=FIRST).status_code == 404


def test_a_follow_of_a_shop_feed_is_accepted_as_shop(app, three_domains, shopnews, peer, monkeypatch):  # noqa: F811
    sends = []
    monkeypatch.setattr(ap_routes, 'send_post_request',
                        lambda uri, body, private_key, key_id, **kw: sends.append((uri, body, key_id)))
    follower = make_user(peer, 'alice')
    follower.ap_fetched_at = utcnow()
    db.session.commit()

    process_inbox_request(inbox_activity(follower, activity_type='Follow', object_uri=f'{SHOP_BASE}/f/shopnews'), True)

    [(uri, body, key_id)] = sends
    assert uri == follower.ap_inbox_url
    assert body['type'] == 'Accept'
    assert body['actor'] == f'{SHOP_BASE}/f/shopnews'
    assert body['id'].startswith(f'{SHOP_BASE}/activities/accept/')
    assert key_id == f'{SHOP_BASE}/f/shopnews#main-key'


@pytest.mark.parametrize('shop_owned', [True, False])
def test_an_announce_of_an_add_is_on_the_feeds_domain(app, three_domains, peer, monkeypatch, shop_owned):  # noqa: F811
    domain = three_domains.shop if shop_owned else three_domains.first
    base = SHOP_BASE if shop_owned else f'https://{FIRST}'
    owner = three_domains.account('feedowner', domain)
    feed = three_domains.local_feed('announcing', domain, owner)
    feed.private_key, feed.public_key = a_keypair()
    community = three_domains.local_community('somewhere', domain)
    peer.inbox = f'https://{peer.domain}/inbox'
    member = make_user(peer, 'bob')
    db.session.add(FeedMember(feed_id=feed.id, user_id=member.id))
    db.session.commit()
    sends = []
    monkeypatch.setattr(shared_feed, 'send_post_request', lambda *args, **kwargs: sends.append(args))
    monkeypatch.setattr(shared_feed, 'instance_banned', lambda domain: False)

    shared_feed.announce_feed_add_remove_to_subscribers('Add', feed.id, community.id)

    [(inbox, activity, _, key_id)] = sends
    assert inbox == peer.inbox
    assert activity['id'].startswith(f'{base}/activities/announce/')
    assert activity['object']['id'].startswith(f'{base}/activities/feedadd/')
    assert activity['actor'] == activity['object']['actor'] == f'{base}/f/announcing'
    assert key_id == f'{base}/f/announcing#main-key'
    if shop_owned:
        assert FIRST not in str(activity)


def test_the_feeds_list_document_is_per_federating_domain(app, three_domains):
    d = three_domains
    for domain, name in ((d.first, 'firstnews'), (d.shop, 'shopnews'), (d.onion, 'onionnews')):
        d.local_feed(name, domain, d.account(f'{name}owner', domain))
    shop = ap_get(app, '/feeds', host=SHOP)
    assert shop.status_code == 200
    assert shop.json['site_view']['public_feeds'] == [f'{SHOP_BASE}/f/shopnews']
    assert shop.json['site_view']['counts']['public_feeds'] == 1
    assert FIRST not in str(shop.json)
    first = ap_get(app, '/feeds', host=FIRST)
    assert first.json['site_view']['public_feeds'] == [f'https://{FIRST}/f/firstnews']
    assert ap_get(app, '/feeds', host=ONION).status_code == 404


def test_an_onion_owned_public_feed_is_in_no_federated_feed_list(app, three_domains):
    """Ruling a2: a public feed an onion account owns is listed by no domain's federated list."""
    d = three_domains
    d.local_feed('onionnews', d.onion, d.account('onionowner', d.onion))
    for domain, name in ((d.first, 'firstnews'), (d.shop, 'shopnews')):
        d.local_feed(name, domain, d.account(f'{name}owner', domain))
    for host in (FIRST, SHOP):
        listed = ap_get(app, '/feeds', host=host).json['site_view']['public_feeds']
        assert listed and not any('onionnews' in url for url in listed)
    assert ap_get(app, '/feeds', host=ONION).status_code == 404


def test_an_onion_owned_feed_on_the_first_domain_is_not_in_its_federated_list(app, three_domains):
    """A feed filed under the first domain but owned by an onion account does not exist to other servers."""
    d = three_domains
    d.local_feed('hidden', d.first, d.account('onionowner', d.onion))
    d.local_feed('firstnews', d.first, d.account('firstowner', d.first))
    listed = ap_get(app, '/feeds', host=FIRST).json['site_view']['public_feeds']
    assert listed == [f'https://{FIRST}/f/firstnews']

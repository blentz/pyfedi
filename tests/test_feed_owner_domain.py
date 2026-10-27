"""A feed's owner domain, names, links and ids follow its home domain, as a community's do (plan F4, task 2)."""
from app import db
from app.federation_urls import activity_id, local_object_for_url, owned_elsewhere, owner_base, owner_domain
from app.local_names import actor_handle, local_path_name, named_by_actor
from app.models import Feed
from app.shared.feed import feed_identity
from tests.factories import make_feed
from tests.three_domains import FIRST, ONION, SHOP, peer, three_domains, two_domains  # noqa: F401


def test_a_local_feeds_owner_is_its_home_domain(app, three_domains, peer):
    for domain in (three_domains.first, three_domains.shop, three_domains.onion):
        feed = three_domains.local_feed('news', domain)
        assert owner_domain(feed) == domain
        assert owner_base(feed) == owner_base(domain)
        assert owned_elsewhere(feed) == (domain != three_domains.first)   # the request here is on the first domain
        assert activity_id('Create', feed).startswith(owner_base(domain))
    assert owner_domain(make_feed(peer, local=False)) is None


def test_feed_identity_for_the_first_domain_is_todays_strings(app, three_domains):
    base = f'https://{FIRST}/f/news'
    assert feed_identity(three_domains.first, 'news') == {
        'ap_profile_id': base, 'ap_public_url': base, 'ap_followers_url': f'{base}/followers',
        'ap_following_url': f'{base}/following', 'ap_outbox_url': f'{base}/outbox', 'ap_domain': FIRST,
        'site_domain_id': three_domains.first.id, 'instance_id': three_domains.first.instance_id}


def test_feed_identity_for_shop_and_the_onion_is_on_their_own_base(app, three_domains):
    for domain, base, authority in ((three_domains.shop, f'https://{SHOP}/f/news', SHOP),
                                    (three_domains.onion, f'http://{ONION}/f/news', ONION)):
        assert feed_identity(domain, 'news') == {
            'ap_profile_id': base, 'ap_public_url': base, 'ap_followers_url': f'{base}/followers',
            'ap_following_url': f'{base}/following', 'ap_outbox_url': f'{base}/outbox', 'ap_domain': authority,
            'site_domain_id': domain.id, 'instance_id': domain.instance_id}


def test_an_onion_feed_is_local_only_and_named_on_the_onion(app, three_domains):
    feed = three_domains.local_feed('news', three_domains.onion)
    assert not owner_domain(feed).federates
    assert feed.stored_link() == f'news@{ONION}'
    assert feed.lemmy_link() == f'~news@{ONION}'


def test_an_unstored_local_feed_url_is_on_its_home_domain(app, three_domains):
    assert Feed(name='bare', site_domain_id=three_domains.shop.id).local_url() == f'https://{SHOP}/f/bare'


def test_each_domains_feed_url_names_its_own_row(app, three_domains):
    first, shop = three_domains.local_feed('news', three_domains.first), three_domains.local_feed('news', three_domains.shop)
    assert local_object_for_url(f'https://{SHOP}/f/news') == shop
    assert local_object_for_url(f'https://{FIRST}/f/news') == first
    assert db.session.query(Feed).filter(named_by_actor(Feed, f'news@{SHOP}')).one() == shop


def test_a_feed_link_is_bare_on_its_storefront_and_qualified_elsewhere(app, three_domains, peer):
    shop = three_domains.local_feed('news', three_domains.shop)
    first = three_domains.local_feed('first', three_domains.first)
    assert shop.stored_link() == f'news@{SHOP}'
    assert first.stored_link() == 'first'
    assert shop.lemmy_link() == f'~news@{SHOP}'
    assert first.lemmy_link() == f'~first@{app.config["SERVER_NAME"]}'
    assert local_path_name(shop) == f'news@{SHOP}'
    assert actor_handle(shop) == f'news@{SHOP}'
    assert shop.link() == f'news@{SHOP}'
    assert first.link() == 'first'
    remote = make_feed(peer, local=False)
    assert remote.link() == remote.ap_id.lower() and remote.stored_link() == remote.ap_id.lower()


def test_an_unstored_feed_ids_fall_back_on_its_home_domain(app, three_domains):
    feed = Feed(name='bare', site_domain_id=three_domains.shop.id)
    assert feed.profile_id() == f'https://{SHOP}/f/bare'
    assert feed.public_url() == f'https://{SHOP}/f/bare'

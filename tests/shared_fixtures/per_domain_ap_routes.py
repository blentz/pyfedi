"""Fixtures from tests/test_per_domain_ap_routes.py that other test modules request (registered in tests/conftest.py)."""
import pytest

from app import db
from app.models import CommunityMember, SiteDomain
from app.storefront import clear_domain_cache, domain_for_host
from tests.test_per_domain_ap_routes import SHOP


@pytest.fixture
def twins(app, two_domains):
    first, onion = two_domains.domains
    db.session.add(SiteDomain(domain=SHOP, scheme='https', federates=True))   # the admin form refuses one until C3
    db.session.commit()
    clear_domain_cache()
    shop = domain_for_host(SHOP)
    shop_bob, shop_art = two_domains.local_user('bob', shop), two_domains.local_community('art', shop)
    onion_bob, onion_art = two_domains.local_user('bob', onion), two_domains.local_community('art', onion)
    first_bob, first_art = two_domains.local_user('bob', first), two_domains.local_community('art', first)
    for user in (shop_bob, onion_bob, first_bob):
        user.ap_public_url = user.ap_profile_id   # as creation stores it: the document names its own domain's URL
    for community, title in ((shop_art, 'Shop art'), (onion_art, 'Onion art'), (first_art, 'First art')):
        community.title = title
    db.session.add(CommunityMember(user_id=two_domains.local_user('fan', shop).id, community_id=shop_art.id))   # the shop's has a follower
    db.session.commit()
    return first_bob, first_art, onion_bob, onion_art, shop_bob, shop_art

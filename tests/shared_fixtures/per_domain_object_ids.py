"""Fixtures from tests/test_per_domain_object_ids.py that other test modules request (registered in tests/conftest.py)."""
from types import SimpleNamespace

import pytest

from app import cache, db
from app.models import SiteDomain
from app.storefront import clear_domain_cache, domain_for_host
from tests.factories import make_community_member, make_instance, make_user
from tests.test_per_domain_object_ids import SHOP, _keyed


@pytest.fixture
def sites(app, two_domains):
    """hal on the first domain, sam on shop.example, a community on each (pit, wares), a peer following both, and rem on
    the peer."""
    first, onion = two_domains.domains
    db.session.add(SiteDomain(domain=SHOP, scheme='https', federates=True))
    db.session.commit()
    clear_domain_cache()
    shop = domain_for_host(SHOP)
    hal, sam = two_domains.local_user('hal', first), two_domains.local_user('sam', shop)
    pit, wares = two_domains.local_community('pit', first), two_domains.local_community('wares', shop)
    for row, kind in ((hal, 'u'), (sam, 'u'), (pit, 'c'), (wares, 'c')):
        _keyed(row, kind)
    peer = make_instance('peer.example', software='lemmy')
    peer.inbox = 'https://peer.example/inbox'
    rem = make_user(peer, 'rem')
    for community in (pit, wares):
        make_community_member(rem, community)
    db.session.commit()
    cache.clear()
    return SimpleNamespace(first=first, onion=onion, shop=shop, hal=hal, sam=sam, pit=pit, wares=wares, peer=peer,
                           rem=rem)

"""Fixtures from tests/test_hosted_url_lookup.py that other test modules request (registered in tests/conftest.py)."""
import socket
from types import SimpleNamespace

import httpx
import pytest
import respx

from app import db
from app.models import SiteDomain
from app.storefront import clear_domain_cache, domain_for_host
from tests.factories import (
    make_community,
    make_instance,
    make_local_feed,
    make_post,
    make_post_reply,
    make_user,
)
from tests.test_hosted_url_lookup import SHOP
from tests.test_no_http_to_hosted import SERVER, _getaddrinfo_without_the_network


@pytest.fixture
def hosted_url_lookup_env(db_session):
    """The first domain (SERVER_NAME) and shop.example, each with an alice and a den: local names are unique per domain
    (C1), so the same name on both is two rows. A shop post in shop's den with a first-domain reply, a site-wide feed, and a
    legacy first-domain post whose ap_id is a URL no path form matches."""
    local = make_instance(SERVER, software='piefed')
    if db.session.query(SiteDomain).filter_by(is_default=True).first() is None:
        db.session.add(SiteDomain(domain=SERVER, is_default=True))
    shop_row = SiteDomain(domain=SHOP)
    db.session.add(shop_row)
    db.session.commit()
    clear_domain_cache()
    shop = make_instance(SHOP, software='piefed')
    peer = make_instance('peer.example', software='lemmy')
    first_alice = make_user(local, 'alice', local=True)   # id 1: make_community's owner
    shop_alice = make_user(shop, 'alice', local=True)
    shop_alice.site_domain_id = shop_row.id
    first_den = make_community('den')
    shop_den = make_community('den', host=SHOP)
    shop_den.site_domain_id, shop_den.instance_id = shop_row.id, shop.id
    db.session.commit()
    post = make_post(shop_den, shop_alice, None, title='a shop post')
    legacy = make_post(first_den, first_alice, f'https://{SERVER}/p/legacy', title='a legacy post')
    reply = make_post_reply(post, first_alice)
    feed = make_local_feed('reads')
    shop_feed = make_local_feed('reads', domain=domain_for_host(SHOP))
    yield SimpleNamespace(local=local, shop=shop, peer=peer, shop_row=shop_row, first_alice=first_alice,
                          shop_alice=shop_alice, first_den=first_den, shop_den=shop_den, post=post, legacy=legacy,
                          reply=reply, feed=feed, shop_feed=shop_feed)
    clear_domain_cache()


@pytest.fixture
def hosted_url_lookup_wire(monkeypatch):
    """As in test_no_http_to_hosted.py: every outbound HTTP request, answered 202. `wire.calls` is what left."""
    monkeypatch.setattr(socket, 'getaddrinfo', _getaddrinfo_without_the_network)
    with respx.mock(assert_all_called=False) as router:
        yield router.route().mock(return_value=httpx.Response(202, html='<html></html>'))

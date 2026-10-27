"""The first domain, a federating sister (shop.example) and the local-only onion, for tests of per-domain feeds (plan F4).

`three_domains` builds on `tests.two_domains.two_domains` and yields a `ThreeDomains`: the three `DomainInfo` rows, and
factories that put an account, a feed or a community straight into a domain. The helpers below it are shared by the
later tasks: `get` and `ap_get` fetch a path as a browser and as an ActivityPub client, `urls_in` lists the URLs a
document names, `sent` records what a delivery posts, and `peer` is a remote instance row."""
from typing import Callable, NamedTuple

import pytest

import app.shared.tasks.pages as pages
import app.activitypub.routes as ap_routes
from app import db
from app.models import Feed, FeedMember, SiteDomain, User
from app.storefront import DomainInfo, clear_domain_cache, domain_for_host
from tests.factories import a_keypair, make_instance, make_local_feed
from tests.test_onion_guard_limits import PROXY
from tests.two_domains import FIRST, ONION, no_http, request_on, routed_get, two_domains  # noqa: F401

AP = {'Accept': 'application/activity+json'}
SHOP = 'shop.example'
PEER = 'peer.example'
__all__ = ['two_domains', 'FIRST', 'ONION', 'SHOP', 'PEER', 'ThreeDomains', 'three_domains', 'request_on', 'get', 'ap_get', 'urls_in',
           'sent', 'peer', 'no_http']


class ThreeDomains(NamedTuple):
    first: DomainInfo
    shop: DomainInfo
    onion: DomainInfo
    account: Callable[..., User]
    local_feed: Callable[..., Feed]
    local_community: Callable[..., object]


@pytest.fixture
def three_domains(app, two_domains, monkeypatch):  # noqa: F811
    monkeypatch.setitem(app.config, 'ONION_PROXY_IP', PROXY)   # so `get(..., host=ONION)` is an onion request
    db.session.add(SiteDomain(domain=SHOP, scheme='https', federates=True))
    db.session.commit()
    clear_domain_cache()

    def account(name: str, domain: DomainInfo) -> User:
        user = two_domains.local_user(name, domain)
        user.private_key, user.public_key = a_keypair()
        user.verified = True
        db.session.commit()
        return user

    def local_feed(name: str, domain: DomainInfo, owner: User | None = None, public: bool = True) -> Feed:
        feed = make_local_feed(name, public=public, domain=domain, user=owner)
        if owner is not None:
            db.session.add(FeedMember(feed_id=feed.id, user_id=owner.id, is_owner=True))
            db.session.commit()
        return feed

    yield ThreeDomains(domain_for_host(FIRST), domain_for_host(SHOP), domain_for_host(ONION), account, local_feed,
                       two_domains.local_community)
    clear_domain_cache()


def ap_get(app, path: str, host: str = FIRST):
    """GET `path` on `host` as an ActivityPub client (through the onion guard for the onion)."""
    return routed_get(app, path, host, headers=AP)


def get(app, path: str, host: str = FIRST):
    """GET `path` on `host` as a browser (no Accept header)."""
    return routed_get(app, path, host, headers={})


def urls_in(document) -> list[str]:
    """Every string in a JSON document that is an http(s) URL, in document order. A loop over no URLs proves nothing,
    so an empty result fails the test at once."""
    found = []

    def walk(node):
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, str) and node.startswith(('http://', 'https://')):
            found.append(node)

    walk(document)
    assert found, 'the document names no URL'
    return found


@pytest.fixture
def sent(monkeypatch):
    """The inboxes a delivery posted to; nothing leaves."""
    inboxes = []

    def recording_send(uri, *args, **kwargs):
        inboxes.append(uri)
        return True

    monkeypatch.setattr(pages, 'send_post_request', recording_send)
    monkeypatch.setattr(ap_routes, 'send_to_remote_instance_fast', lambda inbox, *args: inboxes.append(inbox))
    return inboxes


@pytest.fixture
def peer(app, db_session):
    """A remote instance row."""
    return make_instance(PEER, software='piefed')

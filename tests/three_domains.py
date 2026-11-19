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
from tests.two_domains import FIRST, ONION, request_on, routed_get

AP = {'Accept': 'application/activity+json'}
SHOP = 'shop.example'
PEER = 'peer.example'
# Its fixtures (three_domains, peer, sent, and two_domains' own) are registered by tests/conftest.py, not exported here
__all__ = ['FIRST', 'ONION', 'SHOP', 'PEER', 'ThreeDomains', 'request_on', 'get', 'ap_get', 'urls_in']


class ThreeDomains(NamedTuple):
    first: DomainInfo
    shop: DomainInfo
    onion: DomainInfo
    account: Callable[..., User]
    local_feed: Callable[..., Feed]
    local_community: Callable[..., object]


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



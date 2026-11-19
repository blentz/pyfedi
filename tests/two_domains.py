"""A site hosting two domains, the first domain's default and an onion, for tests of per-domain names (plan C1).

`two_domains` yields a `TwoDomains`: the two `DomainInfo` rows, the first domain's instance row (the onion has its own, made
with its domain row: `domains[1].instance_id`), and factories that insert a local user or community straight into a
domain, on that domain's instance row (creation refuses a name another domain holds until C1's Task 5, and a test of
the helpers needs the duplicate to exist). `onion_request` and `default_request` run a request context addressed to a
domain."""
from contextlib import contextmanager
from typing import NamedTuple

import pytest
from flask import g

import app.activitypub.actor as ap_actor
import app.activitypub.util as ap_util
import app.community.util as community_util
import app.feed.util as feed_util
import app.user.utils as user_utils
from app import db
from app.models import Community, SiteDomain, User
from app.storefront import DomainInfo, clear_domain_cache, domain_for_host
from tests.factories import make_instance, make_user
from tests.test_onion_guard_limits import PROXY, a_session, onion_get

FIRST = 'test.piefed.local'      # the test app's SERVER_NAME
ONION = 'abc.onion'


class TwoDomains(NamedTuple):
    domains: tuple[DomainInfo, DomainInfo]
    instance: object
    local_user: object
    local_community: object


@contextmanager
def request_on(app, domain: DomainInfo):
    with app.test_request_context():
        g.storefront = domain
        yield


@contextmanager
def onion_request(app):
    with request_on(app, domain_for_host(ONION)):
        yield


@contextmanager
def default_request(app):
    with request_on(app, domain_for_host(FIRST)):
        yield


def routed_get(app, path, host=FIRST, headers=None):
    """GET `path` on `host`; the onion goes through the onion guard (a session first, as a browser does it), which needs
    `app.config['ONION_PROXY_IP'] = PROXY` (the `three_domains` fixture sets it)."""
    client = app.test_client()
    if host == ONION:
        a_session(client)
        return onion_get(client, path, headers=headers)
    return client.get(path, headers=headers, base_url=f'https://{host}')


def register_local_only_onion() -> str:
    """the first domain's default row and the local-only onion, with no fixture: for a test that needs only a URL the onion
    names (an "is it ours" guard that refuses it). Returns the onion's origin."""
    if db.session.query(SiteDomain).filter_by(is_default=True).first() is None:
        db.session.add(SiteDomain(domain=FIRST, scheme='https', is_default=True))
    db.session.add(SiteDomain(domain=ONION, scheme='http', federates=False))
    db.session.commit()
    clear_domain_cache()
    return f'http://{ONION}'



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


def register_local_only_onion() -> str:
    """the first domain's default row and the local-only onion, with no fixture: for a test that needs only a URL the onion
    names (an "is it ours" guard that refuses it). Returns the onion's origin."""
    if db.session.query(SiteDomain).filter_by(is_default=True).first() is None:
        db.session.add(SiteDomain(domain=FIRST, scheme='https', is_default=True))
    db.session.add(SiteDomain(domain=ONION, scheme='http', federates=False))
    db.session.commit()
    clear_domain_cache()
    return f'http://{ONION}'


@pytest.fixture
def two_domains(app, db_session):
    instance = make_instance(FIRST, software='piefed')
    if db.session.query(SiteDomain).filter_by(is_default=True).first() is None:
        db.session.add(SiteDomain(domain=FIRST, scheme='https', is_default=True))
    db.session.add(SiteDomain(domain=ONION, scheme='http', federates=False))
    db.session.commit()
    clear_domain_cache()

    def local_user(name: str, domain: DomainInfo) -> User:
        user = User(user_name=name, email=f'{name}-{domain.domain}@example.com', instance_id=domain.instance_id,
                    verified=True, banned=False, site_domain_id=domain.id,
                    ap_profile_id=f'{domain.scheme}://{domain.domain}/u/{name}')
        db.session.add(user)
        db.session.commit()
        return user

    def owner() -> User:
        return User.query.filter_by(user_name='community-owner').first() or \
            make_user(instance, 'community-owner', local=True)

    def local_community(name: str, domain: DomainInfo) -> Community:
        base = f'{domain.scheme}://{domain.domain}/c/{name}'
        community = Community(name=name, title=name, instance_id=domain.instance_id, user_id=owner().id, site_domain_id=domain.id,
                              ap_profile_id=base, ap_public_url=base, ap_followers_url=f'{base}/followers',
                              ap_domain=domain.domain, subscriptions_count=0, local_only=False, nsfw=False)
        db.session.add(community)
        db.session.commit()
        return community

    yield TwoDomains((domain_for_host(FIRST), domain_for_host(ONION)), instance, local_user, local_community)
    clear_domain_cache()


@pytest.fixture
def no_http(monkeypatch):
    """The list of URLs fetched. Any fetch fails the test: a hosted handle is in the database or nowhere."""
    made = []

    def recording_get_request(url, *args, **kwargs):
        made.append(url)
        raise AssertionError(f'fetched {url}')

    for module in (user_utils, community_util, feed_util, ap_util, ap_actor):
        monkeypatch.setattr(module, 'get_request', recording_get_request, raising=False)
    return made

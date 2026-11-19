"""Fixtures from tests/three_domains.py that other test modules request (registered in tests/conftest.py)."""
import pytest

import app.activitypub.routes as ap_routes
import app.shared.tasks.pages as pages
from app import db
from app.models import Feed, FeedMember, SiteDomain, User
from app.storefront import DomainInfo, clear_domain_cache, domain_for_host
from tests.factories import a_keypair, make_instance, make_local_feed
from tests.test_onion_guard_limits import PROXY
from tests.three_domains import PEER, SHOP, ThreeDomains
from tests.two_domains import FIRST, ONION


@pytest.fixture
def three_domains_peer(app, db_session):
    """A remote instance row."""
    return make_instance(PEER, software='piefed')


@pytest.fixture
def three_domains_sent(monkeypatch):
    """The inboxes a delivery posted to; nothing leaves."""
    inboxes = []

    def recording_send(uri, *args, **kwargs):
        inboxes.append(uri)
        return True

    monkeypatch.setattr(pages, 'send_post_request', recording_send)
    monkeypatch.setattr(ap_routes, 'send_to_remote_instance_fast', lambda inbox, *args: inboxes.append(inbox))
    return inboxes


@pytest.fixture
def three_domains(app, two_domains, monkeypatch):
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

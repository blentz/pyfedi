"""Fixtures from tests/test_local_only_invisible.py that other test modules request (registered in tests/conftest.py)."""
from types import SimpleNamespace

import pytest

from app import db
from app.models import CommunityMember, SiteDomain, utcnow
from app.storefront import clear_domain_cache
from tests.factories import make_community, make_instance, make_user


@pytest.fixture
def local_only_invisible_world(app, db_session):
    """The first domain (the default domain) and an onion that does not federate, each with an account and a community.

    alice and bob are the first domain accounts; onionite is the onion's. `clearnet` is the first domain's community, `hidden`
    the onion's. alice owns both communities."""
    make_instance(app.config['SERVER_NAME'])   # the local instance, id 1
    default = db.session.query(SiteDomain).filter_by(is_default=True).first()
    if default is None:
        default = SiteDomain(domain=app.config['SERVER_NAME'], is_default=True)
        db.session.add(default)
    onion = SiteDomain(domain='abc.onion', scheme='http', federates=False)
    db.session.add(onion)
    db.session.commit()
    clear_domain_cache()
    alice = make_user(None, 'alice', local=True, with_keys=True)
    bob = make_user(None, 'bob', local=True)
    onionite = make_user(None, 'onionite', local=True, with_keys=True)
    alice.site_domain_id = default.id
    onionite.site_domain_id = onion.id
    onionite.ap_profile_id = onionite.ap_public_url = 'http://abc.onion/u/onionite'
    for user in (alice, bob, onionite):
        user.last_seen = utcnow()
        # their profiles show every post and comment, as these tests were written for (spec 2026-11-26-profile-privacy)
        user.profile_community_posts = user.profile_comments = True
    clearnet = make_community('clearnet', host=app.config['SERVER_NAME'])
    hidden = make_community('hidden', host='abc.onion')
    clearnet.user_id = hidden.user_id = alice.id
    hidden.site_domain_id = onion.id
    for community in (clearnet, hidden):
        db.session.add(CommunityMember(user_id=alice.id, community_id=community.id, is_owner=True,
                                       is_moderator=True))
    db.session.commit()
    yield SimpleNamespace(default=default, onion=onion, alice=alice, bob=bob, onionite=onionite,
                          clearnet=clearnet, hidden=hidden)
    clear_domain_cache()

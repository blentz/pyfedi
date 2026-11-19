"""Fixtures from tests/test_onion_feeds_hidden.py that other test modules request (registered in tests/conftest.py)."""
import pytest

from app import db
from tests.factories import a_keypair
from tests.test_onion_feeds_hidden import World


@pytest.fixture
def onion_feeds_hidden_world(app, three_domains):
    """An onion account owning a feed on the onion and a legacy one filed under the first domain (ruling N-m8), and a
    first-domain account owning a feed of its own."""
    d = three_domains
    w = World()
    w.d = d
    w.onion_owner = d.account('onionowner', d.onion)
    w.first_owner = d.account('firstowner', d.first)
    w.onion_feed = d.local_feed('onionfeed', d.onion, w.onion_owner)
    w.legacy_feed = d.local_feed('legacyfeed', d.first, w.onion_owner)
    w.first_feed = d.local_feed('firstfeed', d.first, w.first_owner)
    for feed in (w.onion_feed, w.legacy_feed, w.first_feed):
        feed.private_key, feed.public_key = a_keypair()
    db.session.commit()
    return w

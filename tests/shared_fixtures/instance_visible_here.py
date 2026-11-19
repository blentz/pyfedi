"""Fixtures from tests/test_instance_visible_here.py that other test modules request (registered in tests/conftest.py)."""
from types import SimpleNamespace

import pytest

from app import db
from app.models import Instance, Site
from tests.factories import make_instance, make_post
from tests.test_instance_visible_here import PEER
from tests.two_domains import ONION


@pytest.fixture
def instance_visible_here_scene(app, twins):
    """The first domain, the onion and shop.example, each with a local user and community, and a remote peer. The onion's
    row is given what a peer's has (software, a post), so only its being local-only keeps it out."""
    first_bob, first_art, onion_bob, onion_art, shop_bob, shop_art = twins
    onion_bob.private_key = first_bob.private_key = 'a key'   # approved accounts (approval_required)
    onion_row = db.session.get(Instance, onion_bob.instance_id)
    onion_row.software = 'piefed'
    peer = make_instance(PEER, software='piefed')
    db.session.get(Site, 1).private_instance = False   # anonymous visitors reach the pages
    db.session.commit()
    make_post(onion_art, onion_bob, f'http://{ONION}/post/1', title='ONIONPOST').local_only = True   # as creation stamps it
    db.session.commit()
    return SimpleNamespace(first_bob=first_bob, onion_bob=onion_bob, onion_art=onion_art, onion_row=onion_row,
                           shop_row=db.session.get(Instance, shop_bob.instance_id), peer=peer)

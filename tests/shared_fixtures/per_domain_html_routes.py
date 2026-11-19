"""Fixtures from tests/test_per_domain_html_routes.py that other test modules request (registered in tests/conftest.py)."""
from types import SimpleNamespace

import pytest

from app import db
from app.models import Site
from tests.two_domains import FIRST, ONION


@pytest.fixture
def per_domain_html_routes_world(app, two_domains, monkeypatch):
    """Onion rows first, so the heap does not hand the first domain's row to an unscoped lookup by accident of order."""
    first, onion = two_domains.domains
    onion_bob, onion_art = two_domains.local_user('bob', onion), two_domains.local_community('art', onion)
    first_bob, first_art = two_domains.local_user('bob', first), two_domains.local_community('art', first)
    onion_art.title, first_art.title = 'Onion art', 'First art'
    onion_bob.set_password('onion password')
    first_bob.set_password('first password')
    viewers = {ONION: two_domains.local_user('viewer', onion), FIRST: two_domains.local_user('watcher', first)}
    for viewer in viewers.values():
        viewer.private_key = 'a key'   # an approved account (approval_required)
    db.session.get(Site, 1).private_instance = False   # the RSS feeds answer
    monkeypatch.setitem(app.config, 'RSS_FEEDS', True)
    db.session.commit()
    sent = []
    monkeypatch.setattr('app.community.routes.send_post_request', lambda *args, **kwargs: sent.append(args))
    bob = {FIRST: first_bob, ONION: onion_bob}
    art = {FIRST: first_art, ONION: onion_art}
    return SimpleNamespace(bob=bob, art=art, viewers=viewers, sent=sent)

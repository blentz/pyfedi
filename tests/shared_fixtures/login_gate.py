"""Fixtures from tests/test_login_gate.py that other test modules request (registered in tests/conftest.py)."""
import pytest

from app import db
from app.models import Site
from tests.factories import (
    make_community,
    make_instance,
    make_post,
    make_site,
    make_user,
)
from tests.test_login_gate import SECRET, SIDEBAR


@pytest.fixture
def private(app, db_session, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setitem(app.config, 'ENABLE_ALPHA_API', 'true')
    make_site()
    site = db.session.get(Site, 1)
    site.private_instance = True
    make_instance('test.piefed.local', software='piefed')
    peer = make_instance('peer.example', software='lemmy')
    author = make_user(peer, 'author')
    author.ap_profile_id = 'https://peer.example/u/author'
    author.ap_domain = 'peer.example'
    community = make_community('general')
    community.private = False
    community.description_html = f'<p>{SIDEBAR}</p>'
    post = make_post(community, author, 'https://peer.example/p/1', title=SECRET)
    db.session.commit()
    return SimpleNamespace(site=site, community=community, post=post)

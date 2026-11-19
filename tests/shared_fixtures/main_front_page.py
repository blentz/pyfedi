"""Fixtures from tests/test_main_front_page.py that other test modules request (registered in tests/conftest.py)."""
import pytest
from flask import g

from app import db
from app.models import Site
from tests.factories import make_community, make_post


@pytest.fixture
def main_front_page_env(app, api_baseline):
    """A public instance with one post on its front page.

    `api_baseline`'s Site is PRIVATE, which redirects every anonymous page to
    the login screen -- so a test of the public front page has to say
    otherwise, and a test of the gate has to put it back.
    """
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    g.site.private_instance = False
    community = make_community('probeland')
    community.show_all = True
    author = api_baseline.user2
    post = make_post(community, author, ap_id='https://test.piefed.local/z/1')
    post.title = 'the first post'
    db.session.commit()
    return SimpleNamespace(client=app.test_client(), site=g.site,
                           community=community, author=author, post=post,
                           reader=api_baseline.user3,
                           baseline=api_baseline)

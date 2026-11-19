"""Fixtures from tests/test_api_search_resolve.py that other test modules request (registered in tests/conftest.py)."""
import pytest
from flask import g

from app import db
from app.models import Site
from tests.factories import make_community, make_post_reply
from tests.test_api_search_resolve import local_url


@pytest.fixture
def api_search_resolve_env(app, api_baseline):
    """Everything here is addressed by url, so each row's actor needs an
    `ap_profile_id` on this instance."""
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    user = api_baseline.user2
    author = api_baseline.user3
    community = make_community('general')
    community.ap_profile_id = local_url('/c/general')
    community.ap_id = None
    post = api_baseline.post1
    post.ap_id = local_url(f'/p/{post.id}')
    reply = make_post_reply(post, author, body='a reply')
    reply.body_html = '<p>a reply</p>'
    reply.ap_id = local_url(f'/comment/{reply.id}')
    db.session.commit()
    return user, author, community, post, reply, api_baseline

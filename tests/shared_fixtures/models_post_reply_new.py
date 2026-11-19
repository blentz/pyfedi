"""Fixtures from tests/test_models_post_reply_new.py that other test modules request (registered in tests/conftest.py)."""
import pytest
from flask import g

from app import db
from app.models import Site
from tests.factories import make_community, make_community_member, make_post


@pytest.fixture
def models_post_reply_new_env(app, api_baseline):
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    g.site.enable_gif_reply_rep_decrease = False
    g.site.enable_this_comment_filter = False
    community = make_community('probeland')
    author = api_baseline.user2
    replier = api_baseline.user3
    db.session.commit()
    make_community_member(author, community)
    make_community_member(replier, community)
    post = make_post(community, author, ap_id='https://test.piefed.local/p/1')
    db.session.commit()
    return SimpleNamespace(community=community, author=author,
                           replier=replier, post=post,
                           baseline=api_baseline)

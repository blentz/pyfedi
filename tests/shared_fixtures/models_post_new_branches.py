"""Fixtures from tests/test_models_post_new_branches.py that other test modules request (registered in tests/conftest.py)."""
import pytest
from flask import current_app, g

from app import db
from app.models import Site
from tests.factories import make_community, make_community_member, make_user
from tests.test_models_post_new_branches import AUTHOR


@pytest.fixture
def models_post_new_branches_env(app, api_baseline, monkeypatch):
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    community = make_community('probeland')
    author = make_user(api_baseline.instance_remote, 'someone')
    author.ap_id = 'someone@remote.test'
    author.ap_profile_id = AUTHOR
    author.ap_public_url = AUTHOR
    db.session.commit()
    make_community_member(author, community)
    monkeypatch.setitem(current_app.config, 'IMAGE_HASHING_ENDPOINT', '')
    local = make_user(api_baseline.instance_local, 'localuser', local=True)
    local.ap_profile_id = f"{current_app.config['SERVER_URL']}/u/localuser"
    db.session.commit()
    return SimpleNamespace(community=community, author=author, local=local,
                           baseline=api_baseline)

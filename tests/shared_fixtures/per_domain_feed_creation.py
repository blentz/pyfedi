"""Fixtures from tests/test_per_domain_feed_creation.py that other test modules request (registered in tests/conftest.py)."""
import pytest
from flask import g

from app import db
from app.models import Site


@pytest.fixture
def api_site(app, site):
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    g.site.enable_nsfw = g.site.enable_nsfl = True
    db.session.commit()

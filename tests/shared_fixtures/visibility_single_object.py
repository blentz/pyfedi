"""Fixtures from tests/test_visibility_single_object.py that other test modules request (registered in tests/conftest.py)."""
import pytest
from flask import current_app, g

from app import db
from app.models import Language, Site
from tests.factories import make_visibility_world


@pytest.fixture
def visibility_single_object_world(app, db_session, monkeypatch):
    w = make_visibility_world()
    site = db.session.get(Site, 1)
    site.private_instance = False
    g.admin_ids = []
    g.site = site
    monkeypatch.setitem(current_app.config, 'ENABLE_ALPHA_API', 'true')
    db.session.add(Language(code='und', name='Undetermined'))
    db.session.add(Language(code='en', name='English'))
    db.session.commit()
    site.language_id = Language.query.filter_by(code='en').one().id
    return w

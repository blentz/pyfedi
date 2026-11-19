"""Fixtures from tests/test_visibility_moderation.py that other test modules request (registered in tests/conftest.py)."""
import pytest
from flask import current_app, g

from app import db
from app.models import Language, Site
from tests.factories import grant_permission, make_user, make_visibility_world


@pytest.fixture
def visibility_moderation_world(app, db_session, monkeypatch):
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
    w.admin = make_user(w.stranger.instance, 'ada', local=True)
    w.admin.private_key = 'a local account with keys'
    db.session.commit()
    role = grant_permission(w.admin, 'moderate all communities')
    role.built_in = True  # the built-in Admin role (RB16): the web removal routes act only for an admin
    db.session.commit()
    w.stranger.private_key = 'a local account with keys'
    db.session.commit()
    return w

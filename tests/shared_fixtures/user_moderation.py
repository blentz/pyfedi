"""Fixtures from tests/test_user_moderation.py that other test modules request (registered in tests/conftest.py)."""
import pytest

from app import db
from app.models import Site
from tests.factories import make_user
from tests.test_user_moderation import as_user, instance, resolvable


@pytest.fixture
def user_moderation_env(app, db_session):
    site = db.session.get(Site, 1)
    site.private_instance = False
    db.session.commit()
    local = instance()
    founder = make_user(local, 'founder', local=True)
    assert founder.id == 1  # fact 347
    viewer = make_user(local, 'viewer', local=True)
    target = make_user(local, 'target', local=True)
    db.session.commit()
    for user in (founder, viewer, target):
        resolvable(user)
    return as_user(app, viewer), founder, viewer, target

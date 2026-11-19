"""Fixtures from tests/test_user_files_notifications.py that other test modules request (registered in tests/conftest.py)."""
import pytest

from app import db
from app.models import Site
from tests.factories import make_user
from tests.test_user_files_notifications import as_user, instance


@pytest.fixture
def user_files_notifications_env(app, db_session):
    site = db.session.get(Site, 1)
    site.private_instance = False
    db.session.commit()
    local = instance()
    founder = make_user(local, 'founder', local=True)
    assert founder.id == 1  # fact 347
    owner = make_user(local, 'owner', local=True)
    other = make_user(local, 'other', local=True)
    db.session.commit()
    for user in (owner, other):
        user.ap_profile_id = f'https://test.piefed.local/u/{user.user_name}'
    db.session.commit()
    return as_user(app, owner), owner, other

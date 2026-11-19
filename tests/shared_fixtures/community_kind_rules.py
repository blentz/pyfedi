"""Fixtures from tests/test_community_kind_rules.py that other test modules request (registered in tests/conftest.py)."""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app import db
from app.models import Language, Site, utcnow
from tests.factories import (
    make_community,
    make_community_member,
    make_instance,
    make_user,
)
from tests.test_community_kind_rules import csrf, login


@pytest.fixture
def community_kind_rules_web(app, db_session, site):
    """User 1, an admin who clears every gate before `add_local`, who also owns `general`."""
    founder = make_user(make_instance('test.piefed.local'), 'founder', local=True)
    assert founder.id == 1
    founder.verified = True
    founder.private_key = 'a private key'
    founder.created = utcnow() - timedelta(days=30)
    db.session.add(Language(code='und', name='Undetermined'))
    english = Language(code='en', name='English')
    db.session.add(english)
    db.session.get(Site, 1).community_creation_admin_only = False
    community = make_community('general')
    membership = make_community_member(founder, community, is_moderator=True)
    membership.is_owner = True
    db.session.commit()
    client = app.test_client()
    login(client, founder)
    return SimpleNamespace(app=app, client=client, token=csrf(app, client), founder=founder, english=english,
                           community=community)

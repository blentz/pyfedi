"""Fixtures from tests/connections_fake.py and tests/test_connections.py that other test modules request (registered in
tests/conftest.py)."""
from types import SimpleNamespace

import pytest

from app import db
from app.connections import SERVICES, setting_name
from app.connections.tokens import seal_tokens
from app.models import SocialConnection
from app.sealing import generate_key
from app.utils import set_setting
from tests.connections_fake import FakeService


@pytest.fixture
def fake_service(app, db_session, monkeypatch):
    service = FakeService()
    monkeypatch.setitem(app.config, 'CONNECTIONS_SECRETS_KEY', generate_key())
    monkeypatch.setitem(SERVICES, service.name, service)
    set_setting(setting_name(service.name), True)
    return service


@pytest.fixture
def connections_world(app, community_add_post_poster, fake_service):
    client, token, community, author, english = community_add_post_poster
    connection = SocialConnection(user_id=author.id, service='fake', remote_id='acct-1', remote_name='@me@fake',
                                  sealed_tokens=seal_tokens({'access': 'tok-1', 'expires_at': None}))
    db.session.add(connection)
    db.session.commit()
    return SimpleNamespace(client=client, token=token, community=community, author=author, english=english,
                           connection=connection, service=fake_service, app=app)

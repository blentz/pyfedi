"""Blocking an instance from a chat that names no instance (fuzz-routes finding, 2026-11-13): chat.block_instance
inserted an InstanceBlock for any id, and an id with no instance broke its foreign key, a 500. As
/instance/<id>/block does, an instance that does not exist is a 404."""
import pytest

from app import db
from app.models import InstanceBlock
from tests.factories import make_instance, make_user
from tests.test_admin_site_profile import csrf, login

pytestmark = pytest.mark.usefixtures('site')


def test_blocking_an_instance_that_does_not_exist_is_not_found(app, db_session):
    user = make_user(make_instance('test.piefed.local', software='piefed'), 'chatblocker', local=True)
    user.verified = True
    db.session.commit()
    client = app.test_client()
    login(client, user)
    token = csrf(app, client)
    assert client.post('/chat/999999/block_instance', data={'csrf_token': token}).status_code == 404


def test_blocking_an_instance_that_exists_still_blocks_it(app, db_session):
    user = make_user(make_instance('test.piefed.local', software='piefed'), 'chatblocker2', local=True)
    user.verified = True
    remote = make_instance('remote.example', software='piefed')
    db.session.commit()
    client = app.test_client()
    login(client, user)
    token = csrf(app, client)
    assert client.post(f'/chat/{remote.id}/block_instance', data={'csrf_token': token}).status_code == 302
    assert db.session.query(InstanceBlock).filter_by(user_id=user.id, instance_id=remote.id).count() == 1

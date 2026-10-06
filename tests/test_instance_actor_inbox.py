"""Interop D24: /actor advertises /actor/inbox; a peer that ignores sharedInbox delivers there."""
import pytest

from app.activitypub import routes

pytestmark = pytest.mark.usefixtures('site')


def test_actor_inbox_is_handled_by_the_shared_inbox(app, db_session, monkeypatch):
    calls = []
    monkeypatch.setattr(routes, 'shared_inbox', lambda: calls.append('shared') or ('', 200))
    response = app.test_client().post('/actor/inbox', data=b'{}', content_type='application/activity+json')

    assert response.status_code == 200
    assert calls == ['shared']


def test_actor_inbox_refuses_get(app, db_session):
    assert app.test_client().get('/actor/inbox').status_code == 405

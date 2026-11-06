"""The admin's payments area (spec 2026-11-05-monetization-infrastructure-design.md, plan phase 1 Tasks 1 and 9)."""
from types import SimpleNamespace

import pytest
from flask import g

from app import db
from app.models import Site
from tests.factories import grant_permission, make_user


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    return SimpleNamespace(app=app, client=app.test_client(), baseline=api_baseline)


def login(client, user):
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True


def somebody(env, name, *permissions):
    user = make_user(env.baseline.instance_local, name, local=True)
    user.verified = True
    db.session.commit()
    for permission in permissions:
        grant_permission(user, permission)
    return user


class TestTheTab:
    def test_a_holder_sees_it(self, env):
        login(env.client, somebody(env, 'holder', 'manage monetization'))
        assert b'id="payments-tab"' in env.client.get('/admin/payments').data

    def test_somebody_with_another_permission_does_not(self, env):
        login(env.client, somebody(env, 'cms', 'edit cms pages'))
        assert b'id="payments-tab"' not in env.client.get('/admin/pages').data

"""Fixtures from tests/test_ap_new_instance_profile.py that other test modules request (registered in tests/conftest.py)."""
import pytest
from flask import g

from app import db
from app.models import Site
from tests.factories import make_instance
from tests.test_ap_new_instance_profile import DOMAIN


@pytest.fixture
def ap_new_instance_profile_env(app, api_baseline, http_mock, monkeypatch):
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    instance = make_instance(DOMAIN, software='')
    instance.inbox = None
    instance.outbox = None
    db.session.commit()
    monkeypatch.setattr('app.activitypub.util.time.sleep',
                        lambda seconds: None)
    return SimpleNamespace(instance=instance, http=http_mock,
                           baseline=api_baseline)

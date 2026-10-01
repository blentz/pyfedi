"""R207: a request body over `MAX_CONTENT_LENGTH` is refused with 413 before any form
or API schema reads it.

The application set no `MAX_CONTENT_LENGTH`, so an upload was whatever size the client
sent: only a `.gif` on the image form and a banner on the event form were capped, and
only after the whole body had been read. It now comes from the environment, defaulting
to 100 MB (owner ruling 2026-09-30). The rows below shrink the limit to a kilobyte so
nothing large has to be sent.
"""
import io
from types import SimpleNamespace

import pytest
from flask import g

from app import db
from app.models import Site
from config import Config

LIMIT = 1024


@pytest.fixture
def env(app, api_baseline, monkeypatch):
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    monkeypatch.setitem(app.config, 'MAX_CONTENT_LENGTH', LIMIT)
    return SimpleNamespace(client=app.test_client(), app=app)


def test_the_default_limit_is_100_mb():
    assert Config.MAX_CONTENT_LENGTH == 100 * 1024 * 1024


def test_an_oversized_web_form_post_gets_a_rendered_413(env):
    response = env.client.post('/auth/login', data={'user_name': 'x' * (LIMIT * 2)})

    assert response.status_code == 413
    assert 'too large' in response.get_data(as_text=True)


def test_an_oversized_api_upload_gets_a_json_413(env):
    response = env.client.post('/api/alpha/upload/image',
                               data={'file': (io.BytesIO(b'\x00' * (LIMIT * 2)), 'photo.png')},
                               content_type='multipart/form-data')

    assert response.status_code == 413
    assert response.get_json()['code'] == 413

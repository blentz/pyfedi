"""Fixtures from tests/test_local_only_actors_hidden.py that other test modules request (registered in tests/conftest.py)."""
from types import SimpleNamespace

import pytest

from app import db
from app.models import File, utcnow


@pytest.fixture
def local_only_actors_hidden_world(app, instance_visible_here_scene, twins):
    first_bob, first_art, onion_bob, onion_art, *_ = twins
    avatar = File(file_path='app/static/media/avatar.png')
    db.session.add(avatar)
    db.session.flush()
    for user in (first_bob, onion_bob):   # what /instance/people/interesting asks of an account
        user.post_count = user.post_reply_count = user.reputation = 5
        user.last_seen = utcnow()
        user.avatar_id = avatar.id
    db.session.commit()
    return SimpleNamespace(first_bob=first_bob, first_art=first_art, onion_bob=onion_bob, onion_art=onion_art)

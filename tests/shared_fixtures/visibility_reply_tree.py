"""Fixtures from tests/test_visibility_reply_tree.py that other test modules request (registered in tests/conftest.py)."""
import pytest


@pytest.fixture
def visibility_reply_tree_csrf_on(app, monkeypatch):
    # the post page renders form.csrf_token(), which does not exist while CSRF is off
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', True)

"""Fixtures from tests/test_mastodon_statuses.py that other test modules request (registered in tests/conftest.py)."""
import pytest

from tests.factories import make_community, make_user


@pytest.fixture
def mastodon_statuses_world(app, api_baseline):
    author = make_user(None, 'statusauthor', local=True)
    viewer = make_user(None, 'statusviewer', local=True)
    community = make_community('statuses')
    return {'author': author, 'viewer': viewer, 'community': community}

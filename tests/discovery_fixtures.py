"""Helpers shared by the discovery tests (interop D24). Defined once; import them into each test file."""
import pytest

from app import cache


def nobody_excluded(host):
    """An `exclude` callback that lets every host through."""
    return False


@pytest.fixture
def fresh_cache():
    """instance_banned and peertube_isolated_hosts are memoized: start and finish each test with a cold cache.
    Import it into a test file, then request it by name or put it in `pytestmark = pytest.mark.usefixtures(...)`."""
    cache.clear()
    yield
    cache.clear()

"""Fixtures from tests/test_no_http_to_hosted.py that other test modules request (registered in tests/conftest.py)."""
import socket

import httpx
import pytest
import respx

from tests.test_no_http_to_hosted import _getaddrinfo_without_the_network


@pytest.fixture
def no_http_to_hosted_wire(monkeypatch):
    """Every outbound HTTP request, answered 202. `wire.calls` is what left the server."""
    monkeypatch.setattr(socket, 'getaddrinfo', _getaddrinfo_without_the_network)
    with respx.mock(assert_all_called=False) as router:
        yield router.route().mock(return_value=httpx.Response(202, html='<html></html>'))

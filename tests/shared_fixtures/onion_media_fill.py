"""Fixtures from tests/test_onion_media_fill.py that other test modules request (registered in tests/conftest.py)."""
import socket

import httpx
import pytest

from tests.test_onion_media_fill import GIB, Remote, _real_getaddrinfo, free_disk


@pytest.fixture
def remote(app, db_session, onion_redis, tmp_path, monkeypatch):
    def getaddrinfo(host, *args, **kwargs):
        if isinstance(host, str) and host.endswith('.example'):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 0))]
        return _real_getaddrinfo(host, *args, **kwargs)

    web = Remote()
    monkeypatch.setattr(socket, 'getaddrinfo', getaddrinfo)
    monkeypatch.setattr(app, 'debug', False)
    monkeypatch.setitem(app.config, 'MEDIA_PROXY_DIR', str(tmp_path))
    monkeypatch.setattr('app.httpx_client', httpx.Client(transport=httpx.MockTransport(web.handler)))
    free_disk(monkeypatch, 100 * GIB)
    return web

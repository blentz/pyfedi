"""fastapi_server's lifespan: the shared HTTP client and the Redis listener start with the
app, and the client closes with it. (It replaced the deprecated @app.on_event handlers.)"""
from fastapi.testclient import TestClient

import fastapi_server


def test_starting_the_app_opens_the_client_and_starts_the_listener_and_stopping_closes_it(monkeypatch):
    started = []

    async def listener():
        started.append(True)

    monkeypatch.setattr(fastapi_server, 'redis_listener', listener)

    with TestClient(fastapi_server.app):
        client = fastapi_server.http_client
        assert client is not None and not client.is_closed

    assert started == [True]
    assert client.is_closed

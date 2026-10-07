"""fastapi_server.py's /live/stream: one broadcast SSE stream per Live feed.

The stream never ends, so the route is called directly rather than read through
TestClient; the generator and the fan-out are driven with asyncio.run.
"""
import asyncio

import pytest
from fastapi.testclient import TestClient

import fastapi_server


@pytest.fixture(autouse=True)
def no_clients():
    fastapi_server.live_clients.clear()
    fastapi_server.connected_clients.clear()
    yield
    fastapi_server.live_clients.clear()
    fastapi_server.connected_clients.clear()


def test_an_unknown_feed_is_404():
    response = TestClient(fastapi_server.app).get('/live/stream?feed=nosuch')

    assert response.status_code == 404


def test_a_known_feed_returns_an_event_stream_and_registers_nothing_until_it_starts():
    async def scenario():
        response = await fastapi_server.live_stream('microblogs')
        before = dict(fastapi_server.live_clients)
        body = response.body_iterator
        await body.__anext__()
        registered = len(fastapi_server.live_clients['microblogs'])
        await body.aclose()
        return response, before, registered

    response, before, registered = asyncio.run(scenario())

    assert response.media_type == 'text/event-stream'
    assert response.headers['x-accel-buffering'] == 'no'
    assert before == {} and registered == 1
    assert 'microblogs' not in fastapi_server.live_clients


def test_the_stream_says_connected_then_relays_then_heartbeats_then_cleans_up(monkeypatch):
    monkeypatch.setattr(fastapi_server, 'LIVE_HEARTBEAT_SECONDS', 0.01)

    async def scenario():
        other = asyncio.Queue(maxsize=1)
        fastapi_server.live_clients['microblogs'] = {other}
        stream = fastapi_server.live_event_stream('microblogs')
        first = await stream.__anext__()
        q = (fastapi_server.live_clients['microblogs'] - {other}).pop()
        await q.put('{}')
        second = await stream.__anext__()
        third = await stream.__anext__()
        await stream.aclose()
        still_there = fastapi_server.live_clients['microblogs'] == {other}
        other_stream = fastapi_server.live_event_stream('microblogs')
        await other_stream.__anext__()
        fastapi_server.live_clients['microblogs'].discard(other)
        await other_stream.aclose()
        return first, second, third, still_there

    first, second, third, still_there = asyncio.run(scenario())

    assert first == ': connected\n\n'
    assert second == 'data: {}\n\n'
    assert third == ': heartbeat\n\n'
    assert still_there
    assert 'microblogs' not in fastapi_server.live_clients


def test_a_second_wake_up_to_a_full_queue_is_dropped_without_error():
    async def scenario():
        q = asyncio.Queue(maxsize=1)
        fastapi_server.live_clients['microblogs'] = {q}
        await fastapi_server.fan_out_live('live:microblogs', '{}')
        await fastapi_server.fan_out_live('live:microblogs', '{}')
        return q.qsize()

    assert asyncio.run(scenario()) == 1


def test_a_wake_up_reaches_live_clients_and_no_notification_client():
    async def scenario():
        live_q, notif_q = asyncio.Queue(), asyncio.Queue()
        fastapi_server.live_clients['microblogs'] = {live_q}
        fastapi_server.connected_clients['microblogs'] = {notif_q}   # a user id that collides with the feed name
        await fastapi_server.fan_out_live('live:microblogs', '{}')
        await fastapi_server.fan_out_live('live:nobody', '{}')
        return live_q.get_nowait(), notif_q.empty()

    message, notif_empty = asyncio.run(scenario())

    assert message == '{}' and notif_empty


def test_the_listener_subscribes_to_live_channels_and_fans_them_out(monkeypatch):
    patterns = []

    class FakePubSub:
        async def psubscribe(self, *names):
            patterns.extend(names)

        async def listen(self):
            yield {'type': 'pmessage', 'channel': 'live:microblogs', 'data': '{}'}
            raise asyncio.CancelledError

    class FakeRedis:
        def pubsub(self):
            return FakePubSub()

    monkeypatch.setattr(fastapi_server, 'r', FakeRedis())

    async def scenario():
        q = asyncio.Queue()
        fastapi_server.live_clients['microblogs'] = {q}
        with pytest.raises(asyncio.CancelledError):
            await fastapi_server.redis_listener()
        return q.get_nowait()

    assert asyncio.run(scenario()) == '{}'
    assert 'live:*' in patterns


def test_the_listener_passes_over_a_message_on_a_channel_it_does_not_handle(monkeypatch):
    class FakePubSub:
        async def psubscribe(self, *names):
            pass

        async def listen(self):
            yield {'type': 'pmessage', 'channel': 'http_posts:1', 'data': '{"urls": []}'}
            yield {'type': 'pmessage', 'channel': 'other:1', 'data': 'x'}
            yield {'type': 'pmessage', 'channel': 'live:microblogs', 'data': '{}'}
            raise asyncio.CancelledError

    class FakeRedis:
        def pubsub(self):
            return FakePubSub()

    monkeypatch.setattr(fastapi_server, 'r', FakeRedis())

    async def scenario():
        q = asyncio.Queue()
        fastapi_server.live_clients['microblogs'] = {q}
        with pytest.raises(asyncio.CancelledError):
            await fastapi_server.redis_listener()
        return q.get_nowait()

    assert asyncio.run(scenario()) == '{}'

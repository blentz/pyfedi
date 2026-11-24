"""ATProto P3: the Jetstream consumer (spec 2026-11-23-atproto-federation-design.md §7, §16). A local websockets server
plays Jetstream v2: it records the URL each socket asked for and pushes the frames a test gives it."""
import asyncio
import json
import threading
from urllib.parse import parse_qs, urlsplit

import pytest
import websockets

from app import db
from app.atproto import actors, ingest, jetstream
from app.models import AtprotoCursor, SocialConnection, User, UserFollower
from tests.factories import make_user
from tests.test_community_add_post import instance


def frame(seq, collection='app.bsky.feed.post', did='did:plc:abcdefghijklmnopqrstuvwx'):
    return json.dumps({'$type': 'message', 'payload': {
        '$type': 'network.bsky.jetstream.subscribeEvents#commit', 'did': did, 'collection': collection,
        'operation': 'create', 'rkey': f'r{seq}', 'record': {'text': 'x'}, 'seq': seq}})


class FakeJetstream:
    """A Jetstream v2 on 127.0.0.1: each socket gets `frames`, then stays open until the client closes it."""

    def __init__(self, frames):
        self.frames, self.paths = frames, []
        self.ready = threading.Event()

    async def _serve(self, socket):
        self.paths.append(socket.request.path)
        for message in self.frames:
            await socket.send(message)
        try:
            await socket.wait_closed()
        except Exception:   # a client that vanishes
            pass

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()

    async def start(self):
        self.server = await websockets.serve(self._serve, '127.0.0.1', 0, subprotocols=['xrpc.v1.json'])
        self.port = self.server.sockets[0].getsockname()[1]


def an_actor(did):
    user = User(user_name=did, ap_id=did, ap_profile_id=actors.profile_url(did), ap_public_url=actors.profile_url(did),
                ap_domain=actors.DOMAIN, instance_id=actors.instance().id)
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def follows(app, db_session, monkeypatch):
    member = make_user(instance(), 'member', local=True)
    db.session.commit()
    followed = an_actor('did:plc:abcdefghijklmnopqrstuvwx')
    pending = an_actor('did:plc:bbbbbbbbbbbbbbbbbbbbbbbb')
    db.session.add_all([
        UserFollower(local_user_id=member.id, remote_user_id=followed.id, is_inward=False, is_accepted=True),
        UserFollower(local_user_id=member.id, remote_user_id=pending.id, is_inward=False, is_accepted=None),
        SocialConnection(user_id=member.id, service='bluesky', variant='hosted', remote_id='did:plc:cccccccccccccccccccccccc',
                         remote_name='member.example', sealed_tokens='x', is_identity=True),
        SocialConnection(user_id=member.id, service='bluesky', variant='hosted', remote_id='waiting:1',
                         remote_name='w', sealed_tokens='x', is_identity=False)])
    db.session.commit()
    taken = []
    monkeypatch.setattr(ingest, 'take', taken.append)
    return taken


def test_the_wanted_dids_are_the_followed_and_the_identities(follows):
    assert jetstream.wanted_dids() == ['did:plc:abcdefghijklmnopqrstuvwx', 'did:plc:cccccccccccccccccccccccc']


def test_shards_and_the_url(app, monkeypatch):
    monkeypatch.setattr(jetstream, 'SHARD', 2)
    assert jetstream.shards(['a', 'b', 'c']) == [['a', 'b'], ['c']]
    monkeypatch.setitem(app.config, 'ATPROTO_JETSTREAM_URL', 'wss://jetstream.example/subscribe')
    with app.app_context():
        url = jetstream.url_of(['did:a', 'did:b'], 42)
        plain = jetstream.url_of(['did:a'], None)
    query = parse_qs(urlsplit(url).query)
    assert url.startswith('wss://jetstream.example/subscribe?') and query['dids'] == ['did:a', 'did:b']
    assert query['collections'] == list(jetstream.COLLECTIONS) and query['cursor'] == ['42']
    assert 'cursor' not in parse_qs(urlsplit(plain).query)


def test_the_cursor_is_each_jetstreams_own(app, db_session, monkeypatch):
    assert jetstream.load_cursor() is None
    jetstream.save_cursor(10)
    jetstream.save_cursor(25)
    assert jetstream.load_cursor() == 25 and AtprotoCursor.query.count() == 1
    monkeypatch.setitem(app.config, 'ATPROTO_JETSTREAM_URL', 'wss://jetstream.us-west.example/subscribe')
    assert jetstream.load_cursor() is None   # another Jetstream: its numbers are its own


def test_what_a_frame_becomes(follows):
    assert jetstream.on_frame(frame(7)) == 7 and follows[-1]['rkey'] == 'r7'
    assert jetstream.on_frame(frame(8, collection='app.bsky.graph.list')) == 8 and len(follows) == 1   # not taken
    identity = json.dumps({'$type': 'message', 'payload': {'$type': 'x#identity', 'did': 'did:plc:x', 'seq': 'n'}})
    assert jetstream.on_frame(identity) is None and len(follows) == 2
    for odd in ('not json', None, '[]', json.dumps({'$type': 'info'}), json.dumps({'$type': 'message', 'payload': 3})):
        assert jetstream.on_frame(odd) is None
    assert len(follows) == 2


def run_against(server_frames, app, monkeypatch, recheck=0.2, wanted=None, change_after=None, cursor=None):
    """Run one Consumer against a FakeJetstream; `change_after` frames in, the wanted DIDs change."""
    server = FakeJetstream(server_frames)
    loop = asyncio.new_event_loop()
    loop.run_until_complete(server.start())
    monkeypatch.setitem(app.config, 'ATPROTO_JETSTREAM_URL', f'ws://127.0.0.1:{server.port}/xrpc/sub')
    if cursor is not None:
        jetstream.save_cursor(cursor)   # this Jetstream's own
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        calls = []
        original = jetstream.wanted_dids

        def wanted_dids():
            calls.append(1)
            return wanted if wanted is not None and len(calls) > (change_after or 0) else original()
        monkeypatch.setattr(jetstream, 'wanted_dids', wanted_dids)
        jetstream.run_forever(runs=1, recheck=recheck)
    finally:
        asyncio.run_coroutine_threadsafe(server.stop(), loop).result(10)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(5)
        loop.close()
    return server


def test_a_run_follows_its_filter_until_it_changes(follows, app, monkeypatch):
    monkeypatch.setattr(jetstream, 'SAVE_EVERY', 0)
    server = run_against([frame(6), frame(9), 'odd'], app, monkeypatch, wanted=['did:plc:new'], change_after=2,
                         cursor=5)
    query = parse_qs(urlsplit(server.paths[0]).query)
    assert query['dids'] == ['did:plc:abcdefghijklmnopqrstuvwx', 'did:plc:cccccccccccccccccccccccc']
    assert query['cursor'] == ['5'] and [event['rkey'] for event in follows] == ['r6', 'r9']
    assert jetstream.load_cursor() == 9


def test_several_shards_keep_the_least_cursor(follows, app, monkeypatch):
    monkeypatch.setattr(jetstream, 'SHARD', 1)
    monkeypatch.setattr(jetstream, 'SAVE_EVERY', 3600)
    consumer = jetstream.Consumer(['a', 'b'], None)
    consumer.seen(0, 30)
    consumer.seen(1, 12)
    consumer.seen(1, None)
    consumer.save()
    assert jetstream.load_cursor() == 12
    jetstream.Consumer([], None).save()   # nothing seen: nothing written
    assert jetstream.load_cursor() == 12


def test_with_nothing_wanted_it_waits(app, db_session, monkeypatch):
    slept = []

    async def sleep(seconds):
        slept.append(seconds)
    monkeypatch.setattr(jetstream.asyncio, 'sleep', sleep)
    jetstream.run_forever(runs=1, recheck=7)
    monkeypatch.setitem(app.config, 'ATPROTO_JETSTREAM_RECHECK', 3)
    jetstream.run_forever(runs=1)
    assert slept == [7, 3]


def test_actors_that_join_the_filter_are_caught_up(follows, app, monkeypatch):
    caught = []
    monkeypatch.setattr(actors, 'followed', lambda user_id, actor_id, limit: caught.append((user_id, actor_id, limit)))

    async def sleep(seconds):
        pass
    monkeypatch.setattr(jetstream.asyncio, 'sleep', sleep)
    monkeypatch.setattr(jetstream, 'wanted_dids', lambda: [])
    assert asyncio.run(jetstream.run_once(0, None)) == [] and caught == []   # the first run: nothing to catch up
    monkeypatch.setattr(jetstream, 'wanted_dids', lambda: ['did:plc:abcdefghijklmnopqrstuvwx', 'did:plc:new'])

    class Ran:
        def __init__(self, dids, cursor):
            pass

        async def run(self, recheck):
            pass
    monkeypatch.setattr(jetstream, 'Consumer', Ran)
    asyncio.run(jetstream.run_once(0, ['did:plc:new']))
    followed = User.query.filter_by(ap_profile_id=actors.profile_url('did:plc:abcdefghijklmnopqrstuvwx')).one()
    assert caught == [(None, followed.id, jetstream.CATCH_UP)]


def test_a_socket_that_fails_is_tried_again_after_a_wait(follows, app, monkeypatch):
    monkeypatch.setitem(app.config, 'ATPROTO_JETSTREAM_URL', 'ws://127.0.0.1:9/nothing-listens')
    waits = []
    monkeypatch.setattr(jetstream.time, 'sleep', waits.append)
    jetstream.run_forever(runs=2, recheck=0.1)
    assert waits == [1, 5]


def test_the_command(app, monkeypatch):
    ran = []
    monkeypatch.setattr(jetstream, 'run_forever', lambda: ran.append(1))
    jetstream.register_commands(app)   # pyfedi.py registers the commands; the test app has none
    result = app.test_cli_runner().invoke(args=['atproto-jetstream'])
    assert result.exit_code == 0 and ran == [1]

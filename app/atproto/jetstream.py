"""The Jetstream consumer (ATProto spec 2026-11-23 §4, §7): `flask atproto-jetstream`, one long-lived process per
deployment, run as a service of its own.

It subscribes to ATPROTO_JETSTREAM_URL (Jetstream v2) for the DIDs PieFed wants: every ATProto actor someone here
follows, and every member's Bluesky identity. At most SHARD DIDs travel on one socket (the filter is in the URL, and
Jetstream refuses more than 10,000 query parameters: §16), so more DIDs mean more sockets. A v2 filter cannot change
on a live socket, so when the wanted DIDs change (looked at each RECHECK seconds) every socket closes and opens again
with the new filter from the cursor, which loses nothing: a cursor replays from its event on (at least once), and
`atproto_record` makes each record once. Each event is handed to a Celery worker (app.atproto.ingest.take); the
consumer itself never waits on the database beyond reading the DIDs and keeping the cursor.
"""
import asyncio
import json
import time
import zlib
from urllib.parse import urlencode

import websockets
from flask import current_app

from app import db
from app.atproto import actors, ingest
from app.models import AtprotoCursor, SocialConnection, User, UserFollower

COLLECTIONS = (ingest.POST, ingest.REPOST, 'app.bsky.feed.like', 'app.bsky.actor.profile', 'app.bsky.graph.follow')
SHARD = 5000
RECHECK = 60           # seconds between looks at the wanted DIDs (ATPROTO_JETSTREAM_RECHECK)
CATCH_UP = 10          # an actor's latest posts read when it joins the filter: what it wrote before its socket opened
SAVE_EVERY = 5         # seconds between writes of the cursor
BACKOFF = (1, 5, 15, 60)
MAX_FRAME = 4 * 1024 * 1024


def wanted_dids() -> list[str]:
    """Every ATProto actor with a follower here, and every Bluesky identity's DID, in order."""
    followed = db.session.query(User.ap_profile_id).join(UserFollower, UserFollower.remote_user_id == User.id).filter(
        UserFollower.is_inward.is_(False), UserFollower.is_accepted.is_(True), User.ap_domain == actors.DOMAIN,
        User.deleted.is_(False)).distinct()
    prefix = f'{actors.WEB}/profile/'
    dids = {profile.removeprefix(prefix) for (profile,) in followed if (profile or '').startswith(f'{prefix}did:')}
    identities = db.session.query(SocialConnection.remote_id).filter(
        SocialConnection.service == 'bluesky', SocialConnection.is_identity.is_(True),
        SocialConnection.remote_id.like('did:%'))
    dids.update(remote_id for (remote_id,) in identities)
    return sorted(dids)


def shards(dids: list[str]) -> list[list[str]]:
    return [dids[start:start + SHARD] for start in range(0, len(dids), SHARD)]


def url_of(dids: list[str], cursor: int | None) -> str:
    params = [('dids', did) for did in dids] + [('collections', collection) for collection in COLLECTIONS]
    if cursor:
        params.append(('cursor', str(cursor)))
    return f"{current_app.config['ATPROTO_JETSTREAM_URL']}?{urlencode(params)}"


def cursor_id() -> int:
    """The cursor's row: one for each Jetstream, since each numbers its own events (another instance, us-west for
    us-east, starts from none rather than from a number that means nothing there)."""
    return zlib.crc32(current_app.config['ATPROTO_JETSTREAM_URL'].encode()) & 0x7FFFFFFF


def load_cursor() -> int | None:
    row = db.session.get(AtprotoCursor, cursor_id())
    return row.seq if row is not None and row.seq else None


def save_cursor(seq: int) -> None:
    row = db.session.get(AtprotoCursor, cursor_id())
    if row is None:
        row = AtprotoCursor(id=cursor_id(), seq=seq)
        db.session.add(row)
    else:
        row.seq = seq
    db.session.commit()


def on_frame(message) -> int | None:
    """One frame from the socket: an event is handed on, and its `seq` answered; anything else is skipped."""
    try:
        frame = json.loads(message)
    except (TypeError, ValueError):
        return None
    if not isinstance(frame, dict) or frame.get('$type') != 'message' or not isinstance(frame.get('payload'), dict):
        return None
    event = frame['payload']
    seq = event.get('seq')
    if str(event.get('$type', '')).endswith('#commit') and event.get('collection') not in COLLECTIONS:
        return seq if type(seq) is int else None
    ingest.take(event)
    return seq if type(seq) is int else None


class Consumer:
    """The sockets for one filter, until it changes."""

    def __init__(self, dids: list[str], cursor: int | None):
        self.dids, self.cursor = dids, cursor
        self.seqs: dict[int, int] = {}
        self.stop = asyncio.Event()
        self.saved_at = time.monotonic()

    def seen(self, shard: int, seq: int | None) -> None:
        if seq is None:
            return
        self.seqs[shard] = max(seq, self.seqs.get(shard, 0))
        if time.monotonic() - self.saved_at >= SAVE_EVERY:
            self.save()

    def save(self) -> None:
        """The cursor every shard has reached: the least of them, so a slower socket loses nothing."""
        if self.seqs:
            save_cursor(min(self.seqs.values()))
        self.saved_at = time.monotonic()

    async def shard(self, number: int, dids: list[str]) -> None:
        async with websockets.connect(url_of(dids, self.cursor), subprotocols=['xrpc.v1.json'],
                                      max_size=MAX_FRAME, open_timeout=30) as socket:
            while not self.stop.is_set():
                try:
                    message = await asyncio.wait_for(socket.recv(), timeout=1)
                except TimeoutError:
                    continue
                self.seen(number, on_frame(message))

    async def watch(self, recheck: float) -> None:
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=recheck)
            except TimeoutError:
                if wanted_dids() != self.dids:
                    self.stop.set()

    async def run(self, recheck: float = RECHECK) -> None:
        """Every shard's socket, until the wanted DIDs change or a socket fails (which ends them all)."""
        async def shard_then_stop(number, dids):
            try:
                await self.shard(number, dids)
            finally:
                self.stop.set()
        try:
            await asyncio.gather(self.watch(recheck), *(shard_then_stop(number, dids) for number, dids in
                                                        enumerate(shards(self.dids))))
        finally:
            self.save()


def catch_up(added: set[str]) -> None:
    """Actors that joined the filter: what they wrote between the follow and their socket opening is read from
    their PDS (a cursor replays it too when there is one; `atproto_record` makes each record once)."""
    for user in User.query.filter(User.ap_profile_id.in_([actors.profile_url(did) for did in added])).all():
        actors.followed(None, user.id, CATCH_UP)


async def run_once(recheck: float = RECHECK, previous: list[str] | None = None) -> list[str]:
    """One filter's run: the sockets for the DIDs wanted now, from the cursor; or, with none wanted, a wait. The DIDs
    it ran for; those not in `previous` (the last run's) are caught up first."""
    dids = wanted_dids()
    if previous is not None:
        catch_up(set(dids) - set(previous))
    if not dids:
        await asyncio.sleep(recheck)
        return dids
    cursor = load_cursor()
    current_app.logger.info(f'atproto: jetstream: {len(dids)} DIDs on {len(shards(dids))} sockets from {cursor}')
    await Consumer(dids, cursor).run(recheck)
    return dids


def run_forever(runs: int | None = None, recheck: float | None = None) -> None:
    """`flask atproto-jetstream`: run after run, waiting a little longer after each failure. `runs` ends it (tests)."""
    recheck = recheck or current_app.config.get('ATPROTO_JETSTREAM_RECHECK') or RECHECK
    failures, previous = 0, None
    while runs is None or runs > 0:
        runs = None if runs is None else runs - 1
        try:
            previous = asyncio.run(run_once(recheck, previous))
            failures = 0
        except (OSError, websockets.WebSocketException, TimeoutError) as failure:
            db.session.rollback()
            current_app.logger.warning(f'atproto: jetstream: {type(failure).__name__}: {failure}')
            time.sleep(BACKOFF[min(failures, len(BACKOFF) - 1)])
            failures += 1


def register_commands(app) -> None:
    @app.cli.command('atproto-jetstream')
    def atproto_jetstream():
        """Follow Jetstream for the ATProto accounts members follow, and their Bluesky identities."""
        with app.app_context():
            run_forever()

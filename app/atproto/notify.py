"""Notifications in (ATProto spec 2026-11-23 §7): what answers members on Bluesky from accounts nobody here follows.

Jetstream carries only the accounts someone here follows (and the members' own), so a reply, mention, quote, like,
repost or follow by anyone else would never arrive. Each Bluesky connection that may read its notifications (an
account hosted here, or an OAuth one granted the `rpc:` scope) is asked for them every INTERVAL through its PDS,
which proxies `app.bsky.notification.listNotifications` to its AppView. Each notification carries the record that
caused it, which goes through the same code as a Jetstream event (app.atproto.ingest): a record PieFed already has
is made once, whichever way it came.

The newest `indexedAt` read is kept per connection in Redis, and a poll reads back to it, at most MAX_PAGES. A
connection's first poll reads one page.
"""
from flask import current_app

import app as app_pkg
from app import celery, db
from app.atproto import ingest
from app.atproto.identity import session_of
from app.connections import SERVICES, ServiceError
from app.connections import bluesky_hosted as hosted
from app.connections.bluesky import NOTIFY_SCOPE
from app.connections.tokens import REAUTH, usable_tokens
from app.models import SocialConnection
from app.utils import get_task_session, patch_db_session

INTERVAL = 120        # seconds between polls of one connection
PAGE = 50
MAX_PAGES = 5
MARK = 'atproto:notified:'    # + connection id: the newest indexedAt read
DUE = 'atproto:notify-due:'   # + connection id: held for INTERVAL after a poll is queued
TAKES = {ingest.POST: ingest.post, ingest.REPOST: ingest.repost, ingest.LIKE: ingest.like,
         ingest.FOLLOW: ingest.follow}


def may_read(tokens: dict) -> bool:
    """Whether the connection may read its notifications: a hosted account, or OAuth with the `rpc:` scope."""
    return tokens.get('kind') == hosted.HOSTED or NOTIFY_SCOPE in str(tokens.get('scope') or '').split()


def take(item) -> bool:
    """One notification's record into PieFed. Whether it named one PieFed takes."""
    if not isinstance(item, dict) or not isinstance(item.get('uri'), str) or not item['uri'].startswith('at://'):
        return False
    parts = item['uri'].removeprefix('at://').split('/')
    if len(parts) != 3 or parts[1] not in TAKES or not isinstance(item.get('record'), dict):
        return False
    did, collection, rkey = parts
    try:
        TAKES[collection](did, rkey, item['record'], item.get('cid'))
    except ServiceError as failure:   # one record that cannot be read does not hold the others up
        current_app.logger.info(f'atproto: notification {item["uri"]} not taken: {failure.message}')
    return True


def poll(connection_id: int) -> int:
    """Read the connection's notifications since the last poll, oldest first. How many records were taken."""
    tokens = usable_tokens(connection_id, SERVICES['bluesky'])
    if not may_read(tokens):
        return 0
    session = session_of(tokens)
    mark = app_pkg.redis_client.get(f'{MARK}{connection_id}')
    mark = mark.decode() if isinstance(mark, bytes) else mark
    found, cursor = [], None
    for _page in range(MAX_PAGES):
        params = {'limit': PAGE, **({'cursor': cursor} if cursor else {})}
        answer = session.xrpc('GET', 'app.bsky.notification.listNotifications', params=params).json()
        items = [item for item in answer.get('notifications') or [] if isinstance(item, dict) and
                 isinstance(item.get('indexedAt'), str)]
        fresh = [item for item in items if mark is None or item['indexedAt'] > mark]
        found += fresh
        cursor = answer.get('cursor')
        if mark is None or len(fresh) < len(items) or not items or not isinstance(cursor, str) or not cursor:
            break
    taken = sum(take(item) for item in sorted(found, key=lambda item: item['indexedAt']))
    if found:
        app_pkg.redis_client.set(f'{MARK}{connection_id}', max(item['indexedAt'] for item in found))
    return taken


@celery.task
def poll_task(connection_id: int) -> None:
    session = get_task_session()
    try:
        with patch_db_session(session):
            try:
                poll(connection_id)
            except ServiceError as failure:
                current_app.logger.info(f'atproto: notifications of connection {connection_id} not read: '
                                        f'{failure.message}')
    finally:
        session.close()


def pollable() -> list[int]:
    """The Bluesky connections that may have notifications to read: not waiting to be reconnected, and, hosted,
    active. (Whether an OAuth one has the scope is in its sealed tokens: `poll` reads that.)"""
    rows = db.session.query(SocialConnection.id).filter(
        SocialConnection.service == 'bluesky', SocialConnection.state != REAUTH,
        SocialConnection.remote_id.like('did:%'),
        db.or_(SocialConnection.variant != hosted.HOSTED, SocialConnection.slot_state == hosted.ACTIVE))
    return [connection_id for (connection_id,) in rows.order_by(SocialConnection.id)]


def poll_due() -> int:
    """Queue a poll for each connection whose last was INTERVAL ago or more (`flask send-queue`, every minute).
    How many were queued."""
    queued = 0
    for connection_id in pollable():
        if app_pkg.redis_client.set(f'{DUE}{connection_id}', '1', ex=INTERVAL - 5, nx=True):
            if current_app.debug:
                poll_task(connection_id)
            else:
                poll_task.delay(connection_id)
            queued += 1
    return queued

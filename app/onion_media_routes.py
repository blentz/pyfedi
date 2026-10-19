"""`GET /onion_media/<sig>/<key>`: remote media for onion pages, from the disk cache (onion media proxy spec
2026-10-19, sections 3.2 to 3.4 and 3.6).

Onion only, and only for URLs the server signed (`remote_for`), so it is not an open proxy. A hit answers with an empty
body and `X-Accel-Redirect` naming the cached file, which Caddy's onion site serves itself (Range, ETag), so no
gunicorn slot is held while the bytes go out. A miss is decided by the URL's extension, since the type is not known
before the fetch: an image (or an unknown extension) is filled in the request when one of the two in-request slots is
free, and anything else is queued to the `onion_media` Celery queue with a 503 and `Retry-After`. A refused or failed
URL answers 404 and the browser shows its broken-media box.
"""
import contextlib
import os
import pathlib
import uuid

from flask import Blueprint, Response, abort

import app as app_pkg
from app.onion import is_onion_request
from app.onion_media import cached_file, remote_for, url_kind
from app.onion_media_fill import fill, is_dead, queue_fill

bp = Blueprint('onion_media', __name__)

INFLIGHT_KEY = 'onion_media:inflight:{}'
INFLIGHT_SLOTS = 2              # in-request fills at once across all workers, so a cold page cannot hold every slot
INFLIGHT_SECONDS = 20           # a slot outlives the 15 s fill deadline, and a killed worker's slot frees itself
REQUEST_FILL_SECONDS = 15
RETRY_IMAGE = 10
RETRY_AV = 30
SANDBOX = "default-src 'none'; sandbox"


@bp.route('/onion_media/<sig>/<key>', methods=['GET'])
def onion_media(sig, key):
    if not is_onion_request():
        abort(404)
    remote = remote_for(sig, key)
    if remote is None or is_dead(remote):
        abort(404)
    path = cached_file(remote)
    if path is None:
        if url_kind(remote) == 'av':
            return _queued(remote, RETRY_AV)
        slot = _take_slot()
        if slot is None:
            return _queued(remote, RETRY_IMAGE)
        try:
            path = fill(remote, max_seconds=REQUEST_FILL_SECONDS)
        finally:
            _give_back(slot)
        if path is None:
            abort(404)
    with contextlib.suppress(FileNotFoundError):    # evicted since it was found: Caddy answers 404
        os.utime(path)                              # least recently used eviction reads the mtime
    return _accel(path)


def _take_slot() -> tuple[str, str] | None:
    """One of the INFLIGHT_SLOTS in-request fill slots as (key, token), or None when all are taken."""
    token = uuid.uuid4().hex
    for slot in range(INFLIGHT_SLOTS):
        key = INFLIGHT_KEY.format(slot)
        if app_pkg.redis_client.set(key, token, nx=True, ex=INFLIGHT_SECONDS):
            return key, token
    return None


def _give_back(slot: tuple[str, str]) -> None:
    """Free the slot unless it expired and another request has taken it since."""
    key, token = slot
    if app_pkg.redis_client.get(key) == token:
        app_pkg.redis_client.delete(key)


def _queued(remote: str, retry_after: int) -> Response:
    queue_fill(remote)
    return Response('', status=503, mimetype='text/plain', headers={'Retry-After': str(retry_after)})


def _accel(path: pathlib.Path) -> Response:
    """The internal redirect Caddy serves. It names the file only, never a path from the request; Caddy sets the
    Content-Type from the extension. Not text/html, so the onion page rewrite leaves it alone."""
    return Response(status=200, mimetype='application/octet-stream',
                    headers={'X-Accel-Redirect': '/' + path.name, 'X-Content-Type-Options': 'nosniff',
                             'Content-Security-Policy': SANDBOX})

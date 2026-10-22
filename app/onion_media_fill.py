"""The onion media cache fill: a streamed, SSRF-guarded fetch (onion media proxy spec 2026-10-19, section 3.3).

Every hop, redirects included, passes the SSRF guard (`is_invalid_get_request_uri`) and the ban checks before it is
sent, and is sent through `app.httpx_client`, whose pinned transport refuses a name that resolves to a refused
address and connects only to an address it checked (app/pinned_http.py). The body is streamed to a temporary file in
the cache directory under its type's size cap and renamed into place only when complete, so a partial file is never
served. The cache dir shares its disk with Postgres, and eviction runs only every 15 minutes, so a fill is refused,
before the fetch and every CHECK_BYTES written, while the disk has under DISK_RESERVE free or the byte counter
(BYTES_KEY) is past OVER_CAP times the cache cap. That refusal is a local condition and does not mark the URL dead.

Video, audio and the images the route cannot fill in the request are filled by `fill_task` on the `onion_media` Celery
queue. `queue_fill` queues one task per URL in ten minutes, however many requests miss it. The queue shares the
background worker's slots, so a fill must not hold them all: at most one fill per remote host runs at once (in the
request or in the background), at most one background fill of any kind (BG_SLOT_KEY), with BACKGROUND_IMAGE_SECONDS
for a URL that looks like an image (held to the image cap whatever it answers), and once MIN_RATE_AFTER seconds have
passed each RATE_WINDOW seconds of body must bring MIN_BYTES_PER_SECOND on average. A task that finds its slot taken
queues itself again REQUEUE_SECONDS later, at most MAX_REQUEUES times; then it is dropped and the next view queues it
afresh.
"""
import contextlib
import hashlib
import os
import pathlib
import shutil
import uuid
from time import monotonic
from urllib.parse import urlsplit

import httpx
from flask import current_app
from redis.exceptions import RedisError

import app as app_pkg
from app import celery
from app.onion_media import BYTES_KEY, cache_name, kind, url_kind
from app.pinned_http import fetch_deadline
from app.storefront import default_domain
from app.utils import domain_from_url, get_setting, instance_banned, is_invalid_get_request_uri, user_agent

DEAD_KEY = 'onion_media:dead:{}'
DEAD_SECONDS = 3600             # a refused or failed URL is not fetched again for an hour
QUEUED_KEY = 'onion_media:queued:{}'
QUEUED_SECONDS = 600            # one queued fill per URL in this time
BACKGROUND_SECONDS = 600        # a background fill's deadline: a 500 MiB video over a slow link
BACKGROUND_IMAGE_SECONDS = 60   # a background image fill's: a 20 MiB image does not wait behind a 600 s video
BG_SLOT_KEY = 'onion_media:bg_inflight'     # the one background fill, so the worker's other slot stays free
HOST_KEY = 'onion_media:host:{}'            # the one fill from this remote host
SLOT_SLACK = 30                 # a slot outlives its fill's deadline by this, and frees itself if a worker dies
REQUEUE_SECONDS = 30            # a task whose slot is taken runs again this much later
MAX_REQUEUES = 20               # then it is dropped, not marked dead: the queued key lapses and a later view requeues
MIN_RATE_AFTER = 20             # seconds of body transfer before the throughput floor applies
MIN_BYTES_PER_SECOND = 64 * 1024    # a slower body is refused: a trickle would hold a worker for the whole deadline
RATE_WINDOW = 10                # the floor applies to each window of this many seconds, so a fast start buys nothing
MAX_REDIRECTS = 5
READ_TIMEOUT = 10               # per read; the whole fill is bounded by max_seconds
DISK_RESERVE = 20 * 1024 ** 3   # free bytes a fill leaves on the disk: Postgres shares it
CHECK_BYTES = 8 * 1024 * 1024   # the disk and the byte counter are checked again each time a fill writes this much
OVER_CAP = 1.1                  # the byte counter may pass the cache cap by this factor before fills stop
_REDIRECTS = {301, 302, 303, 307, 308}


class Refused(Exception):
    """The URL, or what it answered, is one the proxy does not fetch or keep."""


class NoRoom(OSError):
    """The cache has no room for a fill: the disk is nearly full, or the cache is past its cap. Not the URL's fault."""


def _dead_key(remote: str) -> str:
    return DEAD_KEY.format(hashlib.sha256(remote.encode()).hexdigest())


def is_dead(remote: str) -> bool:
    """Whether `remote` was refused or failed within the last hour. False when Redis cannot answer."""
    try:
        return bool(app_pkg.redis_client.exists(_dead_key(remote)))
    except RedisError:
        return False


def _remember_dead(remote: str) -> None:
    try:
        app_pkg.redis_client.set(_dead_key(remote), '1', ex=DEAD_SECONDS)
    except RedisError as error:
        current_app.logger.warning(f'onion media: could not remember {remote} as dead: {error}')


def is_banned(url: str) -> bool:
    """Whether `url` is on a banned instance or a banned Domain. Checked on each hop of a fill and on each cache hit,
    so a file from a host banned after it was cached is not served."""
    if instance_banned(httpx.URL(url).host):
        return True
    domain = domain_from_url(url, create=False)
    return domain is not None and bool(domain.banned)


def _hop_refused(url: str) -> bool:
    """Whether a hop to `url` is refused before it is sent: the SSRF guard, a banned instance or a banned Domain."""
    return is_invalid_get_request_uri(url) or is_banned(url)


def take_slot(key: str, seconds: int) -> str | None:
    """Take the Redis slot `key` for `seconds` and return its token, or None when another fill holds it."""
    token = uuid.uuid4().hex
    return token if app_pkg.redis_client.set(key, token, nx=True, ex=seconds) else None


def give_back(key: str, token: str) -> None:
    """Free the slot unless it expired and another fill has taken it since."""
    if app_pkg.redis_client.get(key) == token:
        app_pkg.redis_client.delete(key)


def host_key(remote: str) -> str:
    """The slot key for `remote`'s host (lowercase)."""
    return HOST_KEY.format(urlsplit(remote).hostname or '')


def fill(remote: str, *, max_seconds: float) -> pathlib.Path | None:
    """Fetch `remote` into the cache and return its path, or None when it is refused or the fetch fails. Raises
    nothing. A refused or failed URL is remembered for an hour (is_dead); a failure writing the cache is not, since
    it says nothing about the URL. No temporary file is left behind on any path."""
    directory = pathlib.Path(current_app.config['MEDIA_PROXY_DIR'])
    temporary = directory / f'.tmp-{uuid.uuid4().hex}'
    try:
        directory.mkdir(parents=True, exist_ok=True)
        _check_room(directory, 0)
        with fetch_deadline(max_seconds):
            content_type = _fetch(remote, temporary, monotonic() + max_seconds)
        path = directory / cache_name(remote, content_type)
        size = temporary.stat().st_size
        os.replace(temporary, path)
        _count(remote, size)
        return path
    except OSError as error:            # the cache directory or its disk (NoRoom), not the remote server
        current_app.logger.warning(f'onion media: could not store {remote}: {error}')
        return None
    except Exception as error:          # Refused, httpx errors (InvalidURL included), anything the remote caused
        current_app.logger.info(f'onion media: {remote} not cached: {error!r}')
        _remember_dead(remote)
        return None
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink(missing_ok=True)


def _check_room(directory: pathlib.Path, written: int) -> None:
    """Raise NoRoom when the disk under `directory` has less than DISK_RESERVE free, or when `written` more bytes take
    the byte counter past OVER_CAP times the cache cap. A counter Redis cannot read refuses nothing."""
    if shutil.disk_usage(directory).free < DISK_RESERVE:
        raise NoRoom(f'under {DISK_RESERVE} bytes free on the disk')
    try:
        counted = int(app_pkg.redis_client.get(BYTES_KEY) or 0)
    except RedisError:
        return
    cap = get_setting('onion_media_cache_bytes', 10737418240)
    if counted + written > cap * OVER_CAP:
        raise NoRoom(f'the cache holds {counted} bytes, past its cap of {cap}')


def _count(remote: str, size: int) -> None:
    try:
        app_pkg.redis_client.incrby(BYTES_KEY, size)
    except RedisError as error:
        current_app.logger.warning(f'onion media: could not count {remote} in the byte counter: {error}')


def _fetch(remote: str, temporary: pathlib.Path, deadline: float) -> str:
    """Follow at most MAX_REDIRECTS redirects from `remote`, checking each hop before it is sent, and stream the
    final body to `temporary`. Returns the bare Content-Type. Each request is built here rather than by httpx's own
    redirect handling, which would send the next hop unchecked, and carries no cookie and no Referer."""
    url = remote
    # The default domain's name, never the onion's: the onion is local-only, and its address stays off other servers
    headers = {'User-Agent': user_agent(default_domain()),
               'Accept-Encoding': 'identity'}   # uncompressed, so the byte cap is the cap on what is read
    for _ in range(MAX_REDIRECTS + 1):
        if _hop_refused(url):
            raise Refused(f'{url} is not fetched')
        request = app_pkg.httpx_client.build_request('GET', url, headers=headers, timeout=READ_TIMEOUT)
        request.headers.pop('Cookie', None)     # the shared client's cookie jar stays out of it
        response = app_pkg.httpx_client.send(request, stream=True, follow_redirects=False)
        try:
            if response.status_code not in _REDIRECTS:
                return _store(response, temporary, deadline, url_kind(remote) == 'image')
            location = response.headers.get('Location')
            if not location:
                raise Refused(f'{url} redirected without a Location')
            url = str(response.url.join(location))
        finally:
            response.close()
    raise Refused(f'{remote} redirected more than {MAX_REDIRECTS} times')


def _store(response: httpx.Response, temporary: pathlib.Path, deadline: float, image_url: bool) -> str:
    """Stream an allowed, uncompressed 200 body to `temporary` under its type's cap (the image cap for a URL that
    looks like an image, `image_url`, since only that guess chose its deadline), the deadline, the throughput
    floor and the room left (`_check_room`, each CHECK_BYTES)."""
    if response.status_code != 200:
        raise Refused(f'{response.url} answered {response.status_code}')
    content_type = response.headers.get('Content-Type', '').split(';')[0].strip().lower()
    media = kind(content_type)
    if media is None:
        raise Refused(f'{response.url} is {content_type!r}')
    if response.headers.get('Content-Encoding', 'identity').strip().lower() != 'identity':
        raise Refused(f'{response.url} sent a {response.headers["Content-Encoding"]} body')
    if media == 'image' or image_url:
        cap = get_setting('onion_media_image_max_bytes', 20971520)
    else:
        cap = get_setting('onion_media_av_max_bytes', 524288000)
    cap = min(cap, get_setting('onion_media_cache_bytes', 10737418240))   # one file never fills the whole cache
    started = monotonic()
    declared = response.headers.get('Content-Length')
    if declared is not None and (not declared.strip().isdigit() or int(declared) > cap):
        raise Refused(f'{response.url} declared {declared} bytes, over the cap of {cap}')
    written, next_check = 0, CHECK_BYTES
    window_start, window_bytes = started + MIN_RATE_AFTER, 0
    with open(temporary, 'xb') as file:
        for chunk in response.iter_raw():
            written += len(chunk)
            if written > cap:
                raise Refused(f'{response.url} sent more than {cap} bytes')
            if written >= next_check:
                _check_room(temporary.parent, written)
                next_check = written + CHECK_BYTES
            now = monotonic()
            if now > deadline:
                raise Refused(f'{response.url} was still sending at the deadline')
            if now > window_start:
                window_bytes += len(chunk)
                if now - window_start >= RATE_WINDOW:
                    if window_bytes < MIN_BYTES_PER_SECOND * (now - window_start):
                        raise Refused(f'{response.url} sent {window_bytes} bytes in {now - window_start:.0f} s, '
                                      'under the throughput floor')
                    window_start, window_bytes = now, 0
            file.write(chunk)
    return content_type


@celery.task
def fill_task(remote: str, attempt: int = 0) -> None:
    """Fill `remote` in the background, or queue this task again when the one background slot or its host's slot is
    taken. `attempt` counts the re-queues; the one after MAX_REQUEUES is dropped."""
    seconds = BACKGROUND_IMAGE_SECONDS if url_kind(remote) == 'image' else BACKGROUND_SECONDS
    slots = []
    try:
        for key in (BG_SLOT_KEY, host_key(remote)):
            token = take_slot(key, seconds + SLOT_SLACK)
            if token is None:
                if attempt >= MAX_REQUEUES:
                    current_app.logger.info(f'onion media: {remote} not filled, its slot stayed busy')
                    return
                app_pkg.redis_client.set(_queued_key(remote), '1', ex=QUEUED_SECONDS)   # still queued: no duplicate
                fill_task.apply_async((remote, attempt + 1), countdown=REQUEUE_SECONDS)
                return
            slots.append((key, token))
        fill(remote, max_seconds=seconds)
    finally:
        for key, token in slots:
            give_back(key, token)


def _queued_key(remote: str) -> str:
    return QUEUED_KEY.format(hashlib.sha256(remote.encode()).hexdigest())


def queue_fill(remote: str) -> None:
    """Queue `fill_task` for `remote` unless it was queued within the last QUEUED_SECONDS."""
    if app_pkg.redis_client.set(_queued_key(remote), '1', nx=True, ex=QUEUED_SECONDS):
        fill_task.delay(remote)

"""The onion media cache fill: a streamed, SSRF-guarded fetch (onion media proxy spec 2026-10-19, section 3.3).

Every hop, redirects included, passes the SSRF guard (`is_invalid_get_request_uri`) and the ban checks before it is
sent, and is sent through `app.httpx_client`, whose pinned transport refuses a name that resolves to a refused
address and connects only to an address it checked (app/pinned_http.py). The body is streamed to a temporary file in
the cache directory under its type's size cap and renamed into place only when complete, so a partial file is never
served.

Video, audio and the images the route cannot fill in the request are filled by `fill_task` on the `onion_media` Celery
queue. `queue_fill` queues one task per URL in ten minutes, however many requests miss it.
"""
import contextlib
import hashlib
import os
import pathlib
import uuid
from time import monotonic

import httpx
from flask import current_app
from redis.exceptions import RedisError

import app as app_pkg
from app import celery
from app.onion_media import cache_name, kind
from app.pinned_http import fetch_deadline
from app.utils import domain_from_url, get_setting, instance_banned, is_invalid_get_request_uri

DEAD_KEY = 'onion_media:dead:{}'
DEAD_SECONDS = 3600             # a refused or failed URL is not fetched again for an hour
QUEUED_KEY = 'onion_media:queued:{}'
QUEUED_SECONDS = 600            # one queued fill per URL in this time
BACKGROUND_SECONDS = 600        # a background fill's deadline: a 500 MiB video over a slow link
MAX_REDIRECTS = 5
READ_TIMEOUT = 10               # per read; the whole fill is bounded by max_seconds
_REDIRECTS = {301, 302, 303, 307, 308}


class Refused(Exception):
    """The URL, or what it answered, is one the proxy does not fetch or keep."""


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


def _hop_refused(url: str) -> bool:
    """Whether a hop to `url` is refused before it is sent: the SSRF guard, a banned instance or a banned Domain."""
    if is_invalid_get_request_uri(url):
        return True
    if instance_banned(httpx.URL(url).host):
        return True
    domain = domain_from_url(url, create=False)
    return domain is not None and bool(domain.banned)


def fill(remote: str, *, max_seconds: float) -> pathlib.Path | None:
    """Fetch `remote` into the cache and return its path, or None when it is refused or the fetch fails. Raises
    nothing. A refused or failed URL is remembered for an hour (is_dead); a failure writing the cache is not, since
    it says nothing about the URL. No temporary file is left behind on any path."""
    directory = pathlib.Path(current_app.config['MEDIA_PROXY_DIR'])
    temporary = directory / f'.tmp-{uuid.uuid4().hex}'
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with fetch_deadline(max_seconds):
            content_type = _fetch(remote, temporary, monotonic() + max_seconds)
        path = directory / cache_name(remote, content_type)
        os.replace(temporary, path)
        return path
    except OSError as error:            # the cache directory, not the remote server
        current_app.logger.warning(f'onion media: could not store {remote}: {error}')
        return None
    except Exception as error:          # Refused, httpx errors (InvalidURL included), anything the remote caused
        current_app.logger.info(f'onion media: {remote} not cached: {error!r}')
        _remember_dead(remote)
        return None
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink(missing_ok=True)


def _fetch(remote: str, temporary: pathlib.Path, deadline: float) -> str:
    """Follow at most MAX_REDIRECTS redirects from `remote`, checking each hop before it is sent, and stream the
    final body to `temporary`. Returns the bare Content-Type. Each request is built here rather than by httpx's own
    redirect handling, which would send the next hop unchecked, and carries no cookie and no Referer."""
    url = remote
    headers = {'User-Agent': f'PieFed/{current_app.config["VERSION"]}; +https://{current_app.config["SERVER_NAME"]}',
               'Accept-Encoding': 'identity'}   # uncompressed, so the byte cap is the cap on what is read
    for _ in range(MAX_REDIRECTS + 1):
        if _hop_refused(url):
            raise Refused(f'{url} is not fetched')
        request = app_pkg.httpx_client.build_request('GET', url, headers=headers, timeout=READ_TIMEOUT)
        request.headers.pop('Cookie', None)     # the shared client's cookie jar stays out of it
        response = app_pkg.httpx_client.send(request, stream=True, follow_redirects=False)
        try:
            if response.status_code not in _REDIRECTS:
                return _store(response, temporary, deadline)
            location = response.headers.get('Location')
            if not location:
                raise Refused(f'{url} redirected without a Location')
            url = str(response.url.join(location))
        finally:
            response.close()
    raise Refused(f'{remote} redirected more than {MAX_REDIRECTS} times')


def _store(response: httpx.Response, temporary: pathlib.Path, deadline: float) -> str:
    """Stream an allowed, uncompressed 200 body to `temporary` under its type's cap and the deadline."""
    if response.status_code != 200:
        raise Refused(f'{response.url} answered {response.status_code}')
    content_type = response.headers.get('Content-Type', '').split(';')[0].strip().lower()
    media = kind(content_type)
    if media is None:
        raise Refused(f'{response.url} is {content_type!r}')
    if response.headers.get('Content-Encoding', 'identity').strip().lower() != 'identity':
        raise Refused(f'{response.url} sent a {response.headers["Content-Encoding"]} body')
    if media == 'image':
        cap = get_setting('onion_media_image_max_bytes', 20971520)
    else:
        cap = get_setting('onion_media_av_max_bytes', 524288000)
    cap = min(cap, get_setting('onion_media_cache_bytes', 10737418240))   # one file never fills the whole cache
    declared = response.headers.get('Content-Length')
    if declared is not None and (not declared.strip().isdigit() or int(declared) > cap):
        raise Refused(f'{response.url} declared {declared} bytes, over the cap of {cap}')
    written = 0
    with open(temporary, 'xb') as file:
        for chunk in response.iter_raw():
            written += len(chunk)
            if written > cap:
                raise Refused(f'{response.url} sent more than {cap} bytes')
            if monotonic() > deadline:
                raise Refused(f'{response.url} was still sending at the deadline')
            file.write(chunk)
    return content_type


@celery.task
def fill_task(remote: str) -> None:
    fill(remote, max_seconds=BACKGROUND_SECONDS)


def queue_fill(remote: str) -> None:
    """Queue `fill_task` for `remote` unless it was queued within the last QUEUED_SECONDS."""
    key = QUEUED_KEY.format(hashlib.sha256(remote.encode()).hexdigest())
    if app_pkg.redis_client.set(key, '1', nx=True, ex=QUEUED_SECONDS):
        fill_task.delay(remote)

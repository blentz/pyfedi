"""Onion state lives in a RAM-only Redis (onion unlinkability spec 2026-10-26, goal 7 and section 5, "The main Redis
rule" and "The RAM-only store").

The main Redis (`CACHE_REDIS_URL`) holds the Celery broker and keeps its persistence, so queued federation survives a
restart. Nothing an onion request does may write a key there, or change one's value or TTL: a `dump.rdb` would keep
it across a reboot. The onion's state goes to `app.onion_redis_client` (`ONION_REDIS_URL`) instead, a Redis run with
`save ""`, `appendonly no` and no volume, so a reboot forgets it.

- `request_redis()` is the store for state a request writes on behalf of its visitor (vote quota, AI-detection tally,
  feed cache, captcha, settings import): the onion store for an onion request, the main Redis otherwise.
  `read_other_store` reads the other one, so an account's vote quota and AI-detection tally add up across both.
- `OnionRoutedStorage` is the rate limiter's storage. It sends an onion request's buckets to the onion store and every
  other request's to the main Redis, unchanged. When the onion store is down, onion buckets are kept in this process's
  memory meanwhile, so the onion still renders and is still limited; clearnet is not affected either way.
- When the onion store is down, an onion action that needs it answers `onion_store_unavailable()`, a 503 asking the
  visitor to try again shortly (`onion_store_needed`). Browsing does not need it (`onion_store_optional`).
"""
from __future__ import annotations

from contextlib import contextmanager
from time import monotonic
from urllib.parse import urlsplit

from flask import abort, current_app, jsonify, make_response
from flask_babel import _
from limits.storage import MemoryStorage, Storage, storage_from_string
from markupsafe import escape
from redis.exceptions import ConnectionError as RedisConnectionError, RedisError

import app as app_pkg
from app.log_scrub import ONION_SAFE
from app.onion import is_api_path, is_onion_request, single_address

ONION_ROUTED_SCHEME = 'onion-routed'
FALLBACK_WARNING_SECONDS = 60       # the limiter's onion fallback says so at most this often per process


def request_redis():
    """The Redis for the state this request keeps for its visitor: the RAM-only onion store on the onion, the main
    Redis otherwise (and outside a request)."""
    return app_pkg.onion_redis_client if is_onion_request() else app_pkg.redis_client


def onion_configured(config=None) -> bool:
    """Whether this site has an onion at all: `ONION_PROXY_IP` names the proxy onion requests arrive through."""
    return single_address((config if config is not None else current_app.config).get('ONION_PROXY_IP')) is not None


LOOPBACK = {'localhost', '127.0.0.1', '::1'}


def redis_server(url: str) -> tuple:
    """Which Redis server a URL names, whatever database, password or spelling: ('unix', socket path) or
    (host, port), with the loopback names as one host."""
    parts = urlsplit(url.replace('redis+unix://', 'unix://'))
    if parts.scheme == 'unix':
        return 'unix', parts.path
    host = (parts.hostname or 'localhost').lower()
    return 'localhost' if host in LOOPBACK else host, parts.port or 6379


def check_onion_store(app) -> bool:
    """At startup, for a site with an onion: whether `ONION_REDIS_URL` names a Redis of its own. One that shares a
    server with `CACHE_REDIS_URL` or `CELERY_BROKER_URL` is a database of the persisted main Redis, so the onion's keys
    would reach its dump.rdb: that is logged as an ERROR and the caller treats the onion store as down (False). Never a
    reason to fail startup. A Redis Flask cache is warned about: every cache write an onion request makes would go to
    the main Redis."""
    config = app.config
    if not onion_configured(config):
        return True
    if 'redis' in str(config.get('CACHE_TYPE', '')).lower():
        app.logger.warning(f"CACHE_TYPE is {config['CACHE_TYPE']}: the onion's cache writes go to the main Redis, "
                           'which persists to disk. Use a FileSystemCache in /dev/shm.')
    onion = redis_server(config['ONION_REDIS_URL'])
    shared = [name for name in ('CACHE_REDIS_URL', 'CELERY_BROKER_URL') if redis_server(config[name]) == onion]
    if shared:
        app.logger.error(f"ONION_REDIS_URL is on the same Redis server as {' and '.join(shared)}, which persists to "
                         'disk: the onion store is treated as down until it has a Redis of its own.')
        return False
    return True


class RefusedOnionStore:
    """The onion client when `check_onion_store` refused ONION_REDIS_URL: every command raises, as a Redis that is down
    does, so the onion's guard answers 503 and nothing reaches the main Redis."""

    def __getattr__(self, name):
        def refused(*args, **kwargs):
            raise RedisConnectionError('the onion store shares the main Redis (see the startup log)')
        return refused


def read_other_store(read, default):
    """`read(store)` on the store this request does NOT keep its state in, so that a per-account tally kept per store
    (vote quota, AI-detection streak) adds up across clearnet and the onion and neither path is a way round the other.
    Only ever a read: an onion request still writes nothing to the main Redis.

    - On the onion it reads the main Redis; its errors go to the caller's guard as any onion-store error would.
    - On clearnet it reads the onion store, and an onion store that is down, or no onion at all, gives `default`:
      clearnet never depends on it."""
    if is_onion_request():
        return read(app_pkg.redis_client)
    if not onion_configured():
        return default
    try:
        return read(app_pkg.onion_redis_client)
    except RedisError:
        current_app.logger.warning('onion store unavailable, its tally skipped', extra=ONION_SAFE)
        return default


def onion_store_unavailable():
    """The answer to an onion action that needs the onion store while it is down: a 503, "Please try again shortly."
    JSON in the alpha API's shape on an API path, a short page otherwise."""
    message = _('Please try again shortly.')
    if is_api_path():
        response = jsonify({'code': 503, 'message': message, 'status': 'Service Unavailable'})
        response.status_code = 503
    else:
        response = make_response(f'<!doctype html><html><head><meta charset="utf-8"><title>{escape(message)}</title>'
                                 f'</head><body><p>{escape(message)}</p></body></html>', 503)
        response.headers['Content-Type'] = 'text/html; charset=utf-8'
    response.headers['Retry-After'] = '30'
    return response


@contextmanager
def onion_store_needed():
    """Around an onion action that cannot go ahead without the onion store (login, registration, the proof-of-work
    and wait step, a vote's quota): a RedisError on an onion request is answered with `onion_store_unavailable()`.
    On clearnet the error goes on as before."""
    try:
        yield
    except RedisError:
        if not is_onion_request():
            raise
        current_app.logger.warning('onion store unavailable', extra=ONION_SAFE)
        abort(onion_store_unavailable())


@contextmanager
def onion_store_optional():
    """Around onion state a page can do without (a cache, a tally): a RedisError on an onion request is logged and
    the rest of the block skipped, so the page still renders. On clearnet the error goes on as before."""
    try:
        yield
    except RedisError:
        if not is_onion_request():
            raise
        current_app.logger.warning('onion store unavailable', extra=ONION_SAFE)


class OnionRoutedStorage(Storage):
    """Flask-Limiter's storage: an onion request's buckets in the onion store, everything else in the main one.

    Routed by the request rather than by the bucket's key, so a limit with its own key function (the Live views key by
    user id) is routed too. Config: `RATELIMIT_STORAGE_URI = 'onion-routed://'` and `RATELIMIT_STORAGE_OPTIONS` naming
    the `main` and `onion` storage URIs, and `onion_options` for the onion's (its timeouts). If the onion storage raises, onion buckets are counted in this process's
    memory until it answers again; the main storage's errors reach Flask-Limiter as they always did."""

    STORAGE_SCHEME = [ONION_ROUTED_SCHEME]

    def __init__(self, uri: str | None = None, *, main: str, onion: str, onion_options: dict | None = None, clock=monotonic,
                 **options):
        super().__init__(uri, **options)
        self.main = storage_from_string(main)
        self.onion = storage_from_string(onion, **(onion_options or {}))   # its timeouts (ONION_REDIS_TIMEOUT)
        self.clock = clock      # injectable, so a test needs no sleep
        self.fallback_warned_at: float | None = None
        self.onion_fallback: MemoryStorage | None = None   # made on the first failure: it starts a timer thread

    @property
    def base_exceptions(self):
        return self.main.base_exceptions

    def _onion(self, method: str, *args):
        try:
            return getattr(self.onion, method)(*args)
        except self.onion.base_exceptions:
            now = self.clock()
            if self.fallback_warned_at is None or now - self.fallback_warned_at >= FALLBACK_WARNING_SECONDS:
                self.fallback_warned_at = now
                current_app.logger.warning('onion rate-limit store unavailable, counting in memory', extra=ONION_SAFE)
            if self.onion_fallback is None:
                self.onion_fallback = MemoryStorage()
            return getattr(self.onion_fallback, method)(*args)

    def _route(self, method: str, *args):
        if is_onion_request():
            return self._onion(method, *args)
        return getattr(self.main, method)(*args)

    def incr(self, key: str, expiry: int, amount: int = 1) -> int:
        return self._route('incr', key, expiry, amount)

    def get(self, key: str) -> int:
        return self._route('get', key)

    def get_expiry(self, key: str) -> float:
        return self._route('get_expiry', key)

    def clear(self, key: str) -> None:
        return self._route('clear', key)

    def check(self) -> bool:
        """Whether the main storage answers: what Flask-Limiter's fallback, were it on, would ask."""
        return self.main.check()

    def reset(self) -> int | None:
        """Both storages emptied, as Flask-Limiter's reset() asks of one."""
        counts = [self.main.reset(), self.onion.reset()]
        return sum(count or 0 for count in counts)

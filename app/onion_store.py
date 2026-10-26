"""Onion state lives in a RAM-only Redis (onion unlinkability spec 2026-10-26, goal 7 and section 5, "The main Redis
rule" and "The RAM-only store").

The main Redis (`CACHE_REDIS_URL`) holds the Celery broker and keeps its persistence, so queued federation survives a
restart. Nothing an onion request does may write a key there, or change one's value or TTL: a `dump.rdb` would keep
it across a reboot. The onion's state goes to `app.onion_redis_client` (`ONION_REDIS_URL`) instead, a Redis run with
`save ""`, `appendonly no` and no volume, so a reboot forgets it.

- `request_redis()` is the store for state a request writes on behalf of its visitor (vote quota, AI-detection tally,
  feed cache, captcha, settings import): the onion store for an onion request, the main Redis otherwise.
- `OnionRoutedStorage` is the rate limiter's storage. It sends an onion request's buckets to the onion store and every
  other request's to the main Redis, unchanged. When the onion store is down, onion buckets are kept in this process's
  memory meanwhile, so the onion still renders and is still limited; clearnet is not affected either way.
- When the onion store is down, an onion action that needs it answers `onion_store_unavailable()`, a 503 asking the
  visitor to try again shortly (`onion_store_needed`). Browsing does not need it (`onion_store_optional`).
"""
from __future__ import annotations

from contextlib import contextmanager

from flask import abort, current_app, jsonify, make_response
from flask_babel import _
from limits.storage import MemoryStorage, Storage, storage_from_string
from markupsafe import escape
from redis.exceptions import RedisError

import app as app_pkg
from app.onion import is_api_path, is_onion_request

ONION_ROUTED_SCHEME = 'onion-routed'


def request_redis():
    """The Redis for the state this request keeps for its visitor: the RAM-only onion store on the onion, the main
    Redis otherwise (and outside a request)."""
    return app_pkg.onion_redis_client if is_onion_request() else app_pkg.redis_client


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
        current_app.logger.warning('onion store unavailable')
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
        current_app.logger.warning('onion store unavailable')


class OnionRoutedStorage(Storage):
    """Flask-Limiter's storage: an onion request's buckets in the onion store, everything else in the main one.

    Routed by the request rather than by the bucket's key, so a limit with its own key function (the Live views key by
    user id) is routed too. Config: `RATELIMIT_STORAGE_URI = 'onion-routed://'` and `RATELIMIT_STORAGE_OPTIONS` naming
    the `main` and `onion` storage URIs. If the onion storage raises, onion buckets are counted in this process's
    memory until it answers again; the main storage's errors reach Flask-Limiter as they always did."""

    STORAGE_SCHEME = [ONION_ROUTED_SCHEME]

    def __init__(self, uri: str | None = None, *, main: str, onion: str, **options):
        super().__init__(uri, **options)
        self.main = storage_from_string(main)
        self.onion = storage_from_string(onion)
        self.onion_fallback: MemoryStorage | None = None   # made on the first failure: it starts a timer thread

    @property
    def base_exceptions(self):
        return self.main.base_exceptions

    def _onion(self, method: str, *args):
        try:
            return getattr(self.onion, method)(*args)
        except self.onion.base_exceptions as error:
            current_app.logger.warning(f'onion rate-limit store unavailable, counting in memory: {error}')
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

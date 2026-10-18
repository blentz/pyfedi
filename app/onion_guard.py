"""Abuse limits for onion requests (onion storefront spec 2026-10-17, sections 3.7 and 3.7a).

Every onion visitor shares the Tor container's address, so the rate limiter keys onion requests by session, or by
account on the alpha API (`app.onion.onion_rate_key`). This module adds the parts that need the rest of the app:

- **Session first.** A cookieless onion request to a rate-limited web route gets a 303 to the same path and query,
  plus `cookie_check=1`, with a session cookie that carries a fresh sid. The browser comes back with the cookie and is
  sent on to the URL without the marker. A cookieless POST is not processed: it lands back on the form with a "please
  try again" message (its CSRF token could not have been valid anyway). A client that comes back to the marker URL
  still without a cookie gets a page saying cookies are needed, not another redirect. API routes are never sent round:
  their clients do not keep cookies, and are keyed by account instead.
- **API keys.** For an onion alpha-API request, the bearer token's account is found before the view runs, so its
  `with limiter.limit(...)` blocks count per account.
- **Login delay.** Failed onion logins slow down the identifier that was typed: after 5 failures each attempt waits
  `min(2 ** (count - 5), 300)` seconds from the last one. The counter is onion-only, so an onion attacker cannot slow
  the account down on clearnet. It covers web and API login.
"""
from __future__ import annotations

import math
import time
from urllib.parse import quote, quote_from_bytes

from flask import current_app, flash, make_response, redirect, request
from flask_babel import _
from markupsafe import escape
from redis.exceptions import WatchError

import app as app_pkg
from app import limiter
from app.onion import API_RATE_KEY, is_api_path, is_onion_request, mint_onion_sid
from app.utils import authorise_api_user

# Every endpoint with a `@limiter.limit` decorator is in exactly one of these two sets
# (tests/test_onion_guard_limits.py finds the decorators by parsing app/ and fails on one in neither).
ONION_SESSION_ENDPOINTS: frozenset[str] = frozenset({
    'auth.login',
    'auth.register',
    'auth.resend_email',
    'auth.reset_password_request',
    'community.community_invite',
    'community.community_invite_accept',
    'main.list_communities',
    'main.modlog',
    'main.bot_challenge_result',
    'search.run_search',
})
ONION_SESSION_EXEMPT: frozenset[str] = frozenset({
    # Called by other servers, which keep no cookies. Over the onion they share the onion-anon bucket.
    'activitypub.api_is_ip_banned',
    'activitypub.api_is_email_banned',
    'main.receive_webhook',
    # Keyed by the logged-in user's id (their own key_func), not by session; anonymous callers are sent to log in.
    'main.home_live_posts',
    'community.live_posts_fragment',
})

COOKIE_CHECK = 'cookie_check=1'
_COOKIE_CHECK = COOKIE_CHECK.encode()
PATH_SAFE = "/:@!$&'()*+,;=-._~"            # not '%' (the path is decoded) and not '\\' (a browser reads it as '/')
QUERY_SAFE = PATH_SAFE + '%?'               # the query string is still percent-encoded as it arrived

LOGIN_FAILURE_KEY = 'onion_login_fail:{}'
FREE_LOGIN_FAILURES = 5
MAX_LOGIN_WAIT = 300
LOGIN_FAILURE_TTL = 3600
RESERVE_RETRIES = 5


# --------------------------------------------------------------------------
# Session first
# --------------------------------------------------------------------------

def onion_session_missing() -> bool:
    """Whether this is an onion request to a session-first endpoint that brought no session cookie.

    Also a Flask-Limiter request filter: such a request is always answered by `require_onion_session` before its view
    runs, so it is not counted. Otherwise every cookieless visitor's first request would spend the shared bucket, and
    a flood of them would turn first visits into 429s."""
    return (request.endpoint in ONION_SESSION_ENDPOINTS and is_onion_request()
            and current_app.session_interface.get_cookie_name(current_app) not in request.cookies)


limiter.request_filter(onion_session_missing)


def same_url(query: list[bytes]) -> str:
    """This request's path with `query` (raw query-string parts), as a path on this storefront: relative, so never
    built from Host, and with one leading slash, so never protocol-relative."""
    path = '/' + (request.script_root + request.path).lstrip('/')
    location = quote(path, safe=PATH_SAFE)
    return f"{location}?{quote_from_bytes(b'&'.join(query), safe=QUERY_SAFE)}" if query else location


def cookies_required_page(retry: str):
    title = escape(_('Cookies are needed here'))
    message = escape(_('This page needs a session cookie, and your browser did not send one back. '
                       'Allow cookies for this site, then try again.'))
    response = make_response(f'<!doctype html><html><head><meta charset="utf-8"><title>{title}</title></head>'
                             f'<body><h1>{title}</h1><p>{message}</p>'
                             f'<p><a href="{escape(retry)}">{escape(_("Try again"))}</a></p></body></html>', 400)
    response.headers['Content-Type'] = 'text/html; charset=utf-8'
    return response


def require_onion_session():
    """`before_request`: session first for onion requests to the endpoints in ONION_SESSION_ENDPOINTS."""
    if request.endpoint not in ONION_SESSION_ENDPOINTS or not is_onion_request():
        return None
    parts = [part for part in request.query_string.split(b'&') if part]
    checked = _COOKIE_CHECK in parts
    clean = [part for part in parts if part != _COOKIE_CHECK]
    if onion_session_missing():
        if checked:   # sent round once already and still no cookie: say so instead of redirecting again
            return cookies_required_page(same_url(clean))
        mint_onion_sid()
        if request.method not in ('GET', 'HEAD'):
            flash(_('Your session had not started yet, so that was not sent. Please try again.'), 'warning')
        return redirect(same_url(clean + [_COOKIE_CHECK]), 303)
    mint_onion_sid()   # a session cookie without a sid (from a page with no limit) gets one now
    if checked and request.method in ('GET', 'HEAD'):
        return redirect(same_url(clean), 303)
    return None


# --------------------------------------------------------------------------
# Alpha API keys
# --------------------------------------------------------------------------

def resolve_onion_api_rate_key():
    """`before_request`: for an onion alpha-API request with a valid bearer token, key its rate limits by that
    account (`onion-user:<id>`). Without one the request stays in the shared bucket. A token refused for any reason
    is `incorrect_login`; anything else is an application error and is not swallowed."""
    if not is_onion_request() or not is_api_path():
        return None
    auth = request.headers.get('Authorization')
    if auth:
        try:
            request.environ[API_RATE_KEY] = f'onion-user:{authorise_api_user(auth)}'
        except Exception as error:
            if str(error) != 'incorrect_login':
                raise
    return None


# --------------------------------------------------------------------------
# Per-account login delay
# --------------------------------------------------------------------------

def _now() -> float:
    return time.time()


def onion_login_identifier(typed: str) -> str:
    """What an onion login's failure counter is kept under: the identifier as typed (a name, an email or a profile
    URL), stripped and lower-cased. Never the account it resolves to: then failing as `alice` would delay a login
    typed as alice's email, which tells an attacker whose email that is, and an existing identifier would behave
    differently from an unknown one. Web login, API login and verify_credentials all count through this."""
    return typed.strip().lower()


def _login_key(username: str) -> str:
    return LOGIN_FAILURE_KEY.format(onion_login_identifier(username))


def login_wait(count: int) -> int:
    """Seconds an attempt waits after `count` failures: none for the first 5, then doubling from 2, capped at 300."""
    if count <= FREE_LOGIN_FAILURES:
        return 0
    return min(2 ** min(count - FREE_LOGIN_FAILURES, 9), MAX_LOGIN_WAIT)


def _remaining(count, last, now: float) -> int:
    wait = login_wait(int(count or 0))
    return max(math.ceil(float(last or 0) + wait - now), 0) if wait else 0


def _queue_failure(pipe, key: str, now: float) -> None:
    pipe.hincrby(key, 'count', 1)
    pipe.hset(key, 'last', now)
    pipe.expire(key, LOGIN_FAILURE_TTL)


def login_delay_remaining(username: str) -> int:
    """Seconds an onion login for `username` must still wait; 0 when it may go ahead. Reads only."""
    count, last = app_pkg.redis_client.hmget(_login_key(username), 'count', 'last')
    return _remaining(count, last, _now())


def reserve_onion_login_attempt(*usernames: str) -> int:
    """Check and reserve one onion login attempt against every counter in `usernames`, atomically.

    Answers the longest wait among them. When that is 0 the attempt goes ahead and is counted at once, on every
    counter, as a failure: a failure then needs nothing more and a success clears it (`clear_onion_login_failures`).
    Counting before the password is checked is what stops parallel attempts all passing the check before any of them
    has failed: the read and the increment are one optimistic transaction (WATCH/MULTI, which fakeredis supports; it
    has no Lua), and one that another attempt changed under it is decided again on the new state. Past
    RESERVE_RETRIES such collisions the caller is told to wait a second."""
    keys = sorted({_login_key(name) for name in usernames})
    with app_pkg.redis_client.pipeline() as pipe:
        for _ in range(RESERVE_RETRIES):
            try:
                pipe.watch(*keys)
                states = [pipe.hmget(key, 'count', 'last') for key in keys]
                now = _now()
                wait = max(_remaining(count, last, now) for count, last in states)
                if wait:
                    pipe.unwatch()
                    return wait
                pipe.multi()
                for key in keys:
                    _queue_failure(pipe, key, now)
                pipe.execute()
                return 0
            except WatchError:
                continue
    return 1


def record_onion_login_failure(username: str) -> None:
    """One more failed onion login for `username`, counted and timed in one transaction. The key lasts an hour from
    the latest failure."""
    pipe = app_pkg.redis_client.pipeline()
    _queue_failure(pipe, _login_key(username), _now())
    pipe.execute()


def clear_onion_login_failures(*usernames: str) -> None:
    app_pkg.redis_client.delete(*{_login_key(name) for name in usernames})


def onion_login_wait(*usernames: str) -> int:
    """`reserve_onion_login_attempt` for an onion request, 0 on clearnet (never touching Redis). Once it answers 0
    the attempt is already counted as failed, until `onion_login_succeeded` clears it."""
    return reserve_onion_login_attempt(*usernames) if is_onion_request() else 0


def onion_login_succeeded(*usernames: str) -> None:
    if is_onion_request():
        clear_onion_login_failures(*usernames)

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


def login_delay_remaining(username: str) -> int:
    """Seconds the caller must still wait before trying `username`'s password over the onion; 0 to go ahead.

    Going ahead claims the attempt: when a wait has just ended, `last` is moved to now in the same optimistic
    transaction (WATCH/MULTI) as the read, so callers racing this one see the wait again rather than all slipping
    through together. A concurrent change to the key while this one decides counts as a wait too."""
    key = _login_key(username)
    with app_pkg.redis_client.pipeline() as pipe:
        try:
            pipe.watch(key)
            count, last = pipe.hmget(key, 'count', 'last')
            wait = login_wait(int(count or 0))
            if not wait:
                return 0
            now = _now()
            remaining = math.ceil(float(last or 0) + wait - now)
            if remaining > 0:
                return remaining
            pipe.multi()
            pipe.hset(key, 'last', now)
            pipe.execute()
            return 0
        except WatchError:
            return wait


def record_onion_login_failure(username: str) -> None:
    """One more failed onion login for `username`, counted and timed in one transaction. The key lasts an hour from
    the latest failure."""
    key = _login_key(username)
    pipe = app_pkg.redis_client.pipeline()
    pipe.hincrby(key, 'count', 1)
    pipe.hset(key, 'last', _now())
    pipe.expire(key, LOGIN_FAILURE_TTL)
    pipe.execute()


def clear_onion_login_failures(username: str) -> None:
    app_pkg.redis_client.delete(_login_key(username))


def onion_login_wait(username: str) -> int:
    """`login_delay_remaining` for an onion request, 0 on clearnet (never touching Redis)."""
    return login_delay_remaining(username) if is_onion_request() else 0


def onion_login_failed(username: str) -> None:
    if is_onion_request():
        record_onion_login_failure(username)


def onion_login_succeeded(username: str) -> None:
    if is_onion_request():
        clear_onion_login_failures(username)

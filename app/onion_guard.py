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
- **Login delay.** Failed onion logins slow down the identifier that was typed and the account it names: after 5
  failures each attempt waits `min(2 ** (count - 5), 300)` seconds from the last one. The typed identifier's wait is
  shown; the account's is enforced silently, as a wrong password (`OnionLoginGate`). The counters are onion-only, so
  an onion attacker cannot slow the account down on clearnet. It covers web login, API login and verify_credentials.
"""
from __future__ import annotations

import math
import secrets
import time
from typing import NamedTuple
from urllib.parse import quote, quote_from_bytes

from flask import current_app, flash, make_response, redirect, request
from flask_babel import _
from markupsafe import escape
from redis.exceptions import WatchError
from werkzeug.security import generate_password_hash

import app as app_pkg
from app import limiter
from app.models import verify_password_hash
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

# Two namespaces, so a typed name and the account it names never share a counter. Each typed identifier's counter
# (its wait shown) is filled only by attempts typed that way; the account's (enforced silently) by every attempt
# naming it. Shared, attempts typed as an email would have shown up as a wait on the account's name, linking the two.
TYPED_FAILURE_KEY = 'onion_login_fail:typed:{}'
ACCOUNT_FAILURE_KEY = 'onion_login_fail:acct:{}'
FREE_LOGIN_FAILURES = 5
MAX_LOGIN_WAIT = 300
LOGIN_FAILURE_TTL = 3600
RESERVE_RETRIES = 5
# What an account with no password (OAuth or LDAP only) is checked against in a hidden refusal: werkzeug's default
# method and cost, as set_password uses, over a random password nobody knows
DUMMY_PASSWORD_HASH = generate_password_hash(secrets.token_urlsafe(32))


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
    """The one normalisation of a typed onion login identifier (a name or an email): stripped and lower-cased. Web
    login, API login and verify_credentials all count through it."""
    return typed.strip().lower()


class OnionLoginGate(NamedTuple):
    """What an onion login attempt may do. `wait` > 0: the typed identifier is in backoff, and the caller says so
    ("try again in N seconds", or 429). `hidden`: the typed identifier is free but the account it names is in
    backoff; the caller refuses exactly as for a wrong password, without checking it, so nothing shows that this
    identifier and that account are linked (on the onion that would deanonymise). Otherwise it may go ahead."""
    wait: int = 0
    hidden: bool = False

    @property
    def proceed(self) -> bool:
        return not self.wait and not self.hidden


def _login_key(username: str) -> str:
    return TYPED_FAILURE_KEY.format(onion_login_identifier(username))


def _account_key(account_id: int) -> str:
    return ACCOUNT_FAILURE_KEY.format(int(account_id))


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


def _key_remaining(key: str) -> int:
    count, last = app_pkg.redis_client.hmget(key, 'count', 'last')
    return _remaining(count, last, _now())


def login_delay_remaining(username: str) -> int:
    """Seconds an onion login typed as `username` must still wait (its visible wait); 0 if none. Reads only."""
    return _key_remaining(_login_key(username))


def account_delay_remaining(account_id: int) -> int:
    """Seconds the account's own (hidden) counter still holds its onion logins back; 0 if none. Reads only."""
    return _key_remaining(_account_key(account_id))


def reserve_onion_login_attempt(typed: str, account_id: int | None = None) -> OnionLoginGate:
    """Check and reserve one onion login attempt, typed as `typed` and naming the account `account_id` (None when it
    names none), atomically.

    - The typed identifier's counter in backoff: a visible wait, and nothing counted.
    - Otherwise the attempt is counted at once, on both counters, as a failure. A failure then needs nothing more
      and a success clears both (`clear_onion_login_failures`). If the account's counter was in backoff the attempt
      is `hidden`: refused as a wrong password would be. Counting both stops an attacker rotating the ways of naming
      one account (name, email, case, spaces) for fresh free attempts; hiding the account's backoff keeps an email
      from being linked to its account.

    Counting before the password is checked is what stops parallel attempts all passing the check before any of them
    has failed: the read and the increment are one optimistic transaction (WATCH/MULTI, which fakeredis supports; it
    has no Lua), and one that another attempt changed under it is decided again on the new state. Past
    RESERVE_RETRIES such collisions the caller is told to wait a second."""
    typed_key = _login_key(typed)
    keys = [typed_key] if account_id is None else [typed_key, _account_key(account_id)]
    with app_pkg.redis_client.pipeline() as pipe:
        for _ in range(RESERVE_RETRIES):
            try:
                pipe.watch(*keys)
                now = _now()
                wait = _remaining(*pipe.hmget(typed_key, 'count', 'last'), now)
                if wait:
                    pipe.unwatch()
                    return OnionLoginGate(wait=wait)
                hidden = account_id is not None and _remaining(*pipe.hmget(keys[1], 'count', 'last'), now) > 0
                pipe.multi()
                for key in keys:
                    _queue_failure(pipe, key, now)
                pipe.execute()
                return OnionLoginGate(hidden=hidden)
            except WatchError:
                continue
    return OnionLoginGate(wait=1)


def record_onion_login_failure(username: str) -> None:
    """One more failed onion login for `username`, counted and timed in one transaction. The key lasts an hour from
    the latest failure."""
    pipe = app_pkg.redis_client.pipeline()
    _queue_failure(pipe, _login_key(username), _now())
    pipe.execute()


def record_onion_account_failure(account_id: int) -> None:
    """One more failed onion login naming the account `account_id`, as `record_onion_login_failure` counts one typed."""
    pipe = app_pkg.redis_client.pipeline()
    _queue_failure(pipe, _account_key(account_id), _now())
    pipe.execute()


def clear_onion_login_failures(username: str, account_id: int | None = None) -> None:
    """Forget the failures typed as `username`, and the account's own when `account_id` is given."""
    keys = [_login_key(username)] + ([] if account_id is None else [_account_key(account_id)])
    app_pkg.redis_client.delete(*keys)


def onion_login_gate(typed: str, user=None) -> OnionLoginGate:
    """`reserve_onion_login_attempt` for an onion request, typed as `typed` and naming `user` (None for no account);
    always go-ahead on clearnet, never touching Redis. Once it says go ahead the attempt is already counted as
    failed, until `onion_login_succeeded` clears it."""
    if not is_onion_request():
        return OnionLoginGate()
    return reserve_onion_login_attempt(typed, user.id if user is not None else None)


def spend_password_check(user, password) -> None:
    """For a hidden refusal: the verification a wrong password would get, its result ignored, so the refusal takes as
    long as a wrong password does. The attempt is refused whatever it says. It is `User.check_password`'s own
    `verify_password_hash` (werkzeug, then the legacy bcrypt fallback), without check_password's re-save of a legacy
    hash, which a refused attempt must not do. An account with no password is checked against DUMMY_PASSWORD_HASH."""
    verify_password_hash(user.password_hash or DUMMY_PASSWORD_HASH, password)


def onion_login_succeeded(typed: str, user) -> None:
    if is_onion_request():
        clear_onion_login_failures(typed, user.id)

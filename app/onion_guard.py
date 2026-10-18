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
- **Proof-of-work** (3.7a A). An onion login or registration POST must carry a solved challenge (`issue_challenge`,
  solved by app/static/js/onion_pow.js) or, without JavaScript, a wait token at least 15 s old (`issue_wait_token`).
  It is checked first, before the login delay, any account lookup, LDAP or password check, and each proof is good
  once. Both are bound to the session's onion sid and count only when that sid came back from the browser, and a
  session has one wait token outstanding at a time. A refused proof is a form error and costs the account nothing. API logins are not asked: an app cannot solve
  a form's puzzle, so they rely on the login delay and the per-account buckets alone.
"""
from __future__ import annotations

import math
import re
import time
from hashlib import sha256
from secrets import token_hex
from typing import NamedTuple
from urllib.parse import quote, quote_from_bytes

from flask import current_app, flash, make_response, redirect, request, session, url_for
from flask_babel import _, ngettext
from itsdangerous import BadData, TimestampSigner, URLSafeTimedSerializer
from markupsafe import escape
from redis.exceptions import WatchError

import app as app_pkg
from app import limiter
from app.onion import API_RATE_KEY, arrived_onion_sid, is_api_path, is_onion_request, mint_onion_sid
from app.utils import authorise_api_user, get_setting

# Every endpoint with a `@limiter.limit` decorator is in exactly one of these two sets
# (tests/test_onion_guard_limits.py finds the decorators by parsing app/ and fails on one in neither).
ONION_SESSION_ENDPOINTS: frozenset[str] = frozenset({
    'auth.login',
    'auth.onion_wait',
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

POW_SALT = 'onion-pow'
WAIT_SALT = 'onion-wait'
POW_USED_KEY = 'onion_pow_used:{}'
WAIT_USED_KEY = 'onion_wait_used:{}'
WAIT_CURRENT_KEY = 'onion_wait_current:{}'    # per sid: the salt of its one outstanding wait token
POW_MAX_AGE = 600
WAIT_MIN_AGE = 15
WAIT_MAX_AGE = 600
USED_KEY_SLACK = 60    # a used key outlives its token: the stamp is whole seconds, and Redis keeps its own clock
POW_DIFFICULTY_SETTING = 'onion_pow_difficulty'
DEFAULT_POW_DIFFICULTY = 16
MAX_POW_DIFFICULTY = 24       # about 16 million hashes on average: minutes in a browser, so higher would lock people out
NONCE_PATTERN = re.compile(r'[0-9A-Za-z]{1,32}')
MAX_TOKEN_LENGTH = 512
WAIT_TARGETS = {'login': 'auth.login', 'register': 'auth.register'}


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
    backoff; the caller runs its normal flow told to refuse (User.check_password(refuse=True) and the LDAP bind's
    refuse), so it answers and costs exactly what a wrong password does and nothing shows that this identifier and
    that account are linked (on the onion that would deanonymise). Otherwise it may go ahead."""
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
    RESERVE_RETRIES such collisions the caller is told to wait 2 s, the shortest wait a backoff can show."""
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
    # Contention that will not settle: a refusal with nothing counted, showing the first wait a real backoff shows
    return OnionLoginGate(wait=login_wait(FREE_LOGIN_FAILURES + 1))


def clear_onion_login_failures(username: str, account_id: int | None = None) -> None:
    """Forget the failures typed as `username`, and the account's own when `account_id` is given."""
    keys = [_login_key(username)] + ([] if account_id is None else [_account_key(account_id)])
    app_pkg.redis_client.delete(*keys)


def onion_wait_message(seconds: int) -> str:
    """What a typed identifier's visible wait says, on the web (a flash) and the API (the 429's message)."""
    return ngettext('Too many failed logins for this account. Please try again in %(num)d second.',
                    'Too many failed logins for this account. Please try again in %(num)d seconds.', seconds)


def onion_login_gate(typed: str, user=None) -> OnionLoginGate:
    """`reserve_onion_login_attempt` for an onion request, typed as `typed` and naming `user` (None for no account);
    always go-ahead on clearnet, never touching Redis. Once it says go ahead the attempt is already counted as
    failed, until `onion_login_succeeded` clears it."""
    if not is_onion_request():
        return OnionLoginGate()
    return reserve_onion_login_attempt(typed, user.id if user is not None else None)


def onion_login_succeeded(typed: str, user) -> None:
    if is_onion_request():
        clear_onion_login_failures(typed, user.id)


# --------------------------------------------------------------------------
# Proof-of-work and the no-JavaScript wait (3.7a A)
# --------------------------------------------------------------------------

class _ClockSigner(TimestampSigner):
    """Token timestamps on `_now`, the module's one clock, so a test that freezes it freezes token ages too."""

    def get_timestamp(self) -> int:
        return int(_now())


def _serializer(salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.config['SECRET_KEY'], salt=salt, signer=_ClockSigner)


def _load(salt: str, token, max_age: int):
    """The signed body of `token` and its age in seconds, or None for anything not signed here for `salt` within
    `max_age` (a forged, altered, expired or future token, or one that is not a string)."""
    if not isinstance(token, str) or len(token) > MAX_TOKEN_LENGTH:
        return None
    try:
        body, issued = _serializer(salt).loads(token, max_age=max_age, return_timestamp=True)
    except BadData:
        return None
    return body, _now() - issued.timestamp()


def _spend(key: str, salt: str, ttl: int) -> bool:
    """Mark a proof used. SET NX is the decision, so two requests racing with one proof cannot both have it."""
    return bool(app_pkg.redis_client.set(key.format(salt), '1', nx=True, ex=ttl))


def _issuing_sid() -> str | None:
    """The sid a proof issued now is bound to: the session's, which the browser sends back with the form. The pages
    that issue proofs are session first, so it is there (minted during this request at worst, for a session cookie
    that had none)."""
    return session.get('onion_sid')


def _from_this_session(body: dict) -> bool:
    """Whether a proof's body names the sid this request's cookie brought back. A sid minted during this request has
    not been round-tripped, so it proves nothing about a session and matches nothing."""
    sid = arrived_onion_sid()
    return sid is not None and body.get('sid') == sid


def pow_difficulty() -> int:
    """The admin's `onion_pow_difficulty` in leading zero bits, held to 0..MAX_POW_DIFFICULTY. 0 asks no work: any
    nonce will do, but the challenge is still signed, timed and good once."""
    return min(max(int(get_setting(POW_DIFFICULTY_SETTING, DEFAULT_POW_DIFFICULTY)), 0), MAX_POW_DIFFICULTY)


def leading_zero_bits(digest: bytes) -> int:
    return len(digest) * 8 - int.from_bytes(digest, 'big').bit_length()


def _new_challenge() -> tuple[str, str, int]:
    salt, bits = token_hex(16), pow_difficulty()
    return _serializer(POW_SALT).dumps({'salt': salt, 'bits': bits, 'sid': _issuing_sid()}), salt, bits


def issue_challenge() -> str:
    """A signed challenge `{salt, bits, sid}`, good for POW_MAX_AGE seconds in this session."""
    return _new_challenge()[0]


def verify_pow(token, nonce) -> bool:
    """Whether `nonce` solves the challenge `token`: signed here for this session, at most POW_MAX_AGE old, at least as hard as the
    difficulty is now (so raising it applies at once), `sha256(f"{salt}:{nonce}")` with at least its bits of leading
    zeros, and not used before. A success uses it up; a refusal leaves it as it was."""
    loaded = _load(POW_SALT, token, POW_MAX_AGE)
    if loaded is None or not isinstance(nonce, str) or not NONCE_PATTERN.fullmatch(nonce):
        return False
    body = loaded[0]
    if not (isinstance(body, dict) and isinstance(body.get('salt'), str) and type(body.get('bits')) is int
            and _from_this_session(body)):
        return False
    salt, bits = body['salt'], body['bits']
    if bits < pow_difficulty() or leading_zero_bits(sha256(f'{salt}:{nonce}'.encode()).digest()) < bits:
        return False
    return _spend(POW_USED_KEY, salt, POW_MAX_AGE + USED_KEY_SLACK)


def issue_wait_token() -> str:
    """A signed token for the no-JavaScript wait step, good from WAIT_MIN_AGE to WAIT_MAX_AGE seconds after now in
    this session. It replaces the session's last one, so a session cannot queue up waits in parallel. That record is
    in Redis, by sid, not in the session: the session is a cookie, and an old cookie could be sent again."""
    salt, sid = token_hex(16), _issuing_sid()
    if sid is not None:
        app_pkg.redis_client.set(WAIT_CURRENT_KEY.format(sid), salt, ex=WAIT_MAX_AGE)
    return _serializer(WAIT_SALT).dumps({'salt': salt, 'sid': sid})


def wait_token_ready(token) -> bool:
    """Whether `token` is a wait token signed here for this session, WAIT_MIN_AGE to WAIT_MAX_AGE seconds old, the
    session's latest, and not used before. A success uses it up; one that is too early is left as it was."""
    loaded = _load(WAIT_SALT, token, WAIT_MAX_AGE)
    if loaded is None:
        return False
    body, age = loaded
    if not (isinstance(body, dict) and isinstance(body.get('salt'), str) and _from_this_session(body)) \
            or age < WAIT_MIN_AGE:
        return False
    if app_pkg.redis_client.get(WAIT_CURRENT_KEY.format(body['sid'])) != body['salt']:
        return False
    return _spend(WAIT_USED_KEY, body['salt'], WAIT_MAX_AGE + USED_KEY_SLACK)


def onion_proof_refused(form) -> bool:
    """For an onion POST of the login or registration form: True, with the form error flashed, unless it carries a
    wait token that is ready or a solved challenge (either will do, and the one accepted is used up). Always False
    on clearnet, where nothing is asked or read."""
    if not is_onion_request():
        return False
    if wait_token_ready(form.wait_token.data) or verify_pow(form.pow_token.data, form.pow_nonce.data):
        return False
    flash(_("Your browser's check did not go through, or took too long. Please try again."), 'error')
    return True


def prepare_onion_proof(form, target: str):
    """For the onion login (`target` 'login') or registration ('register') page: a fresh challenge on the form's
    hidden fields, the wait token the wait step sent here (if any), and what the template needs to offer the wait
    step. None on clearnet, where the template renders none of it."""
    if not is_onion_request():
        return None
    token, salt, bits = _new_challenge()
    form.pow_token.data = token
    form.pow_nonce.data = ''
    form.wait_token.data = request.args.get('wait_token', '')
    return {'salt': salt, 'bits': bits,
            'wait_url': url_for('auth.onion_wait', form=target, next=request.args.get('next'))}


def onion_wait_target() -> str:
    """Where the wait step sends the browser once its token is ready: the form it came from, as a relative URL (never
    built from Host), carrying the token and the login's `next`."""
    endpoint = WAIT_TARGETS.get(request.args.get('form'), 'auth.login')
    return url_for(endpoint, wait_token=issue_wait_token(), next=request.args.get('next'))

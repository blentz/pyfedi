"""PeerTube's login (spec 2026-11-09-peertube-login-design §2, §3, rulings P1, P2): the domain's local client, handed to
any app that asks; a username and password for a token from that client alone, on the existing OAuth server; and the
logout that revokes a token. Tokens never expire (P2) and are this domain's only, as every OAuth token is.

Errors are PeerTube's problem+json, whose `code` the official app reads to tell bad credentials from the rest."""
from functools import wraps

from flask import g, jsonify, make_response
from werkzeug.exceptions import HTTPException

from app.oauth_server import tokens
from app.oauth_server.protect import require_oauth
from app.oauth_server.server import server
from app.peertube import bp

ERRORS = 'https://docs.joinpeertube.org/api-rest-reference.html#section/Errors/'


def problem(status: int, code: str, detail: str):
    """PeerTube's error: application/problem+json with the machine-readable `code` its apps branch on."""
    response = make_response(jsonify({'type': ERRORS + code, 'title': detail, 'detail': detail, 'status': status,
                                      'code': code}), status)
    response.mimetype = 'application/problem+json'
    return response


REFUSALS = {401: 'invalid_token', 403: 'forbidden', 422: 'invalid_token'}


def signed_in(scope: str = 'read'):
    """The route acts for the person a token names (app.oauth_server.protect.require_oauth, with its checks and the
    login gate left to it), refusing in PeerTube's shape. A 401 is what sends PeerTube's app to refresh its token."""
    def decorator(view):
        @wraps(view)
        def guarded(*args, **kwargs):
            reached = []

            def inner(*inner_args, **inner_kwargs):
                reached.append(True)
                return view(*inner_args, **inner_kwargs)
            result = require_oauth(scope)(inner)(*args, **kwargs)
            if reached:
                return result
            refusal, status = result
            return problem(status, REFUSALS.get(status, 'invalid_token'), refusal.get_json()['error'])
        guarded.has_own_login_gate = True
        return guarded
    return decorator


@bp.route('/api/v1/oauth-clients/local')
def oauth_client_local():
    client, secret = tokens.local_client()
    return jsonify({'client_id': client.client_id, 'client_secret': secret})


def _as_peertube(response):
    """Authlib's answer, its errors in PeerTube's shape: every refusal a 400, as PeerTube's are, which is also what
    the official app takes to mean "log in again" for a refresh."""
    if response.status_code < 400:
        return response
    body = response.get_json(silent=True) or {}
    code = body.get('error') or 'invalid_request'
    return problem(400, code, body.get('error_description') or 'Invalid request')


@bp.route('/api/v1/users/token', methods=['POST'])
def issue_token():
    """Every grant, the password grant counting its guesses itself (app.oauth_server.server.PasswordGrant)."""
    try:
        return _as_peertube(server.create_token_response())
    except HTTPException as refusal:   # the password limit, or the onion login gate's wait
        if refusal.code != 429:
            raise
        return problem(429, 'too_many_requests', 'Too many login attempts, try again later')


@bp.route('/api/v1/users/revoke-token', methods=['POST'])
@signed_in()
def revoke_token():
    tokens.revoke(g.oauth_token)
    return '', 200

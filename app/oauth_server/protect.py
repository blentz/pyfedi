"""What an API route asks of an OAuth token (plan W0.2 step 6; spec
docs/superpowers/specs/2026-11-07-mastodon-client-api-design.md §3.1, §5): unrevoked, its person still able to sign in,
its scopes covering the route. Refusals are Mastodon's."""
from datetime import timedelta
from functools import wraps

from flask import g, jsonify, request

from app import db
from app.models import OAuthToken, User, utcnow
from app.oauth_scopes import covers
from app.oauth_server import tokens
from app.storefront import act_in_this_request
from app.utils import login_required_here

LAST_USED_RESOLUTION = timedelta(hours=1)


def bearer_token() -> OAuthToken | None:
    """The usable token this request's `Authorization: Bearer` header carries, or None."""
    header = request.headers.get('Authorization', '')
    if not header.startswith('Bearer '):
        return None
    token = tokens.token_for(header[len('Bearer '):].strip())
    return token if token is not None and tokens.usable_token(token) else None


def _refuse(status: int, error: str):
    response = jsonify({'error': error})
    if status == 401:
        response.headers['WWW-Authenticate'] = 'Bearer error="invalid_token"'
    return response, status


def _record_use(token: OAuthToken) -> None:
    now = utcnow()
    if token.last_used_at is None or token.last_used_at < now - LAST_USED_RESOLUTION:
        token.last_used_at = now
        db.session.commit()


def require_oauth(scope: str | None = None, user_required: bool = True, optional: bool = False):
    """The route needs a token covering `scope`, and one acting for a person unless `user_required` is False. Sets
    g.oauth_token and g.oauth_user (None for an app's own token, or for nobody). With `optional`, a request with no
    Authorization header comes in as nobody, as Mastodon's public reads do, except on a private instance; a header
    that does not check out is still refused."""
    def decorator(view):
        @wraps(view)
        def guarded(*args, **kwargs):
            if optional and 'Authorization' not in request.headers and not login_required_here():
                g.oauth_token, g.oauth_user = None, None
                return view(*args, **kwargs)
            token = bearer_token()
            if token is None:
                return _refuse(401, 'The access token is invalid')
            if scope is not None and not covers(token.scope, scope):
                return _refuse(403, 'This action is outside the authorized scopes')
            if token.user_id is None and login_required_here():
                return _refuse(401, 'The access token is invalid')   # spec §5: on a private instance, a person's only
            if user_required and token.user_id is None:
                return _refuse(422, 'This method requires an authenticated user')
            user = db.session.get(User, token.user_id) if token.user_id else None
            if user is not None:
                act_in_this_request(user.home_domain())   # its fetches name its domain, as the alpha API's do
            _record_use(token)
            g.oauth_token, g.oauth_user = token, user
            return view(*args, **kwargs)
        # The login gate (app.login_gate._has_own_gate) leaves the token to this check, scope and all: the gate
        # lets no OAuth token past on its own, so one cannot reach a route that would read it as nobody
        guarded.has_own_login_gate = True
        return guarded
    return decorator

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


def require_oauth(scope: str | None = None, user_required: bool = True):
    """The route needs a token covering `scope`, and one acting for a person unless `user_required` is False. Sets
    g.oauth_token and g.oauth_user (None for an app's own token)."""
    def decorator(view):
        @wraps(view)
        def guarded(*args, **kwargs):
            token = bearer_token()
            if token is None:
                return _refuse(401, 'The access token is invalid')
            if scope is not None and not covers(token.scope, scope):
                return _refuse(403, 'This action is outside the authorized scopes')
            if user_required and token.user_id is None:
                return _refuse(422, 'This method requires an authenticated user')
            user = db.session.get(User, token.user_id) if token.user_id else None
            if user is not None:
                act_in_this_request(user.home_domain())   # its fetches name its domain, as the alpha API's do
            _record_use(token)
            g.oauth_token, g.oauth_user = token, user
            return view(*args, **kwargs)
        return guarded
    return decorator

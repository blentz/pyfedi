"""Signing in to gpodder sync (spec §3, ruling G1). An app password (app.oauth_server.tokens.issue_app_password) by
HTTP Basic signs in any call; AntennaPod sends it to login.json alone and carries the `sessionid` cookie that sets: the
token's id, signed, for a day. Every call re-reads the token, so revoking its app ends a session at once. The path's
user name must be the person signed in. The login gate leaves these routes to this check, as require_oauth's. No rate
limit: an app password is a random 256-bit secret, as a bearer token is (whose check is not limited either), and the
account password, which a person could choose weakly, is never taken."""
from functools import wraps

from flask import current_app, g, jsonify, make_response, request
from itsdangerous import BadSignature, URLSafeTimedSerializer

from app import db
from app.models import OAuthToken, User
from app.oauth_scopes import GPODDER_SCOPE, covers
from app.oauth_server import tokens
from app.oauth_server.protect import record_use
from app.gpodder import bp
from app.storefront import act_in_this_request

COOKIE, COOKIE_PATH, SESSION_SECONDS = 'sessionid', '/api/2/', 24 * 60 * 60


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.config['SECRET_KEY'], salt='gpodder-session')


def _usable(token: OAuthToken | None) -> bool:
    return token is not None and covers(token.scope, GPODDER_SCOPE) and tokens.usable_token(token)


def _from_basic() -> OAuthToken | None:
    """The app password Basic carries, if the name with it is its person's."""
    auth = request.authorization
    if auth is None or auth.type != 'basic' or not auth.username or not auth.password:
        return None
    token = tokens.token_for(auth.password)
    if not _usable(token):
        return None
    user = db.session.get(User, token.user_id)
    return token if user.user_name.lower() == auth.username.strip().lower() else None


def _from_cookie() -> OAuthToken | None:
    value = request.cookies.get(COOKIE)
    if not value:
        return None
    try:
        token_id = _serializer().loads(value, max_age=SESSION_SECONDS)
    except BadSignature:
        return None
    token = db.session.get(OAuthToken, token_id) if isinstance(token_id, int) else None
    return token if _usable(token) and token.revoked_at is None and token.domain == tokens.here() else None


def unauthorised():
    response = make_response(jsonify({'error': 'unauthorized'}), 401)
    response.headers['WWW-Authenticate'] = 'Basic realm="PieFed"'
    return response


def signed_in(view):
    """The route acts for the person an app password names, by Basic or the session cookie, who must be the path's
    `username`. Sets g.gpodder_user and g.gpodder_token."""
    @wraps(view)
    def guarded(username, *args, **kwargs):
        token = _from_basic() or _from_cookie()
        if token is None:
            return unauthorised()
        user = db.session.get(User, token.user_id)
        if user.user_name.lower() != username.lower():
            return unauthorised()
        act_in_this_request(user.home_domain())
        record_use(token)
        g.gpodder_user, g.gpodder_token = user, token
        return view(username, *args, **kwargs)
    guarded.has_own_login_gate = True
    return guarded


@bp.route('/api/2/auth/<username>/login.json', methods=['POST'])
@signed_in
def login(username):
    response = make_response('', 200)
    response.set_cookie(COOKIE, _serializer().dumps(g.gpodder_token.id), max_age=SESSION_SECONDS, path=COOKIE_PATH,
                        httponly=True, secure=request.scheme == 'https', samesite='Lax')
    return response


@bp.route('/api/2/auth/<username>/logout.json', methods=['POST'])
@signed_in
def logout(username):
    response = make_response('', 200)
    response.delete_cookie(COOKIE, path=COOKIE_PATH)
    return response

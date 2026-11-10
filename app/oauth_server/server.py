"""The OAuth 2 authorization server: Authlib's, over app.oauth_server.tokens (plan W0.2; spec
docs/superpowers/specs/2026-11-07-mastodon-client-api-design.md §3.1, §5)."""
import time
from functools import cached_property

from authlib.integrations.flask_oauth2 import AuthorizationServer as FlaskAuthorizationServer
from authlib.integrations.flask_oauth2.requests import FlaskOAuth2Payload, FlaskOAuth2Request
from authlib.oauth2.rfc6749 import InvalidGrantError, InvalidRequestError, InvalidScopeError, UnauthorizedClientError, grants
from authlib.oauth2.rfc7009 import RevocationEndpoint as _RevocationEndpoint
from authlib.oauth2.rfc7636 import CodeChallenge
from flask import request as flask_request
from werkzeug.datastructures import MultiDict

from app import db
from app.models import OAuthApp, User
from app.oauth_scopes import covers
from app.oauth_server import admin_scopes, tokens
from app.shared.auth import log_in_with_password

_SECRET_OR_PKCE = ['client_secret_basic', 'client_secret_post', 'none']


def _fields(request) -> MultiDict:
    """The request's fields: its form and query, or a JSON object's string values, which Mastodon also takes."""
    body = request.get_json(silent=True) if request.is_json else None
    if not isinstance(body, dict):
        return request.values
    fields = MultiDict(request.args)
    for name, value in body.items():
        if isinstance(value, str):
            fields[name] = value
    return fields


class _Payload(FlaskOAuth2Payload):
    @cached_property
    def data(self):
        return _fields(self._request)


class _Request(FlaskOAuth2Request):
    def __init__(self, request):
        super().__init__(request)
        self.payload = _Payload(request)

    @property
    def form(self):   # the refresh grant reads its token from here
        return self.payload.data


class AuthorizationServer(FlaskAuthorizationServer):
    def create_oauth2_request(self, request):
        return _Request(flask_request)


class _S256Only(CodeChallenge):
    """PKCE, required of an app that has no secret (RFC 9700), optional for one that has; S256 only, as `plain`
    protects nothing a code does not."""
    SUPPORTED_CODE_CHALLENGE_METHOD = ['S256']


class AuthorizationCodeGrant(grants.AuthorizationCodeGrant):
    TOKEN_ENDPOINT_AUTH_METHODS = _SECRET_OR_PKCE

    def save_authorization_code(self, code, request):
        payload = request.payload.data
        # The redirect as asked for: blank when the app left it out, so its token request need not send one (RFC 6749
        # 4.1.3: required there only if it was in the authorization request)
        # the admin scopes only for a person holding the role they map to (app.oauth_server.admin_scopes)
        tokens.store_code(code, request.client, request.user, request.payload.redirect_uri or '',
                          admin_scopes.granted(request.scope, request.user), payload.get('code_challenge'),
                          payload.get('code_challenge_method'))

    def query_authorization_code(self, code, client):
        return tokens.code_for(client, code)

    def delete_authorization_code(self, authorization_code):
        db.session.delete(authorization_code)
        db.session.commit()

    def authenticate_user(self, authorization_code):
        user = db.session.get(User, authorization_code.user_id)
        return user if tokens.usable_user(user) else None


class RefreshTokenGrant(grants.RefreshTokenGrant):
    """A new access and refresh token for the old refresh token, which ends with its access token. Only with the app's
    secret, which every app here has: a stolen refresh token is no use alone."""
    TOKEN_ENDPOINT_AUTH_METHODS = ['client_secret_basic', 'client_secret_post']
    INCLUDE_NEW_REFRESH_TOKEN = True

    def create_token_response(self):
        """The old token is claimed before the new pair is made, once checked: two refreshes at once get one pair."""
        if not tokens.claim(self.request.refresh_token):
            raise InvalidGrantError()
        return super().create_token_response()

    def authenticate_refresh_token(self, refresh_token):
        """Unknown, revoked, or its person can no longer sign in: all invalid_grant (RFC 6749 5.2)."""
        credential = tokens.refresh_token_for(refresh_token)
        return credential if credential is not None and tokens.usable_token(credential) else None

    def authenticate_user(self, credential):
        return db.session.get(User, credential.user_id)

    def revoke_old_credential(self, credential):
        pass   # claimed in create_token_response, before the new pair


class PasswordGrant(grants.ResourceOwnerPasswordCredentialsGrant):
    """PeerTube's login: a username and password for a token, from this domain's PeerTube local client only (spec
    2026-11-09-peertube-login-design §3, ruling P1), with every check of the API's password login. Any refusal is
    invalid_grant, so it never tells which names have accounts."""
    TOKEN_ENDPOINT_AUTH_METHODS = ['client_secret_basic', 'client_secret_post']

    def validate_token_request(self):
        client = self.authenticate_token_endpoint_client()
        if client.client_id != tokens.local_client_id() or not client.check_grant_type(self.GRANT_TYPE):
            raise UnauthorizedClientError("The client is not authorized to use 'grant_type=password'")
        params = self.request.form
        if not params.get('username') or not params.get('password'):
            raise InvalidRequestError("Missing 'username' or 'password' in request.")
        user = self.authenticate_user(params['username'], params['password'])
        if user is None:
            raise InvalidGrantError('Invalid credentials')
        self.request.client, self.request.user = client, user
        self.validate_requested_scope()

    def authenticate_user(self, username, password):
        try:
            user = log_in_with_password(username, password)
        except Exception as refusal:   # the one exception the login raises for every refusal; an abort passes through
            if str(refusal) == 'incorrect_login':
                return None
            raise
        return user if tokens.usable_user(user) else None


class ClientCredentialsGrant(grants.ClientCredentialsGrant):
    """An app's own token, with no person behind it: read only (spec §3.1). Only with the app's secret."""
    TOKEN_ENDPOINT_AUTH_METHODS = ['client_secret_basic', 'client_secret_post']

    def create_token_response(self):
        # Here rather than in validate_token_request: a scan of app/ for WTForms inline validators reads any
        # validate_<name> as one (tests/test_post_and_user_forms.py). Nothing is issued before this runs.
        if not all(covers('read', name) for name in (self.request.payload.scope or 'read').split()):
            raise InvalidScopeError("An app's own token can only read.")
        return super().create_token_response()


class RevocationEndpoint(_RevocationEndpoint):
    """Either token of a pair ends both (RFC 7009). One nobody knows is no error; another app's is refused."""
    CLIENT_AUTH_METHODS = _SECRET_OR_PKCE

    def query_token(self, token_string, token_type_hint):
        if token_type_hint == 'refresh_token':
            return tokens.refresh_token_for(token_string) or tokens.token_for(token_string)
        return tokens.token_for(token_string) or tokens.refresh_token_for(token_string)

    def revoke_token(self, token, request):
        tokens.revoke(token)


def _query_client(client_id):
    return OAuthApp.query.filter_by(client_id=client_id).first() if client_id else None


def _generate_token(grant_type, client, user=None, scope=None, expires_in=None, include_refresh_token=True):
    """Mastodon's token: no expiry (ruling M6, and P2 for PeerTube's), its scope and when it was made. PeerTube's
    apps ask for no scope, and get what the local client may do."""
    if not scope:
        scope = client.scope if grant_type == 'password' else 'read'
    token = {'token_type': 'Bearer', 'access_token': tokens.new_secret(), 'scope': scope,
             'created_at': int(time.time())}
    if include_refresh_token and user is not None:
        token['refresh_token'] = tokens.new_secret()
    return token


def _save_token(token, request):
    tokens.store_token(request.client, request.user, token['scope'], token['access_token'], token.get('refresh_token'))


server = AuthorizationServer()
server.register_grant(AuthorizationCodeGrant, [_S256Only(required=True)])
server.register_grant(RefreshTokenGrant)
server.register_grant(ClientCredentialsGrant)
server.register_grant(PasswordGrant)
server.register_endpoint(RevocationEndpoint)


def init_app(app):
    server.init_app(app, query_client=_query_client, save_token=_save_token)
    server.register_token_generator('default', _generate_token)

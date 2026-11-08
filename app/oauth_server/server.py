"""The OAuth 2 authorization server: Authlib's, over app.oauth_server.tokens (plan W0.2; spec
docs/superpowers/specs/2026-11-07-mastodon-client-api-design.md §3.1, §5)."""
from authlib.integrations.flask_oauth2 import AuthorizationServer
from authlib.oauth2.rfc6749 import grants
from authlib.oauth2.rfc7636 import CodeChallenge

from app import db
from app.models import OAuthApp, User
from app.oauth_server import tokens


class _S256Only(CodeChallenge):
    """PKCE, required of an app that has no secret (RFC 9700), optional for one that has; S256 only, as `plain`
    protects nothing a code does not."""
    SUPPORTED_CODE_CHALLENGE_METHOD = ['S256']


class AuthorizationCodeGrant(grants.AuthorizationCodeGrant):
    TOKEN_ENDPOINT_AUTH_METHODS = ['client_secret_basic', 'client_secret_post', 'none']

    def save_authorization_code(self, code, request):
        payload = request.payload.data
        # The redirect as asked for: blank when the app left it out, so its token request need not send one (RFC 6749
        # 4.1.3: required there only if it was in the authorization request)
        tokens.store_code(code, request.client, request.user, request.payload.redirect_uri or '', request.scope,
                          payload.get('code_challenge'), payload.get('code_challenge_method'))

    def query_authorization_code(self, code, client):
        return tokens.code_for(client, code)

    def delete_authorization_code(self, authorization_code):
        db.session.delete(authorization_code)
        db.session.commit()

    def authenticate_user(self, authorization_code):
        return db.session.get(User, authorization_code.user_id)


def _query_client(client_id):
    return OAuthApp.query.filter_by(client_id=client_id).first() if client_id else None


def _save_token(token, request):
    raise NotImplementedError('the token endpoint is W0.2 step 4')


server = AuthorizationServer()
server.register_grant(AuthorizationCodeGrant, [_S256Only(required=True)])


def init_app(app):
    server.init_app(app, query_client=_query_client, save_token=_save_token)

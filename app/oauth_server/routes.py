"""The OAuth 2 endpoints client apps call (spec docs/superpowers/specs/2026-11-07-mastodon-client-api-design.md §3.1)."""
from urllib.parse import parse_qs, urlsplit

from authlib.oauth2 import OAuth2Error
from flask import jsonify, make_response, redirect, request, url_for
from flask_babel import lazy_gettext as _l
from flask_login import current_user

from app import limiter
from app.models import OAuthApp
from app.oauth_scopes import SCOPES, is_admin, parse_scope
from app.oauth_server import admin_scopes, bp, tokens
from app.oauth_server.server import server
from app.storefront import current_storefront, domain_base_url
from app.utils import login_required, render_template

OOB = 'urn:ietf:wg:oauth:2.0:oob'
_REFUSED_SCHEMES = frozenset(('javascript', 'data', 'vbscript', 'file', 'blob'))


class _Invalid(ValueError):
    pass


def _refuse(reason: str):
    return jsonify({'error': f'Validation failed: {reason}'}), 422


def _redirect_uri(uri) -> str:
    """`uri` if an app may be sent back to it: OOB, or an absolute URI with no fragment, a host for http(s), and no
    scheme a browser would run or read locally."""
    if not isinstance(uri, str):
        raise _Invalid('Redirect URI must be a string')
    if uri == OOB:
        return uri
    if len(uri) > 2000:
        raise _Invalid('Redirect URI is too long')
    if any(character.isspace() or ord(character) < 0x20 or ord(character) == 0x7f for character in uri):
        raise _Invalid('Redirect URI must not contain whitespace')   # stored one per line
    try:
        parts = urlsplit(uri)
    except ValueError:
        raise _Invalid('Redirect URI must be an absolute URI') from None
    scheme = parts.scheme.lower()
    if not scheme or not (parts.netloc or parts.path) or scheme in _REFUSED_SCHEMES:
        raise _Invalid('Redirect URI must be an absolute URI')
    if parts.fragment or '#' in uri:
        raise _Invalid('Redirect URI must not contain a fragment')
    if scheme in ('http', 'https') and not parts.hostname:
        raise _Invalid('Redirect URI must be an absolute URI')
    return uri


def _website(value) -> str | None:
    """An absolute http(s) URL with a host, or None: the consent page links to it."""
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 1024:
        raise _Invalid('Website is invalid')
    try:
        parts = urlsplit(value)
    except ValueError:
        raise _Invalid('Website is invalid') from None
    if parts.scheme.lower() not in ('http', 'https') or not parts.hostname:
        raise _Invalid('Website is invalid')
    return value


def _redirect_uris(value) -> list[str]:
    if isinstance(value, str):
        value = value.split()
    if not isinstance(value, list) or not value:
        raise _Invalid("Redirect URI can't be blank")
    return [_redirect_uri(uri) for uri in value]


@bp.route('/api/v1/apps', methods=['POST'])
@limiter.limit('30 per 5 minutes;300 per day', methods=['POST'])
def register_app():
    """Mastodon's app registration: open to anyone, as Mastodon's is; the secret is shown here once."""
    fields = request.get_json(silent=True) if request.is_json else request.form
    if not isinstance(fields, dict) and fields is not request.form:
        return _refuse("Name can't be blank")
    name = fields.get('client_name')
    name = name.strip() if isinstance(name, str) else ''
    website = fields.get('website') or None
    try:
        if not name:
            raise _Invalid("Name can't be blank")
        if len(name) > 255:
            raise _Invalid('Name is too long')
        website = _website(website)
        redirect_uris = _redirect_uris(fields.get('redirect_uris'))
        try:
            scope = parse_scope(fields.get('scopes'))
        except ValueError as unknown:   # named, so an app's developer knows which
            raise _Invalid(f'Scopes are invalid ({unknown})') from None
    except _Invalid as invalid:
        return _refuse(str(invalid))
    registered, client_secret = tokens.register_app(name, redirect_uris, scope, website)
    return jsonify({'id': str(registered.id), 'name': registered.name, 'website': registered.website,
                    'scopes': scope.split(), 'redirect_uri': '\n'.join(redirect_uris), 'redirect_uris': redirect_uris,
                    'client_id': registered.client_id, 'client_secret': client_secret, 'client_secret_expires_at': 0})


SCOPE_LABELS = {
    'read': _l('Read everything your account can see'),
    'write': _l('Post, vote, and change your account and its settings'),
    'follow': _l('Follow, block and mute people and communities'),
    'push': _l('Receive notifications'),
    'profile': _l('See your profile'),
    'admin:read': _l('Read moderation and admin data, as far as your role allows'),
    'admin:write': _l('Moderate and administer, as far as your role allows'),
    'security:read': _l("Your account's security settings (not given to apps here)"),
    'security:write': _l("Change your account's security settings (not given to apps here)"),
}


def _framed_never(response):
    response.headers['X-Frame-Options'] = 'DENY'
    policy = response.headers.get('Content-Security-Policy')
    response.headers['Content-Security-Policy'] = f"{policy}; frame-ancestors 'none'" if policy \
        else "frame-ancestors 'none'"
    return response


def _page(template, status=200, **context):
    """An OAuth page: in the site's theme, framed by nobody, kept by no cache (one may hold a code)."""
    response = _framed_never(make_response(render_template(template, **context), status))
    response.headers['Cache-Control'] = 'no-store'
    return response


def _registered_redirect(uri: str | None) -> bool:
    """Whether an error may be sent to `uri`: one the app named in this request registered, and not OOB. Authlib
    attaches only a checked redirect to an error; this does not rely on it."""
    if not uri or uri == OOB:
        return False
    registered = OAuthApp.query.filter_by(client_id=request.args.get('client_id')).first()
    return registered is not None and registered.check_redirect_uri(uri)


@bp.route('/oauth/authorize', methods=['GET', 'POST'])
@login_required(csrf=True)
def authorize():
    """The person, signed in, allows or denies the app what it asks for. Until the app and its redirect check out,
    an error is shown here and nobody is sent anywhere."""
    try:
        grant = server.get_consent_grant(end_user=current_user)
    except OAuth2Error as error:
        if _registered_redirect(error.redirect_uri):
            return server.handle_error_response(None, error)
        return _page('oauth/error.html', 400, error=error.description or error.error)
    if request.method == 'POST':
        allowed = 'approve' in request.form
        response = server.create_authorization_response(grant_user=current_user if allowed else None, grant=grant)
        if grant.redirect_uri != OOB:
            return response
        code = parse_qs(urlsplit(response.headers['Location']).query).get('code', [None])[0]
        return _page('oauth/oob.html', code=code, app=grant.client)
    withheld = not admin_scopes.may_hold(current_user)   # admin scopes asked by someone not holding their role
    scopes = [(name, SCOPE_LABELS.get(name, name), withheld and is_admin(name)) for name in grant.request.scope.split()]
    role = admin_scopes.admin_role()
    return _page('oauth/authorize.html', app=grant.client, scopes=scopes, user=current_user,
                 admin_role=role.name if role is not None else None)


@bp.route('/oauth/token', methods=['POST'])
@limiter.limit('60 per 5 minutes;1000 per day', methods=['POST'])
def issue_token():
    """A code, a refresh token or the app's own credentials, for an access token (Authlib answers the errors)."""
    return server.create_token_response()


@bp.route('/oauth/revoke', methods=['POST'])
@limiter.limit('60 per 5 minutes;1000 per day', methods=['POST'])
def revoke_token():
    return server.create_endpoint_response('revocation')


@bp.route('/.well-known/oauth-authorization-server')
def authorization_server_metadata():
    """RFC 8414, as Mastodon 4.3+ publishes it, for the domain asked."""
    base = domain_base_url(current_storefront())
    return jsonify({
        'issuer': base + '/',
        'authorization_endpoint': base + url_for('oauth_server.authorize'),
        'token_endpoint': base + url_for('oauth_server.issue_token'),
        'revocation_endpoint': base + url_for('oauth_server.revoke_token'),
        'app_registration_endpoint': base + url_for('oauth_server.register_app'),
        'scopes_supported': sorted(SCOPES),
        'response_types_supported': ['code'],
        'response_modes_supported': ['query'],
        'code_challenge_methods_supported': ['S256'],
        'grant_types_supported': ['authorization_code', 'refresh_token', 'client_credentials'],
        'token_endpoint_auth_methods_supported': ['client_secret_basic', 'client_secret_post', 'none'],
    })


@bp.route('/user/settings/apps')
@login_required(csrf=True)
def authorised_apps():
    """The apps this person has allowed, and a button to take each one's access back."""
    return render_template('user/oauth_apps.html', title=_l('Authorised apps'), user=current_user,
                           apps=tokens.authorised_apps(current_user), scope_labels=SCOPE_LABELS)


@bp.route('/user/settings/apps/<int:app_id>/revoke', methods=['POST'])
@login_required(csrf=True)
def revoke_app(app_id):
    tokens.revoke_app_for(current_user, app_id)
    return redirect(url_for('oauth_server.authorised_apps'))

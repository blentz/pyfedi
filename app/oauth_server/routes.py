"""The OAuth 2 endpoints client apps call (spec docs/superpowers/specs/2026-11-07-mastodon-client-api-design.md §3.1)."""
from urllib.parse import urlsplit

from flask import jsonify, request

from app import limiter
from app.oauth_server import bp, tokens

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
            scope = tokens.parse_scope(fields.get('scopes'))
        except ValueError:
            raise _Invalid('Scopes are invalid') from None
    except _Invalid as invalid:
        return _refuse(str(invalid))
    registered, client_secret = tokens.register_app(name, redirect_uris, scope, website)
    return jsonify({'id': str(registered.id), 'name': registered.name, 'website': registered.website,
                    'scopes': scope.split(), 'redirect_uri': '\n'.join(redirect_uris), 'redirect_uris': redirect_uris,
                    'client_id': registered.client_id, 'client_secret': client_secret, 'client_secret_expires_at': 0})

"""OAuth 2 apps, codes and tokens (plan W0.2; spec docs/superpowers/specs/2026-11-07-mastodon-client-api-design.md
§3.1, §5). Every secret is random, shown once, and kept as a SHA-256 digest, so a copy of the database holds no
usable credential; a raw value is found by its digest."""
import secrets
from datetime import timedelta
from hashlib import sha256

from app import db
from app.models import OAuthApp, OAuthCode, OAuthToken, User, utcnow

CODE_LIFETIME_SECONDS = 600

# Mastodon's scopes, without admin (PieFed has no admin API for them). `push` is accepted for the apps that ask for
# it at registration (Tusky asks for `read write follow push`) and grants nothing yet.
_READ = ('accounts', 'blocks', 'bookmarks', 'favourites', 'filters', 'follows', 'lists', 'mutes', 'notifications',
         'search', 'statuses')
_WRITE = ('accounts', 'blocks', 'bookmarks', 'conversations', 'favourites', 'filters', 'follows', 'lists', 'media',
          'mutes', 'notifications', 'reports', 'statuses')
SCOPES = frozenset(('read', 'write', 'follow', 'push', 'profile')
                   + tuple(f'read:{name}' for name in _READ) + tuple(f'write:{name}' for name in _WRITE))


def digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _secret() -> str:
    return secrets.token_urlsafe(32)


def parse_scope(scope: str | None) -> str:
    """The space-separated scopes, normalised; none means `read`, as Mastodon's default. ValueError for one PieFed
    does not know."""
    names = (scope or '').split()
    if not names:
        return 'read'
    unknown = [name for name in names if name not in SCOPES]
    if unknown:
        raise ValueError(f'unknown scope: {unknown[0]}')
    return ' '.join(names)


def register_app(name: str, redirect_uris: list[str], scope: str, website: str | None) -> tuple[OAuthApp, str]:
    """A new app, with the secret it is given once."""
    client_secret = _secret()
    registered = OAuthApp(client_id=secrets.token_urlsafe(24), client_secret_hash=digest(client_secret), name=name,
                          website=website, redirect_uris='\n'.join(redirect_uris), scope=scope)
    db.session.add(registered)
    db.session.commit()
    return registered, client_secret


def issue_code(registered: OAuthApp, user: User, redirect_uri: str, scope: str, code_challenge: str | None,
               code_challenge_method: str | None) -> str:
    raw = _secret()
    db.session.add(OAuthCode(code_hash=digest(raw), app_id=registered.id, user_id=user.id, redirect_uri=redirect_uri,
                             scope=scope, code_challenge=code_challenge,
                             code_challenge_method=code_challenge_method))
    db.session.commit()
    return raw


def code_for(registered: OAuthApp, raw: str) -> OAuthCode | None:
    """The unexpired code `raw` names, if this app was given it."""
    if not raw:
        return None
    return OAuthCode.query.filter(OAuthCode.code_hash == digest(raw), OAuthCode.app_id == registered.id,
                                  OAuthCode.created_at > utcnow() - timedelta(seconds=CODE_LIFETIME_SECONDS)).first()


def issue_token(registered: OAuthApp, user: User | None, scope: str,
                with_refresh: bool = True) -> tuple[str, str | None]:
    """A new access token, and its refresh token unless `with_refresh` is False."""
    access = _secret()
    refresh = _secret() if with_refresh else None
    db.session.add(OAuthToken(app_id=registered.id, user_id=user.id if user else None, access_token_hash=digest(access),
                              refresh_token_hash=digest(refresh) if refresh else None, scope=scope))
    db.session.commit()
    return access, refresh


def token_for(raw: str) -> OAuthToken | None:
    """The unrevoked token whose access token is `raw`."""
    if not raw:
        return None
    return OAuthToken.query.filter(OAuthToken.access_token_hash == digest(raw), OAuthToken.revoked_at.is_(None)).first()


def refresh_token_for(raw: str) -> OAuthToken | None:
    """The unrevoked token whose refresh token is `raw`."""
    if not raw:
        return None
    return OAuthToken.query.filter(OAuthToken.refresh_token_hash == digest(raw),
                                   OAuthToken.revoked_at.is_(None)).first()


def revoke(token: OAuthToken) -> None:
    token.revoked_at = utcnow()
    db.session.commit()

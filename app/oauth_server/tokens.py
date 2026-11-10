"""OAuth 2 apps, codes and tokens (plan W0.2; spec docs/superpowers/specs/2026-11-07-mastodon-client-api-design.md
§3.1, §5). Every secret is random, shown once, and kept as a SHA-256 digest, so a copy of the database holds no
usable credential; a raw value is found by its digest. A code or token belongs to the domain (storefront) it was issued
on and is found only there (owner ruling 2026-11-08): a login on one domain is no credential on another, and cannot be
exchanged, refreshed, revoked or used to link the two."""
import hmac
import secrets
from datetime import timedelta
from hashlib import sha256

from flask import current_app
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from app import db
from app.models import PEERTUBE_LOCAL_CLIENT_PREFIX, OAuthApp, OAuthCode, OAuthToken, User, utcnow
from app.storefront import current_storefront

CODE_LIFETIME_SECONDS = 600
LOCAL_CLIENT_SCOPE = 'read write follow'   # what PeerTube's apps do: read, comment, rate and subscribe


def here() -> str:
    """The domain this request came in on: the one a code or token is issued on, and found on."""
    return current_storefront().domain


def digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def new_secret() -> str:
    return secrets.token_urlsafe(32)


def usable_user(user: User | None) -> bool:
    """Whether a token may act for `user`: the checks the alpha API makes of its own tokens (authorise_api_user)."""
    return user is not None and user.ap_id is None and user.verified and not user.banned and not user.deleted \
        and not user.access_has_lapsed()


def usable_token(token: OAuthToken) -> bool:
    """Whether `token` still acts for its person: one who can sign in, and has not changed password since it was
    made. An app's own token has nobody to check."""
    if token.user_id is None:
        return True
    user = db.session.get(User, token.user_id)
    if not usable_user(user):
        return False
    return not (user.password_updated_at and token.created_at < user.password_updated_at)


def register_app(name: str, redirect_uris: list[str], scope: str, website: str | None) -> tuple[OAuthApp, str]:
    """A new app, with the secret it is given once."""
    client_secret = new_secret()
    registered = OAuthApp(client_id=secrets.token_urlsafe(24), client_secret_hash=digest(client_secret), name=name,
                          website=website, redirect_uris='\n'.join(redirect_uris), scope=scope)
    db.session.add(registered)
    db.session.commit()
    return registered, client_secret


def issue_code(registered: OAuthApp, user: User, redirect_uri: str, scope: str, code_challenge: str | None,
               code_challenge_method: str | None) -> str:
    raw = new_secret()
    store_code(raw, registered, user, redirect_uri, scope, code_challenge, code_challenge_method)
    return raw


def store_code(raw: str, registered: OAuthApp, user: User, redirect_uri: str, scope: str, code_challenge: str | None,
               code_challenge_method: str | None) -> None:
    db.session.add(OAuthCode(code_hash=digest(raw), app_id=registered.id, user_id=user.id, redirect_uri=redirect_uri,
                             scope=scope, code_challenge=code_challenge,
                             code_challenge_method=code_challenge_method, domain=here()))
    db.session.commit()


def code_for(registered: OAuthApp, raw: str) -> OAuthCode | None:
    """The unexpired code `raw` names, if this app was given it."""
    if not raw:
        return None
    return OAuthCode.query.filter(OAuthCode.code_hash == digest(raw), OAuthCode.app_id == registered.id,
                                  OAuthCode.domain == here(),
                                  OAuthCode.created_at > utcnow() - timedelta(seconds=CODE_LIFETIME_SECONDS)).first()


def issue_token(registered: OAuthApp, user: User | None, scope: str,
                with_refresh: bool = True) -> tuple[str, str | None]:
    """A new access token, and its refresh token unless `with_refresh` is False."""
    access = new_secret()
    refresh = new_secret() if with_refresh else None
    store_token(registered, user, scope, access, refresh)
    return access, refresh


def store_token(registered: OAuthApp, user: User | None, scope: str, access: str, refresh: str | None) -> OAuthToken:
    token = OAuthToken(app_id=registered.id, user_id=user.id if user else None, access_token_hash=digest(access),
                       refresh_token_hash=digest(refresh) if refresh else None, scope=scope, domain=here())
    db.session.add(token)
    db.session.commit()
    return token


def token_for(raw: str) -> OAuthToken | None:
    """The unrevoked token whose access token is `raw`, issued on this domain."""
    if not raw:
        return None
    return OAuthToken.query.filter(OAuthToken.access_token_hash == digest(raw), OAuthToken.domain == here(),
                                   OAuthToken.revoked_at.is_(None)).first()


def refresh_token_for(raw: str) -> OAuthToken | None:
    """The unrevoked token whose refresh token is `raw`, issued on this domain."""
    if not raw:
        return None
    return OAuthToken.query.filter(OAuthToken.refresh_token_hash == digest(raw), OAuthToken.domain == here(),
                                   OAuthToken.revoked_at.is_(None)).first()


def revoke(token: OAuthToken) -> None:
    token.revoked_at = utcnow()
    db.session.commit()


def claim(token: OAuthToken) -> bool:
    """End `token` if nothing has yet, in one statement: of two requests racing to refresh it, one gets True."""
    claimed = db.session.execute(update(OAuthToken).where(OAuthToken.id == token.id, OAuthToken.revoked_at.is_(None))
                                 .values(revoked_at=utcnow())).rowcount == 1
    db.session.commit()
    return claimed


def authorised_apps(user: User) -> list[dict]:
    """The apps `user` has an unrevoked token for: each with every scope its tokens hold, when it was first allowed
    and when last used, newest first."""
    apps = {}
    for token in OAuthToken.query.filter(OAuthToken.user_id == user.id, OAuthToken.revoked_at.is_(None)):
        entry = apps.setdefault(token.app_id, {'app': token.app, 'scopes': set(), 'since': token.created_at,
                                               'last_used': None})
        entry['scopes'].update(token.scope.split())
        entry['since'] = min(entry['since'], token.created_at)
        if token.last_used_at and (entry['last_used'] is None or token.last_used_at > entry['last_used']):
            entry['last_used'] = token.last_used_at
    return sorted(apps.values(), key=lambda entry: entry['since'], reverse=True)


def revoke_app_for(user: User, app_id: int) -> None:
    """End every token `user` gave app `app_id`, and any code not yet exchanged."""
    db.session.execute(update(OAuthToken).where(OAuthToken.user_id == user.id, OAuthToken.app_id == app_id,
                                                OAuthToken.revoked_at.is_(None)).values(revoked_at=utcnow()))
    OAuthCode.query.filter(OAuthCode.user_id == user.id, OAuthCode.app_id == app_id).delete()
    db.session.commit()


def _derived(purpose: str) -> str:
    """A value fixed for this domain, made from the site's secret key, so the local client needs no stored secret."""
    key = str(current_app.config['SECRET_KEY']).encode()
    return hmac.new(key, f'{purpose}:{here()}'.encode(), sha256).hexdigest()


def local_client_id() -> str:
    return PEERTUBE_LOCAL_CLIENT_PREFIX + _derived('peertube-client-id')[:40]


def local_client() -> tuple[OAuthApp, str]:
    """This domain's PeerTube local client and its secret, which PeerTube hands to anyone who asks: the password is the
    credential (spec 2026-11-09-peertube-login-design §3, §7, ruling P1). Made the first time it is asked for, and the
    same every time after."""
    client_id, secret = local_client_id(), _derived('peertube-client-secret')
    found = OAuthApp.query.filter_by(client_id=client_id).first()
    if found is None:
        db.session.add(OAuthApp(client_id=client_id, client_secret_hash=digest(secret), name='PeerTube',
                                redirect_uris='', scope=LOCAL_CLIENT_SCOPE))
        try:
            db.session.commit()
        except IntegrityError:   # another request made it first
            db.session.rollback()
        found = OAuthApp.query.filter_by(client_id=client_id).one()
    if not found.check_client_secret(secret):   # the site's secret key has changed since it was made
        found.client_secret_hash = digest(secret)
        db.session.commit()
    return found, secret

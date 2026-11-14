"""A connection's tokens: sealed at rest, refreshed one at a time (spec §8).

Bluesky revokes a whole session when a used refresh token is replayed (spec §2), so two workers must never refresh one
connection at once. The refresh runs holding the connection row (SELECT ... FOR UPDATE), reads the tokens again under
the lock, since another worker may have refreshed them already, and commits the new ones before anything uses them.
"""
from flask_babel import _

from app import db
from app.connections import KEY, AuthFailed, Service
from app.constants import NOTIF_DEFAULT
from app.models import Notification, SocialConnection
from app.sealing import seal_with, unseal_with

REAUTH = 'reauth'


def seal_tokens(tokens: dict) -> str:
    return seal_with(KEY, tokens)


def open_tokens(connection: SocialConnection) -> dict:
    return unseal_with(KEY, connection.sealed_tokens)


def needs_reconnecting(connection: SocialConnection, service: Service, message: str) -> None:
    """Mark the connection as needing a new sign-in and tell the member, once: a connection already marked is not
    announced again. The caller commits."""
    connection.last_error = message[:255]
    if connection.state == REAUTH:
        return
    connection.state = REAUTH
    db.session.add(Notification(
        title=_('Reconnect your %(service)s account %(name)s to keep posting there',
                service=service.label, name=connection.remote_name)[:150],
        url='/user/settings/connections', user_id=connection.user_id, author_id=connection.user_id,
        notif_type=NOTIF_DEFAULT, subtype='connection_reauth', targets={'connection_id': connection.id}))


def usable_tokens(connection_id: int, service: Service) -> dict:
    """The connection's tokens, refreshed first if they have expired. Raises AuthFailed (the connection then needs
    reconnecting) when they cannot be. Ends the caller's transaction: it commits to let the row lock go."""
    connection = db.session.query(SocialConnection).filter_by(id=connection_id).with_for_update().one()
    if connection.state == REAUTH:
        db.session.commit()
        raise AuthFailed(_('This account needs reconnecting.'))
    tokens = open_tokens(connection)
    if not service.expired(tokens):
        db.session.commit()
        return tokens
    try:
        tokens = service.refresh(tokens)
    except AuthFailed as failure:
        needs_reconnecting(connection, service, failure.message)
        db.session.commit()
        raise
    connection.sealed_tokens = seal_tokens(tokens)
    db.session.commit()
    return tokens

"""Accounts coming and going (ATProto spec 2026-11-23 §7, §8; P5): Jetstream's `identity` and `account` events.

For an ATProto account someone here follows:

    identity    its handle (and profile) read again
    account     inactive (deactivated, suspended, taken down): its posts and replies hidden, until it is active
                again; deleted: its content purged and the account deleted, as a remote user's Delete does

For a member's own Bluesky account (a connection's DID):

    identity    the handle it now has (renamed in an app); a hosted account whose DID document names another
                server has moved away (§8): `moved`, and the member is told, and may connect it again by OAuth
    account     suspended or taken down: a hosted account is `suspended`, an OAuth one waits to be reconnected,
                and the member is told; active again: back as it was. Deleted: the connection goes (a hosted one
                PieFed is deleting already is left to that).
"""
from flask_babel import _

from app import db
from app.atproto import actors
from app.atproto import identity as atproto_identity
from app.atproto.resolve import did_document, pds_of
from app.connections import bluesky_hosted as hosted
from app.connections.tokens import REAUTH
from app.constants import NOTIF_DEFAULT
from app.models import Notification, SocialConnection
from app.user.utils import purge_remote_user_then_delete

INACTIVE_STATES = ('takendown', 'suspended')   # what Bluesky's moderation does (a member deactivating is theirs)
STATUS_NOTE = 'Bluesky: account '                 # the start of last_error when Bluesky itself stopped an account


def tell(connection: SocialConnection, title: str) -> None:
    db.session.add(Notification(title=title[:150], url='/user/settings/connections', user_id=connection.user_id,
                                author_id=connection.user_id, notif_type=NOTIF_DEFAULT, subtype='bluesky_account',
                                targets={'connection_id': connection.id}))


def ours(did: str) -> list:
    return SocialConnection.query.filter_by(service='bluesky', remote_id=did).order_by(SocialConnection.id).all()


# --------------------------------------------------------------------------
# identity events
# --------------------------------------------------------------------------

def identity(did: str) -> None:
    """An identity event: the DID's handle or server may have changed."""
    connections = ours(did)
    if connections:
        own_identity(did, connections)
        return
    user = actors.actor_for(did, fetch_new=False)
    if user is not None:
        actors.refresh(user)


def own_identity(did: str, connections: list) -> None:
    document = did_document(did)
    handle = actors.verified_handle(did, document)
    server = pds_of(document)
    for connection in connections:
        if handle != did:
            connection.remote_name = handle[:255]
        if connection.variant == hosted.HOSTED and connection.slot_state not in (hosted.MOVED, hosted.DELETING) and \
                server != hosted.pds_url().rstrip('/'):
            connection.slot_state = hosted.MOVED
            tell(connection, _('Your Bluesky account @%(handle)s moved to another server. Connect it again to keep '
                               'using it here.', handle=connection.remote_name))
    db.session.commit()


# --------------------------------------------------------------------------
# account events
# --------------------------------------------------------------------------

def account(did: str, active, status) -> None:
    """An account event: `active` true or false, and, when false, `status` (deactivated, suspended, takendown,
    deleted...)."""
    if not isinstance(active, bool):
        return
    connections = ours(did)
    if connections:
        for connection in connections:
            own_account(connection, active, status)
        db.session.commit()
        return
    user = actors.actor_for(did, fetch_new=False)
    if user is None or user.deleted:
        return
    if active:
        actors.restore_content(user)
    elif status == 'deleted':
        purge_remote_user_then_delete(user.id)
    else:
        actors.hide_content(user)


def own_account(connection: SocialConnection, active: bool, status) -> None:
    """A member's own account's event. The caller commits."""
    is_hosted = connection.variant == hosted.HOSTED
    if active:
        if is_hosted and connection.slot_state == hosted.SUSPENDED:
            connection.slot_state = hosted.ACTIVE
            tell(connection, _('Your Bluesky account @%(handle)s is back.', handle=connection.remote_name))
            db.session.commit()
            atproto_identity.caught_up(connection.user_id)
        elif not is_hosted and connection.state == REAUTH and (connection.last_error or '').startswith(STATUS_NOTE):
            connection.state, connection.last_error = 'ok', None
            tell(connection, _('Your Bluesky account @%(handle)s is back.', handle=connection.remote_name))
            db.session.commit()
            atproto_identity.caught_up(connection.user_id)
        return
    if status == 'deleted':
        if not (is_hosted and connection.slot_state == hosted.DELETING):
            tell(connection, _('Your Bluesky account @%(handle)s was deleted on Bluesky.',
                               handle=connection.remote_name))
            db.session.delete(connection)
        return
    if status not in INACTIVE_STATES:   # deactivated: a hosted one PieFed turned off itself, or a member did
        return
    if is_hosted and connection.slot_state == hosted.ACTIVE:
        connection.slot_state = hosted.SUSPENDED
    elif not is_hosted:
        connection.state, connection.last_error = REAUTH, f'{STATUS_NOTE}{status}'
    else:
        return
    tell(connection, _('Bluesky suspended your account @%(handle)s: nothing goes to it until Bluesky restores it.',
                       handle=connection.remote_name))

"""What the inbox needs to read an Accept or Reject of the instance actor's Follow (interop D24). It imports nothing
from app.activitypub, so routes.py can import it at the top without a cycle through instance_actor -> signature."""
from urllib.parse import urlsplit

from flask import current_app

from app import db
from app.discovery import SYNC_ACCEPTED, SYNC_FAILED
from app.models import DiscoveryPersonSync, DiscoverySync

# Delivery answers that mean the peer will not take this Follow as sent; a 5xx and 429 are retried by the send
# queue, and a transport failure is transient, so neither is final
DEFINITIVE_REFUSALS = (401, 403, 404, 406, 410)


def instance_actor_url() -> str:
    return f"{current_app.config['SERVER_URL']}/actor"


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or '').lower()
    except ValueError:   # a netloc urlsplit refuses (unbalanced IPv6 bracket) is a host that matches nothing
        return ''


# A channel's row and a Pixelfed person's (spec 2026-10-29) answer the same way: one Follow uuid names one row of one
SYNC_TABLES = (DiscoverySync, DiscoveryPersonSync)


def _by_uuid(session, follow_uuid: str):
    for table in SYNC_TABLES:
        row = session.query(table).filter_by(follow_uuid=follow_uuid).first()
        if row is not None:
            return row
    return None


def _by_target(target: str):
    for table in SYNC_TABLES:
        row = db.session.query(table).filter(db.func.lower(table.follow_target) == target.lower()).first()
        if row is not None:
            return row
    return None


def _row_for(follow, signer: str):
    """The row a Follow (a dict, or its id as a string) names, when `signer` is on the followed actor's host."""
    row = None
    follow_id = follow.get('id') if isinstance(follow, dict) else follow
    if isinstance(follow_id, str) and '/activities/follow/' in follow_id:
        row = _by_uuid(db.session, follow_id.rsplit('/', 1)[-1])
    elif isinstance(follow, dict) and isinstance(follow.get('object'), str):
        row = _by_target(follow['object'])
    if row is None or _host(row.follow_target) != _host(signer):
        return None
    return row


def instance_actor_answer(activity: dict, signer: str):
    """(ours, row) for an Accept/Reject. `ours` is True when the answered Follow came from the instance actor: a
    Follow object whose actor is /actor, or a bare follow id that a sync row holds. `row` is that row, only when the
    signed `signer` is on the followed actor's host (anyone can name a uuid)."""
    obj = activity.get('object')
    if isinstance(obj, dict):
        if obj.get('actor') != instance_actor_url():
            return False, None
        return True, _row_for(obj, signer)
    if isinstance(obj, str):
        held = _by_uuid(db.session, obj.rsplit('/', 1)[-1])
        if held is None:
            return False, None
        return True, _row_for(obj, signer)
    return False, None


def record_answer(row, state: str) -> None:
    row.follow_state = state
    db.session.commit()


def record_delivery_refusal(session, body, status_code) -> None:
    """When a peer refuses the instance actor's Follow outright, mark its sync row failed, so the status table says
    so instead of "Waiting for an answer" for a week. An Accept that already arrived stands."""
    if status_code not in DEFINITIVE_REFUSALS or not isinstance(body, dict):
        return
    if body.get('type') != 'Follow' or body.get('actor') != instance_actor_url():
        return
    follow_id = body.get('id')
    if not isinstance(follow_id, str) or '/activities/follow/' not in follow_id:
        return
    row = _by_uuid(session, follow_id.rsplit('/', 1)[-1])
    if row is None or row.follow_state == SYNC_ACCEPTED:
        return
    row.follow_state = SYNC_FAILED
    row.last_error = f'Follow refused: HTTP {status_code}'
    session.commit()

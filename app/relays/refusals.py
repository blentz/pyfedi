"""A relay that refuses our Follow outright (spec: Subscribing). Imports nothing from app.activitypub."""
from app.discovery.instance_answers import DEFINITIVE_REFUSALS
from app.models import Relay
from app.relays import FOLLOW_PATH, RELAY_ACCEPTED, RELAY_FAILED


def record_relay_refusal(session, body, status_code) -> None:
    """Mark the relay row failed; an Accept already received stands."""
    if status_code not in DEFINITIVE_REFUSALS or not isinstance(body, dict) or body.get('type') != 'Follow':
        return
    follow_id = body.get('id')
    if not isinstance(follow_id, str) or FOLLOW_PATH not in follow_id:
        return
    relay = session.query(Relay).filter_by(follow_activity_id=follow_id).first()
    if relay is None or relay.state == RELAY_ACCEPTED:
        return
    relay.state = RELAY_FAILED
    relay.last_error = f'Follow refused: HTTP {status_code}'
    session.commit()

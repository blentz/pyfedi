"""The instance actor (/actor) as the follower of proactively synced channels and podcasts (interop D24). A Follow
from /actor puts no user in the community: Accept and Reject only change the discovery_sync row."""
import uuid

from flask import current_app

from app import db
from app.activitypub.signature import send_post_request
from app.activitypub.util import host_of
from app.discovery import SYNC_NONE, SYNC_PENDING
from app.models import DiscoverySync, Site, utcnow


def instance_actor_url() -> str:
    return f"{current_app.config['SERVER_URL']}/actor"


def _signing():
    site = db.session.get(Site, 1)   # not g.site: this runs from Celery and the CLI too
    return site.private_key, instance_actor_url() + '#main-key'


def follow_activity(row) -> dict:
    return {'actor': instance_actor_url(), 'to': [row.follow_target], 'object': row.follow_target, 'type': 'Follow',
            'id': f"{current_app.config['SERVER_URL']}/activities/follow/{row.follow_uuid}"}


def send_instance_follow(row, community) -> bool:
    """Send (or re-send, under the same uuid) the instance actor's Follow. An offline or inbox-less peer gets
    nothing and the row stays 'none', which the next reconcile retries."""
    instance = community.instance
    if instance is None or not instance.online() or not community.ap_inbox_url:
        row.follow_state = SYNC_NONE
        db.session.commit()
        return False
    if not row.follow_uuid:
        row.follow_uuid = str(uuid.uuid4())
    private_key, key_id = _signing()
    send_post_request(community.ap_inbox_url, follow_activity(row), private_key, key_id, timeout=10)
    row.follow_state = SYNC_PENDING
    row.followed_at = utcnow()
    db.session.commit()
    return True


def send_instance_undo(row, community) -> None:
    """Best-effort: a peer that cannot be told still loses the row, so a dead host never pins it."""
    if not row.follow_uuid or not community.ap_inbox_url:
        return
    undo = {'type': 'Undo', 'actor': instance_actor_url(), 'object': follow_activity(row),
            'id': f"{current_app.config['SERVER_URL']}/activities/undo/{uuid.uuid4()}"}
    private_key, key_id = _signing()
    try:
        send_post_request(community.ap_inbox_url, undo, private_key, key_id, timeout=10)
    except Exception as error:
        current_app.logger.info(f'discovery sync: undo to {community.ap_inbox_url} failed: {type(error).__name__}')


def _row_for(follow, signer: str):
    """The row a Follow (a dict, or its id as a string) names, when `signer` is on the followed actor's host."""
    row = None
    follow_id = follow.get('id') if isinstance(follow, dict) else follow
    if isinstance(follow_id, str) and '/activities/follow/' in follow_id:
        row = db.session.query(DiscoverySync).filter_by(follow_uuid=follow_id.rsplit('/', 1)[-1]).first()
    elif isinstance(follow, dict) and isinstance(follow.get('object'), str):
        row = db.session.query(DiscoverySync).filter(
            db.func.lower(DiscoverySync.follow_target) == follow['object'].lower()).first()
    if row is None or host_of(row.follow_target).lower() != host_of(signer).lower():
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
        held = db.session.query(DiscoverySync.community_id).filter_by(follow_uuid=obj.rsplit('/', 1)[-1]).first()
        if held is None:
            return False, None
        return True, _row_for(obj, signer)
    return False, None


def record_answer(row, state: str) -> None:
    row.follow_state = state
    db.session.commit()

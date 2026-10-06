"""The instance actor (/actor) as the follower of proactively synced channels and podcasts (interop D24). A Follow
from /actor puts no user in the community: Accept and Reject only change the discovery_sync row."""
import uuid

from flask import current_app

from app import db
from app.activitypub.signature import send_post_request
from app.discovery import SYNC_NONE, SYNC_PENDING
from app.discovery.instance_answers import instance_actor_url
from app.models import Site, utcnow


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
    try:
        undo = {'type': 'Undo', 'actor': instance_actor_url(), 'object': follow_activity(row),
                'id': f"{current_app.config['SERVER_URL']}/activities/undo/{uuid.uuid4()}"}
        private_key, key_id = _signing()
        send_post_request(community.ap_inbox_url, undo, private_key, key_id, timeout=10)
    except Exception as error:
        current_app.logger.info(f'discovery sync: undo to {community.ap_inbox_url} failed: {type(error).__name__}')

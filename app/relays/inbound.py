"""Relay traffic in the inbox (spec: Recognising a relay delivery; What is kept)."""
from flask import current_app
from sqlalchemy import func

from app import celery, db
from app.activitypub.signature import HttpSignature, VerificationError
from app.activitypub.util import (create_resolved_object, find_microblogging_community, host_of, is_top_level,
                                  log_incoming_ap, remote_object_to_json)
from app.constants import APLOG_ANNOUNCE, APLOG_FAILURE, APLOG_IGNORED
from app.models import Community, Post, PostReply, Relay, utcnow
from app.relays import RELAY_ACCEPTED, RELAY_PENDING, RELAY_REFUSED, relay_context
from app.relays.subscribe import RelayError, detect_relay
from app.utils import get_task_session, patch_db_session


def _verified(request, relay) -> bool:
    try:
        HttpSignature.verify_request(request, relay.public_key, skip_date=True)
        return True
    except VerificationError:
        return False


def _verified_with_refetch(request, relay) -> bool:
    if relay.public_key and _verified(request, relay):
        return True
    try:
        relay.public_key = detect_relay(relay.url)['public_key']
    except RelayError:
        return False
    db.session.commit()
    return bool(relay.public_key) and _verified(request, relay)


def _answered_follow_id(activity):
    obj = activity.get('object')
    if isinstance(obj, dict):
        return obj.get('id')
    return obj if isinstance(obj, str) else None


def relay_actor_gate(request, request_json):
    """A response tuple when the activity is from a relay actor with a stored key, else None."""
    actor = request_json.get('actor')
    if not isinstance(actor, str):
        return None
    relay = db.session.query(Relay).filter(Relay.actor_id == actor).first()
    if relay is None:
        return None
    activity_type = request_json.get('type')
    if activity_type == 'Announce' and relay.state != RELAY_ACCEPTED:
        return None   # not subscribed (yet): ordinary traffic, the follow gate applies
    if not _verified_with_refetch(request, relay):
        return '', 401
    if activity_type in ('Accept', 'Reject'):
        if relay.state == RELAY_PENDING and _answered_follow_id(request_json) == relay.follow_activity_id:
            relay.state = RELAY_ACCEPTED if activity_type == 'Accept' else RELAY_REFUSED
            relay.answered_at = utcnow()
            db.session.commit()
        return '', 200
    if activity_type == 'Announce':
        obj = request_json.get('object')
        uri = obj.get('id') if isinstance(obj, dict) else obj
        if isinstance(uri, str):
            if current_app.debug:
                process_relayed_announce(relay.id, uri)
            else:
                process_relayed_announce.apply_async(args=(relay.id, uri), queue='background')
    return '', 200


def _exists(ap_id) -> bool:
    return bool(Post.get_by_ap_id(ap_id) or PostReply.get_by_ap_id(ap_id))


def _target_community(post_data):
    """The known community a relayed object is addressed to, else None."""
    audience = post_data.get('audience')
    if isinstance(audience, str) and audience:
        return db.session.query(Community).filter(func.lower(Community.ap_profile_id) == audience.lower()).first()
    return None


def relayed_object_allowed(obj):
    if not isinstance(obj, dict):
        return False, 'relayed object is not an object'
    reply_to = obj.get('inReplyTo')
    if reply_to:
        target = reply_to.get('id') if isinstance(reply_to, dict) else reply_to
        if not isinstance(target, str) or not _exists(target):
            return False, 'relayed reply to a post this instance does not have'
        return True, ''
    if obj.get('audience') and _target_community(obj) is None:
        return False, 'relayed post for a community this instance does not have'
    return True, ''


def relayed_activity_allowed(activity):
    activity_type = activity.get('type')
    if activity_type == 'Announce':
        return False, 'relayed boost'
    if activity_type == 'Create':
        return relayed_object_allowed(activity.get('object'))
    return True, ''


@celery.task
def process_relayed_announce(relay_id, object_uri):
    session = get_task_session()
    try:
        with patch_db_session(session):
            if Post.get_by_ap_id(object_uri):
                return
            post_data = remote_object_to_json(object_uri)
            if not post_data:
                log_incoming_ap(object_uri, APLOG_ANNOUNCE, APLOG_FAILURE, None, 'Could not fetch relayed object')
                return
            allowed, reason = relayed_object_allowed(post_data)
            if not allowed or not is_top_level(post_data):
                log_incoming_ap(object_uri, APLOG_ANNOUNCE, APLOG_IGNORED, None, reason or 'relayed reply')
                return
            community = _target_community(post_data) or find_microblogging_community()
            with relay_context(relay_id):
                create_resolved_object(object_uri, post_data, host_of(object_uri), community, object_uri, False)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

"""Relay traffic in the inbox (spec: Recognising a relay delivery; What is kept)."""
from flask import current_app
from sqlalchemy import func

import app as app_pkg
from app import celery, db
from app.activitypub.signature import HttpSignature, VerificationError, parse_signature_header
from app.activitypub.util import (create_resolved_object, find_microblogging_community, host_of,
                                  log_incoming_ap, remote_object_to_json)
from app.constants import APLOG_ANNOUNCE, APLOG_FAILURE, APLOG_FOLLOW, APLOG_IGNORED, APLOG_NOTYPE, APLOG_SUCCESS
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


REFETCH_SECONDS = 300


def _names_the_relay_actor(request, relay) -> bool:
    key_id = parse_signature_header(request.headers.get('Signature')).get('keyid', '')
    return key_id.split('#')[0] == relay.actor_id


def _verified_with_refetch(request, relay) -> bool:
    if relay.public_key and _verified(request, relay):
        return True
    # An unauthenticated request must not make this instance fetch: only a signature that names the relay
    # actor's own key, and at most once per relay in five minutes.
    if not _names_the_relay_actor(request, relay):
        return False
    if not app_pkg.redis_client.set(f'relay-refetch:{relay.id}', 1, nx=True, ex=REFETCH_SECONDS):
        return False
    try:
        fetched = detect_relay(relay.url)['public_key']
    except RelayError:
        return False
    if not fetched or fetched == relay.public_key:
        return False
    relay.public_key = fetched
    db.session.commit()
    return _verified(request, relay)


def _answered_follow_id(activity):
    obj = activity.get('object')
    if isinstance(obj, dict):
        return obj.get('id')
    return obj if isinstance(obj, str) else None


def _answer_follow(relay, request_json, activity_type, saved_json):
    """Record the relay's answer when it is for our pending Follow; log either way."""
    activity_id = request_json.get('id') or ''
    follow_id = _answered_follow_id(request_json)
    if relay.state != RELAY_PENDING:
        log_incoming_ap(activity_id, APLOG_FOLLOW, APLOG_IGNORED, saved_json, 'Relay is not awaiting an answer')
    elif follow_id is not None and follow_id != relay.follow_activity_id:
        log_incoming_ap(activity_id, APLOG_FOLLOW, APLOG_IGNORED, saved_json, 'Relay answered a different Follow')
    else:
        relay.state = RELAY_ACCEPTED if activity_type == 'Accept' else RELAY_REFUSED
        relay.answered_at = utcnow()
        db.session.commit()
        log_incoming_ap(activity_id, APLOG_FOLLOW, APLOG_SUCCESS, saved_json)


def relay_actor_gate(request, request_json):
    """A response tuple for a relay's Accept/Reject, or an Announce from an accepted relay; else None.

    Runs after HttpSignature.precheck, so the Digest and date have already been checked."""
    actor = request_json.get('actor')
    if not isinstance(actor, str):
        return None
    activity_type = request_json.get('type')
    if activity_type not in ('Accept', 'Reject', 'Announce'):
        return None
    relay = db.session.query(Relay).filter(Relay.actor_id == actor).first()
    if relay is None or (activity_type == 'Announce' and relay.state != RELAY_ACCEPTED):
        return None   # not a relay, or not subscribed (yet): ordinary traffic, the follow gate applies
    if not _verified_with_refetch(request, relay):
        log_incoming_ap(request_json.get('id') or '', APLOG_NOTYPE, APLOG_FAILURE, None,
                        'Relay signature did not verify')
        return '', 401
    if activity_type == 'Announce':
        obj = request_json.get('object')
        uri = obj.get('id') if isinstance(obj, dict) else obj
        if isinstance(uri, str):
            if current_app.debug:
                process_relayed_announce(relay.id, uri)
            else:
                process_relayed_announce.apply_async(args=(relay.id, uri), queue='background')
    else:
        _answer_follow(relay, request_json, activity_type, None)
    return '', 200


def _exists(ap_id) -> bool:
    return bool(Post.get_by_ap_id(ap_id) or PostReply.get_by_ap_id(ap_id))


def _target_community(post_data):
    """The known community a relayed object is addressed to, else None."""
    audience = post_data.get('audience')
    if isinstance(audience, str) and audience:
        return db.session.query(Community).filter(func.lower(Community.ap_profile_id) == audience.lower()).first()
    return None


def _reply_parent(post_data):
    """The stored Post or PostReply a relayed reply answers, else None."""
    target = post_data.get('inReplyTo')
    if not target:
        return None
    return Post.get_by_ap_id(target) or PostReply.get_by_ap_id(target)


def _reply_target_open(parent) -> bool:
    """False for a parent the normal reply path would not let anyone reply under.

    The reply path itself refuses a locked or archived post, a locked reply, a local-only community and a
    banned author; a deleted parent and a private community it does not."""
    post = parent if isinstance(parent, Post) else parent.post
    if parent.deleted or post.deleted:
        return False
    community = post.community
    return not (community.private or community.local_only or community.banned)


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
            if not allowed:
                log_incoming_ap(object_uri, APLOG_ANNOUNCE, APLOG_IGNORED, None, reason)
                return
            if isinstance(post_data.get('inReplyTo'), dict):   # the reply code reads a parent's URL, not an object
                post_data['inReplyTo'] = post_data['inReplyTo']['id']
            parent = _reply_parent(post_data)
            if parent is not None and not _reply_target_open(parent):
                log_incoming_ap(object_uri, APLOG_ANNOUNCE, APLOG_IGNORED, None, 'relayed reply to a closed thread')
                return
            community = (parent.community if parent is not None else
                         _target_community(post_data) or find_microblogging_community())
            with relay_context(relay_id):
                create_resolved_object(object_uri, post_data, host_of(object_uri), community, object_uri, False)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

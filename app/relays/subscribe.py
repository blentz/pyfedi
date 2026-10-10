"""Subscribing the instance actor to relays (spec: Subscribing)."""
import uuid
from urllib.parse import urlsplit, urlunsplit

from flask import current_app

from app import db
from app.activitypub.signature import send_post_request
from app.activitypub.util import remote_object_to_json
from app.discovery.instance_answers import instance_actor_url
from app.models import Relay, Site, utcnow
from app.relays import FOLLOW_PATH, PUBLIC, RELAY_PENDING, STYLE_LITEPUB, STYLE_MASTODON


class RelayError(Exception):
    pass


def _signing():
    site = db.session.get(Site, 1)
    return site.private_key, instance_actor_url() + '#main-key'


def _actor_parts(document):
    if not isinstance(document, dict) or not isinstance(document.get('id'), str):
        return None
    key = document.get('publicKey')
    pem = key.get('publicKeyPem') if isinstance(key, dict) else None
    return document['id'], document.get('inbox'), pem


RELAY_ACTOR_TYPES = ('Application', 'Service')


RELAY_ACTOR_TYPES = ('Application', 'Service')


def detect_relay(url: str) -> dict:
    parts = urlsplit(url)
    if parts.path.rstrip('/').endswith('/inbox'):
        actor_url = urlunsplit(parts._replace(path=parts.path.rstrip('/')[:-len('inbox')] + 'actor'))
        document = remote_object_to_json(actor_url)
        is_relay = isinstance(document, dict) and document.get('type') in RELAY_ACTOR_TYPES
        found = _actor_parts(document) if is_relay else None
        actor_id, public_key = (found[0], found[2]) if found else (None, None)
        return {'style': STYLE_MASTODON, 'inbox_url': url, 'actor_id': actor_id, 'public_key': public_key}
    document = remote_object_to_json(url)
    found = _actor_parts(document)
    if not found or not isinstance(found[1], str):
        raise RelayError(f'{url} is not a relay actor with an inbox')
    if document.get('type') not in RELAY_ACTOR_TYPES:
        raise RelayError(f'{url} is not a relay actor')
    return {'style': STYLE_LITEPUB, 'inbox_url': found[1], 'actor_id': found[0], 'public_key': found[2]}


def relay_follow_activity(relay) -> dict:
    follow = {'type': 'Follow', 'id': relay.follow_activity_id, 'actor': instance_actor_url()}
    if relay.style == STYLE_MASTODON:
        # Addressed to nobody, as Mastodon's own relay Follow is: Activity-Relay treats anything addressed to
        # Public as an activity to relay, and answers a Follow so addressed with a 202 and nothing else.
        return {**follow, 'object': PUBLIC}
    return {**follow, 'object': relay.actor_id, 'to': [relay.actor_id]}


def _new_follow_id() -> str:
    return f"{current_app.config['SERVER_URL']}{FOLLOW_PATH}{uuid.uuid4()}"


def send_relay_follow(relay) -> None:
    private_key, key_id = _signing()
    send_post_request(relay.inbox_url, relay_follow_activity(relay), private_key, key_id, timeout=10)


def add_relay(url: str) -> Relay:
    url = url.strip()
    if db.session.query(Relay).filter_by(url=url).first():
        raise RelayError(f'{url} is already a relay')
    found = detect_relay(url)
    relay = Relay(url=url, follow_activity_id=_new_follow_id(), state=RELAY_PENDING, created_at=utcnow(), **found)
    db.session.add(relay)
    db.session.commit()
    send_relay_follow(relay)
    return relay


def retry_relay(relay) -> None:
    for name, value in detect_relay(relay.url).items():
        if value is not None:   # a failed fetch must not erase an actor or key we already know
            setattr(relay, name, value)
    relay.follow_activity_id = _new_follow_id()
    relay.state = RELAY_PENDING
    relay.answered_at = None
    relay.last_error = None
    db.session.commit()
    send_relay_follow(relay)


def remove_relay(relay) -> None:
    try:
        undo = {'type': 'Undo', 'actor': instance_actor_url(), 'object': relay_follow_activity(relay),
                'id': f"{current_app.config['SERVER_URL']}/activities/undo/{uuid.uuid4()}"}
        private_key, key_id = _signing()
        send_post_request(relay.inbox_url, undo, private_key, key_id, timeout=10)
    except Exception as error:
        current_app.logger.info(f'relays: undo to {relay.inbox_url} failed: {type(error).__name__}')
    db.session.delete(relay)
    db.session.commit()

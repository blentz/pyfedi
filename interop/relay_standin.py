"""Harness only: a stand-in for Relay by Loops (joinloops/relay, d6c013cbd) on https://relay.test (spec
2026-10-30 §3.5, ruling L4). The published relay cannot run: its inbox job (App\\Jobs\\ProcessInboxActivity), its
AuthController and its migrations are not in the repository. This speaks the JSON the published RelayActor builds
(app/Services/RelayActor.php: the Application actor, Accept wrapping the Follow, Announce of the bare object URI):

- a Follow is accepted at once (the real relay is invite-only and leaves it pending for its admin) and its actor
  becomes a subscriber;
- an Undo of a Follow drops the subscriber;
- a public Create from a subscriber's host is announced, as its object's id, to every other subscriber.

Incoming signatures are not verified: every sender is a harness peer. State is in memory; a restart forgets the
subscribers. Run in the PieFed image (it has cryptography and httpx), behind Caddy."""
import base64
import email.utils
import hashlib
import json
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

DOMAIN = 'relay.test'
ACTOR = f'https://{DOMAIN}/actor'
PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.public_key().public_bytes(serialization.Encoding.PEM,
                                    serialization.PublicFormat.SubjectPublicKeyInfo).decode()
subscribers = {}   # follower actor id -> its delivery inbox
announced = {}     # announce uuid -> object uri
http = httpx.Client(timeout=15, follow_redirects=False)


def actor_document():
    return {'@context': ['https://www.w3.org/ns/activitystreams', 'https://w3id.org/security/v1'],
            'id': ACTOR, 'type': 'Application', 'preferredUsername': 'relay', 'name': 'Loops Relay',
            'summary': 'Harness stand-in for Relay by Loops.', 'inbox': ACTOR + '/inbox', 'outbox': ACTOR + '/outbox',
            'followers': ACTOR + '/followers', 'following': ACTOR + '/following', 'url': ACTOR,
            'endpoints': {'sharedInbox': f'https://{DOMAIN}/inbox'},
            'publicKey': {'id': ACTOR + '#main-key', 'owner': ACTOR, 'publicKeyPem': PEM}}


def announce(object_uri, announce_uuid):
    return {'@context': 'https://www.w3.org/ns/activitystreams', 'id': f'{ACTOR}/activities/{announce_uuid}',
            'type': 'Announce', 'actor': ACTOR,
            'published': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'to': [PUBLIC], 'cc': [ACTOR + '/followers'], 'object': object_uri}


def signed_post(inbox, activity):
    body = json.dumps(activity).encode()
    parts = urlsplit(inbox)
    date = email.utils.formatdate(usegmt=True)
    digest = 'SHA-256=' + base64.b64encode(hashlib.sha256(body).digest()).decode()
    signed = f'(request-target): post {parts.path}\nhost: {parts.netloc}\ndate: {date}\ndigest: {digest}'
    signature = base64.b64encode(KEY.sign(signed.encode(), padding.PKCS1v15(), hashes.SHA256())).decode()
    headers = {'Host': parts.netloc, 'Date': date, 'Digest': digest, 'Content-Type': 'application/activity+json',
               'Accept': 'application/activity+json', 'User-Agent': 'loops-relay-standin',
               'Signature': f'keyId="{ACTOR}#main-key",algorithm="rsa-sha256",'
                            f'headers="(request-target) host date digest",signature="{signature}"'}
    try:
        response = http.post(inbox, content=body, headers=headers)
        print(f'relay: {activity["type"]} -> {inbox} {response.status_code}', flush=True)
    except httpx.HTTPError as error:
        print(f'relay: {activity["type"]} -> {inbox} failed: {error}', flush=True)


def delivery_inbox(actor_id):
    document = http.get(actor_id, headers={'Accept': 'application/activity+json'}).json()
    return (document.get('endpoints') or {}).get('sharedInbox') or document['inbox']


def addressed_publicly(activity):
    obj = activity.get('object') if isinstance(activity.get('object'), dict) else {}
    audience = [*(activity.get('to') or []), *(activity.get('cc') or []), *(obj.get('to') or [])]
    return PUBLIC in audience


def handle(activity):
    kind, actor = activity.get('type'), activity.get('actor')
    if kind == 'Follow' and isinstance(actor, str):
        subscribers[actor] = delivery_inbox(actor)
        signed_post(subscribers[actor], {'@context': 'https://www.w3.org/ns/activitystreams',
                                         'id': f'{ACTOR}/activities/{uuid.uuid4()}', 'type': 'Accept',
                                         'actor': ACTOR, 'object': activity})
    elif kind == 'Undo' and isinstance(activity.get('object'), dict) and activity['object'].get('type') == 'Follow':
        subscribers.pop(actor, None)
    elif kind == 'Create' and addressed_publicly(activity):
        if not any(urlsplit(s).netloc == urlsplit(actor or '').netloc for s in subscribers):
            return   # only subscribed servers may post through the relay
        obj = activity.get('object')
        object_uri = obj.get('id') if isinstance(obj, dict) else obj
        announce_uuid = str(uuid.uuid4())
        announced[announce_uuid] = object_uri
        sender_host = urlsplit(actor).netloc
        for subscriber, inbox in list(subscribers.items()):
            if urlsplit(subscriber).netloc != sender_host:
                signed_post(inbox, announce(object_uri, announce_uuid))


class Handler(BaseHTTPRequestHandler):
    def _json(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/activity+json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split('?')[0]
        if path == '/actor':
            return self._json(200, actor_document())
        if path in ('/actor/followers', '/actor/following', '/actor/outbox'):
            return self._json(200, {'@context': 'https://www.w3.org/ns/activitystreams', 'id': ACTOR + path[6:],
                                    'type': 'OrderedCollection', 'totalItems': len(subscribers), 'orderedItems': []})
        if path.startswith('/actor/activities/') and path[18:] in announced:
            return self._json(200, announce(announced[path[18:]], path[18:]))
        if path == '/.well-known/nodeinfo':
            return self._json(200, {'links': [{'rel': 'http://nodeinfo.diaspora.software/ns/schema/2.0',
                                               'href': f'https://{DOMAIN}/nodeinfo/2.0'}]})
        if path == '/nodeinfo/2.0':
            return self._json(200, {'version': '2.0', 'software': {'name': 'loops-relay', 'version': '1.0.0'},
                                    'protocols': ['activitypub'], 'services': {'inbound': [], 'outbound': []},
                                    'openRegistrations': False, 'usage': {'users': {'total': 1}}, 'metadata': {}})
        if path == '/subscribers':   # harness inspection only
            return self._json(200, subscribers)
        return self._json(404, {'error': 'not found'})

    def do_POST(self):
        if self.path not in ('/inbox', '/actor/inbox'):
            return self._json(404, {'error': 'not found'})
        try:
            activity = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
        except ValueError:
            return self._json(400, {'error': 'bad json'})
        if not isinstance(activity, dict) or 'type' not in activity or 'actor' not in activity:
            return self._json(400, {'error': 'bad activity'})
        self._json(202, {})
        print(f'relay: got {activity["type"]} from {activity["actor"]}', flush=True)
        handle(activity)

    def log_message(self, *args):
        pass


if __name__ == '__main__':
    ThreadingHTTPServer(('0.0.0.0', 8080), Handler).serve_forever()

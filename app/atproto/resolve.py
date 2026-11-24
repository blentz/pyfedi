"""Identity: a handle to its DID, a DID to its document, a document to its PDS and handle (spec §4)."""
from urllib.parse import urlencode

from flask import current_app
from flask_babel import _

from app.atproto.names import HANDLE  # noqa: F401 (re-exported)
from app.connections import Permanent, ServiceError
from app.connections.http import call

DEFAULT_SERVER = 'https://bsky.social'
DOCUMENT_BYTES = 64_000     # an identity document from a server someone named: small, or refused


def json_of(url: str) -> dict:
    try:
        return call('GET', url, max_bytes=DOCUMENT_BYTES, headers={'Accept': 'application/json'}).json()
    except ValueError:
        raise Permanent(_('%(url)s did not answer with JSON.', url=url)) from None


def resolve_handle(handle: str) -> str | None:
    """The DID a handle names: its domain's well-known answer, else the resolver (which also reads DNS)."""
    try:
        answer = call('GET', f'https://{handle}/.well-known/atproto-did', max_bytes=DOCUMENT_BYTES).text.strip()
        if answer.startswith('did:'):
            return answer
    except ServiceError:
        pass
    resolver = current_app.config.get('BLUESKY_RESOLVER') or DEFAULT_SERVER
    try:
        did = json_of(f'{resolver}/xrpc/com.atproto.identity.resolveHandle?{urlencode({"handle": handle})}').get('did')
    except ServiceError:
        return None
    return did if isinstance(did, str) and did.startswith('did:') else None


def did_document(did: str) -> dict:
    if did.startswith('did:plc:'):
        plc = current_app.config.get('BLUESKY_PLC_URL') or 'https://plc.directory'
        return json_of(f'{plc}/{did}')
    if did.startswith('did:web:'):
        return json_of(f'https://{did.removeprefix("did:web:")}/.well-known/did.json')
    raise Permanent(_('Unsupported account identifier %(did)s.', did=did))


def pds_of(document: dict) -> str:
    for service in document.get('service') or []:
        if isinstance(service, dict) and str(service.get('id', '')).endswith('#atproto_pds'):
            endpoint = service.get('serviceEndpoint')
            if isinstance(endpoint, str) and endpoint.startswith('https://'):
                return endpoint.rstrip('/')
    raise Permanent(_('That account names no Bluesky server.'))


def handle_of(document: dict) -> str | None:
    for alias in document.get('alsoKnownAs') or []:
        if isinstance(alias, str) and alias.startswith('at://'):
            return alias.removeprefix('at://')
    return None

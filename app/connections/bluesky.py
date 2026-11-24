"""Bluesky (spec 2026-11-13-connected-accounts-design.md §2, §6, §13 P0; ruling C5: atproto OAuth only).

PieFed is a confidential atproto OAuth client: its client id is its metadata document on the main domain, it signs a
client assertion with BLUESKY_OAUTH_KEY (an ES256 JWK), and every token is DPoP-bound to a key made for that one
connection. The flow the P0 spike ran end to end (interop/spike_bluesky_oauth.py): handle or server -> DID -> PDS ->
its authorization server, PAR, the member signs in there, the code for tokens; then createRecord / deleteRecord on
the PDS with the access token.

This is the OAuth variant of a Bluesky connection (ATProto spec 2026-11-23 §4): OAuthSession is its app.atproto
Session, and what a post becomes is app.atproto.records', by its community (§6, A5): a microblog post goes whole and
is ticked on the post form; a post elsewhere goes as a card linking back, ticked only when the member asks.
"""
import hashlib
import json
import secrets
import time
from base64 import urlsafe_b64encode
from functools import lru_cache
from urllib.parse import urlencode, urlsplit

import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from flask import current_app
from flask_babel import _, lazy_gettext as _l
from jwt.algorithms import ECAlgorithm

from app import storefront
from app.atproto import Session, records
from app.atproto.resolve import DEFAULT_SERVER, DOCUMENT_BYTES, HANDLE, did_document, handle_of, json_of, \
    pds_of, resolve_handle
from app.connections import Account, AuthFailed, Permanent, Progress, Sent, Service, ServiceError, register
from app.connections import bluesky_hosted as hosted
from app.connections.apps import context_domain, current
from app.connections.http import check, fetch

SCOPE = 'atproto repo:app.bsky.feed.post?action=create&action=delete blob:*/*'
VIDEO_CHUNK = 1 << 20
UPLOAD_SECONDS = 120        # a PDS call may carry a picture up
_nonces: dict[str, str] = {}   # the last DPoP nonce each server gave: a stale one costs one retry


# --------------------------------------------------------------------------
# keys and JWTs
# --------------------------------------------------------------------------

@lru_cache(maxsize=4)
def _client_key(serialized: str):
    """BLUESKY_OAUTH_KEY as a private key, or None when it is not an EC P-256 private JWK."""
    try:
        key = ECAlgorithm.from_jwk(serialized)
    except Exception:
        return None
    return key if hasattr(key, 'private_numbers') and key.curve.name == 'secp256r1' else None


def client_key():
    app = current('bluesky')
    return _client_key(app.get('key') or '') if app is not None else None


def public_jwk(key, kid: str | None = None) -> dict:
    jwk = json.loads(ECAlgorithm.to_jwk(key.public_key()))
    jwk.update({'use': 'sig', 'alg': 'ES256'})
    if kid:
        jwk['kid'] = kid
    return jwk


def key_id() -> str:
    """The client key's id: its public key's thumbprint, so a new key is a new id."""
    return _thumbprint(public_jwk(client_key()))


def key_id_of(serialized: str) -> str:
    """The thumbprint of a serialized private key (a domain's own Bluesky key, spec 18)."""
    return _thumbprint(public_jwk(ECAlgorithm.from_jwk(serialized)))


def _thumbprint(jwk: dict) -> str:
    canonical = json.dumps({name: jwk[name] for name in ('crv', 'kty', 'x', 'y')}, separators=(',', ':'))
    return urlsafe_b64encode(hashlib.sha256(canonical.encode()).digest()).rstrip(b'=').decode()


def new_key_jwk() -> str:
    """A new private key, as BLUESKY_OAUTH_KEY takes it (`flask connections-bluesky-key`, a connection's DPoP key)."""
    return ECAlgorithm.to_jwk(ec.generate_private_key(ec.SECP256R1()))


def base_url() -> str:
    """The context domain's address: each domain is its own Bluesky client (spec 18, C9)."""
    return storefront.domain_base_url(storefront.domain_by_id(context_domain()))


def client_id() -> str:
    return f'{base_url()}/connections/bluesky/client-metadata.json'


def redirect_uri() -> str:
    return f'{base_url()}/user/settings/connections/bluesky/callback'


def client_metadata() -> dict:
    return {
        'client_id': client_id(),
        'client_name': storefront.domain_by_id(context_domain()).domain,
        'client_uri': base_url(),
        'application_type': 'web',
        'grant_types': ['authorization_code', 'refresh_token'],
        'response_types': ['code'],
        'redirect_uris': [redirect_uri()],
        'scope': SCOPE,
        'dpop_bound_access_tokens': True,
        'token_endpoint_auth_method': 'private_key_jwt',
        'token_endpoint_auth_signing_alg': 'ES256',
        'jwks_uri': f'{base_url()}/connections/bluesky/jwks.json',
    }


def jwks() -> dict:
    return {'keys': [public_jwk(client_key(), key_id())]}


def _b64(data: bytes) -> str:
    return urlsafe_b64encode(data).rstrip(b'=').decode()


def client_assertion(issuer: str) -> str:
    now = int(time.time())
    return jwt.encode({'iss': client_id(), 'sub': client_id(), 'aud': issuer, 'jti': secrets.token_urlsafe(16),
                       'iat': now, 'exp': now + 60}, client_key(), algorithm='ES256', headers={'kid': key_id()})


def dpop_proof(dpop_jwk: str, method: str, url: str, nonce: str | None, access_token: str | None = None) -> str:
    key = ECAlgorithm.from_jwk(dpop_jwk)
    claims = {'jti': secrets.token_urlsafe(16), 'htm': method, 'htu': url.split('?')[0].split('#')[0],
              'iat': int(time.time())}
    if nonce:
        claims['nonce'] = nonce
    if access_token:
        claims['ath'] = _b64(hashlib.sha256(access_token.encode()).digest())
    public = json.loads(ECAlgorithm.to_jwk(key.public_key()))
    return jwt.encode(claims, key, algorithm='ES256', headers={'typ': 'dpop+jwt', 'jwk': public})


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f'{parts.scheme}://{parts.netloc}'


# --------------------------------------------------------------------------
# identity: handle -> DID -> PDS -> authorization server
# --------------------------------------------------------------------------

def authorization_server(server: str) -> tuple[str, dict]:
    """The issuer and metadata of the authorization server for a PDS, or for an entryway named directly."""
    try:
        issuer = json_of(f'{server}/.well-known/oauth-protected-resource')['authorization_servers'][0]
    except (ServiceError, KeyError, IndexError, TypeError):
        issuer = server
    metadata = json_of(f'{issuer}/.well-known/oauth-authorization-server')
    if metadata.get('issuer') != issuer or not metadata.get('pushed_authorization_request_endpoint'):
        raise Permanent(_('%(server)s is not a Bluesky sign-in server.', server=issuer))
    return issuer, metadata


# --------------------------------------------------------------------------
# OAuth requests (PAR, token, revoke) and XRPC calls, each with its DPoP nonce dance
# --------------------------------------------------------------------------

def _auth_post(url: str, issuer: str, dpop_jwk: str, form: dict):
    for _attempt in range(2):
        fields = dict(form, client_id=client_id(),
                    client_assertion_type='urn:ietf:params:oauth:client-assertion-type:jwt-bearer',
                    client_assertion=client_assertion(issuer))
        response = fetch('POST', url, max_bytes=DOCUMENT_BYTES, data=fields, headers={
            'DPoP': dpop_proof(dpop_jwk, 'POST', url, _nonces.get(_origin(url)))})
        if 'DPoP-Nonce' in response.headers:
            _nonces[_origin(url)] = response.headers['DPoP-Nonce']
        error = _error_code(response)
        if response.status_code == 400 and error == 'use_dpop_nonce':
            continue
        if response.status_code == 400 and error == 'invalid_grant':
            raise AuthFailed(_('Bluesky ended this connection (%(error)s).', error=error))
        return check(response)
    return check(response)


def _error_code(response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    return body.get('error') if isinstance(body, dict) else None


def xrpc(tokens: dict, method: str, nsid: str, **kwargs):
    url = f"{tokens['pds']}/xrpc/{nsid}"
    access = tokens['access_token']
    headers = kwargs.pop('headers', {})
    seconds = kwargs.pop('seconds', UPLOAD_SECONDS)
    stream = kwargs.pop('stream', None)   # a file sent as the body, from its start on each try
    for _attempt in range(2):
        if stream is not None:
            stream.seek(0)
            kwargs['content'] = iter(lambda: stream.read(VIDEO_CHUNK), b'')
        proof = dpop_proof(tokens['dpop_jwk'], method, url, _nonces.get(_origin(url)), access)
        response = fetch(method, url, seconds=seconds, headers={
            **headers, 'Authorization': f'DPoP {access}', 'DPoP': proof}, **kwargs)
        if 'DPoP-Nonce' in response.headers:
            _nonces[_origin(url)] = response.headers['DPoP-Nonce']
        if response.status_code == 401 and 'use_dpop_nonce' in response.headers.get('WWW-Authenticate', ''):
            continue
        return check(response)
    return check(response)


class OAuthSession(Session):
    """An OAuth connection's tokens as an app.atproto Session: each call DPoP-bound (xrpc)."""

    def __init__(self, tokens: dict):
        self.tokens = tokens
        self.did = tokens['did']
        self.pds = tokens['pds']

    def xrpc(self, method: str, nsid: str, **kwargs):
        return xrpc(self.tokens, method, nsid, **kwargs)


# --------------------------------------------------------------------------
# the service
# --------------------------------------------------------------------------

class BlueskyService(Service):
    name = 'bluesky'
    label = 'Bluesky'
    takes_nsfw = True     # with self-labels (spec §5)
    can_edit = False      # posts cannot be edited (spec §2)
    hint_label = _l('Your handle or server, for example alice.bsky.social')
    default_on = False    # a post outside the microblogs is ticked only when the member asks (ATProto spec A5)
    ticks_microblog = True   # a microblog post is always ticked (A5)

    def configured(self) -> bool:
        """Members may connect their own accounts (the OAuth key is set), or have one made here (the PDS is)."""
        return client_key() is not None or hosted.configured()

    def kind_of(self, variant):
        return 'bluesky_hosted' if variant == hosted.HOSTED else self.name   # a hosted account needs no app

    def sendable(self, connection) -> bool:
        return connection.variant != hosted.HOSTED or connection.slot_state == hosted.ACTIVE

    def fits(self, post) -> bool:
        return True

    def authorize_url(self, variant, state, redirect_to, pending, hint=''):
        if client_key() is None:   # only accounts made here are offered
            raise Permanent(_('Connecting an existing Bluesky account is not set up here.'))
        hint = (hint or '').strip().lstrip('@').lower()
        did = login_hint = None
        if hint.startswith('did:'):
            did = login_hint = hint
        elif hint and HANDLE.fullmatch(hint):
            did = resolve_handle(hint)
            login_hint = hint if did else None
        if did:
            server = pds_of(did_document(did))
        elif hint.startswith('https://'):
            server = _origin(hint)
        elif hint and HANDLE.fullmatch(hint):
            server = f'https://{hint}'   # not a handle anyone answers for: maybe a server's name
        elif hint:
            raise Permanent(_('That is neither a handle nor a server.'))
        else:
            server = current_app.config.get('BLUESKY_DEFAULT_SERVER') or DEFAULT_SERVER
        issuer, metadata = authorization_server(server)
        dpop_jwk = new_key_jwk()
        verifier = secrets.token_urlsafe(48)
        form = {'response_type': 'code', 'redirect_uri': redirect_uri(), 'scope': SCOPE, 'state': state,
                'code_challenge': _b64(hashlib.sha256(verifier.encode()).digest()), 'code_challenge_method': 'S256'}
        if login_hint:
            form['login_hint'] = login_hint
        answer = _auth_post(metadata['pushed_authorization_request_endpoint'], issuer, dpop_jwk, form).json()
        pending.update({'issuer': issuer, 'did': did, 'verifier': verifier, 'dpop_jwk': dpop_jwk,
                        'token_endpoint': metadata['token_endpoint'],
                        'revocation_endpoint': metadata.get('revocation_endpoint')})
        return f"{metadata['authorization_endpoint']}?{urlencode({'client_id': client_id(), 'request_uri': answer['request_uri']})}"

    def finish(self, variant, args, redirect_to, pending):
        if args.get('iss') != pending['issuer']:
            raise Permanent(_('That answer came from the wrong sign-in server.'))
        answer = _auth_post(pending['token_endpoint'], pending['issuer'], pending['dpop_jwk'], {
            'grant_type': 'authorization_code', 'code': args.get('code', ''), 'redirect_uri': redirect_uri(),
            'code_verifier': pending['verifier']}).json()
        sub = answer.get('sub')
        if answer.get('token_type') != 'DPoP' or 'atproto' not in str(answer.get('scope', '')).split() or \
                not isinstance(sub, str) or not sub.startswith('did:'):
            raise Permanent(_('Bluesky gave tokens PieFed cannot use.'))
        if pending.get('did') and sub != pending['did']:
            raise Permanent(_('You signed in as a different account than the one you named.'))
        document = did_document(sub)
        pds = pds_of(document)
        if authorization_server(pds)[0] != pending['issuer']:   # a server-started sign-in: is it that account's?
            raise Permanent(_("That sign-in server does not hold this account."))
        tokens = {'access_token': answer['access_token'], 'refresh_token': answer.get('refresh_token'),
                  'expires_at': time.time() + int(answer.get('expires_in') or 300), 'dpop_jwk': pending['dpop_jwk'],
                  'pds': pds, 'issuer': pending['issuer'], 'token_endpoint': pending['token_endpoint'],
                  'revocation_endpoint': pending.get('revocation_endpoint'), 'did': sub}
        return [Account(remote_id=sub, remote_name=handle_of(document) or sub, tokens=tokens, variant='oauth')]

    def refresh(self, tokens):
        if tokens.get('kind') == hosted.HOSTED:
            return hosted.refresh(tokens)
        if not tokens.get('refresh_token'):
            raise AuthFailed(_('This connection has expired.'))
        answer = _auth_post(tokens['token_endpoint'], tokens['issuer'], tokens['dpop_jwk'], {
            'grant_type': 'refresh_token', 'refresh_token': tokens['refresh_token']}).json()
        if answer.get('sub') != tokens['did']:
            raise AuthFailed(_('Bluesky refreshed a different account.'))
        return {**tokens, 'access_token': answer['access_token'],
                'refresh_token': answer.get('refresh_token') or tokens['refresh_token'],
                'expires_at': time.time() + int(answer.get('expires_in') or 300)}

    def send(self, post, tokens, connection, progress=None):
        progress = progress or Progress()
        if tokens.get('kind') == hosted.HOSTED:   # its turn in the PDS-wide budget, just before the record (§9)
            kind = 'backfill' if progress.data.get('backfill') else 'post'
            uri = records.send_copy(post, hosted.HostedSession(tokens), progress, lambda: hosted.spend(kind))
        else:
            uri = records.send_copy(post, OAuthSession(tokens), progress)
        return Sent(remote_id=uri, remote_url=records.web_url(uri))

    def delete(self, tokens, remote_id):
        if tokens.get('kind') == hosted.HOSTED:
            hosted.spend('delete')
            records.delete_record(hosted.HostedSession(tokens), remote_id)
        else:
            records.delete_record(OAuthSession(tokens), remote_id)

    def revoke(self, tokens):   # a hosted account is deactivated or deleted instead (bluesky_hosted)
        if tokens.get('revocation_endpoint') and tokens.get('refresh_token'):
            _auth_post(tokens['revocation_endpoint'], tokens['issuer'], tokens['dpop_jwk'], {
                'token': tokens['refresh_token'], 'token_type_hint': 'refresh_token'})


SERVICE = register(BlueskyService())

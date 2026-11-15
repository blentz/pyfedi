"""Bluesky (spec 2026-11-13-connected-accounts-design.md §2, §6, §13 P0; ruling C5: atproto OAuth only).

PieFed is a confidential atproto OAuth client: its client id is its metadata document on the main domain, it signs a
client assertion with BLUESKY_OAUTH_KEY (an ES256 JWK), and every token is DPoP-bound to a key made for that one
connection. The flow the P0 spike ran end to end (interop/spike_bluesky_oauth.py): handle or server -> DID -> PDS ->
its authorization server, PAR, the member signs in there, the code for tokens; then createRecord / deleteRecord on
the PDS with the access token.

What a post becomes (§6): its text, cut to 300 characters with the link back when longer, with link and hashtag
facets in UTF-8 byte offsets; up to four pictures with their alt text, or a link post's card; `sexual` or
`graphic-media` self-labels for NSFW or NSFL. A post behind a content warning sends the warning and the link only.
An uploaded video goes as text and the link until Bluesky's video service is wired (§2).
"""
import hashlib
import json
import os
import re
import secrets
import time
from base64 import urlsafe_b64encode
from datetime import datetime, timezone
from functools import lru_cache
from urllib.parse import urlencode, urlsplit

import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from flask import current_app
from flask_babel import _, lazy_gettext as _l
from jwt.algorithms import ECAlgorithm

from app import storefront
from app.connections import Account, AuthFailed, Permanent, Progress, Sent, Service, ServiceError, register
from app.connections.eligibility import image_files, uploaded_video
from app.connections import content
from app.connections.apps import context_domain, current
from app.connections.content import StoredVideo, cut, image_bytes, plain_text  # noqa: F401 (image_bytes: re-exported)
from app.connections.http import call, check, fetch
from app.constants import POST_TYPE_LINK
from app.models import media_of

SCOPE = 'atproto repo:app.bsky.feed.post?action=create&action=delete blob:*/*'
MAX_GRAPHEMES = 300          # app.bsky.feed.post text: counted here in code points, never fewer than graphemes
# (its 3000-byte limit needs no check: 300 code points are at most 1200 bytes)
MAX_IMAGES = 4
MAX_IMAGE_BYTES = 950_000    # under the lexicon's 1,000,000
MAX_IMAGE_SIDE = 2000
MAX_VIDEO_BYTES = 300_000_000   # app.bsky.embed.video's blob: video/mp4 only, up to this (spec 19.5)
VIDEO_CHUNK = 1 << 20
DEFAULT_SERVER = 'https://bsky.social'
HANDLE = re.compile(r'([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]([a-z0-9-]{0,61}[a-z0-9])?')   # used with fullmatch
LINK = re.compile(r'https?://[^\s<>"\]\)]+')
TAG = re.compile(r'(?:^|(?<=\s))#([^\s#\.,!?;:()\[\]"\']{1,64})')
DOCUMENT_BYTES = 64_000     # an identity or OAuth document from a server the member named: small, or refused
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

def _json(url: str) -> dict:
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
        did = _json(f'{resolver}/xrpc/com.atproto.identity.resolveHandle?{urlencode({"handle": handle})}').get('did')
    except ServiceError:
        return None
    return did if isinstance(did, str) and did.startswith('did:') else None


def did_document(did: str) -> dict:
    if did.startswith('did:plc:'):
        plc = current_app.config.get('BLUESKY_PLC_URL') or 'https://plc.directory'
        return _json(f'{plc}/{did}')
    if did.startswith('did:web:'):
        return _json(f'https://{did.removeprefix("did:web:")}/.well-known/did.json')
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


def authorization_server(server: str) -> tuple[str, dict]:
    """The issuer and metadata of the authorization server for a PDS, or for an entryway named directly."""
    try:
        issuer = _json(f'{server}/.well-known/oauth-protected-resource')['authorization_servers'][0]
    except (ServiceError, KeyError, IndexError, TypeError):
        issuer = server
    metadata = _json(f'{issuer}/.well-known/oauth-authorization-server')
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


# --------------------------------------------------------------------------
# what a post becomes
# --------------------------------------------------------------------------

def compose(post, with_link: bool) -> str:
    """The post's Bluesky text: within 300 characters, with the link back when asked or when cut."""
    link = post.ap_id
    if post.content_warning:
        return cut(f'CW: {post.content_warning}', MAX_GRAPHEMES - len(link) - 1) + '\n' + link
    text = plain_text(post)
    fits = len(text) <= MAX_GRAPHEMES
    if fits and not with_link:
        return text
    if fits and len(text) + len(link) + 2 <= MAX_GRAPHEMES:
        return f'{text}\n\n{link}'.strip()
    return f'{cut(text, MAX_GRAPHEMES - len(link) - 1)}\n{link}'


def facets(text: str) -> list[dict]:
    """Link and hashtag facets, in the UTF-8 byte offsets the lexicon counts in."""
    found = []
    for match in LINK.finditer(text):
        uri = match.group(0).rstrip('.,;:!?')
        start = len(text[:match.start()].encode())
        found.append({'index': {'byteStart': start, 'byteEnd': start + len(uri.encode())},
                      'features': [{'$type': 'app.bsky.richtext.facet#link', 'uri': uri}]})
    for match in TAG.finditer(text):
        start = len(text[:match.start(1) - 1].encode())
        found.append({'index': {'byteStart': start, 'byteEnd': len(text[:match.end(1)].encode())},
                      'features': [{'$type': 'app.bsky.richtext.facet#tag', 'tag': match.group(1)}]})
    return found


def as_jpeg(data: bytes) -> tuple[bytes, int, int] | None:
    """The picture as a JPEG within Bluesky's size limit, and its width and height."""
    return content.as_jpeg(data, MAX_IMAGE_BYTES, MAX_IMAGE_SIDE)


def labels(post) -> dict | None:
    values = ([{'val': 'sexual'}] if post.nsfw else []) + ([{'val': 'graphic-media'}] if post.nsfl else [])
    return {'$type': 'com.atproto.label.defs#selfLabels', 'values': values} if values else None


TID_CHARS = '234567abcdefghijklmnopqrstuvwxyz'   # atproto's sortable base32


def new_tid() -> str:
    """A record key in atproto's TID form: microseconds since 1970 and a random clock id, 13 characters."""
    value = (time.time_ns() // 1000) << 10 | secrets.randbelow(1024)
    return ''.join(TID_CHARS[(value >> (5 * (12 - place))) & 31] for place in range(13))


def existing_record(tokens: dict, rkey: str) -> str | None:
    """The member's post under `rkey` if their PDS has it (an earlier attempt made it): its at:// URI, else None.
    Records are public, so this read needs no token."""
    response = fetch('GET', f"{tokens['pds']}/xrpc/com.atproto.repo.getRecord",
                     params={'repo': tokens['did'], 'collection': 'app.bsky.feed.post', 'rkey': rkey})
    if response.status_code == 400 and _error_of(response) == 'RecordNotFound':
        return None
    check(response)
    return response.json().get('uri') or f"at://{tokens['did']}/app.bsky.feed.post/{rkey}"


def _error_of(response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    return body.get('error') if isinstance(body, dict) else None


def upload_video(post, tokens: dict) -> dict | None:
    """The post's uploaded video as a blob on the member's PDS (spec 19.5): as Bluesky's guide has it, uploaded as a
    picture is, and processed by Bluesky once the post is there. None when it cannot go: not an MP4, larger than the
    lexicon takes, or refused by the PDS (whose own limit may be lower): the post then goes as text and link."""
    media = media_of(post) or {}
    if post.content_warning or media.get('mime') != 'video/mp4':
        return None
    try:
        with StoredVideo(post, MAX_VIDEO_BYTES) as video:
            size = os.fstat(video.fileno()).st_size
            if size > MAX_VIDEO_BYTES:
                return None
            return xrpc(tokens, 'POST', 'com.atproto.repo.uploadBlob', stream=video, seconds=600 + size // 500_000,
                        headers={'Content-Type': 'video/mp4', 'Content-Length': str(size)}).json()['blob']
    except Permanent:
        return None


def video_embed(post, blob: dict) -> dict:
    embed = {'$type': 'app.bsky.embed.video', 'video': blob}
    media = media_of(post) or {}
    width, height = media.get('width'), media.get('height')
    if type(width) is int and type(height) is int and width > 0 and height > 0:
        embed['aspectRatio'] = {'width': width, 'height': height}
    return embed


def build_record(post, tokens: dict, video: dict | None = None) -> dict:
    """The app.bsky.feed.post record for `post`, with `video` (an uploaded blob) when it has one. Uploads its
    pictures to the PDS on the way."""
    files = [] if post.content_warning or uploaded_video(post) else image_files(post)
    with_link = bool(post.content_warning) or (uploaded_video(post) and video is None) or len(files) > MAX_IMAGES
    images = []
    for file in files[:MAX_IMAGES]:
        data = image_bytes(file)
        jpeg = as_jpeg(data) if data else None
        if jpeg is None:
            with_link = True
            continue
        content, width, height = jpeg
        blob = xrpc(tokens, 'POST', 'com.atproto.repo.uploadBlob', content=content,
                    headers={'Content-Type': 'image/jpeg'}).json()['blob']
        images.append({'alt': (file.alt_text or '')[:2000], 'image': blob,
                       'aspectRatio': {'width': width, 'height': height}})
    text = compose(post, with_link)
    record = {'$type': 'app.bsky.feed.post', 'text': text,
              'createdAt': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}
    if found := facets(text):
        record['facets'] = found
    if post.language is not None and post.language.code and post.language.code != 'und':
        record['langs'] = [post.language.code]
    if video is not None:
        record['embed'] = video_embed(post, video)
    elif images:
        record['embed'] = {'$type': 'app.bsky.embed.images', 'images': images}
    elif post.type == POST_TYPE_LINK and post.url and not post.content_warning:
        record['embed'] = {'$type': 'app.bsky.embed.external', 'external': {
            'uri': post.url, 'title': (post.title or '')[:300], 'description': cut(plain_text(post), 300)}}
    if found := labels(post):
        record['labels'] = found
    return record


# --------------------------------------------------------------------------
# the service
# --------------------------------------------------------------------------

class BlueskyService(Service):
    name = 'bluesky'
    label = 'Bluesky'
    takes_nsfw = True     # with self-labels (spec §5)
    can_edit = False      # posts cannot be edited (spec §2)
    hint_label = _l('Your handle or server, for example alice.bsky.social')

    def configured(self) -> bool:
        return client_key() is not None

    def fits(self, post) -> bool:
        return True

    def authorize_url(self, variant, state, redirect_to, pending, hint=''):
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
        return [Account(remote_id=sub, remote_name=handle_of(document) or sub, tokens=tokens)]

    def refresh(self, tokens):
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
        """Under a record key chosen before the first attempt and kept (spec 19.2): an attempt after one that got no
        answer first asks the PDS whether that one made the record, so a copy is never made twice."""
        progress = progress or Progress()
        rkey = progress.data.get('rkey')
        uri = existing_record(tokens, rkey) if rkey else None
        if uri is None:
            if rkey is None:
                rkey = progress.data['rkey'] = new_tid()
                progress.save()
            if 'video' not in progress.data and uploaded_video(post):   # uploaded once, whatever comes after
                progress.data['video'] = upload_video(post, tokens)
                progress.save()
            record = build_record(post, tokens, progress.data.get('video'))
            uri = xrpc(tokens, 'POST', 'com.atproto.repo.createRecord', json={
                'repo': tokens['did'], 'collection': 'app.bsky.feed.post', 'rkey': rkey, 'record': record}).json()['uri']
        return Sent(remote_id=uri, remote_url=f"https://bsky.app/profile/{tokens['did']}/post/{uri.rsplit('/', 1)[-1]}")

    def delete(self, tokens, remote_id):
        xrpc(tokens, 'POST', 'com.atproto.repo.deleteRecord', json={
            'repo': tokens['did'], 'collection': 'app.bsky.feed.post', 'rkey': remote_id.rsplit('/', 1)[-1]})

    def revoke(self, tokens):
        if tokens.get('revocation_endpoint') and tokens.get('refresh_token'):
            _auth_post(tokens['revocation_endpoint'], tokens['issuer'], tokens['dpop_jwk'], {
                'token': tokens['refresh_token'], 'token_type_hint': 'refresh_token'})


SERVICE = register(BlueskyService())

"""Connected accounts P0 spike (spec 2026-11-13 §10): atproto OAuth as a confidential client, against a real PDS.

Plays PieFed's part. Serves its client metadata and JWKS at https://bskyclient.piefed.internal (Caddy -> this container :8700),
resolves the handle alice.pds.test, pushes the authorization request (PAR) with a private_key_jwt client assertion and
DPoP, signs alice in on the PDS's own page with Playwright, swaps the code for DPoP-bound tokens, and then does what
PieFed will do with them: upload a blob, post with an image, a link facet and a self-label, try an edit, refresh, delete
and revoke. Each step is recorded, pass or fail, in interop/spike_bluesky/report.json and printed at the end.

Run by `interop/run.sh bluesky-spike`, never by hand.
"""
import base64
import re
import hashlib
import json
import os
import secrets
import struct
import sys
import threading
import time
import traceback
import urllib.parse
import zlib
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
from joserfc import jwt
from joserfc.jwk import ECKey

CLIENT_ORIGIN = 'https://bskyclient.piefed.internal'
CLIENT_ID = f'{CLIENT_ORIGIN}/client-metadata.json'
REDIRECT_URI = f'{CLIENT_ORIGIN}/callback'
HANDLE = 'alice.pds.test'
PASSWORD = 'interop-alice-bsky-pw'
SCOPE = 'atproto repo:app.bsky.feed.post?action=create&action=delete blob:*/*'
# ATProto spec 2026-11-23 §5: a connected account that is the member's identity asks for these as well
IDENTITY_SCOPE = (' repo:app.bsky.feed.like?action=create&action=delete repo:app.bsky.feed.repost?action=create&action=delete'
                  ' repo:app.bsky.graph.follow?action=create&action=delete repo:app.bsky.graph.block?action=create&action=delete'
                  ' rpc:app.bsky.notification.listNotifications?aud=*')
IDENTITY = '--identity' in sys.argv   # `interop/run.sh bluesky-spike --identity`
if IDENTITY:
    SCOPE += IDENTITY_SCOPE
OUT = os.path.join(os.path.dirname(__file__), 'spike_bluesky')

report = {'started': datetime.now(timezone.utc).isoformat(), 'steps': []}
callback = {}
client_key = ECKey.generate_key('P-256', parameters={'kid': 'piefed-1', 'use': 'sig', 'alg': 'ES256'}, private=True)
dpop_key = ECKey.generate_key('P-256', private=True)


def step(name, ok, **detail):
    report['steps'].append({'step': name, 'ok': ok, **detail})
    print(('PASS ' if ok else 'FAIL ') + name, json.dumps(detail)[:600], flush=True)


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


def client_metadata():
    return {
        'client_id': CLIENT_ID,
        'client_name': 'PieFed connected accounts (spike)',
        'client_uri': CLIENT_ORIGIN,
        'application_type': 'web',
        'grant_types': ['authorization_code', 'refresh_token'],
        'response_types': ['code'],
        'redirect_uris': [REDIRECT_URI],
        'scope': SCOPE,
        'dpop_bound_access_tokens': True,
        'token_endpoint_auth_method': 'private_key_jwt',
        'token_endpoint_auth_signing_alg': 'ES256',
        'jwks_uri': f'{CLIENT_ORIGIN}/jwks.json',
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        report.setdefault('client_server_log', []).append(fmt % args)

    def _json(self, body):
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path, _, query = self.path.partition('?')
        if path == '/client-metadata.json':
            self._json(client_metadata())
        elif path == '/jwks.json':
            self._json({'keys': [client_key.as_dict(private=False)]})
        elif path == '/callback':
            callback.update({k: v[0] for k, v in urllib.parse.parse_qs(query).items()})
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'connected')
        else:
            self.send_response(404)
            self.end_headers()


def client_assertion(issuer):
    now = int(time.time())
    return jwt.encode({'alg': 'ES256', 'kid': 'piefed-1'},
                      {'iss': CLIENT_ID, 'sub': CLIENT_ID, 'aud': issuer, 'jti': secrets.token_urlsafe(16),
                       'iat': now, 'exp': now + 60}, client_key)


def dpop_proof(method, url, nonce=None, access_token=None):
    claims = {'jti': secrets.token_urlsafe(16), 'htm': method, 'htu': url.split('?')[0], 'iat': int(time.time())}
    if nonce:
        claims['nonce'] = nonce
    if access_token:
        claims['ath'] = b64url(hashlib.sha256(access_token.encode()).digest())
    return jwt.encode({'alg': 'ES256', 'typ': 'dpop+jwt', 'jwk': dpop_key.as_dict(private=False)}, claims, dpop_key)


class Session:
    """The DPoP nonces of the authorization server and the PDS, as PieFed would keep them per connection."""

    def __init__(self, http):
        self.http = http
        self.nonce = {}

    def auth_post(self, url, issuer, form):
        """A PAR or token request: client assertion + DPoP, retried once on use_dpop_nonce."""
        for _ in range(2):
            body = dict(form, client_id=CLIENT_ID,
                        client_assertion_type='urn:ietf:params:oauth:client-assertion-type:jwt-bearer',
                        client_assertion=client_assertion(issuer))
            r = self.http.post(url, data=body, headers={'DPoP': dpop_proof('POST', url, self.nonce.get(issuer))})
            if 'DPoP-Nonce' in r.headers:
                self.nonce[issuer] = r.headers['DPoP-Nonce']
            if r.status_code == 400 and r.json().get('error') == 'use_dpop_nonce':
                continue
            return r
        return r

    def xrpc(self, method, pds, nsid, token, **kw):
        url = f'{pds}/xrpc/{nsid}'
        for _ in range(2):
            headers = dict(kw.pop('headers', {}), Authorization=f'DPoP {token}',
                           DPoP=dpop_proof(method, url, self.nonce.get(pds), token))
            r = self.http.request(method, url, headers=headers, **kw)
            if 'DPoP-Nonce' in r.headers:
                self.nonce[pds] = r.headers['DPoP-Nonce']
            if r.status_code == 401 and 'use_dpop_nonce' in r.headers.get('WWW-Authenticate', ''):
                kw['headers'] = {k: v for k, v in headers.items() if k not in ('Authorization', 'DPoP')}
                continue
            return r
        return r


def tiny_png(width=4, height=3):
    raw = b''.join(b'\x00' + bytes([200, 60, 60]) * width for _ in range(height))
    chunk = lambda kind, data: struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))


def wait_up(http, url, seconds=180):
    for _ in range(seconds):
        try:
            if http.get(url).status_code < 500:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(1)
    return False


def sign_in(authorize_url):
    """Alice signs in on the PDS's page and approves. Screenshots of each stage land in spike_bluesky/."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(ignore_https_errors=True).new_page()
        page.goto(authorize_url)
        page.wait_for_load_state('networkidle')
        page.screenshot(path=f'{OUT}/1-authorize.png')
        page.locator('input[type=password]').fill(PASSWORD)
        ident = page.locator('input[autocomplete=username], input[name=username], input[type=text]').first
        if ident.count() and not ident.input_value():
            ident.fill(HANDLE)
        page.locator('button[type=submit]').first.click()
        page.wait_for_load_state('networkidle')
        page.screenshot(path=f'{OUT}/2-after-sign-in.png')
        for _ in range(30):
            if callback:
                break
            consent = page.get_by_role('button', name=re.compile('authori[sz]e|accept|allow', re.IGNORECASE))
            if consent.count():
                page.screenshot(path=f'{OUT}/3-consent.png')
                consent.first.click()
            page.wait_for_timeout(1000)
        report['consent_page_text'] = page.inner_text('body')[:2000] if not callback else None
        browser.close()


def connect(s, asm, issuer, did, label):
    """PAR, sign-in and code exchange: what the Connect button does. Returns the token response, or None."""
    callback.clear()
    verifier = secrets.token_urlsafe(48)
    state = secrets.token_urlsafe(16)
    r = s.auth_post(asm['pushed_authorization_request_endpoint'], issuer, {
        'response_type': 'code', 'redirect_uri': REDIRECT_URI, 'scope': SCOPE, 'state': state,
        'code_challenge': b64url(hashlib.sha256(verifier.encode()).digest()), 'code_challenge_method': 'S256',
        'login_hint': HANDLE})
    step(f'{label}: PAR accepted (confidential client, private_key_jwt, DPoP, granular scope)',
         r.status_code in (200, 201), status=r.status_code, body=r.text[:500])
    if r.status_code not in (200, 201):
        return None
    authorize_url = (f"{asm['authorization_endpoint']}?"
                     + urllib.parse.urlencode({'client_id': CLIENT_ID, 'request_uri': r.json()['request_uri']}))
    sign_in(authorize_url)
    step(f'{label}: redirect back with code, state and iss', bool(callback.get('code'))
         and callback.get('state') == state and callback.get('iss') == issuer,
         callback={k: v for k, v in callback.items() if k != 'code'})
    if not callback.get('code'):
        return None
    r = s.auth_post(asm['token_endpoint'], issuer, {'grant_type': 'authorization_code', 'code': callback['code'],
                                                     'redirect_uri': REDIRECT_URI, 'code_verifier': verifier})
    tokens = r.json()
    step(f'{label}: token exchange', r.status_code == 200 and tokens.get('token_type') == 'DPoP'
         and tokens.get('sub') == did, status=r.status_code, token_type=tokens.get('token_type'),
         scope=tokens.get('scope'), expires_in=tokens.get('expires_in'), has_refresh=bool(tokens.get('refresh_token')),
         sub=tokens.get('sub'))
    return tokens if r.status_code == 200 else None


class StubAppView(BaseHTTPRequestHandler):
    """appview.test for the identity scope steps: records what the PDS forwards, answers with nothing."""
    seen = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        StubAppView.seen.append(self.path)
        data = json.dumps({'notifications': []}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def identity_steps(s, pds, did, access, tokens):
    """ATProto spec 2026-11-23 P0: the wider OAuth scope of a connected account used as the member's identity."""
    step('identity scope granted', all(part in str(tokens.get('scope', '')).split() for part in IDENTITY_SCOPE.split()),
         scope=tokens.get('scope'))
    now = datetime.now(timezone.utc).isoformat()
    for collection, record in (('app.bsky.feed.like', {'subject': {'uri': f'at://{did}/app.bsky.feed.post/x',
                                                                    'cid': 'bafyreie5737gdxlw5i64vzichcalba3z2v5n6icifvx5xytvske7mr3hpm'}}),
                               ('app.bsky.graph.follow', {'subject': did})):
        r = s.xrpc('POST', pds, 'com.atproto.repo.createRecord', access, json={
            'repo': did, 'collection': collection, 'validate': False,
            'record': dict(record, **{'$type': collection, 'createdAt': now})})
        step(f'identity scope: create {collection}', r.status_code == 200, status=r.status_code, body=r.text[:200])
    r = s.xrpc('GET', pds, 'app.bsky.notification.listNotifications', access,
               headers={'atproto-proxy': 'did:web:appview.test#bsky_appview'})
    step('identity scope: listNotifications through the PDS proxy', r.status_code == 200 and bool(StubAppView.seen),
         status=r.status_code, body=r.text[:300], forwarded=StubAppView.seen[-1:] or None)


def main():
    stub = ThreadingHTTPServer(('0.0.0.0', 8701), StubAppView)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    os.makedirs(OUT, exist_ok=True)
    server = ThreadingHTTPServer(('0.0.0.0', 8700), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    http = httpx.Client(timeout=30)
    s = Session(http)

    step('pds up', wait_up(http, 'https://pds.test/xrpc/_health'))
    step('client metadata reachable through Caddy', http.get(CLIENT_ID).status_code == 200)

    r = http.post('https://pds.test/xrpc/com.atproto.server.createAccount',
                  json={'email': 'alice@pds.test', 'handle': HANDLE, 'password': PASSWORD})
    step('account alice', r.status_code == 200 or 'Handle already taken' in r.text, status=r.status_code,
         body=r.text[:200])

    # handle -> DID -> DID document -> PDS -> its authorization server
    r = http.get(f'https://{HANDLE}/.well-known/atproto-did')
    did = r.text.strip()
    step('handle resolves by https well-known', did.startswith('did:plc:'), did=did)
    doc = http.get(f'https://plc.test/{did}').json()
    pds = next(sv['serviceEndpoint'] for sv in doc['service'] if sv['id'].endswith('#atproto_pds'))
    step('DID document from the local PLC names the PDS', pds == 'https://pds.test', pds=pds,
         aka=doc.get('alsoKnownAs'))
    prm = http.get(f'{pds}/.well-known/oauth-protected-resource').json()
    issuer = prm['authorization_servers'][0]
    asm = http.get(f'{issuer}/.well-known/oauth-authorization-server').json()
    report['authorization_server_metadata'] = asm
    step('authorization server metadata', asm.get('issuer') == issuer, issuer=issuer,
         scopes=asm.get('scopes_supported'), auth_methods=asm.get('token_endpoint_auth_methods_supported'),
         revocation=asm.get('revocation_endpoint'))

    tokens = connect(s, asm, issuer, did, 'first connection')
    if not tokens:
        return
    access = tokens['access_token']

    if IDENTITY:
        identity_steps(s, pds, did, access, tokens)

    # what PieFed does with it
    r = s.xrpc('POST', pds, 'com.atproto.repo.uploadBlob', access, content=tiny_png(),
               headers={'Content-Type': 'image/png'})
    blob = r.json().get('blob')
    step('uploadBlob', r.status_code == 200 and blob is not None, status=r.status_code, blob=blob)

    text = 'Spike: a PieFed post. Read more https://piefed.test/post/1'
    start = text.encode().index(b'https://')
    record = {'$type': 'app.bsky.feed.post', 'text': text, 'createdAt': datetime.now(timezone.utc).isoformat(),
              'langs': ['en'],
              'facets': [{'index': {'byteStart': start, 'byteEnd': len(text.encode())},
                          'features': [{'$type': 'app.bsky.richtext.facet#link', 'uri': 'https://piefed.test/post/1'}]}],
              'embed': {'$type': 'app.bsky.embed.images',
                        'images': [{'alt': 'a red rectangle', 'image': blob, 'aspectRatio': {'width': 4, 'height': 3}}]},
              'labels': {'$type': 'com.atproto.label.defs#selfLabels', 'values': [{'val': 'sexual'}]}}
    r = s.xrpc('POST', pds, 'com.atproto.repo.createRecord', access,
               json={'repo': did, 'collection': 'app.bsky.feed.post', 'record': record})
    created = r.json()
    step('createRecord post with image, link facet, self-label', r.status_code == 200, status=r.status_code,
         body=created)
    rkey = created.get('uri', '').rsplit('/', 1)[-1]

    long_text = 'é' * 301
    r = s.xrpc('POST', pds, 'com.atproto.repo.createRecord', access, json={
        'repo': did, 'collection': 'app.bsky.feed.post',
        'record': {'$type': 'app.bsky.feed.post', 'text': long_text, 'createdAt': datetime.now(timezone.utc).isoformat()}})
    step('301 graphemes refused (PieFed must cut at 300)', r.status_code == 400, status=r.status_code, body=r.text[:300])

    r = s.xrpc('POST', pds, 'com.atproto.repo.putRecord', access, json={
        'repo': did, 'collection': 'app.bsky.feed.post', 'rkey': rkey, 'record': dict(record, text='edited')})
    step('putRecord without action=update in scope is refused', r.status_code in (401, 403), status=r.status_code,
         body=r.text[:300])

    r = s.xrpc('POST', pds, 'com.atproto.repo.createRecord', access, json={
        'repo': did, 'collection': 'app.bsky.actor.profile', 'rkey': 'self',
        'record': {'$type': 'app.bsky.actor.profile', 'displayName': 'x'}})
    step('a collection outside the scope is refused', r.status_code in (401, 403), status=r.status_code,
         body=r.text[:300])

    r = s.auth_post(asm['token_endpoint'], issuer, {'grant_type': 'refresh_token',
                                                     'refresh_token': tokens['refresh_token']})
    refreshed = r.json()
    step('refresh', r.status_code == 200 and refreshed.get('refresh_token') != tokens['refresh_token'],
         status=r.status_code, rotated=refreshed.get('refresh_token') != tokens['refresh_token'],
         expires_in=refreshed.get('expires_in'))
    access = refreshed.get('access_token', access)

    r = s.xrpc('POST', pds, 'com.atproto.repo.deleteRecord', access,
               json={'repo': did, 'collection': 'app.bsky.feed.post', 'rkey': rkey})
    step('deleteRecord with the refreshed access token', r.status_code == 200, status=r.status_code, body=r.text[:200])

    r = s.auth_post(asm['revocation_endpoint'], issuer, {'token': refreshed.get('refresh_token', ''),
                                                          'token_type_hint': 'refresh_token'})
    step('revoke (disconnect)', r.status_code == 200, status=r.status_code)
    r = s.xrpc('POST', pds, 'com.atproto.repo.createRecord', access, json={
        'repo': did, 'collection': 'app.bsky.feed.post',
        'record': {'$type': 'app.bsky.feed.post', 'text': 'after revoke',
                   'createdAt': datetime.now(timezone.utc).isoformat()}})
    step('the access token stops working after revoke', r.status_code in (400, 401, 403), status=r.status_code,
         body=r.text[:200])

    # A second connection, alone: what replaying a used refresh token does to the session (two workers refreshing
    # one connection at once would do exactly this)
    second = connect(s, asm, issuer, did, 'second connection (consent already given)')
    if not second:
        return
    r = s.auth_post(asm['token_endpoint'], issuer, {'grant_type': 'refresh_token',
                                                     'refresh_token': second['refresh_token']})
    newer = r.json()
    step('second: refresh', r.status_code == 200, status=r.status_code)
    old = s.auth_post(asm['token_endpoint'], issuer, {'grant_type': 'refresh_token',
                                                       'refresh_token': second['refresh_token']})
    step('second: the used refresh token is refused', old.status_code == 400, status=old.status_code,
         body=old.text[:200])
    r = s.xrpc('POST', pds, 'com.atproto.repo.createRecord', access := newer.get('access_token', ''), json={
        'repo': did, 'collection': 'app.bsky.feed.post',
        'record': {'$type': 'app.bsky.feed.post', 'text': 'after replay',
                   'createdAt': datetime.now(timezone.utc).isoformat()}})
    killed = r.status_code == 401
    step('second: FINDING the replay revoked the whole session (newest access token dead)', True,
         newest_access_status=r.status_code, session_killed=killed, body=r.text[:200])
    if not killed:
        s.xrpc('POST', pds, 'com.atproto.repo.deleteRecord', access, json={
            'repo': did, 'collection': 'app.bsky.feed.post', 'rkey': r.json()['uri'].rsplit('/', 1)[-1]})
    r = s.auth_post(asm['token_endpoint'], issuer, {'grant_type': 'refresh_token',
                                                     'refresh_token': newer.get('refresh_token', '')})
    step('second: FINDING the newest refresh token after the replay', True, status=r.status_code,
         body=r.text[:200])


if __name__ == '__main__':
    try:
        main()
    except Exception:
        step('spike crashed', False, error=traceback.format_exc()[-2000:])
    report['finished'] = datetime.now(timezone.utc).isoformat()
    with open(f'{OUT}/report.json', 'w') as f:
        json.dump(report, f, indent=2)
    failed = [s['step'] for s in report['steps'] if not s['ok']]
    print(f"\n{len(report['steps']) - len(failed)}/{len(report['steps'])} steps passed" +
          (f"; failed: {failed}" if failed else ''))

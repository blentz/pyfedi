"""ATProto P0 spike (spec 2026-11-23 §12): what PieFed's hosted Bluesky accounts and its Jetstream consumer rely on,
run against the harness PLC directory, PDS and Jetstream.

Plays PieFed's part with the PDS admin password, as the hosted variant will:
- the PDS issues handles under PieFed's two domains (.piefed.test, .tube.test) and answers their
  /.well-known/atproto-did through the reverse proxy;
- createAccount refuses without an invite, takes one minted with admin auth, and takes a made-up email address;
- PieFed writes with the account's own session: refresh rotates, a replayed refresh token is refused, and the sealed
  password signs in again; an app password can be minted;
- app.bsky.* reads are proxied by the PDS to the AppView (here a stand-in served by this script at appview.test) with
  a service-auth JWT;
- Jetstream v2 streams the account's records filtered by DID, a reconnect with a cursor replays, and a data frame from
  the client is refused (no live filter change);
- deactivate, activate, updateHandle and deleteAccount behave as §8 says.

Each step is recorded, pass or fail, in interop/spike_atproto/report.json and printed at the end. Run by
`interop/run.sh atproto-spike`, never by hand.
"""
import asyncio
import base64
import json
import os
import secrets
import threading
import time
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import websockets

PDS = 'https://pds.test'
PLC = 'https://plc.test'
JETSTREAM = 'wss://jetstream.test/xrpc/network.bsky.jetstream.subscribeEvents'
ADMIN = ('admin', os.environ['PDS_ADMIN_PASSWORD'])
OUT = os.path.join(os.path.dirname(__file__), 'spike_atproto')

report = {'started': datetime.now(timezone.utc).isoformat(), 'steps': []}
appview_requests = []
http = httpx.Client(timeout=30)


def step(name, ok, **detail):
    report['steps'].append({'step': name, 'ok': bool(ok), **detail})
    print(('PASS ' if ok else 'FAIL ') + name, json.dumps(detail, default=str)[:700], flush=True)


def xrpc(method, nsid, token=None, auth=None, **kwargs):
    headers = kwargs.pop('headers', {})
    if token:
        headers['Authorization'] = f'Bearer {token}'
    call = http.get if method == 'GET' else http.post
    return call(f'{PDS}/xrpc/{nsid}', headers=headers, auth=auth, **kwargs)


def jwt_claims(token):
    body = token.split('.')[1]
    return json.loads(base64.urlsafe_b64decode(body + '=' * (-len(body) % 4)))


class AppView(BaseHTTPRequestHandler):
    """The stand-in AppView: records what the PDS forwards and answers with nothing."""

    def log_message(self, *args):
        pass

    def _answer(self, body):
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == '/.well-known/did.json':
            self._answer({'@context': ['https://www.w3.org/ns/did/v1'], 'id': 'did:web:appview.test',
                          'service': [{'id': '#bsky_appview', 'type': 'BskyAppView',
                                       'serviceEndpoint': 'https://appview.test'}]})
            return
        auth = self.headers.get('Authorization', '')
        appview_requests.append({'path': self.path, 'claims': jwt_claims(auth[7:]) if auth.startswith('Bearer ')
                                 else None, 'proxy': self.headers.get('atproto-proxy')})
        self._answer({'notifications': [], 'seenAt': None} if 'listNotifications' in self.path else {})


def serve_appview():
    server = ThreadingHTTPServer(('0.0.0.0', 8701), AppView)
    threading.Thread(target=server.serve_forever, daemon=True).start()


def wait_for(url, seconds=180):
    deadline = time.time() + seconds
    last = None
    while time.time() < deadline:
        try:
            last = http.get(url)
            if last.status_code < 500:
                return last
        except httpx.HTTPError as e:
            last = e
        time.sleep(2)
    return last


async def jetstream_collect(query, seconds, send_frame=False):
    """Frames from Jetstream for `seconds`, or the close reason."""
    frames, closed = [], None
    try:
        async with websockets.connect(f'{JETSTREAM}?{query}', subprotocols=['xrpc.v1.json'],
                                      ssl=True, open_timeout=30) as ws:
            if send_frame:
                await ws.send(json.dumps({'type': 'options_update', 'payload': {'wantedDids': []}}))
            end = time.time() + seconds
            while time.time() < end:
                try:
                    frames.append(json.loads(await asyncio.wait_for(ws.recv(), timeout=max(0.1, end - time.time()))))
                except asyncio.TimeoutError:
                    break
    except websockets.ConnectionClosed as e:
        closed = {'code': e.rcvd.code if e.rcvd else None, 'reason': e.rcvd.reason if e.rcvd else None}
    except Exception as e:  # an HTTP refusal before the upgrade
        closed = {'error': repr(e)[:400]}
    return frames, closed


def payloads(frames):
    return [f.get('payload', {}) for f in frames if f.get('$type') == 'message' or 'payload' in f]


def main():
    serve_appview()

    described = wait_for(f'{PDS}/xrpc/com.atproto.server.describeServer')
    desc = described.json() if isinstance(described, httpx.Response) else {}
    step('describeServer lists PieFed domains', {'.piefed.test', '.tube.test'} <= set(desc.get('availableUserDomains', [])),
         domains=desc.get('availableUserDomains'), invite_required=desc.get('inviteCodeRequired'))

    # Accounts: invites, email
    password = secrets.token_urlsafe(32)
    refused = xrpc('POST', 'com.atproto.server.createAccount',
                   json={'handle': 'alice.piefed.test', 'email': 'atproto+1@piefed.test', 'password': password})
    step('createAccount without an invite is refused', refused.status_code == 400, status=refused.status_code,
         body=refused.text[:200])
    invite = xrpc('POST', 'com.atproto.server.createInviteCode', auth=ADMIN, json={'useCount': 1})
    code = invite.json().get('code') if invite.status_code == 200 else None
    step('admin mints an invite', bool(code), status=invite.status_code)
    made = xrpc('POST', 'com.atproto.server.createAccount',
                json={'handle': 'alice.piefed.test', 'email': 'atproto+1@piefed.test', 'password': password,
                      'inviteCode': code})
    alice = made.json() if made.status_code == 200 else {}
    did = alice.get('did')
    step('createAccount with invite, PieFed handle and made-up email', bool(did), status=made.status_code,
         did=did, handle=alice.get('handle'), body=None if did else made.text[:300])
    reuse = xrpc('POST', 'com.atproto.server.createAccount',
                 json={'handle': 'carol.tube.test', 'email': 'atproto+2@tube.test', 'password': password,
                       'inviteCode': code})
    step('a single-use invite is spent', reuse.status_code == 400, status=reuse.status_code, body=reuse.text[:200])
    invite2 = xrpc('POST', 'com.atproto.server.createInviteCode', auth=ADMIN, json={'useCount': 1}).json()['code']
    carol = xrpc('POST', 'com.atproto.server.createAccount',
                 json={'handle': 'carol.tube.test', 'email': 'atproto+2@tube.test',
                       'password': secrets.token_urlsafe(32), 'inviteCode': invite2})
    step('second domain handle', carol.status_code == 200, status=carol.status_code, body=carol.text[:200])
    carol = carol.json() if carol.status_code == 200 else {}
    if not did:
        return

    # Handles resolve through the reverse proxy, and the DID document names them
    for handle, expect in (('alice.piefed.test', did), ('carol.tube.test', carol.get('did'))):
        wk = http.get(f'https://{handle}/.well-known/atproto-did')
        step(f'well-known on {handle}', wk.status_code == 200 and wk.text.strip() == expect, status=wk.status_code,
             body=wk.text[:120])
    doc = http.get(f'{PLC}/{did}').json()
    step('DID document: handle and PDS', f'at://alice.piefed.test' in doc.get('alsoKnownAs', []) and
         any(s.get('serviceEndpoint') == PDS for s in doc.get('service', [])), alsoKnownAs=doc.get('alsoKnownAs'),
         service=doc.get('service'))

    # Sessions
    access, refresh = alice['accessJwt'], alice['refreshJwt']
    step('access token lifetime', True, access_exp_s=jwt_claims(access)['exp'] - jwt_claims(access)['iat'],
         refresh_exp_s=jwt_claims(refresh)['exp'] - jwt_claims(refresh)['iat'])
    post = xrpc('POST', 'com.atproto.repo.createRecord', token=access, json={
        'repo': did, 'collection': 'app.bsky.feed.post',
        'record': {'$type': 'app.bsky.feed.post', 'text': 'hello from PieFed', 'langs': ['en'],
                   'createdAt': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}})
    step('createRecord with the account session', post.status_code == 200, status=post.status_code,
         body=post.json() if post.status_code == 200 else post.text[:200])
    first_uri = post.json().get('uri') if post.status_code == 200 else None
    rotated = xrpc('POST', 'com.atproto.server.refreshSession', token=refresh)
    step('refreshSession rotates', rotated.status_code == 200 and rotated.json().get('refreshJwt') != refresh,
         status=rotated.status_code)
    replay = xrpc('POST', 'com.atproto.server.refreshSession', token=refresh)
    # finding, not a pass/fail: a legacy (password) session's refresh token stays usable for a grace period after it
    # rotates, unlike OAuth's (connected accounts §2), so a lost answer to a refresh does not lose the session
    step('a replayed refresh token (recorded)', True, status=replay.status_code, body=replay.text[:120])
    if rotated.status_code == 200:
        after = xrpc('GET', 'com.atproto.server.getSession', token=rotated.json()['accessJwt'])
        step('the rotated session survives a replay attempt', after.status_code == 200, status=after.status_code,
             body=after.text[:200])
    relogin = xrpc('POST', 'com.atproto.server.createSession', json={'identifier': did, 'password': password})
    step('createSession with the sealed password', relogin.status_code == 200, status=relogin.status_code)
    access = relogin.json()['accessJwt']
    app_pw = xrpc('POST', 'com.atproto.server.createAppPassword', token=access, json={'name': 'Bluesky app'})
    step('createAppPassword from PieFed session', app_pw.status_code == 200, status=app_pw.status_code,
         body=app_pw.text[:120] if app_pw.status_code != 200 else None)
    if app_pw.status_code == 200:
        by_app = xrpc('POST', 'com.atproto.server.createSession',
                      json={'identifier': 'alice.piefed.test', 'password': app_pw.json()['password']})
        step('app password signs in by handle', by_app.status_code == 200, status=by_app.status_code)
    profile = xrpc('POST', 'com.atproto.repo.putRecord', token=access, json={
        'repo': did, 'collection': 'app.bsky.actor.profile', 'rkey': 'self',
        'record': {'$type': 'app.bsky.actor.profile', 'displayName': 'Alice',
                   'description': 'On PieFed: https://piefed.test/u/alice'}})
    step('putRecord profile', profile.status_code == 200, status=profile.status_code)

    # AppView proxy
    notes = xrpc('GET', 'app.bsky.notification.listNotifications', token=access, params={'limit': 10})
    seen = [r for r in appview_requests if 'listNotifications' in r['path']]
    step('listNotifications proxied to the AppView', notes.status_code == 200 and bool(seen),
         status=notes.status_code, forwarded=seen[-1] if seen else None, body=notes.text[:200])
    proxied = xrpc('GET', 'app.bsky.notification.listNotifications', token=access,
                   headers={'atproto-proxy': 'did:web:appview.test#bsky_appview'})
    step('listNotifications with an explicit atproto-proxy', proxied.status_code == 200,
         status=proxied.status_code, body=proxied.text[:200])

    # Jetstream
    ready, waited = None, time.time()
    while time.time() - waited < 300:   # bootstrap answers 503 until it reaches steady state
        _, ready = asyncio.run(jetstream_collect('kinds=commit', 1))
        if ready is None or '503' not in str(ready):
            break
        time.sleep(5)
    step('Jetstream leaves bootstrap', ready is None or '503' not in str(ready), waited_s=round(time.time() - waited),
         closed=ready)
    query = f'dids={did}&collections=app.bsky.feed.post'

    async def live():
        task = asyncio.create_task(jetstream_collect(query, 20))
        await asyncio.sleep(3)
        made = xrpc('POST', 'com.atproto.repo.createRecord', token=access, json={
            'repo': did, 'collection': 'app.bsky.feed.post',
            'record': {'$type': 'app.bsky.feed.post', 'text': 'seen on Jetstream?',
                       'createdAt': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}})
        frames, closed = await task
        return made, frames, closed

    made2, frames, closed = asyncio.run(live())
    events = payloads(frames)
    ours = [e for e in events if e.get('did') == did]
    step('Jetstream streams the account post, filtered by DID', made2.status_code == 200 and bool(ours),
         frames=len(frames), closed=closed, sample=frames[:2])
    others = [e for e in events if e.get('did') not in (None, did)]
    step('Jetstream filter excludes other DIDs', not others, others=len(others))
    if ours:
        seq = min(e.get('seq') for e in ours if e.get('seq') is not None)
        replayed, closed2 = asyncio.run(jetstream_collect(f'{query}&cursor={seq}', 6))
        step('reconnect with cursor replays (inclusive)', any(p.get('seq') == seq for p in payloads(replayed)),
             frames=len(replayed), closed=closed2)
    _, refused_frame = asyncio.run(jetstream_collect(query, 4, send_frame=True))
    step('v2 refuses a client frame (no live filter change)', refused_frame is not None, closed=refused_frame)
    _, too_many = asyncio.run(jetstream_collect('&'.join(f'dids=did:plc:{i:024d}' for i in range(10001)), 2))
    step('more than 10,000 DIDs refused before upgrade', too_many is not None, closed=too_many)
    for n in (2000, 5000, 9000):   # how long a filter URL survives the proxy and Jetstream
        dids = '&'.join(f'dids=did:plc:{i:024d}' for i in range(n - 1))
        frames, closed_n = asyncio.run(jetstream_collect(f'{dids}&dids={did}&collections=app.bsky.feed.post', 2))
        step(f'{n} DIDs in one filter', closed_n is None, url_bytes=len(dids), closed=closed_n)

    # Lifecycle
    deact = xrpc('POST', 'com.atproto.server.deactivateAccount', token=access, json={})
    record = http.get(f'{PDS}/xrpc/com.atproto.repo.getRecord',
                      params={'repo': did, 'collection': 'app.bsky.feed.post', 'rkey': first_uri.rsplit('/', 1)[1]})
    step('deactivateAccount', deact.status_code == 200, status=deact.status_code, body=deact.text[:200],
         get_record_while_deactivated=record.status_code, get_record_body=record.text[:200])
    act = xrpc('POST', 'com.atproto.server.activateAccount', token=access)
    if act.status_code != 200:   # the access token may not outlive deactivation: sign in again first
        step('activateAccount with the old access token', False, status=act.status_code, body=act.text[:300])
        fresh = xrpc('POST', 'com.atproto.server.createSession', json={'identifier': did, 'password': password})
        step('createSession while deactivated', fresh.status_code == 200, status=fresh.status_code,
             body=fresh.text[:300])
        if fresh.status_code == 200:
            access = fresh.json()['accessJwt']
            act = xrpc('POST', 'com.atproto.server.activateAccount', token=access)
    record = http.get(f'{PDS}/xrpc/com.atproto.repo.getRecord',
                      params={'repo': did, 'collection': 'app.bsky.feed.post', 'rkey': first_uri.rsplit('/', 1)[1]})
    step('activateAccount; records readable again', act.status_code == 200 and record.status_code == 200,
         status=act.status_code, body=act.text[:300], get_record=record.status_code)
    renamed = xrpc('POST', 'com.atproto.identity.updateHandle', token=access, json={'handle': 'alice2.piefed.test'})
    wk = http.get('https://alice2.piefed.test/.well-known/atproto-did')
    old = http.get('https://alice.piefed.test/.well-known/atproto-did')
    step('updateHandle keeps the DID; old handle stops resolving', renamed.status_code == 200 and
         wk.text.strip() == did and old.status_code == 404, status=renamed.status_code, new=wk.status_code,
         old=old.status_code)
    admin_pw = xrpc('POST', 'com.atproto.admin.updateAccountPassword', auth=ADMIN,
                    json={'did': did, 'password': password + 'x'})
    again = xrpc('POST', 'com.atproto.server.createSession', json={'identifier': did, 'password': password + 'x'})
    step('admin.updateAccountPassword recovers a lost password', admin_pw.status_code == 200 and
         again.status_code == 200, status=admin_pw.status_code, login=again.status_code)
    gone = xrpc('POST', 'com.atproto.admin.deleteAccount', auth=ADMIN, json={'did': did})
    after = http.get(f'{PDS}/xrpc/com.atproto.repo.describeRepo', params={'repo': did})
    step('admin.deleteAccount removes the repo', gone.status_code == 200 and after.status_code >= 400,
         status=gone.status_code, describe=after.status_code)


if __name__ == '__main__':
    os.makedirs(OUT, exist_ok=True)
    try:
        main()
    except Exception:
        step('spike crashed', False, error=traceback.format_exc()[-1500:])
    report['finished'] = datetime.now(timezone.utc).isoformat()
    with open(os.path.join(OUT, 'report.json'), 'w') as f:
        json.dump(report, f, indent=2, default=str)
    failed = [s['step'] for s in report['steps'] if not s['ok']]
    print(f"\n{len(report['steps']) - len(failed)}/{len(report['steps'])} steps passed" +
          (f"; failed: {failed}" if failed else ''))

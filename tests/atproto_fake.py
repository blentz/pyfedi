"""A stand-in Bluesky PDS for tests, on respx: accounts, sessions, invites, records, as the P0 spike found the real
one answering (interop/spike_atproto/report.json). interop/ runs the same code against a real PDS."""
import base64
import itertools
import json
import time

import httpx
import jwt

PDS = 'https://pds.example'
PLC = 'https://plc.example'
ADMIN_PASSWORD = 'pds-admin'
_numbers = itertools.count(1)
BASE32 = 'abcdefghijklmnopqrstuvwxyz234567'
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=')


def new_did() -> str:
    """A did:plc of the real shape: 24 characters of base32."""
    number, chars = next(_numbers) + 10_000, []
    for _place in range(24):
        number, digit = divmod(number, 32)
        chars.append(BASE32[digit])
    return 'did:plc:' + ''.join(reversed(chars))


class FakePDS:
    def __init__(self, router, domains: list[str], invite: bool = False):
        self.domains, self.invite, self.router = domains, invite, router
        self.accounts: dict[str, dict] = {}     # did -> handle, password, active
        self.sessions: dict[str, str] = {}      # access or refresh token -> did
        self.records: dict[str, dict] = {}      # at:// uri -> record
        self.taken: set[str] = set()            # handles another account has
        self.invites: set[str] = set()
        self.notifications: dict[str, list] = {}   # did -> its notifications, newest first
        self.servers: dict[str, str] = {}          # did -> the server its document names, when not this one (a move)
        self.preferences: dict[str, list] = {}   # did -> its app.bsky preferences
        self.calls: list[str] = []
        self.requests: list[httpx.Request] = []
        self.fail: dict[str, httpx.Response] = {}   # nsid -> the answer it gives instead, once
        router.route(host='pds.example').mock(side_effect=self._handle)
        router.route(host='plc.example').mock(side_effect=self._plc)

    # ------------------------------------------------------------------
    def account(self, handle: str, password: str = 'pw', active: bool = True) -> str:
        did = new_did()
        self.accounts[did] = {'handle': handle, 'password': password, 'active': active}
        return did

    def put(self, did: str, collection: str, rkey: str, record: dict) -> str:
        """A record in an account's repo, as if its owner wrote it elsewhere: its at:// URI."""
        uri = f'at://{did}/{collection}/{rkey}'
        self.records[uri] = record
        return uri

    def _plc(self, request: httpx.Request) -> httpx.Response:
        did = request.url.path.lstrip('/')
        if did not in self.accounts:
            return httpx.Response(404, json={'message': 'DID not registered'})
        return httpx.Response(200, json={'id': did, 'alsoKnownAs': [f"at://{self.accounts[did]['handle']}"],
                                         'service': [{'id': '#atproto_pds', 'type': 'AtprotoPersonalDataServer',
                                                      'serviceEndpoint': self.servers.get(did, PDS)}]})

    def session(self, did: str, lifetime: int = 7200) -> dict:
        number = next(_numbers)
        access = jwt.encode({'sub': did, 'exp': int(time.time()) + lifetime, 'n': number}, 'fake-pds-signing-key-of-32-bytes!', algorithm='HS256')
        refresh = f'refresh-{number}'
        self.sessions[access] = self.sessions[refresh] = did
        return {'did': did, 'handle': self.accounts[did]['handle'], 'accessJwt': access, 'refreshJwt': refresh}

    def tokens(self, did: str, lifetime: int = 7200) -> dict:
        """A hosted connection's tokens for `did`, as bluesky_hosted keeps them."""
        answer = self.session(did, lifetime)
        return {'kind': 'hosted', 'did': did, 'handle': answer['handle'], 'password': self.accounts[did]['password'],
                'access_token': answer['accessJwt'], 'refresh_token': answer['refreshJwt'],
                'expires_at': time.time() + lifetime, 'pds': PDS}

    # ------------------------------------------------------------------
    def _handle(self, request: httpx.Request) -> httpx.Response:
        nsid = request.url.path.removeprefix('/xrpc/')
        self.calls.append(nsid)
        self.requests.append(request)
        if nsid in self.fail:
            return self.fail.pop(nsid)
        body = json.loads(request.content) if request.content and request.headers.get(
            'Content-Type', '').startswith('application/json') else {}
        handler = getattr(self, '_' + nsid.replace('.', '_'), None)
        if handler is None:
            return httpx.Response(501, json={'error': 'MethodNotImplemented', 'message': nsid})
        return handler(request, body)

    def _caller(self, request) -> str | None:
        token = request.headers.get('Authorization', '').removeprefix('Bearer ')
        return self.sessions.get(token)

    def _admin(self, request) -> bool:
        expected = 'Basic ' + base64.b64encode(f'admin:{ADMIN_PASSWORD}'.encode()).decode()
        return request.headers.get('Authorization') == expected

    @staticmethod
    def _error(status, error, message=''):
        return httpx.Response(status, json={'error': error, 'message': message or error})

    def _com_atproto_server_describeServer(self, request, body):
        return httpx.Response(200, json={'did': 'did:web:pds.example', 'inviteCodeRequired': self.invite,
                                         'availableUserDomains': [f'.{domain}' for domain in self.domains]})

    def _com_atproto_server_createInviteCode(self, request, body):
        if not self._admin(request):
            return self._error(401, 'AuthMissing')
        code = f'invite-{next(_numbers)}'
        self.invites.add(code)
        return httpx.Response(200, json={'code': code})

    def _com_atproto_server_createAccount(self, request, body):
        if self.invite and body.get('inviteCode') not in self.invites:
            return self._error(400, 'InvalidInviteCode')
        self.invites.discard(body.get('inviteCode'))
        handle = body['handle']
        if handle in self.taken or any(account['handle'] == handle for account in self.accounts.values()):
            return self._error(400, 'InvalidRequest', f'Handle already taken: {handle}')   # as the real PDS
        if not any(handle.endswith(f'.{domain}') for domain in self.domains):
            return self._error(400, 'UnsupportedDomain')
        did = self.account(handle, body['password'])
        return httpx.Response(200, json=self.session(did))

    def _com_atproto_server_createSession(self, request, body):
        did = next((did for did, account in self.accounts.items()
                    if body.get('identifier') in (did, account['handle'])), None)
        if did is None or self.accounts[did]['password'] != body.get('password'):
            return self._error(401, 'AuthenticationRequired', 'Invalid identifier or password')
        return httpx.Response(200, json=self.session(did))

    def _com_atproto_server_refreshSession(self, request, body):
        did = self._caller(request)
        if did is None:
            return self._error(400, 'ExpiredToken')
        return httpx.Response(200, json=self.session(did))

    def _com_atproto_admin_updateAccountPassword(self, request, body):
        if not self._admin(request):
            return self._error(401, 'AuthMissing')
        if body['did'] not in self.accounts:
            return self._error(400, 'AccountNotFound')
        self.accounts[body['did']]['password'] = body['password']
        return httpx.Response(200)

    def _com_atproto_admin_deleteAccount(self, request, body):
        if not self._admin(request):
            return self._error(401, 'AuthMissing')
        self.accounts.pop(body['did'], None)
        return httpx.Response(200)

    def _com_atproto_server_deactivateAccount(self, request, body):
        did = self._caller(request)
        if did is None:
            return self._error(401, 'AuthMissing')
        self.accounts[did]['active'] = False
        return httpx.Response(200)

    def _com_atproto_server_activateAccount(self, request, body):
        did = self._caller(request)
        if did is None:
            return self._error(401, 'AuthMissing')
        if request.content:
            return self._error(400, 'InvalidRequest', 'A request body was provided when none was expected')
        self.accounts[did]['active'] = True
        return httpx.Response(200)

    def _com_atproto_identity_updateHandle(self, request, body):
        did = self._caller(request)
        if did is None:
            return self._error(401, 'AuthMissing')
        if body['handle'] in self.taken:
            return self._error(400, 'HandleNotAvailable')
        self.accounts[did]['handle'] = body['handle']
        return httpx.Response(200)

    def _com_atproto_server_createAppPassword(self, request, body):
        if self._caller(request) is None:
            return self._error(401, 'AuthMissing')
        return httpx.Response(200, json={'name': body['name'], 'password': 'abcd-efgh-ijkl-mnop'})

    def _app_bsky_actor_getPreferences(self, request, body):
        did = self._caller(request)
        if did is None:
            return self._error(401, 'AuthMissing')
        return httpx.Response(200, json={'preferences': self.preferences.get(did, [])})

    def _app_bsky_actor_putPreferences(self, request, body):
        did = self._caller(request)
        if did is None:
            return self._error(401, 'AuthMissing')
        self.preferences[did] = body['preferences']
        return httpx.Response(200)

    def _com_atproto_repo_uploadBlob(self, request, body):
        if self._caller(request) is None:
            return self._error(401, 'AuthMissing')
        return httpx.Response(200, json={'blob': {'$type': 'blob', 'ref': {'$link': f'bafk{next(_numbers)}'},
                                                  'mimeType': request.headers.get('Content-Type'), 'size': 9}})

    def _write(self, request, body, rkey):
        did = self._caller(request)
        if did is None or did != body.get('repo'):
            return None, self._error(401, 'AuthMissing')
        if not self.accounts[did]['active']:
            return None, self._error(400, 'AccountDeactivated')
        return f"at://{did}/{body['collection']}/{rkey}", None

    def _com_atproto_repo_createRecord(self, request, body):
        uri, refused = self._write(request, body, body.get('rkey') or f'r{next(_numbers)}')
        if refused:
            return refused
        self.records[uri] = body['record']
        return httpx.Response(200, json={'uri': uri, 'cid': 'bafyc'})

    def _com_atproto_repo_putRecord(self, request, body):
        uri, refused = self._write(request, body, body['rkey'])
        if refused:
            return refused
        self.records[uri] = body['record']
        return httpx.Response(200, json={'uri': uri, 'cid': 'bafyp'})

    def _com_atproto_repo_deleteRecord(self, request, body):
        uri, refused = self._write(request, body, body['rkey'])
        if refused:
            return refused
        self.records.pop(uri, None)
        return httpx.Response(200)

    def _com_atproto_repo_listRecords(self, request, body):
        params = request.url.params
        prefix = f"at://{params['repo']}/{params['collection']}/"
        found = [{'uri': uri, 'cid': 'bafyl', 'value': value} for uri, value in sorted(self.records.items(), reverse=True)
                 if uri.startswith(prefix)][:int(params.get('limit', 50))]
        return httpx.Response(200, json={'records': found})

    def _com_atproto_sync_getBlob(self, request, body):
        return httpx.Response(200, content=PNG, headers={'Content-Type': 'image/png'})

    def _com_atproto_identity_resolveHandle(self, request, body):
        handle = request.url.params.get('handle')
        did = next((did for did, account in self.accounts.items() if account['handle'] == handle), None)
        if did is None:
            return self._error(400, 'InvalidRequest', 'Unable to resolve handle')
        return httpx.Response(200, json={'did': did})

    def _com_atproto_repo_getRecord(self, request, body):
        params = request.url.params
        uri = f"at://{params['repo']}/{params['collection']}/{params['rkey']}"
        if uri not in self.records:
            return self._error(400, 'RecordNotFound')
        return httpx.Response(200, json={'uri': uri, 'cid': 'bafyg', 'value': self.records[uri]})

    def _app_bsky_notification_listNotifications(self, request, body):
        """The caller's notifications (`self.notifications[did]`, newest first), a page at a time: the cursor is
        where the next page starts."""
        did = self._caller(request)
        if did is None:
            return self._error(401, 'AuthMissing')
        items = self.notifications.get(did, [])
        start = int(request.url.params.get('cursor') or 0)
        end = start + int(request.url.params.get('limit', 50))
        answer = {'notifications': items[start:end]}
        if end < len(items):
            answer['cursor'] = str(end)
        return httpx.Response(200, json=answer)

"""A stand-in Bluesky PDS for tests, on respx: accounts, sessions, invites, records, as the P0 spike found the real
one answering (interop/spike_atproto/report.json). interop/ runs the same code against a real PDS."""
import base64
import itertools
import json
import time

import httpx
import jwt

PDS = 'https://pds.example'
ADMIN_PASSWORD = 'pds-admin'
_numbers = itertools.count(1)


class FakePDS:
    def __init__(self, router, domains: list[str], invite: bool = False):
        self.domains, self.invite = domains, invite
        self.accounts: dict[str, dict] = {}     # did -> handle, password, active
        self.sessions: dict[str, str] = {}      # access or refresh token -> did
        self.records: dict[str, dict] = {}      # at:// uri -> record
        self.taken: set[str] = set()            # handles another account has
        self.invites: set[str] = set()
        self.calls: list[str] = []
        self.fail: dict[str, httpx.Response] = {}   # nsid -> the answer it gives instead, once
        router.route(host='pds.example').mock(side_effect=self._handle)

    # ------------------------------------------------------------------
    def account(self, handle: str, password: str = 'pw', active: bool = True) -> str:
        did = f'did:plc:fake{next(_numbers)}'
        self.accounts[did] = {'handle': handle, 'password': password, 'active': active}
        return did

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

    def _com_atproto_repo_getRecord(self, request, body):
        params = request.url.params
        uri = f"at://{params['repo']}/{params['collection']}/{params['rkey']}"
        if uri not in self.records:
            return self._error(400, 'RecordNotFound')
        return httpx.Response(200, json={'uri': uri, 'value': self.records[uri]})

"""A connected-accounts service for tests: every call is an HTTP request to https://fake.example, which a test's
`http_mock` (respx) answers, so the real code paths run from the Service interface down to the wire.

`fake_service` registers it and turns it on (a Fernet key, the admin switch); monkeypatch undoes both."""
from datetime import datetime

import pytest

from app.connections import SERVICES, Account, Sent, Service, setting_name
from app.connections.http import call
from app.sealing import generate_key
from app.utils import set_setting

BASE = 'https://fake.example'


class FakeService(Service):
    name = 'fake'
    label = 'Fake'
    can_edit = True

    def __init__(self):
        self.fit = True
        self.has_credentials = True

    def configured(self):
        return self.has_credentials

    def fits(self, post):
        return self.fit

    def fits_type(self, post_type):
        return post_type != 99

    def can_delete(self, variant):
        return variant != 'nodelete'

    def variant_label(self, variant):
        return f'Fake ({variant})' if variant else self.label

    def authorize_url(self, variant, state, redirect_uri, pending, hint=''):
        pending['verifier'] = 'v-123'
        pending['hint'] = hint
        call('GET', f'{BASE}/ping')
        return f'{BASE}/authorize?state={state}'

    def finish(self, variant, args, redirect_uri, pending):
        body = call('POST', f'{BASE}/token', data={'code': args.get('code', ''),
                                                   'verifier': pending['verifier']}).json()
        return [Account(remote_id=entry['id'], remote_name=entry['name'], variant=variant,
                        tokens={'access': entry['token'], 'expires_at': entry.get('expires_at')})
                for entry in body['accounts']]

    def refresh(self, tokens):
        body = call('POST', f'{BASE}/refresh', data={'access': tokens['access']}).json()
        return {'access': body['token'], 'expires_at': datetime.now().timestamp() + 3600}

    def send(self, post, tokens, connection):
        body = call('POST', f'{BASE}/posts', json={'text': post.title},
                    headers={'Authorization': f"Bearer {tokens['access']}"}).json()
        return Sent(remote_id=body['id'], remote_url=body.get('url'))

    def edit(self, post, tokens, remote_id):
        call('PUT', f'{BASE}/posts/{remote_id}', json={'text': post.title})

    def delete(self, tokens, remote_id):
        call('DELETE', f'{BASE}/posts/{remote_id}')

    def revoke(self, tokens):
        call('POST', f'{BASE}/revoke')


@pytest.fixture
def fake_service(app, db_session, monkeypatch):
    service = FakeService()
    monkeypatch.setitem(app.config, 'CONNECTIONS_SECRETS_KEY', generate_key())
    monkeypatch.setitem(SERVICES, service.name, service)
    set_setting(setting_name(service.name), True)
    return service

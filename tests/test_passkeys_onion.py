"""A passkey is bound to the storefront it was made on (WebAuthn rp id), and the origin it must verify against comes
from configuration, never from the Host header (onion storefront spec §3)."""
from unittest.mock import patch

import pytest

from tests.test_passkeys import _passkey, _seed, authenticated
from tests.test_storefront_api import CANONICAL, ONION, onion  # noqa: F401  (the fixture registers abc.onion)

pytestmark = pytest.mark.usefixtures('site')

ONION_URL = f'http://{ONION}'


def test_login_options_on_the_onion_carry_the_onion_rp_id(app, db_session, onion):
    instance, alice = _seed()
    response = app.test_client().post('/auth/passkeys/login_options', json={'username': 'alice'},
                                      base_url=ONION_URL)
    assert response.get_json()['rpId'] == ONION


def test_login_options_on_the_clearnet_are_unchanged(app, db_session):
    instance, alice = _seed()
    response = app.test_client().post('/auth/passkeys/login_options', json={'username': 'alice'})
    assert response.get_json()['rpId'] == CANONICAL


def test_login_on_the_onion_expects_the_onion_origin(app, db_session, onion):
    instance, alice = _seed()
    _passkey(alice)
    with patch('app.auth.passkeys.parse_authentication_credential_json', return_value='CRED'):
        with patch('app.auth.passkeys.verify_authentication_response', return_value=authenticated()) as verify:
            app.test_client().post('/auth/passkeys/login_verification',
                                   json={'username': f'alice@{CANONICAL}', 'redirect': '/', 'response': {}},
                                   base_url=ONION_URL)
    assert verify.call_args.kwargs['expected_rp_id'] == ONION
    assert verify.call_args.kwargs['expected_origin'] == ONION_URL


def test_the_origin_is_not_taken_from_the_host_header(app, db_session, onion):
    """The Host header names a registered storefront, but its port is the client's: the origin has none."""
    instance, alice = _seed()
    _passkey(alice)
    with patch('app.auth.passkeys.parse_authentication_credential_json', return_value='CRED'):
        with patch('app.auth.passkeys.verify_authentication_response', return_value=authenticated()) as verify:
            app.test_client().post('/auth/passkeys/login_verification',
                                   json={'username': f'alice@{CANONICAL}', 'redirect': '/', 'response': {}},
                                   base_url=f'{ONION_URL}:8999')
    assert verify.call_args.kwargs['expected_origin'] == ONION_URL


def test_registration_on_the_onion_uses_the_onion_rp_id_and_origin(app, db_session, onion):
    instance, alice = _seed()
    client = app.test_client()
    with client.session_transaction(base_url=ONION_URL) as sess:
        sess['_user_id'] = str(alice.id)
        sess['_fresh'] = True
    with patch('app.user.passkeys.cache.set'):
        options = client.post('/user/passkeys/registration/options', base_url=ONION_URL)
    assert options.get_json()['rp']['id'] == ONION
    verification = type('Verification', (), {'credential_id': b'c', 'credential_public_key': b'k',
                                             'sign_count': 0})()
    with patch('app.user.passkeys.parse_registration_credential_json', return_value='CRED'):
        with patch('app.user.passkeys.verify_registration_response', return_value=verification) as verify:
            client.post('/user/passkeys/registration/verification', json={'response': {}, 'device': 'x'},
                        base_url=ONION_URL)
    assert verify.call_args.kwargs['expected_rp_id'] == ONION
    assert verify.call_args.kwargs['expected_origin'] == ONION_URL

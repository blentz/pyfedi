"""app/auth/passkeys.py and app/user/passkeys.py -- WebAuthn login and registration.

MEASUREMENT BASIS, from the full-suite --cov=app run at 342bae12b:

    app/auth/passkeys.py   18.987   64 gaps
    app/user/passkeys.py   42.857   36 gaps

Neither was referenced by any existing test.

One defect is pinned here and repaired with it:

  P1  `if not user.passkeys:` is always False. User.passkeys is
      lazy='dynamic', so the attribute is an AppenderQuery, and a query object
      is truthy whether or not it would return rows.

TWO SERIOUS SHAPES ARE REGISTERED RATHER THAN REPAIRED, both pinned here as the
behaviour they are: the cloned-authenticator check is disabled (R1) and the
options endpoint enumerates usernames (R2). Both are now fixed (D886/D887
and D888, owner ruling 2026-09-30). See the design note for why neither
is a coverage round's call.

THE WEBAUTHN LIBRARY IS MOCKED AT ITS BOUNDARY. Producing a real authenticator
assertion needs a signing key and a live credential; what these rows are about
is what this application does with the library's answer, so
verify_authentication_response and its registration twin are patched and their
ARGUMENTS asserted -- which for a security check is the behaviour, not a
detail (the campaign's mock rule, since the call IS the thing under test).
"""
import base64
import importlib
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import text
from webauthn.helpers.exceptions import (InvalidAuthenticationResponse,
                                         InvalidRegistrationResponse)

from app import cache, db
from app.models import Passkey, Site, utcnow
from tests.factories import make_instance, make_user

pytestmark = pytest.mark.usefixtures('site')


def _seed():
    instance = make_instance('test.piefed.local', software='piefed')
    burn = make_user(instance, 'burnseat', local=True)
    assert burn.id == 1
    alice = make_user(instance, 'alice', local=True)
    alice.email = 'alice@example.com'
    db.session.commit()
    return instance, alice


def _passkey(user, device='phone', counter=0, passkey_id='aGVsbG8='):
    """A stored passkey. passkey_id is base64: allowed_credentials b64-decodes
    it, so a non-base64 value raises there rather than in the code under test.
    """
    passkey = Passkey(user_id=user.id, passkey_id=passkey_id, public_key=b'rawkey',
                      device=device, counter=counter)
    db.session.add(passkey)
    db.session.commit()
    return passkey


def authenticated(new_sign_count=0):
    """What verify_authentication_response returns: the login reads
    `new_sign_count` from it and stores it (D886)."""
    return SimpleNamespace(new_sign_count=new_sign_count)


def login(client, user):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def csrf(app, client):
    """A real CSRF token for the delete route.

    NOTE the asymmetry this exists to serve: `user_passkey_delete` is a plain
    `@login_required` POST and enforces CSRF, while the two registration
    endpoints beside it are `@login_required(csrf=False)` -- R3. A row that
    forgot the token here would fail with "The CSRF token is missing." and
    read like a broken route.
    """
    from flask import session as flask_session
    from flask_wtf.csrf import generate_csrf

    with app.test_request_context():
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with client.session_transaction() as sess:
        sess['csrf_token'] = raw
    return token


# --------------------------------------------------------------------------
# P1: the guard that never guarded
# --------------------------------------------------------------------------


def test_a_user_with_no_passkeys_is_told_what_anyone_is_told(app, db_session):
    """D888 residue, fixed (owner ruling 2026-09-30). This account used to get
    'No passkeys found for alice', which told an unauthenticated caller the
    account exists; it now gets the message an unknown name and a failed
    assertion get.

    P1's history: before its repair the old message could not be produced at all:

        PROBE h1 type: AppenderQuery
        PROBE h1 count: 0
        PROBE h1 bool(user.passkeys): True

    `User.passkeys` is lazy='dynamic', so the attribute is a QUERY, and a query
    is truthy whether or not it would return rows. The `else` arm ran instead,
    looped over nothing, and reported 'No valid passkeys found' -- the same
    refusal by a different route, which is why nothing looked wrong.
    """
    instance, alice = _seed()
    client = app.test_client()

    # parse_authentication_credential_json runs BEFORE the user lookup and
    # rejects an empty object outright, so it is patched in every row here that
    # is about what happens after it.
    with patch('app.auth.passkeys.parse_authentication_credential_json',
               return_value='CRED'):
        response = client.post('/auth/passkeys/login_verification',
                               json={'username': 'alice', 'redirect': '/',
                                     'response': {}})

    assert response.get_json() == {'verified': False,
                                   'message': 'No valid passkeys found for alice'}


def test_a_user_whose_passkeys_all_fail_is_told_none_are_valid(app, db_session):
    """The other arm, and the one that keeps the repair honest: with a passkey
    present the message must still be the 'no VALID passkeys' one, so a fix
    that reported 'no passkeys' for both would fail here.
    """
    instance, alice = _seed()
    _passkey(alice)
    client = app.test_client()

    with patch('app.auth.passkeys.parse_authentication_credential_json',
               return_value='CRED'):
        with patch('app.auth.passkeys.verify_authentication_response',
                   side_effect=InvalidAuthenticationResponse('nope')):
            response = client.post('/auth/passkeys/login_verification',
                                   json={'username': 'alice', 'redirect': '/',
                                         'response': {}})

    assert response.get_json() == {'verified': False,
                                   'message': 'No valid passkeys found for alice'}


# --------------------------------------------------------------------------
# R1 (D886, fixed): the cloned-authenticator check
# --------------------------------------------------------------------------


def test_the_stored_signature_counter_is_checked_and_updated(app, db_session):
    """D886, fixed (owner ruling 2026-09-30). The login passed a hardcoded
    `credential_current_sign_count=0`, so WebAuthn's cloned-authenticator check
    accepted every count, and then did `counter += 1`. It now passes the stored
    counter, so the library refuses a count that does not exceed it (both 0 is
    allowed: an authenticator with no counter), and stores the count the
    authenticator reported.
    """
    instance, alice = _seed()
    _passkey(alice, counter=41)
    client = app.test_client()

    with patch('app.auth.passkeys.parse_authentication_credential_json',
               return_value='CRED'):
        with patch('app.auth.passkeys.verify_authentication_response',
                   return_value=authenticated(57)) as verify:
            response = client.post('/auth/passkeys/login_verification',
                                   json={'username': 'alice', 'redirect': '/', 'response': {}})

    assert response.get_json()['verified'] is True
    assert verify.call_args.kwargs['credential_current_sign_count'] == 41
    db.session.expire_all()
    assert Passkey.query.filter_by(user_id=alice.id).first().counter == 57


def test_a_regressed_signature_counter_is_refused(app, db_session):
    """D886: the library's refusal of a count that did not advance is a failed
    login like any other, and the stored counter is left alone."""
    instance, alice = _seed()
    _passkey(alice, counter=41)
    client = app.test_client()

    with patch('app.auth.passkeys.parse_authentication_credential_json',
               return_value='CRED'):
        with patch('app.auth.passkeys.verify_authentication_response',
                   side_effect=InvalidAuthenticationResponse('count 3 not greater than 41')):
            response = client.post('/auth/passkeys/login_verification',
                                   json={'username': 'alice', 'redirect': '/', 'response': {}})

    assert response.get_json()['verified'] is False
    db.session.expire_all()
    assert Passkey.query.filter_by(user_id=alice.id).first().counter == 41


def test_the_migration_resets_every_invented_counter(app, db_session):
    """D886: every stored counter was made up by `counter += 1` rather than
    reported by an authenticator, and a counter-less (synced) passkey reports 0
    forever -- checked against 41 it would be locked out. The migration sets them
    all to 0, from where the real counts are tracked."""
    instance, alice = _seed()
    _passkey(alice, counter=41)
    _passkey(alice, counter=3, passkey_id='d29ybGQ=')
    migration = importlib.import_module('migrations.versions.9c3e5a1d7b20_passkey_counter_reset')

    with patch.object(migration.op, 'execute', side_effect=lambda sql: db.session.execute(text(sql))):
        migration.upgrade()
    db.session.commit()

    assert [p.counter for p in Passkey.query.filter_by(user_id=alice.id)] == [0, 0]


def test_a_successful_login_stamps_the_passkey_as_used(app, db_session):
    """`passkey.used = utcnow()` -- what the passkey list shows the owner, and
    the only record that a credential is still in service.
    """
    instance, alice = _seed()
    passkey = _passkey(alice)
    passkey.used = utcnow().replace(year=2020)
    db.session.commit()
    client = app.test_client()

    with patch('app.auth.passkeys.parse_authentication_credential_json',
               return_value='CRED'):
        with patch('app.auth.passkeys.verify_authentication_response',
                   return_value=authenticated()):
            client.post('/auth/passkeys/login_verification',
                        json={'username': 'alice', 'redirect': '/', 'response': {}})

    db.session.expire_all()
    assert Passkey.query.filter_by(user_id=alice.id).first().used.year > 2020


# --------------------------------------------------------------------------
# R2 (D888, fixed): the options endpoint and username enumeration
# --------------------------------------------------------------------------


def test_the_options_endpoint_answers_an_unknown_user_like_a_known_one(app, db_session):
    """D888, fixed. The endpoint used to answer
    {"error": "Could not find user nobody"}, so an unauthenticated caller could
    test whether an account exists. An unknown name now gets options of the same
    status and shape as a real account's, offering no credential, and the login
    then fails with the verification endpoint's generic message.
    """
    _seed()
    client = app.test_client()

    with patch('app.auth.passkeys.cache.set') as cache_set:
        unknown = client.post('/auth/passkeys/login_options', json={'username': 'nobody'})
        known = client.post('/auth/passkeys/login_options', json={'username': 'alice'})

    assert unknown.status_code == known.status_code == 200
    assert unknown.content_type == known.content_type
    assert set(unknown.get_json()) == set(known.get_json())
    assert unknown.get_json()['allowCredentials'] == []
    # Nobody to cache a challenge for: only alice's request stored one.
    assert cache_set.call_count == 1


def test_the_verification_endpoint_does_not_name_an_unknown_user(app, db_session):
    """R2's other half, and the evidence that the asymmetry is real: a missing
    user gets exactly the message a present-but-unusable one gets.
    """
    _seed()
    client = app.test_client()

    with patch('app.auth.passkeys.parse_authentication_credential_json',
               return_value='CRED'):
        response = client.post('/auth/passkeys/login_verification',
                               json={'username': 'nobody', 'redirect': '/',
                                     'response': {}})

    assert response.get_json() == {'verified': False,
                                   'message': 'No valid passkeys found for nobody'}


def test_a_known_user_is_given_a_challenge(app, db_session):
    """The success arm: a challenge is returned AND cached under the user's id,
    because the verification endpoint reads it back from there.
    """
    instance, alice = _seed()
    _passkey(alice)
    client = app.test_client()

    with patch('app.auth.passkeys.cache.set') as cache_set:
        response = client.post('/auth/passkeys/login_options',
                               json={'username': 'alice'})

    assert response.content_type == 'application/json'
    assert 'challenge' in response.get_json()
    # Asserted on the SET, not a later GET: tests/conftest.py configures
    # NullCache, so nothing stored is ever retrievable and cache.get() answers
    # None for a challenge that was correctly saved. Fact 344.
    assert cache_set.call_args.args[0] == f'challenge_{alice.id}'


def test_a_user_can_be_found_by_email(app, db_session):
    """The `|` in the lookup: the field is labelled 'username' but an email
    address is accepted, which is what the login form's placeholder promises.
    """
    instance, alice = _seed()
    _passkey(alice)
    client = app.test_client()

    response = client.post('/auth/passkeys/login_options',
                           json={'username': 'alice@example.com'})

    assert 'challenge' in response.get_json()


@pytest.mark.parametrize('column, value', [
    ('banned', True),
    ('ap_id', 'alice@remote.example'),
])
def test_a_banned_or_remote_account_gets_no_usable_challenge(app, db_session, column, value):
    """The two filters beside the name match. A remote account has no local
    credential to offer, and a banned one must not be handed a login path --
    it is answered exactly as an unknown name is (D888): options offering no
    credential, with no challenge stored against the account.
    """
    instance, alice = _seed()
    _passkey(alice)
    setattr(alice, column, value)
    db.session.commit()
    client = app.test_client()

    with patch('app.auth.passkeys.cache.set') as cache_set:
        response = client.post('/auth/passkeys/login_options', json={'username': 'alice'})

    assert response.get_json()['allowCredentials'] == []
    assert cache_set.call_args_list == []


# --------------------------------------------------------------------------
# allowed_credentials, and a successful login
# --------------------------------------------------------------------------


def test_the_challenge_offers_every_stored_credential(app, db_session):
    """`allowed_credentials` -- the browser is told which credentials this
    account has, so it can pick one the device holds. Two are stored so a
    handler returning only the first would fail.
    """
    from app.auth.passkeys import allowed_credentials

    instance, alice = _seed()
    _passkey(alice, device='phone', passkey_id=base64.b64encode(b'one').decode())
    _passkey(alice, device='laptop', passkey_id=base64.b64encode(b'two').decode())

    with app.test_request_context('/'):
        credentials = allowed_credentials(alice)

    assert sorted(c.id for c in credentials) == [b'one', b'two']


def test_an_account_with_no_credentials_offers_an_empty_list(app, db_session):
    """The `else` arm of `if user.passkeys.count():` -- the spelling P1's guard
    should have used, two functions above it in the same file.
    """
    from app.auth.passkeys import allowed_credentials

    instance, alice = _seed()

    with app.test_request_context('/'):
        assert allowed_credentials(alice) == []


def test_a_valid_passkey_logs_the_owner_in(app, db_session):
    """The success path end to end: the response carries the redirect and the
    session is authenticated afterwards.
    """
    instance, alice = _seed()
    _passkey(alice)
    client = app.test_client()

    with patch('app.auth.passkeys.parse_authentication_credential_json',
               return_value='CRED'):
        with patch('app.auth.passkeys.verify_authentication_response',
                   return_value=authenticated()):
            response = client.post('/auth/passkeys/login_verification',
                                   json={'username': 'alice', 'redirect': '/feed',
                                         'response': {}})

    assert response.get_json() == {'verified': True, 'redirectTo': '/feed'}
    with client.session_transaction() as sess:
        assert str(sess['_user_id']) == str(alice.id)


def test_the_login_is_verified_against_this_host_and_challenge(app, db_session):
    """The three arguments that BIND an assertion to this site and this login
    attempt: expected_rp_id, expected_origin and expected_challenge.

    Mutants replacing any of the three survived a suite that asserted only the
    outcome -- because the library is mocked, a wrong host or a missing
    challenge still "verifies". These are exactly the arguments whose values
    are the security property, so they are asserted directly. The registration
    side already had this; the login side did not.
    """
    instance, alice = _seed()
    _passkey(alice)
    client = app.test_client()

    with patch('app.auth.passkeys.cache.get', return_value='CHALLENGE'):
        with patch('app.auth.passkeys.parse_authentication_credential_json',
                   return_value='CRED'):
            with patch('app.auth.passkeys.verify_authentication_response',
                   return_value=authenticated()) as verify:
                client.post('/auth/passkeys/login_verification',
                            json={'username': 'alice', 'redirect': '/',
                                  'response': {}})

    assert verify.call_args.kwargs['expected_rp_id'] == 'test.piefed.local'
    assert verify.call_args.kwargs['expected_origin'] == 'https://test.piefed.local'
    assert verify.call_args.kwargs['expected_challenge'] == 'CHALLENGE'
    assert verify.call_args.kwargs['credential'] == 'CRED'


def test_a_passkey_login_is_remembered(app, db_session):
    """`login_user(user, remember=True)`. A passkey is a device-bound
    credential, so the session is meant to outlive the browser being closed --
    without the flag the user is asked to re-authenticate every time, which
    defeats the point of registering a device.
    """
    instance, alice = _seed()
    _passkey(alice)
    client = app.test_client()

    with patch('app.auth.passkeys.parse_authentication_credential_json',
               return_value='CRED'):
        with patch('app.auth.passkeys.verify_authentication_response',
                   return_value=authenticated()):
            response = client.post('/auth/passkeys/login_verification',
                                   json={'username': 'alice', 'redirect': '/',
                                         'response': {}})

    cookies = response.headers.getlist('Set-Cookie')
    assert any('remember_token=' in cookie for cookie in cookies)


def test_a_login_with_no_redirect_goes_to_the_front_page(app, db_session):
    """`redirect or '/'` -- the browser sends an empty string when it has no
    page to return to."""
    instance, alice = _seed()
    _passkey(alice)
    client = app.test_client()

    with patch('app.auth.passkeys.parse_authentication_credential_json',
               return_value='CRED'):
        with patch('app.auth.passkeys.verify_authentication_response',
                   return_value=authenticated()):
            response = client.post('/auth/passkeys/login_verification',
                                   json={'username': 'alice', 'redirect': '',
                                         'response': {}})

    assert response.get_json()['redirectTo'] == '/'


def test_the_second_passkey_is_tried_when_the_first_fails(app, db_session):
    """The loop's whole purpose: a device holding the SECOND credential must
    still log in. Without this row the `except InvalidAuthenticationResponse:
    pass` could be a `raise` and every single-passkey test would still pass.
    """
    instance, alice = _seed()
    _passkey(alice, device='phone', passkey_id=base64.b64encode(b'one').decode())
    _passkey(alice, device='laptop', passkey_id=base64.b64encode(b'two').decode())
    client = app.test_client()

    attempts = [InvalidAuthenticationResponse('first one is not this device'), authenticated()]

    with patch('app.auth.passkeys.parse_authentication_credential_json',
               return_value='CRED'):
        with patch('app.auth.passkeys.verify_authentication_response',
                   side_effect=attempts):
            response = client.post('/auth/passkeys/login_verification',
                                   json={'username': 'alice', 'redirect': '/',
                                         'response': {}})

    assert response.get_json()['verified'] is True


def test_a_public_key_cannot_be_stored_as_text_at_all():
    """Why the str-handling branch was removed rather than covered.

    `Passkey.public_key` is `LargeBinary`. The ORM refuses to write a str to it
    -- `TypeError: can't escape str to binary` -- and a value inserted as
    `'plain text'::bytea` through raw SQL still reads back as `bytes`:

        PROBE j1 type after raw insert: bytes
        PROBE j1 isinstance str: False

    So `isinstance(passkey.public_key, str)` was always False for any row
    loaded from the database, and the nested base64/utf-8 fallback beneath it
    could not execute. Five lines of dead code in an authentication path,
    removed per D822. This row records the column type the argument rests on,
    so a future change to it re-opens the question.
    """
    from app.models import Passkey as PasskeyModel
    from sqlalchemy import LargeBinary

    assert isinstance(PasskeyModel.__table__.c.public_key.type, LargeBinary)


def test_a_public_key_stored_as_bytes_is_used_as_it_is(app, db_session):
    """The `else` arm: the column's declared type, and the ordinary case."""
    instance, alice = _seed()
    client = app.test_client()
    _passkey(alice)

    with patch('app.auth.passkeys.parse_authentication_credential_json',
               return_value='CRED'):
        with patch('app.auth.passkeys.verify_authentication_response',
                   return_value=authenticated()) as verify:
            client.post('/auth/passkeys/login_verification',
                        json={'username': 'alice', 'redirect': '/', 'response': {}})

    assert verify.call_args.kwargs['credential_public_key'] == b'rawkey'


# --------------------------------------------------------------------------
# app/user/passkeys.py -- registration and management
# --------------------------------------------------------------------------


def test_the_passkey_list_needs_a_login(app, db_session):
    _seed()
    client = app.test_client()

    response = client.get('/user/passkeys')

    assert response.status_code == 302
    assert '/auth/login' in response.headers['Location']


def test_the_passkey_list_shows_the_owners_credentials(app, db_session):
    instance, alice = _seed()
    _passkey(alice, device='phone')
    client = app.test_client()
    login(client, alice)

    with patch('app.user.passkeys.render_template', return_value='rendered') as render:
        response = client.get('/user/passkeys')

    assert response.status_code == 200
    assert [p.device for p in render.call_args.kwargs['passkeys']] == ['phone']
    assert render.call_args.kwargs['add_passkey'] is False


def test_the_list_opens_the_add_dialog_when_asked(app, db_session):
    """`request.args.get('add') is not None` -- the flag is presence-based, so
    `?add` with no value has to count.
    """
    instance, alice = _seed()
    client = app.test_client()
    login(client, alice)

    with patch('app.user.passkeys.render_template', return_value='rendered') as render:
        client.get('/user/passkeys?add=')

    assert render.call_args.kwargs['add_passkey'] is True


def test_a_passkey_can_be_deleted_by_its_owner(app, db_session):
    instance, alice = _seed()
    passkey = _passkey(alice)
    client = app.test_client()
    login(client, alice)

    token = csrf(app, client)

    response = client.post(f'/user/passkeys/delete/{passkey.id}',
                           data={'csrf_token': token})

    assert response.status_code == 302
    assert Passkey.query.filter_by(id=passkey.id).first() is None


def test_a_passkey_cannot_be_deleted_by_anyone_else(app, db_session):
    """The `Passkey.user_id == current_user.id` conjunct. Without it this route
    would delete any passkey by id -- the classic insecure direct object
    reference, and the one thing in this file that must not regress.
    """
    instance, alice = _seed()
    bob = make_user(instance, 'bob', local=True)
    db.session.commit()
    victim = _passkey(alice)
    client = app.test_client()
    login(client, bob)

    token = csrf(app, client)

    response = client.post(f'/user/passkeys/delete/{victim.id}',
                           data={'csrf_token': token})

    assert response.status_code == 302
    assert Passkey.query.filter_by(id=victim.id).first() is not None


def test_deleting_a_passkey_that_is_not_there_is_not_an_error(app, db_session):
    """The `if passkey:` false arm -- a double-submitted delete redirects
    rather than raising.
    """
    instance, alice = _seed()
    client = app.test_client()
    login(client, alice)

    token = csrf(app, client)

    response = client.post('/user/passkeys/delete/9999',
                           data={'csrf_token': token})

    assert response.status_code == 302


def test_the_registration_options_carry_the_site_and_the_user(app, db_session):
    instance, alice = _seed()
    client = app.test_client()
    login(client, alice)

    with patch('app.user.passkeys.cache.set') as cache_set:
        response = client.post('/user/passkeys/registration/options')

    assert response.content_type == 'application/json'
    body = response.get_json()
    # rp.id binds the credential to this host: a credential minted under
    # another rp_id will not be offered to, or accepted by, this site.
    assert body['rp']['id'] == 'test.piefed.local'
    assert body['rp']['name'] == 'Test Site'
    assert body['user']['name'] == alice.user_name
    assert cache_set.call_args.args[0] == f'challenge_{alice.id}'


def test_the_registration_options_exclude_credentials_already_registered(app, db_session):
    """`exclude_credentials` stops a device registering itself twice, which the
    browser enforces from this list.
    """
    from app.user.passkeys import exclude_credentials

    instance, alice = _seed()
    _passkey(alice, passkey_id=base64.b64encode(b'one').decode())
    client = app.test_client()
    login(client, alice)

    with client:
        client.get('/user/passkeys')
        credentials = exclude_credentials()

    assert [c.id for c in credentials] == [b'one']


def test_a_user_with_no_credentials_excludes_nothing(app, db_session):
    from app.user.passkeys import exclude_credentials

    instance, alice = _seed()
    client = app.test_client()
    login(client, alice)

    with client:
        client.get('/user/passkeys')

        assert exclude_credentials() == []


@pytest.mark.parametrize('user_id, expected', [
    (1, b'\x01'),
    (255, b'\xff'),
    (256, b'\x01\x00'),
])
def test_a_user_handle_is_the_id_as_bytes(user_id, expected):
    """`integer_to_bytes` and its inverse. The 256 row is what makes the
    width calculation load-bearing: a one-byte handle would truncate it.
    """
    from app.user.passkeys import bytes_to_integer, integer_to_bytes

    assert integer_to_bytes(user_id) == expected
    assert bytes_to_integer(expected) == user_id


def test_registering_a_passkey_stores_it_against_the_owner(app, db_session):
    """The success path. `credential_id` is stored base64-encoded and the
    public key raw, which is the pairing app/auth/passkeys.py reads back.
    """
    instance, alice = _seed()
    client = app.test_client()
    login(client, alice)
    verification = type('Verification', (), {'credential_id': b'newcred',
                                             'credential_public_key': b'newkey',
                                             'sign_count': 0})()

    with patch('app.user.passkeys.parse_registration_credential_json',
               return_value='CRED'):
        with patch('app.user.passkeys.verify_registration_response',
                   return_value=verification):
            response = client.post('/user/passkeys/registration/verification',
                                   json={'response': {}, 'device': 'laptop'})

    assert response.data == alice.user_name.encode()
    stored = Passkey.query.filter_by(user_id=alice.id).first()
    assert stored.device == 'laptop'
    assert stored.passkey_id == base64.b64encode(b'newcred').decode()
    assert stored.public_key == b'newkey'


def test_a_registration_the_library_rejects_stores_nothing(app, db_session):
    """The `except InvalidRegistrationResponse` arm. The row asserts the
    absence of a row as well as the response, because a handler that returned
    'FAILED' after storing would satisfy the first assertion alone.
    """
    instance, alice = _seed()
    client = app.test_client()
    login(client, alice)

    with patch('app.user.passkeys.parse_registration_credential_json',
               return_value='CRED'):
        with patch('app.user.passkeys.verify_registration_response',
                   side_effect=InvalidRegistrationResponse('nope')):
            response = client.post('/user/passkeys/registration/verification',
                                   json={'response': {}, 'device': 'laptop'})

    assert response.data == b'FAILED'
    assert Passkey.query.filter_by(user_id=alice.id).first() is None


def test_the_registration_is_verified_against_this_host(app, db_session):
    """`expected_rp_id` and `expected_origin` are what bind a credential to
    this site; a credential minted for another origin must not verify here.
    """
    instance, alice = _seed()
    client = app.test_client()
    login(client, alice)
    verification = type('Verification', (), {'credential_id': b'c',
                                             'credential_public_key': b'k',
                                             'sign_count': 0})()

    with patch('app.user.passkeys.parse_registration_credential_json',
               return_value='CRED'):
        with patch('app.user.passkeys.verify_registration_response',
                   return_value=verification) as verify:
            client.post('/user/passkeys/registration/verification',
                        json={'response': {}, 'device': 'laptop'})

    assert verify.call_args.kwargs['expected_rp_id'] == 'test.piefed.local'
    assert verify.call_args.kwargs['expected_origin'] == 'https://test.piefed.local'


def test_the_registration_stores_the_authenticators_sign_count(app, db_session):
    """D887, fixed (owner ruling 2026-09-30): `registration_verification.sign_count`
    was discarded, so every credential started at 0 however many times its
    authenticator had been used. It is the starting counter now."""
    instance, alice = _seed()
    client = app.test_client()
    login(client, alice)
    verification = type('Verification', (), {'credential_id': b'c',
                                             'credential_public_key': b'k',
                                             'sign_count': 97})()

    with patch('app.user.passkeys.parse_registration_credential_json',
               return_value='CRED'):
        with patch('app.user.passkeys.verify_registration_response',
                   return_value=verification):
            client.post('/user/passkeys/registration/verification',
                        json={'response': {}, 'device': 'laptop'})

    assert Passkey.query.filter_by(user_id=alice.id).first().counter == 97

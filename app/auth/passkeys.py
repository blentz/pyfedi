import base64
import hashlib
import hmac

from flask import request, make_response, abort, jsonify, current_app
from flask_login import login_user
from sqlalchemy import false
from webauthn import generate_authentication_options, options_to_json, verify_authentication_response
from webauthn.helpers import parse_authentication_credential_json
from webauthn.helpers.exceptions import InvalidAuthenticationResponse
from webauthn.helpers.structs import UserVerificationRequirement, PublicKeyCredentialDescriptor

from app import db, cache
from app.auth import bp
from app.auth.util import split_handle
from app.models import User, utcnow
from app.storefront import current_storefront, on_domain, storefront_url
from app.utils import safe_redirect_target


# ----------------------------------------------------------------------
def passkey_user(username):
    """The local, unbanned account a typed name or email means: the name on this storefront's domain (or the domain a
    full handle names), the email anywhere on the site. None for anything else, a non-string included."""
    if not isinstance(username, str):
        return None
    name, domain = split_handle(username, current_storefront())
    named = (User.user_name == name) & on_domain(domain) if domain else false()
    return User.query.filter(named | (User.email == username), User.ap_id == None, User.banned == False).first()


# ----------------------------------------------------------------------
# Passkey options
@bp.route('/passkeys/login_options', methods=['POST'])
def passkey_options():
    request_json = request.get_json(force=True)
    if not isinstance(request_json, dict):
        abort(400)
    username = request_json.get('username')
    user = passkey_user(username)
    # D888: an unknown (or banned, or remote) name, or an account with no passkey,
    # gets options of the same shape as an account with one, offering a decoy
    # credential, so this endpoint cannot be used to test whether an account
    # exists or has a passkey. The login then fails generically.
    credentials = allowed_credentials(user) if user else []
    options = generate_authentication_options(
        rp_id=current_storefront().domain,
        timeout=120000,
        allow_credentials=credentials or [decoy_credential(username)],
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    if user:
        cache.set(f'challenge_{user.id}', options.challenge, timeout=3600)
    json_obj = options_to_json(options)
    response = make_response(json_obj)
    response.content_type = 'application/json'
    return response


# ----------------------------------------------------------------------
def decoy_credential(username) -> PublicKeyCredentialDescriptor:
    """A credential no device holds, the same on every request for the same name (D888)."""
    key = str(current_app.config['SECRET_KEY']).encode()
    return PublicKeyCredentialDescriptor(
        id=hmac.new(key, str(username or '').lower().encode(), hashlib.sha256).digest())


def allowed_credentials(user):
    if user.passkeys.count():
        return [PublicKeyCredentialDescriptor(id=base64.b64decode(pk.passkey_id)) for pk in user.passkeys]
    else:
        return []


# ----------------------------------------------------------------------
# Passkey verification
@bp.route('/passkeys/login_verification', methods=['POST'])
def passkey_verification():
    request_json = request.get_json(force=True)
    if not isinstance(request_json, dict):
        abort(400)
    # `.get`, not `[...]`: this endpoint is unauthenticated and takes whatever
    # body it is posted, so a missing key was a 500 rather than a refusal. The
    # same three keys are read below.
    username = request_json.get('username')
    redirect = request_json.get('redirect')
    error_message = ''

    auth_credential = parse_authentication_credential_json(request_json.get('response'))
    user = passkey_user(username)
    if user:
        # .count(), not truthiness: User.passkeys is lazy='dynamic', so the
        # attribute is an AppenderQuery and `not user.passkeys` was always
        # False -- this message could never be produced. allowed_credentials
        # above already uses the correct spelling.
        if not user.passkeys.count():
            # D888: the same answer as an unknown name or a failed assertion, so
            # this endpoint cannot be used to test whether an account exists
            error_message = f'No valid passkeys found for {username}'
        else:
            challenge = cache.get(f'challenge_{user.id}')
            success = False
            for passkey in user.passkeys:
                try:
                    # Passkey.public_key is LargeBinary, so this is always bytes.
                    # The str handling that used to sit here -- base64-decode,
                    # falling back to .encode('utf-8') -- could not run: the ORM
                    # refuses to write a str to the column ("can't escape str to
                    # binary") and a value inserted as 'text'::bytea in raw SQL
                    # still reads back as bytes, so isinstance(..., str) was
                    # always False for any row loaded from the database.
                    credential_public_key = passkey.public_key

                    # D886: the stored counter, so the library refuses a count that did
                    # not advance (a cloned authenticator), unless both are 0 -- an
                    # authenticator with no counter. The reported count is then kept.
                    verification = verify_authentication_response(
                        credential=auth_credential,
                        expected_rp_id=current_storefront().domain,
                        expected_challenge=challenge,
                        expected_origin=storefront_url(),
                        credential_public_key=credential_public_key,
                        credential_current_sign_count=passkey.counter,
                        require_user_verification=False,
                    )
                    # print(f'{passkey} is valid')
                    passkey.counter = verification.new_sign_count
                    passkey.used = utcnow()
                    success = True
                    break
                except InvalidAuthenticationResponse:
                    pass  # try another passkey instead by continuing to loop through all their passkeys
            if not success:
                error_message = f'No valid passkeys found for {username}'
            db.session.commit()
    else:
        # print(f'No user with email {username}')
        error_message = f'No valid passkeys found for {username}'

    if error_message:
        return jsonify({'verified': False, 'message': error_message})
    else:
        login_user(user, remember=True)
        # D1373. `redirect_to = redirect or '/'`, where `redirect` is the posted
        # body's value and app/static/js/scripts.js:1375 assigns it to
        # `location.href`. scripts.js:1336 fills it from `?next=` on the login
        # page, so `/auth/login?next=<anything>` chose where a visitor went the
        # moment their passkey verified -- including
        # `javascript:alert(1)`, which `location.href` EXECUTES, in this origin,
        # on a page where the session has just been authenticated.
        #
        # The password arm of the same login form already runs the same `?next=`
        # through this function (`redirect_next_page`, app/auth/util.py:447) and
        # says why. One control, two paths, checked on one -- D1359's shape.
        redirect_to = safe_redirect_target(redirect, '/')
        return jsonify({'verified': True, 'redirectTo': redirect_to})

import base64

from flask import request, make_response, abort, jsonify
from flask_login import login_user
from webauthn import generate_authentication_options, options_to_json, verify_authentication_response
from webauthn.helpers import parse_authentication_credential_json
from webauthn.helpers.exceptions import InvalidAuthenticationResponse
from webauthn.helpers.structs import UserVerificationRequirement, PublicKeyCredentialDescriptor

from app import db, cache
from app.auth import bp
from app.models import User, utcnow


# ----------------------------------------------------------------------
# Passkey options
@bp.route('/passkeys/login_options', methods=['POST'])
def passkey_options():
    request_json = request.get_json(force=True)
    user = User.query.filter(
        (User.user_name == request_json["username"])
        | (User.email == request_json["username"]),
        User.ap_id == None,
        User.banned == False,
    ).first()
    if user:
        options = generate_authentication_options(
            rp_id=request.host,
            timeout=120000,
            allow_credentials=allowed_credentials(user),
            user_verification=UserVerificationRequirement.PREFERRED,
        )
        cache.set(f'challenge_{user.id}', options.challenge, timeout=3600)
        json_obj = options_to_json(options)
        response = make_response(json_obj)
        response.content_type = 'application/json'
        return response
    else:
        return jsonify({"error": f"Could not find user {request_json['username']}"})


# ----------------------------------------------------------------------
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
    username = request_json['username']
    redirect = request_json['redirect']
    error_message = ''

    auth_credential = parse_authentication_credential_json(request_json['response'])
    user = User.query.filter(
        (User.user_name == request_json["username"])
        | (User.email == request_json["username"]),
        User.ap_id == None,
        User.banned == False,
    ).first()
    if user:
        # .count(), not truthiness: User.passkeys is lazy='dynamic', so the
        # attribute is an AppenderQuery and `not user.passkeys` was always
        # False -- this message could never be produced. allowed_credentials
        # above already uses the correct spelling.
        if not user.passkeys.count():
            error_message = f'No passkeys found for {username}'
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

                    verify_authentication_response(
                        credential=auth_credential,
                        expected_rp_id=request.host,
                        expected_challenge=challenge,
                        expected_origin=f'https://{request.host}',
                        credential_public_key=credential_public_key,
                        credential_current_sign_count=0,
                        require_user_verification=False,
                    )
                    # print(f'{passkey} is valid')
                    passkey.counter += 1
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
        redirect_to = redirect or '/'
        return jsonify({'verified': True, 'redirectTo': redirect_to})

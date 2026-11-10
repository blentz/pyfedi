"""The site and logging in (spec 2026-11-10-lemmy-v3-design §2).

GET /api/v3/site is the Lemmy site document other servers and crawlers have long read
(app.activitypub.routes.lemmy_site). Jerboa reads it too, before and right after logging in. Sent a token, the
document also carries `my_user`, which Jerboa reads without a null check, and is never cached.

POST /api/v3/user/login is alpha's password login under Lemmy's names. The token is alpha's own JWT, good in both
APIs."""
from flask import jsonify, request

from app import limiter
from app.api.alpha.routes import enable_api, is_trusted_request
from app.api.alpha.views import user_view
from app.lemmy import bp, shapes
from app.lemmy.errors import refusal
from app.shared.auth import log_in_with_password
from app.utils import authorise_api_user, orjson_response

LOGIN_LIMIT = '20/hour'   # alpha's (app.api.alpha.routes.post_alpha_user_login), a bucket of its own per address


@bp.before_request
def alpha_enabled():
    """The adapter is alpha under other names, so it is served where alpha is."""
    if not enable_api():
        return refusal('api_disabled')
    return None


def signed_in() -> bool:
    """Whether the site document is asked for with a token: then it is the person's own, and is not cached."""
    return bool(request.headers.get('Authorization'))


def answer(document: dict):
    """The site document, with the person's own `my_user` when a token was sent: a token that is no longer good is
    refused as the login gate refuses one (401 incorrect_login)."""
    if not signed_in():
        return orjson_response(document)
    try:
        user = authorise_api_user(request.headers.get('Authorization'), return_type='model')
    except Exception as refusal_reason:   # the one exception authorise_api_user raises for every way a token is wrong
        return refusal('access_lapsed', 403) if str(refusal_reason) == 'access_lapsed' \
            else refusal('incorrect_login', 401)
    return orjson_response({**document, 'my_user': shapes.my_user(user_view(user=user, variant=6), user)})


@bp.route('/api/v3/user/login', methods=['POST'])
def login():
    fields = request.get_json(silent=True)
    fields = fields if isinstance(fields, dict) else {}
    name, password = fields.get('username_or_email'), fields.get('password')
    if not isinstance(name, str) or not isinstance(password, str) or not name.strip() or not password.strip():
        return refusal('incorrect_login')
    with limiter.limit(LOGIN_LIMIT, scope='lemmy-password-login', exempt_when=is_trusted_request):
        user = log_in_with_password(name, password)
    return jsonify(jwt=user.encode_jwt_token(), registration_created=False, verify_email_sent=False)


@bp.route('/api/v3/user/validate_auth')
def validate_auth():
    """Jerboa asks whether its stored token is still good on every start, and marks the account for a new login if
    not."""
    authorise_api_user(request.headers.get('Authorization'))
    return jsonify(success=True)

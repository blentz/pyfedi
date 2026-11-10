"""Uploading an image as Lemmy does (spec 2026-11-10-lemmy-v3-design §2): Lemmy serves pict-rs at /pictrs/image, and
Jerboa posts `images[]` there with its token as the `jwt` cookie, then names each upload by the URL its library makes,
/pictrs/image/<file>. Each image is stored by alpha's upload (its checks and its 15/hour), and its `file` is the stored
URL signed with the site's key: GET /pictrs/image/<file> redirects to the stored image, and only to an image this
server signed, so it is no open redirect. A post whose link is one of these is stored with the image's own URL."""
from flask import current_app, jsonify, redirect, request
from itsdangerous import BadSignature, URLSafeSerializer

from app import limiter
from app.api.alpha.routes import UPLOAD_LIMIT, UPLOAD_SCOPE, is_trusted_request
from app.api.alpha.utils.upload import post_upload_image
from app.lemmy import bp
from app.lemmy.errors import refusal

PREFIX = '/pictrs/image/'
MAX_IMAGES = 10   # in one request: Jerboa sends one


def _signer() -> URLSafeSerializer:
    return URLSafeSerializer(current_app.config['SECRET_KEY'], salt='lemmy-pictrs')


def stored_url(url: str | None) -> str | None:
    """`url` itself, unless it names one of this server's /pictrs/image uploads: then the stored image's own URL."""
    if not url or PREFIX not in url:
        return url
    try:
        return _signer().loads(url.split(PREFIX, 1)[1])
    except BadSignature:
        return url


@bp.route('/pictrs/image', methods=['POST'])
def upload_image():
    jwt = request.cookies.get('jwt')
    auth = request.headers.get('Authorization') or (f'Bearer {jwt}' if jwt else None)
    if auth is None:
        return refusal('incorrect_login', 401)
    images = request.files.getlist('images[]')
    if not images or len(images) > MAX_IMAGES:
        return refusal('invalid_request')
    files = []
    for image in images:
        with limiter.shared_limit(UPLOAD_LIMIT, scope=UPLOAD_SCOPE, exempt_when=is_trusted_request):   # alpha's
            token = _signer().dumps(post_upload_image(auth, image)['url'])
        files.append({'file': token, 'delete_token': token})
    return jsonify(msg='ok', files=files)


upload_image.has_own_login_gate = True   # signed in by the token Jerboa sends as a cookie, which the gate never reads


@bp.route('/pictrs/image/<token>')
def image(token):
    try:
        return redirect(_signer().loads(token))
    except BadSignature:
        return refusal('not_found', 404)


image.has_own_login_gate = True   # a redirect to a stored image, which is open as every stored image is

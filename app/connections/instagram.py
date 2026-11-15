"""Instagram (spec 2026-11-13-connected-accounts-design.md §2, §6; ruling C6: both APIs).

Two ways in, each a variant of one service:

* `facebook_login`: the Facebook app and sign-in (FACEBOOK_APP_ID), for an Instagram professional account linked to
  a Page the member manages. Calls go to graph.facebook.com as Facebook's do (POST, method override, proof), with the
  Page's token, which does not expire. Deletes sync (instagram_manage_contents).
* `instagram_login`: Instagram's own sign-in (INSTAGRAM_APP_ID), no Page needed. Its long-lived token lasts 60 days
  and is refreshed before then. No API deletes its posts: the author is told to delete by hand.

No API edits a caption either (§2), so copies are never edited. A post with pictures becomes one photo, or a carousel
of up to ten. Instagram does not take the pictures: it fetches each from a public address, so PieFed serves each as
a JPEG at a signed address on its main domain that works for an hour (`/connections/media/<token>.jpg`), padded into
the aspect ratios Instagram allows.
"""
import io
import time
from urllib.parse import urlencode

from flask import current_app
from flask_babel import _, lazy_gettext as _l
from itsdangerous import BadData, URLSafeTimedSerializer
from PIL import Image

from app.connections import Account, AuthFailed, Permanent, Sent, Service, Transient, main_base_url, register
from app.connections.content import as_jpeg, cut, image_bytes, plain_text
from app.connections.eligibility import image_files, uploaded_video
from app.connections.apps import current
from app.connections.facebook import answer_of, graph, meta_app, paused_until
from app.connections.http import fetch
from app.constants import POST_TYPE_GALLERY, POST_TYPE_IMAGE

VIA_FACEBOOK = 'facebook_login'
DIRECT = 'instagram_login'
FACEBOOK_SCOPES = ('instagram_basic,instagram_content_publish,instagram_manage_contents,pages_show_list,'
                   'pages_read_engagement')
DIRECT_SCOPES = 'instagram_business_basic,instagram_business_content_publish'
MAX_CAPTION = 2200
MAX_HASHTAGS = 30
MAX_ITEMS = 10
MAX_PHOTO_BYTES = 8_000_000
MAX_PHOTO_SIDE = 1440
NARROWEST, WIDEST = 4 / 5, 1.91      # Instagram's aspect ratios, width over height
MEDIA_SALT = 'connections-instagram-media'
MEDIA_SECONDS = 3600
REFRESH_BEFORE = 7 * 24 * 3600       # a direct token is refreshed when it has a week left


# --------------------------------------------------------------------------
# the pictures Instagram fetches
# --------------------------------------------------------------------------

def _signer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.config['SECRET_KEY'], salt=MEDIA_SALT)


def media_url(file_id: int) -> str:
    return f"{main_base_url()}/connections/media/{_signer().dumps({'f': file_id})}.jpg"


def media_file_id(token: str) -> int | None:
    """The file a media address names, while its hour lasts; None for a forged, altered or stale one."""
    try:
        payload = _signer().loads(token, max_age=MEDIA_SECONDS)
    except BadData:
        return None
    file_id = payload.get('f') if isinstance(payload, dict) else None
    return file_id if isinstance(file_id, int) and not isinstance(file_id, bool) else None


def fitted(data: bytes) -> bytes | None:
    """The picture as a JPEG Instagram takes: padded (never cropped) into 4:5 .. 1.91:1, at most 1440 px a side and
    8 MB. Padding is white."""
    jpeg = as_jpeg(data, MAX_PHOTO_BYTES, MAX_PHOTO_SIDE)
    if jpeg is None:
        return None
    content, width, height = jpeg
    ratio = width / height
    if NARROWEST <= ratio <= WIDEST:
        return content
    if ratio < NARROWEST:
        size = (round(height * NARROWEST), height)
    else:
        size = (width, round(width / WIDEST))
    canvas = Image.new('RGB', size, (255, 255, 255))
    canvas.paste(Image.open(io.BytesIO(content)), ((size[0] - width) // 2, (size[1] - height) // 2))
    out = io.BytesIO()
    canvas.save(out, 'JPEG', quality=85)
    return out.getvalue()


# --------------------------------------------------------------------------
# Instagram Login's own Graph host
# --------------------------------------------------------------------------

def ig_app():
    """The Instagram Login app this call runs under: the context domain's own, or the instance's."""
    app = current('instagram')
    if app is None:
        raise Permanent(_('No Instagram app is set up here.'))
    return app


def ig_url(path: str) -> str:
    version = current_app.config.get('FACEBOOK_GRAPH_VERSION') or 'v27.0'
    return f'https://graph.instagram.com/{version}/{path.lstrip("/")}'


def ig(method: str, path: str, token: str, params: dict | None = None) -> dict:
    """A call on graph.instagram.com (Instagram Login). Its token goes as a bearer header, never in a URL."""
    headers = {'Authorization': f'Bearer {token}'}
    if method == 'GET':
        response = fetch('GET', ig_url(path), params=params or {}, headers=headers)
    else:
        response = fetch(method, ig_url(path), data=params or {}, headers=headers)
    return answer_of(response, 'Instagram')


def caption_of(post) -> str:
    """The post's text, its tags as hashtags (up to Instagram's 30), and the link back, which Instagram shows but
    does not link."""
    link = post.ap_id
    tags = ' '.join(f'#{tag.name}' for tag in (post.tags or [])[:MAX_HASHTAGS] if getattr(tag, 'name', None))
    tail = '\n\n'.join(part for part in (tags, link) if part)
    files = image_files(post)
    if len(files) > MAX_ITEMS:
        tail = _('(+%(count)d more pictures)', count=len(files) - MAX_ITEMS) + '\n\n' + tail
    return f'{cut(plain_text(post), MAX_CAPTION - len(tail) - 2)}\n\n{tail}'.strip()


# --------------------------------------------------------------------------
# the service
# --------------------------------------------------------------------------

class InstagramService(Service):
    name = 'instagram'
    label = 'Instagram'

    @property
    def variants(self) -> tuple:
        return tuple(variant for variant, on in ((VIA_FACEBOOK, self._via_facebook()), (DIRECT, self._direct()))
                     if on)

    def _via_facebook(self) -> bool:
        return current('facebook') is not None

    def _direct(self) -> bool:
        return current('instagram') is not None

    def configured(self) -> bool:
        return bool(self.variants)

    def variant_label(self, variant):
        if variant == VIA_FACEBOOK:
            return str(_l('Instagram, through Facebook'))
        return self.label

    def fits(self, post) -> bool:
        return not uploaded_video(post) and bool(image_files(post))

    def fits_type(self, post_type: int) -> bool:
        return post_type in (POST_TYPE_IMAGE, POST_TYPE_GALLERY)

    def kind_of(self, variant) -> str:
        return 'facebook' if variant == VIA_FACEBOOK else 'instagram'

    def can_delete(self, variant) -> bool:
        return variant == VIA_FACEBOOK

    # connecting --------------------------------------------------------------

    def authorize_url(self, variant, state, redirect_uri, pending, hint=''):
        if variant == VIA_FACEBOOK:
            version = current_app.config.get('FACEBOOK_GRAPH_VERSION') or 'v27.0'
            return f'https://www.facebook.com/{version}/dialog/oauth?' + urlencode({
                'client_id': meta_app().client_id, 'redirect_uri': redirect_uri, 'state': state,
                'scope': FACEBOOK_SCOPES, 'response_type': 'code'})
        return 'https://www.instagram.com/oauth/authorize?' + urlencode({
            'client_id': ig_app().client_id, 'redirect_uri': redirect_uri, 'state': state,
            'scope': DIRECT_SCOPES, 'response_type': 'code'})

    def finish(self, variant, args, redirect_uri, pending):
        return self._finish_facebook(args, redirect_uri) if variant == VIA_FACEBOOK else \
            self._finish_direct(args, redirect_uri)

    def _finish_facebook(self, args, redirect_uri) -> list:
        app_id, secret = meta_app().client_id, meta_app().get('client_secret')
        short = graph('GET', 'oauth/access_token', None, {'client_id': app_id, 'client_secret': secret,
                                                          'redirect_uri': redirect_uri, 'code': args.get('code', '')})
        long = graph('GET', 'oauth/access_token', None, {'grant_type': 'fb_exchange_token', 'client_id': app_id,
                                                         'client_secret': secret,
                                                         'fb_exchange_token': short.get('access_token', '')})
        user_token = long.get('access_token')
        if not user_token:
            raise Permanent(_('Facebook gave no token.'))
        me = graph('GET', 'me', user_token, {'fields': 'id'})
        pages = graph('GET', 'me/accounts', user_token, {
            'fields': 'id,name,access_token,instagram_business_account{id,username}', 'limit': 100})
        accounts = []
        for page in pages.get('data') or []:
            linked = page.get('instagram_business_account') if isinstance(page, dict) else None
            if isinstance(linked, dict) and linked.get('id') and page.get('access_token'):
                accounts.append(Account(
                    remote_id=str(linked['id']), remote_name=f"@{linked.get('username') or linked['id']}",
                    variant=VIA_FACEBOOK,
                    tokens={'token': page['access_token'], 'user_token': user_token,
                            'user_id': str(me.get('id', '')), 'expires_at': None}))
        return accounts

    def _finish_direct(self, args, redirect_uri) -> list:
        answer = answer_of(fetch('POST', 'https://api.instagram.com/oauth/access_token', data={
            'client_id': ig_app().client_id,
            'client_secret': ig_app().get('client_secret'), 'grant_type': 'authorization_code',
            'redirect_uri': redirect_uri, 'code': args.get('code', '')}), 'Instagram')
        if not answer.get('access_token'):
            raise Permanent(_('Instagram gave no token.'))
        long = answer_of(fetch('GET', 'https://graph.instagram.com/access_token', params={
            'grant_type': 'ig_exchange_token', 'client_secret': ig_app().get('client_secret'),
            'access_token': answer['access_token']}), 'Instagram')
        token = long.get('access_token')
        if not token:
            raise Permanent(_('Instagram gave no token.'))
        me = ig('GET', 'me', token, {'fields': 'user_id,username'})
        account_id = str(me.get('user_id') or answer.get('user_id') or '')
        if not account_id:
            raise Permanent(_('Instagram named no account.'))
        return [Account(remote_id=account_id, remote_name=f"@{me.get('username') or account_id}", variant=DIRECT,
                        tokens={'token': token, 'expires_at': time.time() + int(long.get('expires_in') or 0)})]

    # tokens ------------------------------------------------------------------

    def expired(self, tokens) -> bool:
        expires_at = tokens.get('expires_at')
        return expires_at is not None and time.time() >= expires_at - REFRESH_BEFORE

    def refresh(self, tokens):
        answer = answer_of(fetch('GET', 'https://graph.instagram.com/refresh_access_token', params={
            'grant_type': 'ig_refresh_token', 'access_token': tokens['token']}), 'Instagram')
        if not answer.get('access_token'):
            raise AuthFailed(_('Instagram would not renew this connection.'))
        return {**tokens, 'token': answer['access_token'],
                'expires_at': time.time() + int(answer.get('expires_in') or 0)}

    # posting -----------------------------------------------------------------

    def _call(self, variant, method, path, token, params=None) -> dict:
        if variant == VIA_FACEBOOK:
            return graph(method, path, token, params)
        return ig(method, path, token, params)

    def send(self, post, tokens, connection):
        variant, account, token = connection.variant, connection.remote_id, tokens['token']
        files = [file for file in image_files(post)[:MAX_ITEMS] if fitted_ok(file)]
        if not files:
            raise Permanent(_('None of the pictures could be read.'))
        caption = caption_of(post)
        if len(files) == 1:
            container = self._call(variant, 'POST', f'{account}/media', token, {
                'image_url': media_url(files[0].id), 'caption': caption, 'alt_text': files[0].alt_text or ''})
        else:
            children = [self._call(variant, 'POST', f'{account}/media', token, {
                'image_url': media_url(file.id), 'is_carousel_item': 'true', 'alt_text': file.alt_text or ''})['id']
                for file in files]
            container = self._call(variant, 'POST', f'{account}/media', token, {
                'media_type': 'CAROUSEL', 'children': ','.join(children), 'caption': caption})
        media_id = self._call(variant, 'POST', f'{account}/media_publish', token, {'creation_id': container['id']})['id']
        try:
            permalink = self._call(variant, 'GET', str(media_id), token, {'fields': 'permalink'}).get('permalink')
        except Transient:
            permalink = None   # published: a link to it is a nicety, never a reason to publish again
        return Sent(remote_id=str(media_id), remote_url=permalink)

    def paused_until(self, connection):
        return paused_until(connection.remote_id) if connection.variant == VIA_FACEBOOK else None

    def delete(self, tokens, remote_id):
        graph('DELETE', remote_id, tokens['token'])

    def revoke(self, tokens):
        if tokens.get('user_token') and tokens.get('user_id'):
            graph('DELETE', f"{tokens['user_id']}/permissions", tokens['user_token'])


def fitted_ok(file) -> bool:
    """The picture can be read and fitted, so Instagram's fetch of it will be answered."""
    data = image_bytes(file)
    return bool(data) and fitted(data) is not None


SERVICE = register(InstagramService())

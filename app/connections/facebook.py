"""Facebook Pages (spec 2026-11-13-connected-accounts-design.md §2, §6; ruling C1: Pages only).

Facebook Login for Business with the Page permissions: the member signs in, PieFed swaps the code for a user token,
lengthens it, and lists the Pages the member may post to; one is connected (a sign-in with several asks which). A
Page token made from a long-lived user token does not expire. Every Graph call carries appsecret_proof.

What a post becomes (§6): the Page post's message is the post's text and its link back; a link post passes its link,
so Facebook draws the card; one picture is a photo post, a gallery a post with up to ten photos uploaded unpublished
and attached. An uploaded video goes as text and link. A content warning sends the warning and the link only.
Edits change the message (C4); deletes delete.
"""
import hashlib
import hmac
from urllib.parse import urlencode

from flask import current_app
from flask_babel import _

from app.connections import Account, AuthFailed, Permanent, Sent, Service, Transient, register
from app.connections.content import as_jpeg, cut, image_bytes, plain_text
from app.connections.eligibility import image_files, uploaded_video
from app.connections.http import fetch
from app.constants import POST_TYPE_LINK

SCOPES = 'pages_show_list,pages_manage_posts,pages_read_engagement'
MAX_MESSAGE = 60_000          # a Page post's message (Facebook's limit is 63,206)
MAX_PHOTOS = 10
MAX_PHOTO_BYTES = 4_000_000   # Facebook takes photos up to 4 MB
MAX_PHOTO_SIDE = 4096
AUTH_CODES = {190, 102, 10, 200, 210, 230}           # the token, or a permission, is gone: reconnect
LATER_CODES = {1, 2, 4, 17, 32, 341, 368, 613, 80001}   # temporary, or a rate limit: try again later


def graph_url(path: str) -> str:
    version = current_app.config.get('FACEBOOK_GRAPH_VERSION') or 'v27.0'
    return f'https://graph.facebook.com/{version}/{path.lstrip("/")}'


def proof(token: str) -> str:
    """appsecret_proof: the token signed with the app secret, so a token taken elsewhere is useless without it."""
    return hmac.new(current_app.config['FACEBOOK_APP_SECRET'].encode(), token.encode(), hashlib.sha256).hexdigest()


def graph(method: str, path: str, token: str | None, params: dict | None = None, **kwargs) -> dict:
    """One Graph API call, its answer as JSON, its failure as the error it means. Graph puts nearly every failure
    in a 400 with an error code; the code says whether it is the token, a rate limit, or the request."""
    fields = {**(params or {}), **kwargs.pop('data', {})}
    if token:
        fields.update(access_token=token, appsecret_proof=proof(token))
    if method != 'POST':   # Graph's method override: tokens and secrets stay in a body, out of any URL
        fields['method'] = method.lower()
    response = fetch('POST', graph_url(path), data=fields, **kwargs)
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code < 400 and isinstance(body, dict):
        return body
    error = body.get('error') if isinstance(body, dict) else None
    error = error if isinstance(error, dict) else {}
    message = f"Facebook: {str(error.get('message') or f'HTTP {response.status_code}')[:200]}"
    code = error.get('code')
    if code in AUTH_CODES or response.status_code in (401, 403):
        raise AuthFailed(message)
    if code in LATER_CODES or error.get('is_transient') or response.status_code == 429 or \
            response.status_code >= 500:
        raise Transient(message)
    raise Permanent(message)


def message_of(post) -> str:
    link = post.ap_id
    if post.content_warning:
        return f'CW: {cut(post.content_warning, 500)}\n\n{link}'
    return f'{cut(plain_text(post), MAX_MESSAGE - len(link) - 2)}\n\n{link}'.strip()


def _photo(file) -> bytes | None:
    data = image_bytes(file)
    jpeg = as_jpeg(data, MAX_PHOTO_BYTES, MAX_PHOTO_SIDE) if data else None
    return jpeg[0] if jpeg else None


class FacebookService(Service):
    name = 'facebook'
    label = 'Facebook Page'
    can_edit = True

    def configured(self) -> bool:
        return bool(current_app.config.get('FACEBOOK_APP_ID') and current_app.config.get('FACEBOOK_APP_SECRET'))

    def fits(self, post) -> bool:
        return True

    def authorize_url(self, variant, state, redirect_uri, pending, hint=''):
        version = current_app.config.get('FACEBOOK_GRAPH_VERSION') or 'v27.0'
        return f'https://www.facebook.com/{version}/dialog/oauth?' + urlencode({
            'client_id': current_app.config['FACEBOOK_APP_ID'], 'redirect_uri': redirect_uri, 'state': state,
            'scope': SCOPES, 'response_type': 'code'})

    def finish(self, variant, args, redirect_uri, pending):
        app_id, secret = current_app.config['FACEBOOK_APP_ID'], current_app.config['FACEBOOK_APP_SECRET']
        short = graph('GET', 'oauth/access_token', None, {'client_id': app_id, 'client_secret': secret,
                                                          'redirect_uri': redirect_uri, 'code': args.get('code', '')})
        long = graph('GET', 'oauth/access_token', None, {'grant_type': 'fb_exchange_token', 'client_id': app_id,
                                                         'client_secret': secret,
                                                         'fb_exchange_token': short.get('access_token', '')})
        user_token = long.get('access_token')
        if not user_token:
            raise Permanent(_('Facebook gave no token.'))
        me = graph('GET', 'me', user_token, {'fields': 'id'})
        pages = graph('GET', 'me/accounts', user_token, {'fields': 'id,name,access_token,tasks', 'limit': 100})
        accounts = []
        for page in pages.get('data') or []:
            if isinstance(page, dict) and page.get('access_token') and 'CREATE_CONTENT' in (page.get('tasks') or []):
                accounts.append(Account(remote_id=str(page['id']), remote_name=str(page.get('name') or page['id']),
                                        tokens={'page_token': page['access_token'], 'user_token': user_token,
                                                'user_id': str(me.get('id', '')), 'expires_at': None}))
        return accounts

    def send(self, post, tokens, connection):
        page, token = connection.remote_id, tokens['page_token']
        files = [] if post.content_warning or uploaded_video(post) else image_files(post)[:MAX_PHOTOS]
        photos = [photo for photo in (_photo(file) for file in files) if photo]
        message = message_of(post)
        if len(photos) == 1:
            answer = graph('POST', f'{page}/photos', token, data={'caption': message, 'published': 'true'},
                           files={'source': ('picture.jpg', photos[0], 'image/jpeg')}, seconds=120)
            post_id = answer.get('post_id') or f"{page}_{answer.get('id')}"
        else:
            fields = {'message': message}
            for number, photo in enumerate(photos):
                uploaded = graph('POST', f'{page}/photos', token, data={'published': 'false'},
                                 files={'source': ('picture.jpg', photo, 'image/jpeg')}, seconds=120)
                fields[f'attached_media[{number}]'] = f'{{"media_fbid":"{uploaded["id"]}"}}'
            if post.type == POST_TYPE_LINK and post.url and not post.content_warning and not photos:
                fields['link'] = post.url
            post_id = graph('POST', f'{page}/feed', token, data=fields)['id']
        return Sent(remote_id=str(post_id), remote_url=f'https://www.facebook.com/{post_id}')

    def edit(self, post, tokens, remote_id):
        graph('POST', remote_id, tokens['page_token'], data={'message': message_of(post)})

    def delete(self, tokens, remote_id):
        graph('DELETE', remote_id, tokens['page_token'])

    def revoke(self, tokens):
        if tokens.get('user_token') and tokens.get('user_id'):
            graph('DELETE', f"{tokens['user_id']}/permissions", tokens['user_token'])


SERVICE = register(FacebookService())

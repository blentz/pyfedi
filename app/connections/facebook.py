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
import json
import os
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

from flask import current_app
from flask_babel import _

import app as app_pkg
from app.connections import Account, AuthFailed, Permanent, Sent, Service, Transient, publishing, register
from app.connections.apps import App, current
from app.connections.content import StoredVideo, as_jpeg, cut, image_bytes, plain_text
from app.connections.eligibility import image_files, uploaded_video
from app.connections.http import fetch
from app.constants import POST_TYPE_LINK
from app.models import media_of

SCOPES = 'pages_show_list,pages_manage_posts,pages_read_engagement'
MAX_MESSAGE = 60_000          # a Page post's message (Facebook's limit is 63,206)
MAX_PHOTOS = 10
MAX_PHOTO_BYTES = 4_000_000   # Facebook takes photos up to 4 MB
MAX_PHOTO_SIDE = 4096
AUTH_CODES = {190, 102, 10, 200, 210, 230}           # the token, or a permission, is gone: reconnect
LATER_CODES = {1, 2, 4, 9, 17, 32, 341, 368, 613, 9007, 80001}   # temporary, a rate limit, or media not ready yet
RATE_CODES = {4, 17, 32, 613, 80001, 80002, 80003, 80004, 80005, 80006, 80008, 80009, 80014}   # Graph's rate limits
USAGE_LIMIT = 90              # percent of a Graph allowance past which PieFed pauses (spec 18.3)
APP_PAUSE = 3600              # seconds: the whole app's pause, as Graph's own app limit window
UNNAMED_PAUSE = 900           # seconds: a rate limit that names no account pauses the app this long
MAX_VIDEO_BYTES = 10 * 1024 ** 3   # a Page video
VIDEO_PREFIX = 'video:'


def meta_app() -> App:
    """The Meta app this call runs under: the context domain's own, or the instance's (spec 18)."""
    app = current('facebook')
    if app is None:
        raise Permanent(_('No Facebook app is set up here.'))
    return app


def meta_app_id() -> str | None:
    app = current('facebook')
    return app.client_id if app is not None else None


def graph_url(path: str) -> str:
    version = current_app.config.get('FACEBOOK_GRAPH_VERSION') or 'v27.0'
    return f'https://graph.facebook.com/{version}/{path.lstrip("/")}'


def proof(token: str) -> str:
    """appsecret_proof: the token signed with the app secret, so a token taken elsewhere is useless without it."""
    return hmac.new(meta_app().get('client_secret').encode(), token.encode(), hashlib.sha256).hexdigest()


def graph(method: str, path: str, token: str | None, params: dict | None = None, **kwargs) -> dict:
    """One Graph API call, its answer as JSON, its failure as the error it means. Graph puts nearly every failure
    in a 400 with an error code; the code says whether it is the token, a rate limit, or the request."""
    fields = {**(params or {}), **kwargs.pop('data', {})}
    if token:
        fields.update(access_token=token, appsecret_proof=proof(token))
    if method != 'POST':   # Graph's method override: tokens and secrets stay in a body, out of any URL
        fields['method'] = method.lower()
    return answer_of(fetch('POST', graph_url(path), data=fields, **kwargs), 'Facebook')


# --------------------------------------------------------------------------
# pausing before Graph refuses (spec 18.3, taken from Mixpost)
# --------------------------------------------------------------------------

def _pause_key(scope: str, ident) -> str:
    return f'connections:pause:meta:{scope}:{ident}'


def pause(scope: str, ident, seconds: int) -> None:
    """Hold every copy of the app (scope 'app') or of one Page or account (scope 'object') for `seconds`."""
    if ident and seconds > 0:
        app_pkg.redis_client.set(_pause_key(scope, ident), int(time.time()) + seconds, ex=seconds)


def paused_until(object_id: str | None) -> datetime | None:
    """When the app's pause, or the account's, ends (naive UTC, as the database keeps), or None."""
    ends = [app_pkg.redis_client.get(_pause_key('app', meta_app_id()))]
    if object_id:
        ends.append(app_pkg.redis_client.get(_pause_key('object', object_id)))
    latest = max((int(end) for end in ends if end), default=0)
    if latest <= time.time():
        return None
    return datetime.fromtimestamp(latest, timezone.utc).replace(tzinfo=None)


def _usage(value: str | None):
    try:
        return json.loads(value) if value else None
    except ValueError:
        return None


def _over(usage) -> bool:
    return isinstance(usage, dict) and any(
        isinstance(usage.get(name), (int, float)) and usage[name] > USAGE_LIMIT
        for name in ('call_count', 'total_cputime', 'total_time'))


def note_usage(response) -> bool:
    """Read how much of Graph's allowance is spent (X-App-Usage, X-Business-Use-Case-Usage) and pause the app, or the
    account, past USAGE_LIMIT. Whether anything was paused."""
    paused = False
    if _over(_usage(response.headers.get('X-App-Usage'))):
        pause('app', meta_app_id(), APP_PAUSE)
        paused = True
    business = _usage(response.headers.get('X-Business-Use-Case-Usage'))
    for object_id, entries in (business.items() if isinstance(business, dict) else ()):
        for entry in entries if isinstance(entries, list) else ():
            if _over(entry):
                minutes = entry.get('estimated_time_to_regain_access')
                pause('object', object_id, int(minutes) * 60 if isinstance(minutes, int) and minutes > 0 else APP_PAUSE)
                paused = True
    return paused


def answer_of(response, who: str) -> dict:
    """A Graph answer (Facebook's or Instagram's) as JSON, or the failure its error code means. Every answer's usage
    headers are read first, so the next copy waits before Graph would refuse it."""
    noted = note_usage(response)
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code < 400 and isinstance(body, dict):
        return body
    error = body.get('error') if isinstance(body, dict) else None
    error = error if isinstance(error, dict) else {}
    if error.get('code') in RATE_CODES and not noted:   # a limit whose headers named nothing: hold the app a while
        pause('app', meta_app_id(), UNNAMED_PAUSE)
    message = f"{who}: {str(error.get('error_user_msg') or error.get('message') or f'HTTP {response.status_code}')[:200]}"
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
        return current('facebook') is not None

    def fits(self, post) -> bool:
        return True

    def authorize_url(self, variant, state, redirect_uri, pending, hint=''):
        version = current_app.config.get('FACEBOOK_GRAPH_VERSION') or 'v27.0'
        return f'https://www.facebook.com/{version}/dialog/oauth?' + urlencode({
            'client_id': meta_app().client_id, 'redirect_uri': redirect_uri, 'state': state,
            'scope': SCOPES, 'response_type': 'code'})

    def finish(self, variant, args, redirect_uri, pending):
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
        pages = graph('GET', 'me/accounts', user_token, {'fields': 'id,name,access_token,tasks', 'limit': 100})
        accounts = []
        for page in pages.get('data') or []:
            if isinstance(page, dict) and page.get('access_token') and 'CREATE_CONTENT' in (page.get('tasks') or []):
                accounts.append(Account(remote_id=str(page['id']), remote_name=str(page.get('name') or page['id']),
                                        tokens={'page_token': page['access_token'], 'user_token': user_token,
                                                'user_id': str(me.get('id', '')), 'expires_at': None}))
        return accounts

    def paused_until(self, connection):
        return paused_until(connection.remote_id)

    def send(self, post, tokens, connection, progress=None):
        """Unpublished photos and video parts may be sent twice harmlessly; the call that publishes may not, so
        no answer to it makes the copy unconfirmed (spec 19.2)."""
        page, token = connection.remote_id, tokens['page_token']
        if uploaded_video(post) and not post.content_warning:
            return self._send_video(post, page, token)
        files = [] if post.content_warning else image_files(post)[:MAX_PHOTOS]
        photos = [photo for photo in (_photo(file) for file in files) if photo]
        message = message_of(post)
        if len(photos) == 1:
            with publishing(self.label):
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
            with publishing(self.label):
                post_id = graph('POST', f'{page}/feed', token, data=fields)['id']
        return Sent(remote_id=str(post_id), remote_url=f'https://www.facebook.com/{post_id}')

    def _send_video(self, post, page: str, token: str) -> Sent:
        """The stored video by Graph's chunked upload (spec 18.3, as Mixpost does it): start, then each chunk Graph
        asks for by its offsets, then finish with the post's text as the description."""
        mime = (media_of(post) or {}).get('mime') or 'video/mp4'
        with StoredVideo(post, MAX_VIDEO_BYTES) as video:
            size = os.fstat(video.fileno()).st_size
            start = graph('POST', f'{page}/videos', token, data={'upload_phase': 'start', 'file_size': str(size)})
            session, video_id = start.get('upload_session_id'), start.get('video_id')
            if not session or not video_id:
                raise Transient(_('Facebook opened no video upload.'))
            begin, end = int(start.get('start_offset', 0)), int(start.get('end_offset', 0))
            while begin < end:
                video.seek(begin)
                step = graph('POST', f'{page}/videos', token, seconds=300,
                             data={'upload_phase': 'transfer', 'upload_session_id': session, 'start_offset': str(begin)},
                             files={'video_file_chunk': ('video', video.read(end - begin), mime)})
                following = int(step.get('start_offset', begin)), int(step.get('end_offset', end))
                if following[0] <= begin and following[0] < following[1]:
                    raise Transient(_('Facebook stopped taking the video part way.'))
                begin, end = following
        with publishing(self.label):
            done = graph('POST', f'{page}/videos', token, seconds=120, data={
                'upload_phase': 'finish', 'upload_session_id': session, 'description': message_of(post)})
        if not done.get('success'):
            raise Transient(_('Facebook did not finish the video.'))
        return Sent(remote_id=f'{VIDEO_PREFIX}{video_id}', remote_url=f'https://www.facebook.com/{page}/videos/{video_id}')

    def edit(self, post, tokens, remote_id):
        if remote_id.startswith(VIDEO_PREFIX):
            graph('POST', remote_id.removeprefix(VIDEO_PREFIX), tokens['page_token'],
                  data={'description': message_of(post)})
        else:
            graph('POST', remote_id, tokens['page_token'], data={'message': message_of(post)})

    def delete(self, tokens, remote_id):
        graph('DELETE', remote_id.removeprefix(VIDEO_PREFIX), tokens['page_token'])

    def revoke(self, tokens):
        if tokens.get('user_token') and tokens.get('user_id'):
            graph('DELETE', f"{tokens['user_id']}/permissions", tokens['user_token'])


SERVICE = register(FacebookService())

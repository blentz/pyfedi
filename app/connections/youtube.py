"""YouTube (spec 2026-11-13-connected-accounts-design.md §2, §6, §8).

Google OAuth with PKCE and offline access, scope `youtube` (videos.update and videos.delete need it; upload alone
would need only youtube.upload). The channel is the member's own (`channels?mine=true`). Access tokens last an hour
and are refreshed with the refresh token; an `invalid_grant` means reconnect.

What a post becomes: its uploaded video, uploaded whole through a resumable session (PUT of the stored original,
streamed; a failed upload is tried again from the start by the sweep), titled with the post's title, described with
its text and link back, tagged with its tags, public and not made for kids. Edits change the title, description and
tags; deletes delete. The poster frame becomes the thumbnail when YouTube takes it (best effort).

Every Google project may upload 100 videos a day (§2, reset at midnight Pacific), shared by every member of the
instance: a Redis counter by Pacific date keeps the count, and past it a copy waits for the next day (QuotaWait),
as it does when YouTube itself answers that the quota is spent.
"""
import base64
import hashlib
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from flask import current_app
from flask_babel import _

import app as app_pkg
from app.connections import (Account, AuthFailed, Permanent, Progress, QuotaWait, Sent, Service, Transient, register)
from app.connections.apps import App, current
from app.connections.content import StoredVideo, cut, plain_text
from app.connections.eligibility import uploaded_video
from app.connections.http import fetch
from app.constants import POST_TYPE_VIDEO
from app.models import media_of

AUTHORIZE = 'https://accounts.google.com/o/oauth2/v2/auth'
TOKEN = 'https://oauth2.googleapis.com/token'
REVOKE = 'https://oauth2.googleapis.com/revoke'
API = 'https://www.googleapis.com/youtube/v3'
UPLOAD = 'https://www.googleapis.com/upload/youtube/v3'
SCOPE = 'https://www.googleapis.com/auth/youtube'
PACIFIC = ZoneInfo('America/Los_Angeles')
MAX_TITLE = 100
MAX_DESCRIPTION = 5000      # bytes, in YouTube's count; the cut below counts characters, so it keeps to 1250 x 4
MAX_TAGS = 500              # characters of all tags together
CATEGORY = '22'             # People & Blogs: required on update, and a neutral default
QUOTA_REASONS = {'quotaExceeded', 'dailyLimitExceeded', 'uploadLimitExceeded', 'rateLimitExceeded'}
CHUNK = 1024 * 1024
MAX_VIDEO_BYTES = 256 * 1024 ** 3   # what videos.insert takes


# --------------------------------------------------------------------------
# Google's answers
# --------------------------------------------------------------------------

def google_app() -> App:
    """The Google client this call runs under: the context domain's own, or the instance's (spec 18)."""
    app = current('youtube')
    if app is None:
        raise Permanent(_('No Google app is set up here.'))
    return app


def next_reset() -> datetime:
    """The next midnight Pacific, as the naive UTC time the database keeps."""
    now = datetime.now(PACIFIC)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=1, second=0, microsecond=0)
    return midnight.astimezone(timezone.utc).replace(tzinfo=None)


def answer_of(response) -> dict:
    """A Google answer as JSON, or the failure it means: quota (wait for the reset), the token (reconnect), later, or
    refused."""
    try:
        body = response.json() if response.content else {}
    except ValueError:
        body = {}
    if response.status_code < 400:
        return body if isinstance(body, dict) else {}
    error = body.get('error') if isinstance(body, dict) else None
    detail = error if isinstance(error, dict) else {}
    reasons = {item.get('reason') for item in detail.get('errors') or [] if isinstance(item, dict)}
    text = detail.get('message') or (error if isinstance(error, str) else None) or \
        (body.get('error_description') if isinstance(body, dict) else None) or f'HTTP {response.status_code}'
    message = f'YouTube: {str(text)[:200]}'
    if reasons & QUOTA_REASONS:
        raise QuotaWait(_("Waiting for YouTube's daily upload allowance."), next_reset())
    if response.status_code == 401 or error == 'invalid_grant' or \
            reasons & {'forbidden', 'insufficientPermissions', 'authError'}:
        raise AuthFailed(message)
    if response.status_code == 429 or response.status_code >= 500:
        raise Transient(message)
    raise Permanent(message)


def api(method: str, url: str, token: str, **kwargs) -> dict:
    headers = {'Authorization': f'Bearer {token}', **kwargs.pop('headers', {})}
    return answer_of(fetch(method, url, headers=headers, **kwargs))


# --------------------------------------------------------------------------
# the daily allowance
# --------------------------------------------------------------------------

def take_upload() -> None:
    """Count one upload against today's allowance (Pacific date), or wait for the reset when it is spent."""
    app = google_app()   # each Google project has its own allowance (spec 18.2)
    configured = app.get('daily_uploads', current_app.config.get('YOUTUBE_DAILY_UPLOADS'))
    limit = 100 if configured is None else int(configured)   # 0 is a setting too: no uploads at all
    key = f"connections:youtube:uploads:{app.client_id}:{datetime.now(PACIFIC).date().isoformat()}"
    count = app_pkg.redis_client.incr(key)
    app_pkg.redis_client.expire(key, 2 * 24 * 3600)
    if count > limit:
        raise QuotaWait(_("Waiting for YouTube's daily upload allowance."), next_reset())


# --------------------------------------------------------------------------
# what a post becomes
# --------------------------------------------------------------------------

def _plain(text: str) -> str:
    return text.replace('<', '‹').replace('>', '›')   # YouTube refuses < and > in titles and descriptions


def snippet_of(post) -> dict:
    text = plain_text(post)
    title = (post.title or '').strip() or text.split('\n', 1)[0].strip() or _('Video')
    link = post.ap_id
    description = f'{cut(_plain(text), MAX_DESCRIPTION // 4 - len(link) - 2)}\n\n{link}'.strip()
    tags, used = [], 0
    for tag in post.tags or []:
        name = getattr(tag, 'name', None)
        if name and used + len(name) + 1 <= MAX_TAGS:
            tags.append(name)
            used += len(name) + 1
    return {'title': cut(_plain(title), MAX_TITLE), 'description': description, 'tags': tags,
            'categoryId': CATEGORY}


def _chunks(handle):
    while chunk := handle.read(CHUNK):
        yield chunk


# --------------------------------------------------------------------------
# the service
# --------------------------------------------------------------------------

class YouTubeService(Service):
    name = 'youtube'
    label = 'YouTube'
    can_edit = True

    def configured(self) -> bool:
        return current('youtube') is not None

    def fits(self, post) -> bool:
        return uploaded_video(post)

    def fits_type(self, post_type: int) -> bool:
        return post_type == POST_TYPE_VIDEO

    def authorize_url(self, variant, state, redirect_uri, pending, hint=''):
        verifier = secrets.token_urlsafe(48)
        pending['verifier'] = verifier
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        return f'{AUTHORIZE}?' + urlencode({
            'client_id': google_app().client_id, 'redirect_uri': redirect_uri,
            'response_type': 'code', 'scope': SCOPE, 'state': state, 'access_type': 'offline', 'prompt': 'consent',
            'code_challenge': challenge, 'code_challenge_method': 'S256'})

    def finish(self, variant, args, redirect_uri, pending):
        answer = answer_of(fetch('POST', TOKEN, data={
            'code': args.get('code', ''), 'client_id': google_app().client_id,
            'client_secret': google_app().get('client_secret'), 'redirect_uri': redirect_uri,
            'grant_type': 'authorization_code', 'code_verifier': pending.get('verifier', '')}))
        if not answer.get('access_token') or not answer.get('refresh_token'):
            raise Permanent(_('Google gave no lasting access. Try again, and allow PieFed to manage your videos.'))
        channels = api('GET', f'{API}/channels', answer['access_token'], params={'part': 'snippet', 'mine': 'true'})
        items = channels.get('items') or []
        if not items:
            raise Permanent(_('That Google account has no YouTube channel.'))
        channel = items[0]
        tokens = {'access_token': answer['access_token'], 'refresh_token': answer['refresh_token'],
                  'expires_at': time.time() + int(answer.get('expires_in') or 3600)}
        return [Account(remote_id=str(channel['id']),
                        remote_name=str((channel.get('snippet') or {}).get('title') or channel['id']), tokens=tokens)]

    def refresh(self, tokens):
        answer = answer_of(fetch('POST', TOKEN, data={
            'client_id': google_app().client_id,
            'client_secret': google_app().get('client_secret'),
            'refresh_token': tokens['refresh_token'], 'grant_type': 'refresh_token'}))
        if not answer.get('access_token'):
            raise AuthFailed(_('Google would not renew this connection.'))
        return {**tokens, 'access_token': answer['access_token'],
                'refresh_token': answer.get('refresh_token') or tokens['refresh_token'],
                'expires_at': time.time() + int(answer.get('expires_in') or 3600)}

    def send(self, post, tokens, connection, progress=None):
        """The video by a resumable upload whose address is kept (spec 19.2): an attempt after one that got no answer
        asks the upload how far that one got and carries on, or reads the finished video from it, so a video is never
        uploaded twice. Only a new upload counts against the day's allowance."""
        progress = progress or Progress()
        token = tokens['access_token']
        media = media_of(post) or {}
        with StoredVideo(post, MAX_VIDEO_BYTES) as video:
            size = os.fstat(video.fileno()).st_size
            location = progress.data.get('upload')
            uploaded = self._resume(location, token, video, size, media) if location else None
            if uploaded is None:
                location = progress.data['upload'] = self._open_upload(post, token, media)
                progress.save()
                uploaded = self._put(location, token, video, 0, size, media)
        video_id = uploaded.get('id')
        if not video_id:
            raise Transient(_('YouTube took the video but named no id.'))
        self._thumbnail(post, token, video_id)
        return Sent(remote_id=str(video_id), remote_url=f'https://www.youtube.com/watch?v={video_id}')

    def _open_upload(self, post, token, media) -> str:
        take_upload()
        session = fetch('POST', f'{UPLOAD}/videos', params={'uploadType': 'resumable', 'part': 'snippet,status'},
                        json={'snippet': snippet_of(post),
                              'status': {'privacyStatus': 'public', 'selfDeclaredMadeForKids': False}},
                        headers={'Authorization': f'Bearer {token}',
                                 'X-Upload-Content-Type': media.get('mime') or 'video/*'})
        answer_of(session)
        location = session.headers.get('Location')
        if not location or not location.startswith(f'{UPLOAD}/'):
            raise Transient(_('YouTube opened no upload.'))
        return location

    def _put(self, location, token, video, offset: int, size: int, media) -> dict:
        """The video from `offset` on."""
        video.seek(offset)
        headers = {'Content-Type': media.get('mime') or 'application/octet-stream', 'Content-Length': str(size - offset)}
        if offset:
            headers['Content-Range'] = f'bytes {offset}-{size - 1}/{size}'
        return api('PUT', location, token, content=_chunks(video), seconds=600 + (size - offset) // 500_000,
                   headers=headers)

    def _resume(self, location, token, video, size: int, media) -> dict | None:
        """What became of an earlier attempt's upload: the video, when it finished or once the rest is sent; None
        when the upload is gone and a new one has to start."""
        status = fetch('PUT', location, headers={'Authorization': f'Bearer {token}', 'Content-Length': '0',
                                                 'Content-Range': f'bytes */{size}'})
        if status.status_code in (404, 410):
            return None
        if status.status_code != 308:   # finished (200, 201), or an error answer_of raises
            return answer_of(status)
        received = re.fullmatch(r'bytes=0-(\d+)', status.headers.get('Range', ''))
        return self._put(location, token, video, int(received.group(1)) + 1 if received else 0, size, media)

    def _thumbnail(self, post, token, video_id) -> None:
        """The post's poster frame as the video's thumbnail: a channel that may not set one says so, and that is
        no reason to fail the copy."""
        image = post.image if post.image_id else None
        path = image.file_path if image is not None else None
        if not path or not os.path.isfile(path):
            return
        with open(path, 'rb') as handle:
            data = handle.read()
        try:
            api('POST', f'{UPLOAD}/thumbnails/set', token, params={'videoId': video_id}, content=data,
                headers={'Content-Type': 'image/jpeg'})
        except (Permanent, Transient, AuthFailed, QuotaWait):
            pass

    def edit(self, post, tokens, remote_id):
        api('PUT', f'{API}/videos', tokens['access_token'], params={'part': 'snippet'},
            json={'id': remote_id, 'snippet': snippet_of(post)})

    def delete(self, tokens, remote_id):
        api('DELETE', f'{API}/videos', tokens['access_token'], params={'id': remote_id})

    def revoke(self, tokens):
        if tokens.get('refresh_token'):
            answer_of(fetch('POST', REVOKE, data={'token': tokens['refresh_token']}))


SERVICE = register(YouTubeService())

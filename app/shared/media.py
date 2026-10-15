"""Uploaded video and audio (spec §3.4, §5.1, A13): who may upload, how big, what type, and the media record."""
import os
import shutil
import uuid

from flask import current_app

from app.constants import (COMMUNITY_KIND_PODCAST, COMMUNITY_KIND_VIDEO_CHANNEL, MEDIA_KIND_AUDIO,
                           MEDIA_KIND_VIDEO, POST_STATUS_PROCESSING)
from app.models import served_path
from app.shared.tasks import task_selector
from app.utils import can_upload_audio, can_upload_video, ensure_directory_exists, get_setting, gibberish

SNIFF_BYTES = 64
DEFAULT_LIMITS = {MEDIA_KIND_VIDEO: 524288000, MEDIA_KIND_AUDIO: 209715200}
_KIND_OF_COMMUNITY = {COMMUNITY_KIND_VIDEO_CHANNEL: MEDIA_KIND_VIDEO, COMMUNITY_KIND_PODCAST: MEDIA_KIND_AUDIO}
_SWITCH = {MEDIA_KIND_VIDEO: can_upload_video, MEDIA_KIND_AUDIO: can_upload_audio}
_AUDIO_BRANDS = (b'M4A ', b'M4B ')
MEDIA_EXT = {'video/mp4': '.mp4', 'video/quicktime': '.mov', 'video/webm': '.webm',
             'audio/mpeg': '.mp3', 'audio/mp4': '.m4a', 'audio/ogg': '.ogg'}
MEDIA_DIRECTORY = 'app/static/media/posts'
COPY_CHUNK = 1024 * 1024


class MediaRefused(Exception):
    """store_uploaded_media's refusal, raised by edit_post so the web form can show it as a field error."""


def media_kind_for(community) -> str | None:
    return _KIND_OF_COMMUNITY.get(community.kind)


def can_upload_media(user, community) -> bool:
    kind = media_kind_for(community)
    return kind is not None and community.is_moderator(user) and _SWITCH[kind](user)


def upload_byte_limit(kind: str) -> int:
    return int(get_setting(f'max_{kind}_upload_bytes', DEFAULT_LIMITS[kind]))


def sniff_media_mime(head: bytes, kind: str) -> str | None:
    """The type the first bytes prove, if it is one `kind` accepts. Extension and client mime are never read."""
    if len(head) >= 12 and head[4:8] == b'ftyp':
        brand = head[8:12]
        if brand in _AUDIO_BRANDS:
            mime = 'audio/mp4'
        elif brand == b'qt  ':
            mime = 'video/quicktime'
        else:
            mime = 'video/mp4'
    elif head[:4] == b'\x1a\x45\xdf\xa3':
        mime = 'video/webm' if b'webm' in head[:SNIFF_BYTES] else None
    elif head[:4] == b'OggS':
        mime = 'audio/ogg'
    elif head[:3] == b'ID3' or (len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0):
        mime = 'audio/mpeg'
    else:
        mime = None
    if mime is None or not mime.startswith(kind + '/'):
        return None
    return mime


def media_of(post) -> dict | None:
    extensions = post.extensions if isinstance(post.extensions, dict) else {}
    media = extensions.get('media')
    return media if isinstance(media, dict) else None


def with_media(post, **fields) -> None:
    extensions = dict(post.extensions) if isinstance(post.extensions, dict) else {}
    extensions['media'] = {**(media_of(post) or {}), **fields}
    post.extensions = extensions   # new dicts: db.JSON does not track in-place changes


def uploaded_size(uploaded_file) -> int:
    """The exact size of a parsed upload, read from its stream without loading it."""
    stream = uploaded_file.stream
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    stream.seek(0)
    return size


def store_uploaded_media(post, uploaded_file, kind) -> str | None:
    """Store an upload as `post`'s media (A13.4) and leave the post processing (A13.1).

    Returns the refusal to show the uploader, or None once the file is stored. Nothing is written before both
    checks pass: the first bytes must prove a type `kind` accepts, and the size must be within its limit."""
    stream = uploaded_file.stream
    stream.seek(0)
    mime = sniff_media_mime(stream.read(SNIFF_BYTES), kind)
    if mime is None:
        return f'This is not a supported {kind} file.'
    size = uploaded_size(uploaded_file)
    if size > upload_byte_limit(kind):   # the request cap includes form slack; this is the exact limit
        return f'This {kind} file is too large.'
    name = gibberish(15)
    directory = f'{MEDIA_DIRECTORY}/{name[0:2]}/{name[2:4]}'
    ensure_directory_exists(directory)
    path = f'{directory}/{name}{MEDIA_EXT[mime]}'
    with open(path, 'wb') as out:
        shutil.copyfileobj(stream, out, COPY_CHUNK)
    post.url = f"{current_app.config['SERVER_URL']}{served_path(path)}"
    post.status = POST_STATUS_PROCESSING
    with_media(post, status='processing', uuid=str(uuid.uuid4()), mime=mime, bytes=size)
    if kind == MEDIA_KIND_AUDIO:
        post.extensions = {**post.extensions, 'media_type': 'audio'}   # A13.5: the shape inbound Audio gets
    return None


def media_local_path(post) -> str | None:
    """The local file behind `post.url`, when it is media this server stored."""
    prefix = current_app.config['SERVER_URL'] + served_path(MEDIA_DIRECTORY) + '/'
    if not isinstance(post.url, str) or not post.url.startswith(prefix):
        return None
    return 'app' + post.url[len(current_app.config['SERVER_URL']):]


def queue_probe(post_id) -> None:
    """Hand a stored upload to probe_media on the background queue (A13.9)."""
    task_selector('probe_media', post_id=post_id)

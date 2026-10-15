"""Uploaded video and audio (spec §3.4, §5.1, A13): who may upload, how big, what type, and the media record."""
from app.constants import (COMMUNITY_KIND_PODCAST, COMMUNITY_KIND_VIDEO_CHANNEL, MEDIA_KIND_AUDIO,
                           MEDIA_KIND_VIDEO)
from app.utils import can_upload_audio, can_upload_video, get_setting

SNIFF_BYTES = 64
DEFAULT_LIMITS = {MEDIA_KIND_VIDEO: 524288000, MEDIA_KIND_AUDIO: 209715200}
_KIND_OF_COMMUNITY = {COMMUNITY_KIND_VIDEO_CHANNEL: MEDIA_KIND_VIDEO, COMMUNITY_KIND_PODCAST: MEDIA_KIND_AUDIO}
_SWITCH = {MEDIA_KIND_VIDEO: can_upload_video, MEDIA_KIND_AUDIO: can_upload_audio}
_AUDIO_BRANDS = (b'M4A ', b'M4B ')


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

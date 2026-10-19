"""Signed URLs and cache file names for the onion remote-media proxy (onion media proxy spec 2026-10-19, section 3.2).

An onion page never links a remote fediverse image directly, because the visitor's Tor browser would then reach the
remote server. It links `/onion_media/<sig>/<key>`, where `<key>` is the remote URL in unpadded urlsafe base64 and
`<sig>` is an HMAC of that URL. The server fetches only URLs it signed, so the route is not an open proxy.

The cache file name is the sha256 of the remote URL plus an extension chosen from the response type, so Caddy's
`file_server` sends the right Content-Type without a record of the type kept beside the file.

`kind` sorts an allowed type into images and video/audio, which have separate size caps. The fill itself is in
app/onion_media_fill.py: it needs app.utils, which imports app.models, which imports this module through
app.storefront.
"""
import base64
import hashlib
import hmac
import pathlib

from flask import current_app

EXTENSIONS = {'image/jpeg': '.jpg', 'image/png': '.png', 'image/gif': '.gif', 'image/webp': '.webp',
              'image/avif': '.avif', 'image/apng': '.apng', 'video/mp4': '.mp4', 'video/webm': '.webm',
              'video/ogg': '.ogv', 'audio/mpeg': '.mp3', 'audio/ogg': '.ogg', 'audio/mp4': '.m4a',
              'audio/wav': '.wav', 'audio/webm': '.weba', 'audio/flac': '.flac'}


def _sign(remote: str) -> str:
    key = hashlib.sha256(f"{current_app.config['SECRET_KEY']}:onion-media".encode()).digest()
    return hmac.new(key, remote.encode(), hashlib.sha256).hexdigest()


def proxy_url(remote: str) -> str:
    key = base64.urlsafe_b64encode(remote.encode()).decode().rstrip('=')
    return f'/onion_media/{_sign(remote)}/{key}'


def remote_for(sig: str, key: str) -> str | None:
    """The remote URL when `sig` signs it, else None. Never raises."""
    try:
        remote = base64.urlsafe_b64decode(key + '=' * (-len(key) % 4)).decode()
        if hmac.compare_digest(sig.encode(), _sign(remote).encode()):
            return remote
    except ValueError:      # bad base64, non-UTF-8 bytes (UnicodeDecodeError is a ValueError)
        pass
    return None


def cache_name(remote: str, content_type: str) -> str:
    """Raises KeyError for a type that is not allowed."""
    return hashlib.sha256(remote.encode()).hexdigest() + EXTENSIONS[content_type]


def cached_file(remote: str) -> pathlib.Path | None:
    directory = pathlib.Path(current_app.config['MEDIA_PROXY_DIR'])
    for content_type in EXTENSIONS:
        path = directory / cache_name(remote, content_type)
        if path.exists():
            return path
    return None


def kind(content_type: str) -> str | None:
    """'image' or 'av' (video or audio) for a type the proxy keeps, else None. Parameters and case are ignored."""
    content_type = content_type.split(';')[0].strip().lower()
    if content_type not in EXTENSIONS:
        return None
    return 'image' if content_type.startswith('image/') else 'av'

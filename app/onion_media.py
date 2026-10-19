"""Signed URLs and cache file names for the onion remote-media proxy (onion media proxy spec 2026-10-19, section 3.2).

An onion page never links a remote fediverse image directly, because the visitor's Tor browser would then reach the
remote server. It links `/onion_media/<sig>/<key>`, where `<key>` is the remote URL in unpadded urlsafe base64 and
`<sig>` is an HMAC of that URL. The server fetches only URLs it signed, so the route is not an open proxy.

The cache file name is the sha256 of the remote URL plus an extension chosen from the response type, so Caddy's
`file_server` sends the right Content-Type without a record of the type kept beside the file.

`kind` sorts an allowed type into images and video/audio, which have separate size caps. `url_kind` guesses the
same from a URL before anything is fetched, so the route can choose to fill in the request or in the background.
`evict` keeps the cache under the admin's size cap by deleting the least recently used files.
The fill itself is in app/onion_media_fill.py: it needs app.utils, which imports app.models, which imports this
module through app.storefront.
"""
import base64
import hashlib
import hmac
import os
import pathlib
import posixpath
import re
import time
from urllib.parse import urlsplit

from flask import current_app

EXTENSIONS = {'image/jpeg': '.jpg', 'image/png': '.png', 'image/gif': '.gif', 'image/webp': '.webp',
              'image/avif': '.avif', 'image/apng': '.apng', 'video/mp4': '.mp4', 'video/webm': '.webm',
              'video/ogg': '.ogv', 'audio/mpeg': '.mp3', 'audio/ogg': '.ogg', 'audio/mp4': '.m4a',
              'audio/wav': '.wav', 'audio/webm': '.weba', 'audio/flac': '.flac'}

CACHE_FILE = re.compile(r'[0-9a-f]{64}\.[a-z0-9]+')
TEMPORARY_FILE = re.compile(r'\.tmp-[0-9a-f]{32}')
TEMPORARY_MAX_AGE = 3600        # seconds; a fill that is still running does not take an hour
EVICT_TARGET = 0.9


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


def url_kind(remote: str) -> str:
    """'image' or 'av' by the extension of the URL's path (one of EXTENSIONS, any case), else 'unknown'. A pict-rs
    `/pictrs/image/` path has no extension and is an image."""
    path = urlsplit(remote).path
    extension = posixpath.splitext(path)[1].lower()
    for content_type, known in EXTENSIONS.items():
        if extension == known:
            return 'image' if content_type.startswith('image/') else 'av'
    return 'image' if '/pictrs/image/' in path else 'unknown'


def evict(cap_bytes: int) -> int:
    """Bytes freed. Deletes `.tmp-*` files older than an hour. When the cache files total more than `cap_bytes`, deletes
    the oldest by mtime (a hit refreshes it) until the total is at most 90% of the cap. Only regular files named like a
    cache file or a temporary one are looked at, and a symlink is never followed, so anything else in the directory stays."""
    freed = 0
    cached = []
    total = 0
    now = time.time()
    try:
        entries = list(os.scandir(current_app.config['MEDIA_PROXY_DIR']))
    except FileNotFoundError:
        return 0
    for entry in entries:
        if not entry.is_file(follow_symlinks=False):
            continue
        try:
            info = entry.stat(follow_symlinks=False)
            if TEMPORARY_FILE.fullmatch(entry.name):
                if now - info.st_mtime > TEMPORARY_MAX_AGE:
                    os.unlink(entry.path)
                    freed += info.st_size
            elif CACHE_FILE.fullmatch(entry.name):
                cached.append((info.st_mtime, info.st_size, entry.path))
                total += info.st_size
        except FileNotFoundError:       # a concurrent eviction or fill removed it
            continue
    if total > cap_bytes:
        for _mtime, size, path in sorted(cached):
            if total <= cap_bytes * EVICT_TARGET:
                break
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            total -= size
            freed += size
    return freed

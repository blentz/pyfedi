"""Search PeerTube videos across the network through SepiaSearch (interop D24). Only when the viewer ticks "Include
results from the wider network" and the admin allows it: the search text leaves this server. Results are cached ten
minutes per query; a failure shows nothing and is not cached."""
import hashlib
from urllib.parse import urlsplit, urlunsplit

from flask import current_app

from app import cache, db
from app.discovery import sources
from app.discovery.filters import host_is_excluded
from app.models import Post
from app.post.util import hosted_post_for_viewer
from app.storefront import is_hosted_uri

SEPIASEARCH_VIDEOS_URL = 'https://sepiasearch.org/api/v1/search/videos'
RESULT_LIMIT = 10
TIMEOUT_SECONDS = 3
CACHE_SECONDS = 600
MAX_BYTES = 512 * 1024


def clean_video_url(url) -> tuple[str, str] | None:
    """(rebuilt url, host) for a plain https video link, or None. No backslash, control character or space, and the
    authority is the bare host (no userinfo, no port), so no parser can read another host into it."""
    if not isinstance(url, str) or '\\' in url or any(ord(c) <= 0x20 or ord(c) == 0x7f for c in url):
        return None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    host = parsed.netloc.lower()
    if parsed.scheme != 'https' or not sources.is_hostname(host):
        return None
    return urlunsplit(('https', host, parsed.path, parsed.query, '')), host


def video_from(raw) -> dict | None:
    """One SepiaSearch video, or None. Its url must be https on the host its channel names, so a row cannot send a
    click to a third party."""
    if not isinstance(raw, dict) or not isinstance(raw.get('channel'), dict):
        return None
    url, host, title = raw.get('url'), raw['channel'].get('host'), raw.get('name')
    if not isinstance(host, str) or not sources.is_hostname(host.lower()):
        return None
    if not isinstance(title, str) or not title.strip():
        return None
    cleaned = clean_video_url(url)
    if cleaned is None or cleaned[1] != host.lower():
        return None
    url, host = cleaned
    return {'url': url, 'title': title.strip(),
            'channel': sources.display_name(raw['channel'].get('displayName'), host), 'host': host,
            'nsfw': raw.get('nsfw') is True}


def _cache_key(q: str) -> str:
    return 'discovery:videos:' + hashlib.sha1(q.lower().encode()).hexdigest()


def _videos_for(q: str) -> list:
    key = _cache_key(q)
    cached = cache.get(key)
    if cached is not None:
        return cached
    payload = sources.fetch_json(SEPIASEARCH_VIDEOS_URL, params={'search': q, 'count': RESULT_LIMIT},
                                 max_bytes=MAX_BYTES, max_seconds=TIMEOUT_SECONDS)
    data = payload.get('data') if isinstance(payload, dict) else None
    if not isinstance(data, list):
        if payload is not None:
            current_app.logger.info('discovery: sepiasearch video search answered with no data list')
        return []
    videos = [video for video in map(video_from, data) if video is not None]
    cache.set(key, videos, timeout=CACHE_SECONDS)
    return videos


def search_videos(q: str, allow_nsfw: bool) -> list:
    q = (q or '').strip()
    if not q:
        return []
    videos, local_post_ids = [], set()
    for video in _videos_for(q)[:RESULT_LIMIT]:   # the cut comes first: each own-host hit below costs queries
        if not (allow_nsfw or not video['nsfw']):
            continue
        if is_hosted_uri(video['url']):
            # one of our own domains: listed when the viewer may open the local post it names (opened from the
            # database, never fetched), and once however many URLs name it
            post = hosted_post_for_viewer(video['url'])
            if post is None or post.id in local_post_ids:
                continue
            local_post_ids.add(post.id)
        elif host_is_excluded(video['host'], frozenset()):
            continue
        videos.append(video)
    remote = [v for v in videos if not is_hosted_uri(v['url'])]
    if remote:
        stored = {ap_id for (ap_id,) in db.session.query(Post.ap_id).filter(
            Post.ap_id.in_([v['url'] for v in remote]))}
        videos = [v for v in videos if v['url'] not in stored]
    return videos

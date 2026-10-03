"""Castopod podcasts from the Podcast Index API (interop D24). Used only when the admin has entered an
API key and secret (site settings, never rendered back, never logged). A podcast is kept only when its
feed opts in to ActivityPub through <podcast:socialInteract protocol="activitypub">, which Podcast Index
reports on episode items; the actor url comes from that tag's accountUrl.

Signing (Podcast Index docs): Authorization = sha1(api_key + api_secret + X-Auth-Date) in hex.
"""
import hashlib
import re
import time
from urllib.parse import urlparse

from app.discovery import KIND_COMMUNITY, sources
from app.utils import get_setting

PODCASTINDEX_API = 'https://api.podcastindex.org/api/1.0'
SOURCE = 'podcastindex'
MAX_RESULTS = 200
MAX_EPISODE_LOOKUPS = 40
SETTING_KEY = 'podcastindex_api_key'
SETTING_SECRET = 'podcastindex_api_secret'

_CASTOPOD_FEED = re.compile(r'https://[^/\s]+/@[^/\s]+/feed(?:\.xml)?')


def now_unix() -> int:
    return int(time.time())


def podcastindex_headers(api_key: str, api_secret: str, now: int) -> dict:
    stamp = str(now)
    return {'X-Auth-Key': api_key, 'X-Auth-Date': stamp,
            'Authorization': hashlib.sha1(f'{api_key}{api_secret}{stamp}'.encode('utf-8')).hexdigest()}


def is_castopod_feed(feed) -> bool:
    if not isinstance(feed, dict):
        return False
    generator, url = feed.get('generator'), feed.get('url')
    if isinstance(generator, str) and 'castopod' in generator.lower():
        return True
    return isinstance(url, str) and _CASTOPOD_FEED.fullmatch(url) is not None


def _https_host(url):
    """The hostname of a well-formed https url naming a real host, else None."""
    if not isinstance(url, str):
        return None
    try:
        parsed = urlparse(url)
        host = parsed.hostname
    except ValueError:
        return None
    if parsed.scheme != 'https' or not sources.is_hostname(host):
        return None
    return host


def actor_url_from_social_interact(items) -> str | None:
    """The first https accountUrl of an activitypub socialInteract tag on any item."""
    if not isinstance(items, list):
        return None
    for item in items:
        interacts = item.get('socialInteract') if isinstance(item, dict) else None
        if not isinstance(interacts, list):
            continue
        for interact in interacts:
            if not isinstance(interact, dict) or interact.get('protocol') != 'activitypub':
                continue
            account = interact.get('accountUrl')
            if _https_host(account) is not None:
                return account
    return None


def podcast_to_entry(feed, actor_url: str) -> dict | None:
    host = _https_host(actor_url)
    if not isinstance(feed, dict) or host is None:
        return None
    image = feed.get('artwork') or feed.get('image')
    title = feed.get('title')
    return sources.make_entry(kind=KIND_COMMUNITY, platform='castopod', actor_url=actor_url,
                              name=title if isinstance(title, str) else '', host=host,
                              avatar=image if isinstance(image, str) else None,
                              followers=sources.as_count(feed.get('trendScore')), nsfw=feed.get('explicit') is True,
                              source=SOURCE)


def _signed_json(path: str, params: dict, api_key: str, api_secret: str):
    return sources.fetch_json(f'{PODCASTINDEX_API}{path}', params=params,
                              headers=podcastindex_headers(api_key, api_secret, now_unix()))


def fetch_castopod_podcasts(exclude) -> list[dict]:
    api_key = (get_setting(SETTING_KEY, '') or '').strip()
    api_secret = (get_setting(SETTING_SECRET, '') or '').strip()
    if not api_key or not api_secret:
        return []
    trending = _signed_json('/podcasts/trending', {'max': MAX_RESULTS}, api_key, api_secret)
    feeds = trending.get('feeds') if isinstance(trending, dict) else None
    if not isinstance(feeds, list):
        raise sources.DiscoverySourceError(f'{SOURCE}: no feed list')
    entries = []
    lookups = 0
    for feed in feeds[:MAX_RESULTS]:
        if lookups >= MAX_EPISODE_LOOKUPS:
            break
        if not is_castopod_feed(feed) or isinstance(feed.get('id'), bool) or not isinstance(feed.get('id'), int):
            continue
        sources.polite_pause()
        lookups += 1
        episodes = _signed_json('/episodes/byfeedid', {'id': feed['id'], 'max': 1}, api_key, api_secret)
        actor_url = actor_url_from_social_interact(episodes.get('items') if isinstance(episodes, dict) else None)
        if actor_url is None or exclude(_https_host(actor_url)):
            continue
        entry = podcast_to_entry(feed, actor_url)
        if entry is not None:
            entries.append(entry)
    return entries

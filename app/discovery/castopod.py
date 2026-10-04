"""Castopod podcasts from the Podcast Index API (interop D24). Used only when the admin has entered an
API key and secret (site settings, never rendered back, never logged). A podcast is kept only when its
feed opts in to ActivityPub through <podcast:socialInteract protocol="activitypub">, which Podcast Index
reports on episode items. The actor url is that tag's accountUrl or, because Castopod leaves accountUrl
empty, https://host/@handle built from its accountId when the account lives on the feed's own host.

Podcast Index cannot filter by platform and its trending list holds no Castopod shows (0 of 1000 when
measured on 2026-10-04), so the feeds come from the three lists that do surface some: value-tagged feeds,
newly added feeds and a search for "castopod".

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
MAX_RESULTS = 1000
MAX_EPISODE_LOOKUPS = 40
SETTING_KEY = 'podcastindex_api_key'
SETTING_SECRET = 'podcastindex_api_secret'

_CASTOPOD_FEED = re.compile(r'https://[^/\s]+/@[^/\s]+/feed(?:\.xml)?')
_ACCOUNT_ID = re.compile(r'@([A-Za-z0-9_.-]+)@([^@/\s]+)')

FEED_LISTS = (
    ('/podcasts/bytag', {'podcast-value': '', 'max': MAX_RESULTS}),
    ('/recent/newfeeds', {'max': MAX_RESULTS}),
    ('/search/byterm', {'q': 'castopod', 'max': MAX_RESULTS}),
)


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


def _actor_url_from_account_id(account_id, feed_host) -> str | None:
    """https://host/@handle for an accountId @handle@host on the feed's own host. An account on another host
    is someone else's, and its url layout is unknown, so it is not guessed."""
    match = _ACCOUNT_ID.fullmatch(account_id) if isinstance(account_id, str) else None
    if match is None or feed_host is None or match.group(2).lower() != feed_host:
        return None
    return f'https://{feed_host}/@{match.group(1)}'


def actor_url_from_social_interact(items, feed_url) -> str | None:
    """The actor of the first activitypub socialInteract tag on any item: its https accountUrl, else the
    actor its accountId names on the feed's host."""
    if not isinstance(items, list):
        return None
    feed_host = _https_host(feed_url)
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
            actor_url = _actor_url_from_account_id(interact.get('accountId'), feed_host)
            if actor_url is not None:
                return actor_url
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
    feeds = []
    answered = False
    for path, params in FEED_LISTS:
        listed = _signed_json(path, params, api_key, api_secret)
        listed = listed.get('feeds') if isinstance(listed, dict) else None
        if isinstance(listed, list):   # a failing list is skipped; the others still count
            answered = True
            feeds += listed[:MAX_RESULTS]
    if not answered:
        raise sources.DiscoverySourceError(f'{SOURCE}: no feed list')
    entries = []
    seen = set()
    lookups = 0
    for feed in feeds:
        if lookups >= MAX_EPISODE_LOOKUPS:
            break
        if not is_castopod_feed(feed) or isinstance(feed.get('id'), bool) or not isinstance(feed.get('id'), int):
            continue
        if feed['id'] in seen:   # a feed can be on more than one list
            continue
        seen.add(feed['id'])
        sources.polite_pause()
        lookups += 1
        episodes = _signed_json('/episodes/byfeedid', {'id': feed['id'], 'max': 1}, api_key, api_secret)
        actor_url = actor_url_from_social_interact(episodes.get('items') if isinstance(episodes, dict) else None,
                                                   feed.get('url'))
        if actor_url is None or exclude(_https_host(actor_url)):
            continue
        entry = podcast_to_entry(feed, actor_url)
        if entry is not None:
            entries.append(entry)
    return entries

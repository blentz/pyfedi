"""PeerTube channels from SepiaSearch (interop D24). Public publisher channels. SepiaSearch cannot sort
by followers, so up to MAX_PAGES pages are read and ranked here."""
from urllib.parse import urlparse

from flask import current_app

from app.discovery import KIND_COMMUNITY, sources

SEPIASEARCH_URL = 'https://sepiasearch.org/api/v1/search/video-channels'
SOURCE = 'sepiasearch'
PAGE_SIZE = 100
MAX_PAGES = 5


def _largest_avatar(avatars):
    if not isinstance(avatars, list):
        return None
    best_width, best_url = -1, None
    for avatar in avatars:
        if isinstance(avatar, dict) and isinstance(avatar.get('url'), str):
            width = sources.as_count(avatar.get('width'))
            if width > best_width:
                best_width, best_url = width, avatar['url']
    return best_url


def channel_to_entry(channel) -> dict | None:
    """One SepiaSearch channel row as a normalised entry. The channel url is its ActivityPub id, and it
    must be on the host the row names, so a directory row cannot point us at a third party."""
    if not isinstance(channel, dict):
        return None
    url, host = channel.get('url'), channel.get('host')
    if not isinstance(url, str) or not isinstance(host, str) or not sources.is_hostname(host.lower()):
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme != 'https' or parsed.hostname != host.lower():
        return None
    name = channel.get('displayName') or channel.get('name')
    return sources.make_entry(kind=KIND_COMMUNITY, platform='peertube', actor_url=url,
                              name=name if isinstance(name, str) else '', host=host.lower(),
                              avatar=_largest_avatar(channel.get('avatars')),
                              followers=sources.as_count(channel.get('followersCount')), nsfw=False, source=SOURCE)


def fetch_peertube_channels(exclude) -> list[dict]:
    """At most MAX_PAGES pages of PAGE_SIZE channels, excluded hosts dropped, most-followed first."""
    entries = []
    for page in range(MAX_PAGES):
        if page:
            sources.polite_pause()
        payload = sources.fetch_json(SEPIASEARCH_URL, params={'start': page * PAGE_SIZE, 'count': PAGE_SIZE})
        data = payload.get('data') if isinstance(payload, dict) else None
        if not isinstance(data, list):
            if page == 0:
                raise sources.DiscoverySourceError(f'{SOURCE}: no channel list')
            current_app.logger.info(f'discovery: {SOURCE} page {page + 1} unreadable, paging stopped')
            break
        for channel in data:
            entry = channel_to_entry(channel)
            if entry is not None and not exclude(entry['host']):
                entries.append(entry)
        if len(data) < PAGE_SIZE:
            break
    return sorted(entries, key=lambda entry: entry['followers'], reverse=True)

"""Castopod podcasts from index.castopod.org (interop D24): its public export lists every podcast published on
Castopod, with no auth. Publishing on Castopod is the opt-in, so every live podcast whose page is a Castopod actor
(https://host/@handle, which is also its ActivityPub id) is listed, most popular first.
"""
import re
from urllib.parse import urlparse

from app.discovery import KIND_COMMUNITY, sources
from app.discovery.filters import clean_https_url

CASTOPOD_INDEX_URL = 'https://index.castopod.org/podcastindex.json'
SOURCE = 'castopod-index'
MAX_INDEX_BYTES = 16 * 1024 * 1024   # the export is about 4 MB

_CASTOPOD_ACTOR = re.compile(r'https://([^/@:?#\s]+)/@[A-Za-z0-9_.-]+')


def _actor_host(link) -> str | None:
    """The host of a clean https Castopod actor url https://host/@handle, else None. Never a port: an entry's host
    is a bare hostname by design, because it is matched against instance and domain bans, which name hosts."""
    if clean_https_url(link) is None:
        return None
    match = _CASTOPOD_ACTOR.fullmatch(link)
    host = urlparse(link).hostname if match is not None else None
    return host if sources.is_hostname(host) and host == match.group(1).lower() else None


def podcast_to_entry(podcast) -> dict | None:
    """One index.castopod.org row as a normalised entry, or None for a dead podcast or a link that is not a
    Castopod actor."""
    if not isinstance(podcast, dict) or podcast.get('dead') == 1:
        return None
    link = podcast.get('link')
    host = _actor_host(link)
    if host is None:
        return None
    title = podcast.get('title')
    return sources.make_entry(kind=KIND_COMMUNITY, platform='castopod', actor_url=link,
                              name=title if isinstance(title, str) else '', host=host,
                              avatar=sources.avatar_of(podcast.get('imageUrl')),
                              followers=sources.as_count(podcast.get('popularityScore')),
                              nsfw=podcast.get('explicit') == 1, source=SOURCE)


def fetch_castopod_podcasts(exclude) -> list[dict]:
    """Every live Castopod podcast in the index, excluded hosts dropped, most popular first."""
    podcasts = sources.fetch_json(CASTOPOD_INDEX_URL, max_bytes=MAX_INDEX_BYTES)
    if not isinstance(podcasts, list):
        raise sources.DiscoverySourceError(f'{SOURCE}: no podcast list')
    entries = []
    for podcast in podcasts:
        entry = podcast_to_entry(podcast)
        if entry is not None and not exclude(entry['host']):
            entries.append(entry)
    return sorted(entries, key=lambda entry: entry['followers'], reverse=True)

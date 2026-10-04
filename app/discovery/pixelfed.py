"""Pixelfed people (interop D24): Pixelfed hosts from FediDB, then up to MAX_PAGES pages of each host's
landing directory, which lists only public accounts marked `is_suggestable`. A 404 means the host's
admin has the directory off; that host is skipped."""
from app.discovery import KIND_PERSON, sources

FEDIDB_URL = 'https://api.fedidb.org/v1/servers'
SOURCE = 'fedidb'
MAX_SERVERS = 20
MAX_PAGES = 2
FEDIDB_LIMIT = 40


def pixelfed_hosts(payload) -> list[str]:
    """FediDB's host names, lower-cased, junk rows and repeats dropped, in FediDB's order."""
    data = payload.get('data') if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    hosts = []
    for row in data:
        if isinstance(row, dict) and isinstance(row.get('domain'), str):
            domain = row['domain'].strip().lower()
            if sources.is_hostname(domain) and domain not in hosts:
                hosts.append(domain)
    return hosts


def profile_to_entry(profile, host: str) -> dict | None:
    if not isinstance(profile, dict) or not sources.is_username(profile.get('username')):
        return None
    username = profile['username']
    return sources.make_entry(kind=KIND_PERSON, platform='pixelfed', actor_url=f'https://{host}/users/{username}',
                              name=sources.display_name(profile.get('name'), username), host=host,
                              avatar=sources.avatar_of(profile.get('avatar')),
                              followers=sources.as_count(profile.get('followers_count')), nsfw=False, source=SOURCE)


def fetch_pixelfed_people(exclude) -> list[dict]:
    """Up to MAX_PAGES directory pages from each of the first MAX_SERVERS hosts that are not excluded."""
    payload = sources.fetch_json(FEDIDB_URL, params={'software': 'pixelfed', 'limit': FEDIDB_LIMIT})
    if not isinstance(payload, dict):
        raise sources.DiscoverySourceError(f'{SOURCE}: no server list')
    entries = []
    asked_hosts = 0
    calls = 0
    for host in pixelfed_hosts(payload):
        if asked_hosts >= MAX_SERVERS:
            break
        if exclude(host):
            continue
        asked_hosts += 1
        cursor = None
        for _page in range(MAX_PAGES):
            if calls:
                sources.polite_pause()
            calls += 1
            body = sources.fetch_json(f'https://{host}/api/landing/v1/directory',
                                      params={'cursor': cursor} if cursor else None)
            data = body.get('data') if isinstance(body, dict) else None
            if not isinstance(data, list):
                break
            for profile in data:
                entry = profile_to_entry(profile, host)
                if entry is not None:
                    entries.append(entry)
            meta = body.get('meta')
            cursor = meta.get('next_cursor') if isinstance(meta, dict) else None
            if not isinstance(cursor, str) or not cursor:
                break
    return entries

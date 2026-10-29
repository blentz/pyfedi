"""Pixelfed people (interop D24): up to MAX_PAGES pages of each host's landing directory, which lists only public
accounts marked `is_suggestable`. A 404 means the host's admin has the directory off; that host is skipped.

Two sources of hosts, each refreshed and capped on its own. `pixelfed.org`: the servers pixelfed.org lists at
/servers (they applied and follow its guidelines), by user count; one it files under Adult has its people marked NSFW.
`fedidb`: FediDB's Pixelfed hosts, leaving out those the curated list covers, so no directory is read twice; if the
curated list cannot be read, none is left out."""
from app.discovery import KIND_PERSON, sources

FEDIDB_URL = 'https://api.fedidb.org/v1/servers'
SOURCE = 'fedidb'
PIXELFED_ORG_URL = 'https://pixelfed.org/api/v1/servers/all.json'
CURATED_SOURCE = 'pixelfed.org'
ADULT_CATEGORY = 'Adult'
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


def curated_hosts(payload) -> list[tuple[str, bool]]:
    """pixelfed.org's active servers as (host, filed under Adult), most users first, junk rows and repeats dropped."""
    data = payload.get('data') if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    rows, seen = [], set()
    for row in data:
        if not isinstance(row, dict) or row.get('active') is not True or not isinstance(row.get('domain'), str):
            continue
        domain = row['domain'].strip().lower()
        if not sources.is_hostname(domain) or domain in seen:
            continue
        seen.add(domain)
        stats = row.get('stats') if isinstance(row.get('stats'), dict) else {}
        rows.append((sources.as_count(stats.get('user_count')), domain, row.get('category') == ADULT_CATEGORY))
    rows.sort(key=lambda row: -row[0])   # stable: equal counts keep the list's order
    return [(domain, adult) for _users, domain, adult in rows]


def curated_servers() -> list[tuple[str, bool]] | None:
    """pixelfed.org's curated servers (curated_hosts), or None when the list cannot be read."""
    payload = sources.fetch_json(PIXELFED_ORG_URL)
    if not isinstance(payload, dict) or not isinstance(payload.get('data'), list):
        return None
    return curated_hosts(payload)


def profile_to_entry(profile, host: str, nsfw: bool = False, source: str = SOURCE) -> dict | None:
    if not isinstance(profile, dict) or not sources.is_username(profile.get('username')):
        return None
    username = profile['username']
    return sources.make_entry(kind=KIND_PERSON, platform='pixelfed', actor_url=f'https://{host}/users/{username}',
                              name=sources.display_name(profile.get('name'), username), host=host,
                              avatar=sources.avatar_of(profile.get('avatar')),
                              followers=sources.as_count(profile.get('followers_count')), nsfw=nsfw, source=source)


def fetch_pixelfed_org_people(exclude) -> list[dict]:
    """The directories of pixelfed.org's curated servers, most users first (source `pixelfed.org`)."""
    servers = curated_servers()
    if servers is None:
        raise sources.DiscoverySourceError(f'{CURATED_SOURCE}: no server list')
    return _directories(servers, exclude, CURATED_SOURCE)


def fetch_pixelfed_people(exclude) -> list[dict]:
    """The directories of FediDB's Pixelfed hosts that the curated list does not cover (source `fedidb`)."""
    payload = sources.fetch_json(FEDIDB_URL, params={'software': 'pixelfed', 'limit': FEDIDB_LIMIT})
    if not isinstance(payload, dict):
        raise sources.DiscoverySourceError(f'{SOURCE}: no server list')
    curated = {host for host, _adult in curated_servers() or []}
    return _directories([(host, False) for host in pixelfed_hosts(payload) if host not in curated], exclude, SOURCE)


def _directories(servers, exclude, source: str) -> list[dict]:
    """Up to MAX_PAGES directory pages from each of the first MAX_SERVERS of `servers`, (host, adult) pairs, that
    are not excluded; an adult host's people are marked NSFW."""
    entries = []
    asked_hosts = 0
    calls = 0
    for host, adult in servers:
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
                entry = profile_to_entry(profile, host, nsfw=adult, source=source)
                if entry is not None:
                    entries.append(entry)
            meta = body.get('meta')
            cursor = meta.get('next_cursor') if isinstance(meta, dict) else None
            if not isinstance(cursor, str) or not cursor:
                break
    return entries

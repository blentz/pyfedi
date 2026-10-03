"""Mastodon people (interop D24): the top MAX_SERVERS servers from joinmastodon.org, then one page of
each server's local profile directory. The directory lists only accounts that opted in (`discoverable`);
a row that says otherwise, or a bot, is dropped here as well."""
from urllib.parse import urlparse

from app.discovery import KIND_PERSON, sources

SERVERS_URL = 'https://api.joinmastodon.org/servers'
SOURCE = 'joinmastodon'
MAX_SERVERS = 20
DIRECTORY_LIMIT = 80


def top_mastodon_servers(servers) -> list[str]:
    """Server domains, busiest first, junk rows dropped."""
    if not isinstance(servers, list):
        return []
    rows = []
    for server in servers:
        if not isinstance(server, dict) or not isinstance(server.get('domain'), str):
            continue
        domain = server['domain'].strip().lower()
        if sources.is_hostname(domain):
            rows.append((sources.as_count(server.get('last_week_users')), domain))
    rows.sort(key=lambda row: row[0], reverse=True)
    return [domain for _users, domain in rows]


def _uri_is_on(uri, domain: str) -> bool:
    if not isinstance(uri, str):
        return False
    try:
        parsed = urlparse(uri)
        return parsed.scheme == 'https' and parsed.hostname == domain
    except ValueError:
        return False


def account_to_entry(account, domain: str) -> dict | None:
    if not isinstance(account, dict) or account.get('discoverable') is not True or account.get('bot') is True:
        return None
    username = account.get('username')
    if not sources.is_username(username):
        return None
    uri = account.get('uri')
    actor_url = uri if _uri_is_on(uri, domain) else f'https://{domain}/users/{username}'
    name = account.get('display_name')
    return sources.make_entry(kind=KIND_PERSON, platform='mastodon', actor_url=actor_url,
                              name=name if isinstance(name, str) and name.strip() else username, host=domain,
                              avatar=account.get('avatar') if isinstance(account.get('avatar'), str) else None,
                              followers=sources.as_count(account.get('followers_count')), nsfw=False, source=SOURCE)


def fetch_mastodon_people(exclude) -> list[dict]:
    """One directory page from each of the MAX_SERVERS busiest servers that are not excluded."""
    servers = sources.fetch_json(SERVERS_URL)
    if not isinstance(servers, list):
        raise sources.DiscoverySourceError(f'{SOURCE}: no server list')
    entries = []
    asked = 0
    for domain in top_mastodon_servers(servers):
        if asked >= MAX_SERVERS:
            break
        if exclude(domain):
            continue
        if asked:
            sources.polite_pause()
        asked += 1
        try:
            accounts = sources.fetch_json(f'https://{domain}/api/v1/directory',
                                          params={'local': 'true', 'order': 'active', 'limit': DIRECTORY_LIMIT})
        except sources.DiscoverySourceError:
            continue
        if not isinstance(accounts, list):
            continue
        for account in accounts:
            entry = account_to_entry(account, domain)
            if entry is not None:
                entries.append(entry)
    return entries

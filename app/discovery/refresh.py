"""The daily discovery refresh (interop D24): `flask refresh_discovery`, from daily.sh. Never on a request."""
from datetime import timedelta
from urllib.parse import urlparse

from flask import current_app
from sqlalchemy.dialects.postgresql import insert

from app import db
from app.community.util import is_bad_name
from app.discovery import KIND_COMMUNITY, KIND_PERSON, PLATFORMS, castopod, mastodon, peertube, pixelfed, sources
from app.discovery.filters import URL_LIMIT, banned_domain_names, clean_https_url, clean_name, host_is_excluded, \
    looks_nsfw, peertube_isolated_hosts
from app.models import DiscoveryEntry, utcnow

EXPIRY_DAYS = 30
MAX_PER_HOST = 20
MAX_PER_SOURCE = 500
HOST_LIMIT = 255  # DiscoveryEntry.host is String(255)

FETCHERS = {
    peertube.SOURCE: peertube.fetch_peertube_channels,
    castopod.SOURCE: castopod.fetch_castopod_podcasts,
    mastodon.SOURCE: mastodon.fetch_mastodon_people,
    pixelfed.SOURCE: pixelfed.fetch_pixelfed_people,
}

_UPDATED = ('kind', 'platform', 'name', 'host', 'avatar_url', 'followers', 'nsfw', 'source', 'last_seen')


def clean_entries(entries, exclude) -> list[dict]:
    """Validated, filtered, de-duplicated and capped copies of one source's entries, in source order."""
    kept, per_host, seen = [], {}, set()
    for entry in entries:
        if len(kept) >= MAX_PER_SOURCE:
            break
        if not isinstance(entry, dict) or entry.get('kind') not in (KIND_COMMUNITY, KIND_PERSON) \
                or entry.get('platform') not in PLATFORMS:
            continue
        if isinstance(entry.get('actor_url'), str) and len(entry['actor_url']) > URL_LIMIT:
            # DiscoveryEntry.actor_url is String(URL_LIMIT) under a unique index: one such row would fail the
            # source's whole upsert, so it is dropped here. The url itself is not logged; it may be huge.
            current_app.logger.info(f'discovery: {entry.get("source")} entry skipped: over-long actor_url '
                                    f'({len(entry["actor_url"])} characters)')
            continue
        actor_url = clean_https_url(entry.get('actor_url'))
        name = clean_name(entry.get('name'))
        host = entry.get('host').strip().lower() if isinstance(entry.get('host'), str) else ''
        if actor_url is None or name is None or len(host) > HOST_LIMIT or urlparse(actor_url).hostname != host:
            continue
        if actor_url in seen or per_host.get(host, 0) >= MAX_PER_HOST or exclude(host) or is_bad_name(name):
            continue
        seen.add(actor_url)
        per_host[host] = per_host.get(host, 0) + 1
        known = {key: entry.get(key) for key in sources.ENTRY_KEYS}   # a peer's extra keys are not carried on
        kept.append({**known, 'actor_url': actor_url, 'name': name, 'host': host,
                     'avatar': clean_https_url(entry.get('avatar')),
                     'followers': sources.as_count(entry.get('followers')),
                     'nsfw': entry.get('nsfw') is True or looks_nsfw(name)})
    return kept


def upsert_entries(entries, now) -> int:
    if not entries:
        return 0
    rows = [{'kind': e['kind'], 'platform': e['platform'], 'actor_url': e['actor_url'], 'name': e['name'],
             'host': e['host'], 'avatar_url': e['avatar'], 'followers': e['followers'], 'nsfw': e['nsfw'],
             'source': e['source'], 'first_seen': now, 'last_seen': now} for e in entries]
    statement = insert(DiscoveryEntry).values(rows)
    statement = statement.on_conflict_do_update(index_elements=['actor_url'],
                                                set_={column: statement.excluded[column] for column in _UPDATED})
    db.session.execute(statement)
    db.session.commit()
    return len(rows)


def expire_entries(now) -> int:
    expired = db.session.query(DiscoveryEntry).filter(
        DiscoveryEntry.last_seen < now - timedelta(days=EXPIRY_DAYS)).delete(synchronize_session=False)
    db.session.commit()
    return expired


def refresh_discovery(now=None) -> dict:
    now = now or utcnow()
    isolated = peertube_isolated_hosts()
    banned_domains = banned_domain_names()

    def exclude(host):
        return host_is_excluded(host, isolated, banned_domains)

    results = {}
    for source, fetch in FETCHERS.items():
        try:
            fetched = fetch(exclude)
        except sources.DiscoverySourceError as error:
            current_app.logger.warning(f'discovery: {source} skipped: {error}')
            results[source] = 'failed'
            continue
        except Exception:  # not bare: a worker shutdown must propagate. One broken source must not stop the rest.
            current_app.logger.exception(f'discovery: {source} failed')
            db.session.rollback()
            results[source] = 'failed'
            continue
        try:
            results[source] = upsert_entries(clean_entries(fetched, exclude), now)
        except Exception:
            current_app.logger.exception(f'discovery: {source} could not be stored')
            db.session.rollback()
            results[source] = 'failed'
    results['expired'] = expire_entries(now)
    return results

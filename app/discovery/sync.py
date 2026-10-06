"""Proactive sync of PeerTube channels and Castopod podcasts (interop D24,
docs/superpowers/specs/2026-10-05-proactive-discovery-sync-design.md). Each remote host's top N directory entries
are followed by the instance actor and polled daily; the rest are left alone."""
from flask import current_app
from sqlalchemy import func

from app import celery, db
from app.discovery import KIND_COMMUNITY
from app.discovery.backfill import run_backfill
from app.discovery.filters import host_is_excluded
from app.models import Community, DiscoveryEntry, DiscoverySync, utcnow
from app.utils import get_setting

SYNC_PLATFORMS = ('peertube', 'castopod')
MAX_PER_HOST = 50


def sync_per_host() -> int:
    """discovery_sync_per_host, or 0 for anything that is not an int from 0 to MAX_PER_HOST (bools included)."""
    value = get_setting('discovery_sync_per_host', 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return min(value, MAX_PER_HOST)


def sync_platforms() -> list:
    value = get_setting('discovery_sync_platforms', list(SYNC_PLATFORMS))
    if not isinstance(value, list):
        return []
    return [platform for platform in SYNC_PLATFORMS if platform in value]


def _unusable_community_urls() -> set:
    """ap_profile_ids of communities an admin banned or the remote deleted: never synced, never counted."""
    rows = db.session.query(Community.ap_profile_id).filter(
        (Community.banned == True) | (Community.ap_deleted_at != None), Community.ap_profile_id != None)
    return {url for (url,) in rows}


def desired_entries() -> dict:
    per_host, platforms = sync_per_host(), sync_platforms()
    if per_host == 0 or not platforms:
        return {}
    unusable = _unusable_community_urls()
    query = db.session.query(DiscoveryEntry).filter(DiscoveryEntry.kind == KIND_COMMUNITY,
                                                    DiscoveryEntry.platform.in_(platforms),
                                                    DiscoveryEntry.nsfw == False) \
        .order_by(func.lower(DiscoveryEntry.host), DiscoveryEntry.followers.desc(), DiscoveryEntry.name,
                  DiscoveryEntry.id)
    desired, excluded = {}, {}
    for entry in query:
        host = entry.host.lower()
        if host not in excluded:
            excluded[host] = host_is_excluded(host, frozenset())
        if excluded[host] or entry.actor_url.lower() in unusable:
            continue
        chosen = desired.setdefault(host, [])
        if len(chosen) < per_host:
            chosen.append(entry)
    return {host: entries for host, entries in desired.items() if entries}


ERROR_LIMIT = 255   # discovery_sync.last_error


def _unusable(community) -> bool:
    return community is None or community.banned or community.ap_deleted_at is not None


@celery.task
def poll_synced_community(community_id: int) -> None:
    """Re-walk one synced outbox, newest first, up to the first post already here. Failures are recorded on the row
    and left for tomorrow's run: no retry here (D738/D775)."""
    row = db.session.get(DiscoverySync, community_id)
    if row is None:
        return
    if _unusable(db.session.get(Community, community_id)):
        db.session.delete(row)
        db.session.commit()
        return
    try:
        outcome = run_backfill(community_id, stop_at_known=True)
    except Exception as error:
        current_app.logger.info(f'discovery sync: poll of community {community_id} failed: {type(error).__name__}')
        db.session.rollback()
        outcome = f'{type(error).__name__}: {error}'
    row = db.session.get(DiscoverySync, community_id)   # the backfill ran on its own task session
    row.last_polled_at = utcnow()
    row.last_error = outcome[:ERROR_LIMIT] if outcome else None
    db.session.commit()

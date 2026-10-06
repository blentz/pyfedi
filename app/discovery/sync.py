"""Proactive sync of PeerTube channels and Castopod podcasts (interop D24,
docs/superpowers/specs/2026-10-05-proactive-discovery-sync-design.md). Each remote host's top N directory entries
are followed by the instance actor and polled daily; the rest are left alone."""
from datetime import timedelta

from flask import current_app
from sqlalchemy import func

from app import celery, db
from app.activitypub.util import find_actor_or_create
from app.discovery import KIND_COMMUNITY, SYNC_NONE, SYNC_PENDING
from app.discovery.backfill import backfill_in_progress, queue_backfill, run_backfill
from app.discovery.filters import host_is_excluded
from app.discovery.instance_actor import send_instance_follow, send_instance_undo
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
        drop_row(row)
        return
    try:
        outcome = run_backfill(community_id, stop_at_known=True)
    except Exception as error:
        current_app.logger.info(f'discovery sync: poll of community {community_id} failed: {type(error).__name__}')
        db.session.rollback()
        outcome = f'{type(error).__name__}: {error}'
    row = db.session.get(DiscoverySync, community_id)   # the backfill ran on its own task session
    if row is None:   # a concurrent reconcile dropped it mid-poll
        return
    row.last_polled_at = utcnow()
    row.last_error = outcome[:ERROR_LIMIT] if outcome else None
    db.session.commit()


ADDS_PER_HOST_PER_RUN = 10   # a larger N ramps up over several days rather than bursting at one host
FOLLOW_RETRY_DAYS = 7
POLL_SPACING_SECONDS = 2


def _host(url: str) -> str:
    return url.split('/')[2].lower() if url.count('/') >= 2 else ''


def drop_row(row) -> None:
    """Unfollow and forget a synced community. The community and its posts stay."""
    community = db.session.get(Community, row.community_id)
    if community is not None:
        send_instance_undo(row, community)
    db.session.delete(row)
    db.session.commit()


def _needs_refollow(row, now) -> bool:
    if row.follow_state == SYNC_NONE:
        return True
    return row.follow_state == SYNC_PENDING and (row.followed_at is None
                                                 or row.followed_at < now - timedelta(days=FOLLOW_RETRY_DAYS))


def _add(entry, held) -> bool:
    community = db.session.query(Community).filter(Community.ap_profile_id == entry.actor_url.lower()).first()
    if community is None:
        community = find_actor_or_create(entry.actor_url, community_only=True)
        if not isinstance(community, Community):
            current_app.logger.info(f'discovery sync: {entry.actor_url} did not resolve to a community')
            return False
    if db.session.get(DiscoverySync, community.id) is not None:   # another entry already resolved to this community
        return False
    if not community.post_count:
        queue_backfill(community.id)
    row = DiscoverySync(community_id=community.id, entry_id=entry.id, follow_target=entry.actor_url)
    db.session.add(row)
    db.session.commit()
    held[row.follow_target.lower()] = row
    send_instance_follow(row, community)
    return True


def reconcile_sync() -> dict:
    desired = desired_entries()
    wanted = {entry.actor_url.lower() for entries in desired.values() for entry in entries}
    summary = {'added': 0, 'dropped': 0, 'refollowed': 0, 'failed_hosts': []}
    held = {}
    for row in db.session.query(DiscoverySync).all():
        community = db.session.get(Community, row.community_id)
        if _unusable(community) or row.follow_target.lower() not in wanted:
            try:
                drop_row(row)
                summary['dropped'] += 1
            except Exception:
                current_app.logger.exception(f'discovery sync: dropping community {row.community_id} failed')
                db.session.rollback()
        else:
            held[row.follow_target.lower()] = row
    now = utcnow()
    for host in sorted(desired):
        try:
            added = 0
            for entry in desired[host]:
                row = held.get(entry.actor_url.lower())
                if row is not None:
                    if _needs_refollow(row, now):
                        if send_instance_follow(row, db.session.get(Community, row.community_id)):
                            summary['refollowed'] += 1
                elif added < ADDS_PER_HOST_PER_RUN and _add(entry, held):
                    added += 1
            summary['added'] += added
        except Exception:
            current_app.logger.exception(f'discovery sync: reconcile of {host} failed')
            db.session.rollback()
            summary['failed_hosts'].append(host)
    return summary


def enqueue_polls() -> int:
    """One poll per synced community; one host sees at most one poll every POLL_SPACING_SECONDS."""
    per_host = {}
    queued = 0
    for row in db.session.query(DiscoverySync).order_by(DiscoverySync.community_id).all():
        if backfill_in_progress(row.community_id):   # the first backfill is still running
            continue
        slot = per_host.get(_host(row.follow_target), 0)
        per_host[_host(row.follow_target)] = slot + 1
        if current_app.debug:
            poll_synced_community(row.community_id)
        else:
            poll_synced_community.apply_async(args=[row.community_id], countdown=slot * POLL_SPACING_SECONDS)
        queued += 1
    return queued


@celery.task
def reconcile_sync_task() -> dict:
    summary = reconcile_sync()
    enqueue_polls()
    return summary

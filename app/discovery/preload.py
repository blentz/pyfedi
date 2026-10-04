"""Pre-load PeerTube channels and Castopod podcasts (interop D24, goal (a)). Always a manual admin
action; runs as a Celery task; idempotent, because a community this instance already knows is skipped;
each subscription goes through the existing join path, do_subscribe(admin_preload=True)."""
from flask import current_app
from sqlalchemy import exists, func

from app import celery, db
from app.activitypub.util import find_actor_or_create
from app.community.routes import do_subscribe
from app.discovery import KIND_COMMUNITY
from app.models import Community, DiscoveryEntry, User
from app.utils import instance_banned

PRELOAD_PLATFORMS = ('peertube', 'castopod')
# Subscribe as user 1, the first instance admin, as the lemmyverse pre-load does (see admin_federation_preload)
PRELOAD_USER_ID = 1


def preload_user_can_subscribe(user_id) -> bool:
    """The subscribing user exists and is neither deleted nor banned; checked before anything is enqueued and
    again when the task runs."""
    user = db.session.get(User, user_id)
    return user is not None and not user.deleted and not user.banned


def preload_candidates(count: int, platforms) -> list[DiscoveryEntry]:
    wanted = [platform for platform in (platforms or []) if platform in PRELOAD_PLATFORMS]
    if not wanted or count is None or count < 1:
        return []
    known = exists().where(Community.ap_profile_id == func.lower(DiscoveryEntry.actor_url))
    query = db.session.query(DiscoveryEntry).filter(DiscoveryEntry.kind == KIND_COMMUNITY,
                                                    DiscoveryEntry.platform.in_(wanted),
                                                    DiscoveryEntry.nsfw == False, ~known) \
        .order_by(DiscoveryEntry.followers.desc(), DiscoveryEntry.name)
    candidates = []
    for entry in query:
        if instance_banned(entry.host):
            continue
        candidates.append(entry)
        if len(candidates) >= count:
            break
    return candidates


@celery.task
def preload_discovered_communities(entry_ids, user_id):
    if not preload_user_can_subscribe(user_id):
        current_app.logger.error(f'discovery: pre-load not run: user {user_id} cannot subscribe '
                                 f'(missing, deleted or banned)')
        return []
    results = []
    for entry_id in entry_ids:
        try:
            results.append(_preload_one(entry_id, user_id))
        except Exception:   # one peer's failure must not cost the admin every subscription after it
            current_app.logger.exception(f'discovery: pre-load of entry {entry_id} failed')
            db.session.rollback()
            results.append({'entry': entry_id, 'status': 'error'})
    return results


def _preload_one(entry_id, user_id) -> dict:
    entry = db.session.get(DiscoveryEntry, entry_id)
    if entry is None:
        return {'entry': entry_id, 'status': 'gone'}
    # Re-check at run time: the ids were chosen when the admin clicked, and things change before the task runs
    if entry.kind != KIND_COMMUNITY or entry.platform not in PRELOAD_PLATFORMS:
        return {'entry': entry_id, 'status': 'gone'}
    if entry.nsfw or instance_banned(entry.host):
        return {'entry': entry_id, 'status': 'skipped'}
    if db.session.query(Community.id).filter(Community.ap_profile_id == entry.actor_url.lower()).first():
        return {'entry': entry_id, 'status': 'already known'}
    community = find_actor_or_create(entry.actor_url, community_only=True)
    if not isinstance(community, Community):
        return {'entry': entry_id, 'status': 'not found'}
    return do_subscribe(community.ap_id, user_id, admin_preload=True)

"""Backfill for a community that discovery created (interop D24). "Open to join" and the admin pre-load create a
community through find_actor_or_create, which, unlike adding a community by name (search_for_community), queues no
backfill, so the community opened empty. This runs the same backfill: moderators, then up to 50 outbox items."""
from flask import current_app

from app import cache, celery, db
from app.activitypub.util import remote_object_to_json
from app.community.util import retrieve_mods_and_backfill
from app.models import Community


# While a backfill is queued or running, the community's empty page says its posts are on the way. The flag also
# expires on its own, so a lost task cannot leave the hint up for good.
IN_PROGRESS_SECONDS = 600


def _in_progress_key(community_id: int) -> str:
    return f'discovery:backfilling:{community_id}'


def backfill_in_progress(community_id) -> bool:
    return bool(community_id) and cache.get(_in_progress_key(community_id)) is not None


def run_backfill(community_id: int, stop_at_known: bool = False):
    """Moderators, then the outbox, as a first backfill does; with stop_at_known (a proactive sync poll) the walk ends
    at the first post already stored. Returns retrieve_mods_and_backfill's outcome, None for a missing community."""
    community = db.session.get(Community, community_id)
    if community is None or not community.ap_profile_id:
        return None
    # The actor document gives the moderators of a PeerTube channel (attributedTo), which its videos are
    # attributed to; without it the backfill skips every video.
    community_json = remote_object_to_json(community.ap_profile_id)
    return retrieve_mods_and_backfill(community.id, community.ap_domain, community.name,
                                      community_json if isinstance(community_json, dict) else None,
                                      stop_at_known=stop_at_known)


@celery.task
def backfill_discovered_community(community_id: int):
    try:
        run_backfill(community_id)
    finally:
        cache.delete(_in_progress_key(community_id))


def queue_backfill(community_id: int) -> None:
    cache.set(_in_progress_key(community_id), 1, timeout=IN_PROGRESS_SECONDS)
    if current_app.debug:
        backfill_discovered_community(community_id)
    else:
        backfill_discovered_community.delay(community_id)

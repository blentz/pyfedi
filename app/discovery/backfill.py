"""Backfill for a community that discovery created (interop D24). "Open to join" and the admin pre-load create a
community through find_actor_or_create, which, unlike adding a community by name (search_for_community), queues no
backfill, so the community opened empty. This runs the same backfill: moderators, then up to 50 outbox items."""
from flask import current_app

from app import celery, db
from app.activitypub.util import remote_object_to_json
from app.community.util import retrieve_mods_and_backfill
from app.models import Community


@celery.task
def backfill_discovered_community(community_id: int):
    community = db.session.get(Community, community_id)
    if community is None or not community.ap_profile_id:
        return
    # The actor document gives the moderators of a PeerTube channel (attributedTo), which its videos are
    # attributed to; without it the backfill skips every video.
    community_json = remote_object_to_json(community.ap_profile_id)
    retrieve_mods_and_backfill(community.id, community.ap_domain, community.name,
                               community_json if isinstance(community_json, dict) else None)


def queue_backfill(community_id: int) -> None:
    if current_app.debug:
        backfill_discovered_community(community_id)
    else:
        backfill_discovered_community.delay(community_id)

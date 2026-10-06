"""Discovery seeding for non-Lemmy platforms, and Castopod podcasts as communities (interop D24).

Fork-only (D16/D17). Constants only: this module imports nothing, so every module,
upstream or fork, can import it without risking a cycle.
"""
KIND_COMMUNITY = 'community'
KIND_PERSON = 'person'
PLATFORMS = ('peertube', 'castopod', 'mastodon', 'pixelfed')

# discovery_sync.follow_state (proactive sync): no Follow sent yet / sent, unanswered / accepted / refused
SYNC_NONE = 'none'
SYNC_PENDING = 'pending'
SYNC_ACCEPTED = 'accepted'
SYNC_REJECTED = 'rejected'

# Instance.software values whose communities are video channels or podcasts (lower-case)
MEDIA_SOFTWARE = ('peertube', 'castopod')

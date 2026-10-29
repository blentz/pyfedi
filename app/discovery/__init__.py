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
SYNC_FAILED = 'failed'   # the peer refused the Follow's delivery outright (4xx); retried after a week

# Instance.software values whose communities are video channels or podcasts (lower-case)
MEDIA_SOFTWARE = ('peertube', 'castopod')

# The proactive sync's limits, shared by channels (sync.py) and Pixelfed people (people.py)
MAX_PER_HOST = 50
ADDS_PER_HOST_PER_RUN = 10   # a larger N ramps up over several days rather than bursting at one host
# ...and the whole run is bounded too: each add queues a backfill whose posts fan out into per-post tasks, and an
# uncapped first run (hundreds of hosts) buried an instance's inbox under ~17,000 queued tasks
ADDS_PER_RUN = 50
FOLLOW_RETRY_DAYS = 7
POLL_SPACING_SECONDS = 2

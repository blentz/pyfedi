"""Discovery seeding for non-Lemmy platforms, and Castopod podcasts as communities (interop D24).

Fork-only (D16/D17). Constants only: this module imports nothing, so every module,
upstream or fork, can import it without risking a cycle.
"""
KIND_COMMUNITY = 'community'
KIND_PERSON = 'person'
PLATFORMS = ('peertube', 'castopod', 'mastodon', 'pixelfed')

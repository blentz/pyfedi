"""Shared reads: what a client API may show a viewer, as model rows and queries.

Every client API PieFed serves (the alpha API now; the Mastodon, PeerTube and Lemmy v3 APIs of the API-completeness
plan, docs/superpowers/plans/2026-11-07-api-completeness.md, W0.1) reads through here, so that a block, a ban, a private
community or a local-only post is honoured the same way by all of them. Nothing here builds an API's response shape.
"""

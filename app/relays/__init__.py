"""ActivityPub relay support (spec docs/superpowers/specs/2026-10-08-activitypub-relays-design.md).
Imports nothing from app.models, so models.py can import the context at the top."""
from contextlib import contextmanager
from contextvars import ContextVar

RELAY_PENDING = 'pending'
RELAY_ACCEPTED = 'accepted'
RELAY_REFUSED = 'refused'
RELAY_FAILED = 'failed'
STYLE_MASTODON = 'mastodon'
STYLE_LITEPUB = 'litepub'
PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'

# The relay a post being ingested came through; Post.new records it as post.relay_id.
current_relay_id: ContextVar = ContextVar('current_relay_id', default=None)


@contextmanager
def relay_context(relay_id):
    token = current_relay_id.set(relay_id)
    try:
        yield
    finally:
        current_relay_id.reset(token)

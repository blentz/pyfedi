"""One builder per outbound object shape, used by both push (send_post) and fetch (post_to_page), so the object a
peer is sent and the object it fetches are the same object (spec 2026-10-14 §4, amended §10 A1)."""
from app.activitypub.serializers.microblog import (microblog_note, note_addressing, note_attachments, note_content,
                                                   uses_microblog_note)

__all__ = ['microblog_note', 'note_addressing', 'note_attachments', 'note_content', 'uses_microblog_note']

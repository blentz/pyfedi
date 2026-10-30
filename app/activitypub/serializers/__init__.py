"""One builder per outbound object shape, used by both push (send_post) and fetch (post_to_page), so the object a
peer is sent and the object it fetches are the same object (spec 2026-10-14 §4, amended §10 A1)."""
from app.activitypub.serializers.microblog import (microblog_note, note_addressing, note_attachments, note_content,
                                                   uses_microblog_note)
from app.activitypub.serializers.peertube import peertube_video, uses_peertube_video
from app.activitypub.serializers.loops import loops_video, uses_loops_video
from app.activitypub.serializers.podcast import episode_attachment, uses_podcast_episode

__all__ = ['microblog_note', 'note_addressing', 'note_attachments', 'note_content', 'uses_microblog_note',
           'peertube_video', 'uses_peertube_video', 'loops_video', 'uses_loops_video', 'episode_attachment', 'uses_podcast_episode']

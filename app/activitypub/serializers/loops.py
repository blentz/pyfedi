"""The Note a local video_channel post is pushed to Loops as (spec 2026-10-30 §3.3, O1). Loops v1.0.0-beta.15 stores
only a Create of a Note whose attachment is a video/mp4 Document with a string url (CreateValidator), takes the
caption from `content` alone, and names the post by its `url` in Likes and replies."""
from markupsafe import escape

from app.activitypub.serializers.microblog import _starts_with_title, link_hashtags
from app.activitypub.serializers.peertube import PUBLIC, uses_peertube_video
from app.federation_urls import object_base
from app.models import media_of
from app.utils import ap_datetime, guess_mime_type, html_to_text

LOOPS_MAX_BYTES = 100 * 1024 * 1024   # ProcessRemoteVideoJob refuses a Content-Length over 100 MB


def uses_loops_video(post) -> bool:
    """A video PeerTube is sent as a Video, in the one container Loops accepts and within its size limit."""
    if not uses_peertube_video(post):
        return False
    media = media_of(post)
    return media['mime'] == 'video/mp4' and media['bytes'] <= LOOPS_MAX_BYTES


def loops_content(post) -> str:
    """The title as its own paragraph, then the body: Loops shows `content` and nothing else. As note_content, the
    title is left out when the body already starts with it, but never as a link to the file."""
    original = post.body_html or ''
    body = link_hashtags(original, object_base(post))[0]
    title = (post.title or '').strip()
    if not title or _starts_with_title(html_to_text(original).strip(), title):
        return body
    return f'<p>{escape(title)}</p>{body}'


def loops_video(post) -> dict:
    """The Note, without @context."""
    media = media_of(post)
    image = post.image
    return {
        'id': post.public_url(),
        'type': 'Note',
        'attributedTo': post.author.public_url(),
        'url': post.public_url(),
        'content': loops_content(post),
        'summary': None,
        'inReplyTo': None,
        'sensitive': bool(post.nsfw or post.nsfl or post.content_warning),
        'published': ap_datetime(post.posted_at),
        'to': [PUBLIC],
        'cc': [post.author.followers_url()],
        'attachment': [{'type': 'Document', 'mediaType': 'video/mp4', 'url': post.url, 'name': None,
                        'width': media.get('width'), 'height': media['height']}],
        'preview': {'type': 'Image', 'name': image.alt_text, 'width': media['thumbnail_width'],
                    'height': media['thumbnail_height'],
                    'url': {'href': image.source_url, 'mediaType': guess_mime_type(image.source_url)}},
        'tag': [tag for tag in post.tags_for_activitypub() if tag.get('type') == 'Hashtag'],
    }

"""The PeerTube Video a local video_channel post is pushed and fetched as (spec 2026-10-14 §4.3, §10 A11, A15.4).
Every field PeerTube v8.3.1 requires is always present; an optional field is sent only in a form PeerTube accepts,
since one bad field rejects the whole object."""
from flask import current_app

from app.constants import COMMUNITY_KIND_VIDEO_CHANNEL, POST_TYPE_VIDEO
from app.models import media_of
from app.utils import ap_datetime, guess_mime_type
from app.visibility import is_open

PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'
NAME_MIN, NAME_MAX = 3, 120
CONTENT_MIN = 3
TAG_MIN, TAG_MAX = 2, 30
COMMENTS_ENABLED, COMMENTS_DISABLED = 1, 2
# PeerTube keeps a remote file Link only for mp4, webm and ogg; a MOV holds the same streams an mp4 does
LINK_MIME = {'video/quicktime': 'video/mp4'}


def uses_peertube_video(post) -> bool:
    """A public, ready video upload in a local video_channel community whose thumbnail has a size. Anything else in
    that community is a Page (spec §4.1)."""
    community = post.community
    media = media_of(post)
    image = post.image
    return bool(community.kind == COMMUNITY_KIND_VIDEO_CHANNEL and community.is_local()
                and post.type == POST_TYPE_VIDEO and is_open(post)
                and media is not None and media.get('status') == 'ready'
                and str(media.get('mime', '')).startswith('video/')
                and image is not None and image.width and image.height)


def video_name(title) -> str:
    name = (title or '').strip()[:NAME_MAX]
    return name.ljust(NAME_MIN, '.')


def _file_link(post, media) -> dict:
    link = {'type': 'Link', 'mediaType': LINK_MIME.get(media['mime'], media['mime']), 'href': post.url,
            'height': media['height'], 'size': media['bytes']}
    if media.get('width'):
        link['width'] = media['width']
    if media.get('fps'):
        link['fps'] = media['fps']
    return link


def _hashtags(post) -> list:
    return [tag for tag in post.tags_for_activitypub()
            if tag.get('type') == 'Hashtag' and TAG_MIN <= len(tag['name'].lstrip('#')) <= TAG_MAX]


def peertube_video(post) -> dict:
    """The Video, without @context."""
    media = media_of(post)
    community = post.community
    base = f"{current_app.config['SERVER_URL']}/post/{post.id}"
    video = {
        'id': post.public_url(),
        'type': 'Video',
        'uuid': media['uuid'],
        'name': video_name(post.title),
        'duration': f"PT{media['duration_s']}S",
        'published': ap_datetime(post.posted_at),
        'updated': ap_datetime(post.edited_at or post.posted_at),
        'views': 0,
        'likes': f'{base}/likes',
        'dislikes': f'{base}/dislikes',
        'url': [_file_link(post, media), {'type': 'Link', 'mediaType': 'text/html', 'href': post.public_url()}],
        'icon': [{'type': 'Image', 'url': post.image.source_url, 'mediaType': guess_mime_type(post.image.source_url),
                  'width': post.image.width, 'height': post.image.height}],
        'attributedTo': [{'type': 'Person', 'id': post.author.public_url()},
                         {'type': 'Group', 'id': community.public_url()}],
        'to': [PUBLIC],
        'cc': [community.ap_followers_url],
        'commentsPolicy': COMMENTS_ENABLED if post.comments_enabled else COMMENTS_DISABLED,
        'sensitive': bool(post.nsfw or post.nsfl or post.content_warning),
        'state': 1,
        'waitTranscoding': False,
        'isLiveBroadcast': False,
        'downloadEnabled': True,
        'tag': _hashtags(post),
    }
    if len((post.body or '').strip()) >= CONTENT_MIN:
        video['content'] = post.body
        video['mediaType'] = 'text/markdown'
    if post.language_id:
        video['language'] = {'identifier': post.language_code(), 'name': post.language_name()}
    return video

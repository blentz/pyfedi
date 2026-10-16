"""Property checks for the video channel surface (tier T8): the ffprobe JSON that decides what an uploaded file's
media record holds, and the PeerTube Video a local video_channel post is sent and fetched as.

validate_probe reads ffprobe's JSON about a file an author uploaded, so any JSON value can reach it. MediaToolError
is its one refusal; anything else is a finding. peertube_video turns a post and its media record into an object
PeerTube accepts or rejects whole, so for any record uses_peertube_video lets through, the object must keep the
limits PeerTube enforces (Global Constraints, "PeerTube limits"): name 3-120 characters, duration PT<n>S, content
absent or 3+ characters, hashtags 2-30, the file Link's height and size, the icon's width and height, and the
public audience in full.

The record is fuzzed freely -- missing keys, odd types -- and the builder is called whenever uses_peertube_video
accepts the post; a record it rejects is a Page, which is fine.

No database and no network. The post is built unsaved and never added to a session; the builder target needs an app
for SERVER_URL.
"""

import re
from unittest.mock import MagicMock, patch

from flask import current_app

from app.activitypub.serializers.peertube import PUBLIC, peertube_video, uses_peertube_video
from app.constants import COMMUNITY_KIND_VIDEO_CHANNEL, MEDIA_KIND_AUDIO, MEDIA_KIND_VIDEO, POST_TYPE_VIDEO
from app import db
from app.models import Community, File, Post, Tag, User, utcnow
from app.shared.ffmpeg import MediaToolError
from app.shared.tasks.media import validate_probe
from tests.fuzz.targets._target import Target
from tests.fuzz.targets.signatures import _fields, _json_or_text, _text

DURATION = re.compile(r'PT\d+S')
MAX_TAGS = 8
# tags_for_activitypub looks a body's :tokens: up in the emoji table; no database here, so that query finds none.
_NO_EMOJI = {'return_value.filter.return_value.order_by.return_value.all.return_value': []}


def check_validate_probe(data: bytes) -> None:
    """validate_probe either refuses with MediaToolError or returns a record: a positive int duration_s, a codec
    string, and for video positive int width and height. Anything else raised is a finding."""
    probe = _json_or_text(data)
    if not isinstance(probe, dict):
        return
    for kind in (MEDIA_KIND_VIDEO, MEDIA_KIND_AUDIO):
        try:
            meta = validate_probe(probe, kind)
        except MediaToolError:
            continue
        assert type(meta['duration_s']) is int and meta['duration_s'] > 0, f'{kind}: duration_s {meta["duration_s"]!r}'
        assert isinstance(meta['codec'], str)
        if kind == MEDIA_KIND_VIDEO:
            assert meta['width'] > 0 and meta['height'] > 0, f'video dimensions {meta["width"]!r}x{meta["height"]!r}'
            assert 'fps' not in meta or meta['fps'] > 0, f'fps {meta["fps"]!r}'


def _video_post(record, title: str, body: str, tags: list[str], flags: int) -> Post:
    server = current_app.config['SERVER_URL']
    community = Community(name='chan', kind=COMMUNITY_KIND_VIDEO_CHANNEL, ap_followers_url=f'{server}/c/chan/followers')
    author = User(user_name='alice')
    post = Post(id=1, title=title, body=body, type=POST_TYPE_VIDEO, url=f'{server}/static/media/posts/ab/cd/v.mp4',
                posted_at=utcnow(), comments_enabled=bool(flags & 1), nsfw=bool(flags & 2),
                extensions={'media': record})
    post.community, post.author = community, author
    post.image = File(source_url=f'{server}/static/media/posts/ef/gh/thumb.jpg', width=640, height=360)
    post.tags = [Tag(name=name) for name in tags]
    return post


def check_peertube_video(data: bytes) -> None:
    """Fields: the media record (JSON, merged over a ready record, so a key can be overwritten with anything or the
    whole record replaced by a non-dict), the title, the body, newline-separated tag names. A post
    uses_peertube_video rejects is a Page, which is fine; one it accepts must build a Video within the limits."""
    record_bytes, title_bytes, body_bytes, tag_bytes = _fields(data, 4)
    title, body = _text(title_bytes), _text(body_bytes)
    tags = _text(tag_bytes).split('\n')[:MAX_TAGS]
    fuzzed = _json_or_text(record_bytes)
    record = {'status': 'ready', 'uuid': '2f3c8f0e-6f8d-4b5e-9c1a-0d6a5e1b7f42', 'mime': 'video/mp4', 'bytes': 12741,
              'duration_s': 10, 'width': 640, 'height': 360, 'fps': 30, 'thumbnail_width': 640,
              'thumbnail_height': 360}
    if isinstance(fuzzed, dict):
        record.update(fuzzed)
    post = _video_post(record, title, body, tags, len(record_bytes))
    if not uses_peertube_video(post):
        return
    with patch.object(db.session, 'query', MagicMock(**_NO_EMOJI)):
        video = peertube_video(post)
    name = video['name']
    assert 3 <= len(name) <= 120, f'name {name!r}'
    assert DURATION.fullmatch(video['duration']), f'duration {video["duration"]!r}'
    if 'content' in video:
        assert len(video['content']) >= 3 and video['mediaType'] == 'text/markdown', f'content {video["content"]!r}'
    for tag in video['tag']:
        assert 2 <= len(tag['name'].lstrip('#')) <= 30, f'tag {tag!r}'
    link = video['url'][0]
    icon = video['icon'][0]
    for what, value in (('Link height', link['height']), ('Link size', link['size']), ('icon width', icon['width']),
                        ('icon height', icon['height']),
                        *((f'Link {key}', link[key]) for key in ('width', 'fps') if key in link)):
        assert type(value) is int and value > 0, f'{what} {value!r}'
    assert video['to'] == [PUBLIC]
    assert video['commentsPolicy'] in (1, 2)


# None is budgeted: both are linear in the input. Only the builder needs an app (SERVER_URL).
TARGETS = [
    Target(name='validate_probe', check=check_validate_probe, surface='video_channels', budget_ms=None,
           needs_app=False),
    Target(name='peertube_video', check=check_peertube_video, surface='video_channels', budget_ms=None,
           needs_app=True),
]

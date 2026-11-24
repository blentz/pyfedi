"""What a post becomes on ATProto (spec §6), through a Session (app.atproto).

A microblog post becomes a full post: its text, cut to 300 characters with the link back when longer, with link and
hashtag facets in UTF-8 byte offsets; up to four pictures with their alt text, or one MP4 video, or a link post's
card; `sexual` or `graphic-media` self-labels for NSFW or NSFL. A post behind a content warning sends the warning
and the link only.

A post in any other community becomes a card: its title, and an external embed linking back to it with its
description and its first picture as the thumbnail.
"""
import os
import re
import secrets
import time
from datetime import datetime, timezone

from app.atproto import Session
from app.connections import Permanent
from app.connections import content
from app.connections.content import StoredVideo, body_text, cut, image_bytes, plain_text
from app.connections.eligibility import image_files, uploaded_video
from app.connections.http import check, fetch
from app.constants import POST_TYPE_LINK
from app.models import media_of
from app.utils import is_local_microblogs

POST = 'app.bsky.feed.post'
MAX_GRAPHEMES = 300          # app.bsky.feed.post text: counted here in code points, never fewer than graphemes
# (its 3000-byte limit needs no check: 300 code points are at most 1200 bytes)
MAX_IMAGES = 4
MAX_IMAGE_BYTES = 950_000    # under the lexicon's 1,000,000 (an image, and an external card's thumb)
MAX_IMAGE_SIDE = 2000
MAX_VIDEO_BYTES = 300_000_000   # app.bsky.embed.video's blob: video/mp4 only, up to this (CA spec 19.5)
LINK = re.compile(r'https?://[^\s<>"\]\)]+')
TAG = re.compile(r'(?:^|(?<=\s))#([^\s#\.,!?;:()\[\]"\']{1,64})')
TID_CHARS = '234567abcdefghijklmnopqrstuvwxyz'   # atproto's sortable base32


def compose(post, with_link: bool) -> str:
    """The post's text: within 300 characters, with the link back when asked or when cut."""
    link = post.ap_id
    if post.content_warning:
        return cut(f'CW: {post.content_warning}', MAX_GRAPHEMES - len(link) - 1) + '\n' + link
    text = plain_text(post)
    fits = len(text) <= MAX_GRAPHEMES
    if fits and not with_link:
        return text
    if fits and len(text) + len(link) + 2 <= MAX_GRAPHEMES:
        return f'{text}\n\n{link}'.strip()
    return f'{cut(text, MAX_GRAPHEMES - len(link) - 1)}\n{link}'


def facets(text: str) -> list[dict]:
    """Link and hashtag facets, in the UTF-8 byte offsets the lexicon counts in."""
    found = []
    for match in LINK.finditer(text):
        uri = match.group(0).rstrip('.,;:!?')
        start = len(text[:match.start()].encode())
        found.append({'index': {'byteStart': start, 'byteEnd': start + len(uri.encode())},
                      'features': [{'$type': 'app.bsky.richtext.facet#link', 'uri': uri}]})
    for match in TAG.finditer(text):
        start = len(text[:match.start(1) - 1].encode())
        found.append({'index': {'byteStart': start, 'byteEnd': len(text[:match.end(1)].encode())},
                      'features': [{'$type': 'app.bsky.richtext.facet#tag', 'tag': match.group(1)}]})
    return found


def as_jpeg(data: bytes) -> tuple[bytes, int, int] | None:
    """The picture as a JPEG within the lexicon's size limit, and its width and height."""
    return content.as_jpeg(data, MAX_IMAGE_BYTES, MAX_IMAGE_SIDE)


def labels(post) -> dict | None:
    values = ([{'val': 'sexual'}] if post.nsfw else []) + ([{'val': 'graphic-media'}] if post.nsfl else [])
    return {'$type': 'com.atproto.label.defs#selfLabels', 'values': values} if values else None


def new_tid() -> str:
    """A record key in atproto's TID form: microseconds since 1970 and a random clock id, 13 characters."""
    value = (time.time_ns() // 1000) << 10 | secrets.randbelow(1024)
    return ''.join(TID_CHARS[(value >> (5 * (12 - place))) & 31] for place in range(13))


def now() -> str:
    return stamp(datetime.now(timezone.utc))


def stamp(moment: datetime) -> str:
    """A time as the lexicon's datetime: UTC, with a Z (a naive time is UTC, as PieFed stores them)."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def existing_record(session: Session, rkey: str, collection: str = POST) -> str | None:
    """The account's record under `rkey` if its PDS has it (an earlier attempt made it): its at:// URI, else None.
    Records are public, so this read needs no token."""
    response = fetch('GET', f'{session.pds}/xrpc/com.atproto.repo.getRecord',
                     params={'repo': session.did, 'collection': collection, 'rkey': rkey})
    if response.status_code == 400 and _error_of(response) == 'RecordNotFound':
        return None
    check(response)
    return response.json().get('uri') or f'at://{session.did}/{collection}/{rkey}'


def _error_of(response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    return body.get('error') if isinstance(body, dict) else None


def upload_blob(session: Session, data: bytes, mime: str) -> dict:
    return session.xrpc('POST', 'com.atproto.repo.uploadBlob', content=data,
                        headers={'Content-Type': mime}).json()['blob']


def upload_video(post, session: Session) -> dict | None:
    """The post's uploaded video as a blob on the account's PDS (CA spec 19.5): uploaded as a picture is, and
    processed by Bluesky once the post is there. None when it cannot go: not an MP4, larger than the lexicon takes,
    or refused by the PDS (whose own limit may be lower): the post then goes as text and link."""
    media = media_of(post) or {}
    if post.content_warning or media.get('mime') != 'video/mp4':
        return None
    try:
        with StoredVideo(post, MAX_VIDEO_BYTES) as video:
            size = os.fstat(video.fileno()).st_size
            if size > MAX_VIDEO_BYTES:
                return None
            return session.xrpc('POST', 'com.atproto.repo.uploadBlob', stream=video, seconds=600 + size // 500_000,
                                headers={'Content-Type': 'video/mp4', 'Content-Length': str(size)}).json()['blob']
    except Permanent:
        return None


def video_embed(post, blob: dict) -> dict:
    embed = {'$type': 'app.bsky.embed.video', 'video': blob}
    media = media_of(post) or {}
    width, height = media.get('width'), media.get('height')
    if type(width) is int and type(height) is int and width > 0 and height > 0:
        embed['aspectRatio'] = {'width': width, 'height': height}
    return embed


def _finish(record: dict, post) -> dict:
    """The parts every record of a post has: facets for its text, its language, its self-labels."""
    if found := facets(record['text']):
        record['facets'] = found
    if post.language is not None and post.language.code and post.language.code != 'und':
        record['langs'] = [post.language.code]
    if found := labels(post):
        record['labels'] = found
    return record


def build_record(post, session: Session, video: dict | None = None, created_at: str | None = None) -> dict:
    """The full app.bsky.feed.post record for `post`, with `video` (an uploaded blob) when it has one. Uploads its
    pictures to the PDS on the way. `created_at` is the record's time when it is not now (a backfilled post's)."""
    files = [] if post.content_warning or uploaded_video(post) else image_files(post)
    with_link = bool(post.content_warning) or (uploaded_video(post) and video is None) or len(files) > MAX_IMAGES
    images = []
    for file in files[:MAX_IMAGES]:
        data = image_bytes(file)
        jpeg = as_jpeg(data) if data else None
        if jpeg is None:
            with_link = True
            continue
        jpeg_bytes, width, height = jpeg
        images.append({'alt': (file.alt_text or '')[:2000], 'image': upload_blob(session, jpeg_bytes, 'image/jpeg'),
                       'aspectRatio': {'width': width, 'height': height}})
    record = {'$type': POST, 'text': compose(post, with_link), 'createdAt': created_at or now()}
    if video is not None:
        record['embed'] = video_embed(post, video)
    elif images:
        record['embed'] = {'$type': 'app.bsky.embed.images', 'images': images}
    elif post.type == POST_TYPE_LINK and post.url and not post.content_warning:
        record['embed'] = {'$type': 'app.bsky.embed.external', 'external': {
            'uri': post.url, 'title': (post.title or '')[:300], 'description': cut(plain_text(post), 300)}}
    return _finish(record, post)


def build_card(post, session: Session, created_at: str | None = None) -> dict:
    """The card record for a post outside the microblogs (spec §6, A5): its title as the text, and an external
    embed linking back to it, with its description and its first picture as the thumbnail. Behind a content
    warning: the warning as the text, and the card without description or picture."""
    title = (post.title or '').strip()
    external = {'uri': post.ap_id, 'title': cut(title, 300), 'description': ''}
    if post.content_warning:
        text = cut(f'CW: {post.content_warning}', MAX_GRAPHEMES)
    else:
        text = cut(title, MAX_GRAPHEMES)
        external['description'] = cut(body_text(post), 300)
        for file in image_files(post)[:1]:
            data = image_bytes(file)
            jpeg = as_jpeg(data) if data else None
            if jpeg is not None:
                external['thumb'] = upload_blob(session, jpeg[0], 'image/jpeg')
    record = {'$type': POST, 'text': text, 'createdAt': created_at or now(),
              'embed': {'$type': 'app.bsky.embed.external', 'external': external}}
    return _finish(record, post)


def goes_whole(post) -> bool:
    """A post in the local microblogs goes as a full post; any other as a card (spec §6, A5)."""
    community = getattr(post, 'community', None)
    return community is not None and is_local_microblogs(community)


def send_copy(post, session: Session, progress, before_write=None) -> str:
    """Make the post's copy (a full post or a card) and answer its at:// URI. Under a record key chosen before the
    first attempt and kept in `progress` (CA spec 19.2): an attempt after one that got no answer first asks the PDS
    whether that one made the record, so a copy is never made twice. A video is uploaded once, whatever comes after.
    A backfilled copy (`progress` says `backfill`) keeps the post's own time. `before_write` is called just before the
    record is made (the hosted variant takes its budget there)."""
    rkey = progress.data.get('rkey')
    uri = existing_record(session, rkey) if rkey else None
    if uri is not None:
        return uri
    if rkey is None:
        rkey = progress.data['rkey'] = new_tid()
        progress.save()
    created_at = stamp(post.posted_at) if progress.data.get('backfill') and post.posted_at else None
    if not goes_whole(post):
        record = build_card(post, session, created_at)
    else:
        if 'video' not in progress.data and uploaded_video(post):
            progress.data['video'] = upload_video(post, session)
            progress.save()
        record = build_record(post, session, progress.data.get('video'), created_at)
    if before_write is not None:
        before_write()
    return session.xrpc('POST', 'com.atproto.repo.createRecord', json={
        'repo': session.did, 'collection': POST, 'rkey': rkey, 'record': record}).json()['uri']


def delete_record(session: Session, uri: str) -> None:
    """Delete one of the account's records, named by its at:// URI."""
    _did, collection, rkey = uri.removeprefix('at://').split('/', 2)
    session.xrpc('POST', 'com.atproto.repo.deleteRecord', json={'repo': session.did, 'collection': collection,
                                                                 'rkey': rkey})


def web_url(uri: str) -> str:
    """Where a post's at:// URI is seen on the web."""
    did, _collection, rkey = uri.removeprefix('at://').split('/', 2)
    return f'https://bsky.app/profile/{did}/post/{rkey}'

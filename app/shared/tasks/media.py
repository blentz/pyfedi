"""probe_media (spec §5.2, A13): validate an uploaded file in a sandbox, thumbnail it, then publish its post."""
import json
import math
import os
from datetime import datetime, timedelta

import pillow_avif  # NOQA  # registers Pillow's AVIF plugin, for an instance whose MEDIA_IMAGE_FORMAT is AVIF
from boto3.exceptions import Boto3Error
from botocore.exceptions import BotoCoreError, ClientError
from flask import current_app
from sqlalchemy import or_, text
from flask_babel import force_locale, gettext
from PIL import Image, ImageOps

from app import celery, db
from app.activitypub.util import make_image_sizes, notify_about_post
from app.constants import (MEDIA_KIND_AUDIO, MEDIA_KIND_VIDEO, NOTIF_DEFAULT, POST_STATUS_PROCESSING,
                           POST_STATUS_PUBLISHED, POST_STATUS_SCHEDULED)
from app.models import CommunityMember, File, Notification, Post, served_path, utcnow
from app.shared.ffmpeg import MediaToolError, extract_cover, extract_frame, run_ffprobe
from app.shared.media import MEDIA_DIRECTORY, can_upload_media, media_local_path, media_of, queue_probe, with_media
from app.shared.tasks import task_selector
from app.utils import (can_create_post, ensure_directory_exists, get_recipient_language, get_task_session, gibberish,
                       guess_mime_type, patch_db_session, save_image_without_metadata, shorten_string,
                       store_files_in_s3, to_srgb, upload_file_to_s3, user_banned_from_community)

VIDEO_CODECS = {'h264', 'vp9', 'av1'}
AUDIO_CODECS = {'aac', 'mp3', 'opus', 'vorbis'}
THUMBNAIL_AT = 0.1   # a video's thumbnail is the frame a tenth of the way in
TMP_DIRECTORY = 'app/static/tmp'
# What the S3 work can raise. Their texts can name the endpoint or the bucket, so the author is told less.
_STORE_ERRORS = (BotoCoreError, ClientError, Boto3Error, OSError)


def _fps(rate) -> int | None:
    try:
        num, den = (int(x) for x in rate.split('/'))
        return round(num / den) if den else None
    except (AttributeError, ValueError):
        return None


def _duration(probe: dict) -> float:
    try:
        return float(probe['format']['duration'])
    except (KeyError, TypeError, ValueError):
        return 0.0


def _is_cover_art(stream: dict) -> bool:
    return bool(stream.get('disposition', {}).get('attached_pic'))


def validate_probe(probe: dict, kind: str) -> dict:
    """The media record's metadata fields for ffprobe's `probe` of a `kind` file, or MediaToolError(reason)."""
    streams = probe.get('streams')
    if not isinstance(streams, list):
        raise MediaToolError('ffprobe output was unreadable')
    duration = _duration(probe)
    if not duration > 0:
        raise MediaToolError('the file has no duration')
    if kind == MEDIA_KIND_VIDEO:
        video = next((s for s in streams if s.get('codec_type') == 'video' and not _is_cover_art(s)), None)
        if video is None:
            raise MediaToolError('the file has no video stream')
        if video.get('codec_name') not in VIDEO_CODECS:
            raise MediaToolError(f"video codec {video.get('codec_name')} is not h264, vp9 or av1")
        width, height = video.get('width') or 0, video.get('height') or 0
        if width <= 0 or height <= 0:
            raise MediaToolError('the video has no dimensions')
        meta = {'duration_s': math.ceil(duration), 'width': width, 'height': height, 'codec': video['codec_name']}
        fps = _fps(video.get('avg_frame_rate'))
        if fps:
            meta['fps'] = fps
        return meta
    audio = next((s for s in streams if s.get('codec_type') == 'audio'), None)
    if audio is None:
        raise MediaToolError('the file has no audio stream')
    if audio.get('codec_name') not in AUDIO_CODECS:
        raise MediaToolError(f"audio codec {audio.get('codec_name')} is not aac, mp3, opus or vorbis")
    return {'duration_s': math.ceil(duration), 'codec': audio['codec_name']}


# One UPDATE moves the media status, only from `from_status` and only for an undeleted post, and merges `fields` and the
# attempt count into the media record. Postgres re-reads the WHERE of a row another UPDATE changed meanwhile, so of
# two runs racing for one upload exactly one moves it: the compare-and-set is the statement, no read-check-write.
_MOVE_MEDIA_STATUS = text(
    "UPDATE post SET extensions = jsonb_set(extensions::jsonb, '{media}', (extensions::jsonb -> 'media')"
    " || CAST(:fields AS jsonb)"
    " || jsonb_build_object('attempts', COALESCE((extensions -> 'media' ->> 'attempts')::int, 0) + :bump))::json"
    " WHERE id = :post_id AND deleted = false AND extensions -> 'media' ->> 'status' = :from_status")
STUCK_AFTER = timedelta(minutes=30)   # a probe is bounded by its tools' 60 s limits and one S3 upload
MAX_ATTEMPTS = 3


def _move_media_status(post_id, from_status: str, to_status: str, bump: int = 0, **stamps) -> bool:
    moved = db.session.execute(_MOVE_MEDIA_STATUS, {
        'post_id': post_id, 'from_status': from_status, 'bump': bump,
        'fields': json.dumps({'status': to_status, **stamps})})
    db.session.commit()
    return moved.rowcount == 1


@celery.task
def probe_media(send_async, post_id):
    session = get_task_session()
    try:
        with patch_db_session(session):
            # 'probing' while this run works: a second delivery of the task finds it claimed and leaves it alone
            if not _move_media_status(post_id, 'processing', 'probing', bump=1,
                                      probing_since=utcnow().isoformat()):
                return
            try:
                _probe(post_id)
            except Exception:
                db.session.rollback()
                _move_media_status(post_id, 'probing', 'processing')   # not left claimed by a run that died
                raise
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _probe(post_id) -> None:
    """The claimed upload of an undeleted local post: ready and published, or failed with a reason."""
    post = db.session.get(Post, post_id)
    media = media_of(post)
    written = []        # every image file this run has started to write, so a failure can remove it
    thumbnail = None    # the thumbnail's File row, once it exists
    try:
        path = media_local_path(post)
        if path is None or not os.path.isfile(path):
            raise MediaToolError('media file is missing')
        kind = MEDIA_KIND_VIDEO if media['mime'].startswith('video/') else MEDIA_KIND_AUDIO
        probe = run_ffprobe(path)
        meta = validate_probe(probe, kind)
        try:
            thumbnail = _thumbnail(post, path, kind, probe, written)
        except (OSError, Image.DecompressionBombError) as error:
            current_app.logger.warning(f'probe_media: post {post.id}: no thumbnail: {error!r}')
            raise MediaToolError('the thumbnail could not be made') from error
        if store_files_in_s3():   # only once the file is known good and its thumbnail made
            _move_to_s3(post, path, thumbnail, written)
    except MediaToolError as error:
        _fail(post, thumbnail, written, str(error))
        return
    refusal = publish_refusal(post)
    if refusal is not None:
        _fail(post, thumbnail, written, refusal)
        return
    if thumbnail is not None:
        meta['thumbnail_file_id'] = post.image_id
    with_media(post, status='ready', **meta)
    publish_ready_post(post)
    if thumbnail is not None:
        make_image_sizes(thumbnail.id, 512, 1200, 'posts', post.community.low_quality)


def _thumbnail(post, path, kind, probe, written) -> File | None:
    """The post's thumbnail as a File that is also post.image (A13.4), or None for audio with no cover art."""
    if kind == MEDIA_KIND_VIDEO:
        def grab(out):
            extract_frame(path, _duration(probe) * THUMBNAIL_AT, out)
    elif any(_is_cover_art(stream) for stream in probe['streams']):
        def grab(out):
            extract_cover(path, out)
    else:
        return None
    ensure_directory_exists(TMP_DIRECTORY)
    frame = f'{TMP_DIRECTORY}/{gibberish(15)}.jpg'
    try:
        grab(frame)
        image_place = _save_thumbnail_image(frame, written)
    finally:
        _remove(frame)
    file = File(source_url=f"{current_app.config['SERVER_URL']}{served_path(image_place)}")
    db.session.add(file)
    post.image = file
    db.session.flush()
    return file


def _save_thumbnail_image(frame, written) -> str:
    """Re-encode `frame` as edit_post does an uploaded image, into the media tree; returns where it went."""
    image_format = (current_app.config['MEDIA_IMAGE_FORMAT'] or 'JPEG').upper()
    max_dimension = current_app.config['MEDIA_IMAGE_MAX_DIMENSION']
    name = gibberish(15)
    directory = f'{MEDIA_DIRECTORY}/{name[0:2]}/{name[2:4]}'
    ensure_directory_exists(directory)
    extension = '.jpg' if image_format == 'JPEG' else '.' + image_format.lower()
    image_place = f'{directory}/{name}{extension}'
    with Image.open(frame) as img:
        img = ImageOps.exif_transpose(img)
        img = to_srgb(img) if image_format == 'JPEG' else img.convert('RGBA')
        img.thumbnail((max_dimension, max_dimension), resample=Image.LANCZOS)
        written.append(image_place)
        save_image_without_metadata(img, image_place, format=image_format, optimize=True,
                                    quality=int(current_app.config['MEDIA_IMAGE_QUALITY']))
    return image_place


def _remove(path: str) -> None:
    if os.path.exists(path):
        os.unlink(path)


def _s3_key(local_path: str) -> str:
    return 'posts/' + local_path.removeprefix(MEDIA_DIRECTORY + '/')   # posts/xx/yy/<name><ext>


def _move_to_s3(post, path, thumbnail, written) -> None:
    """The thumbnail, then the media, into the bucket. The media goes last: if it fails, the thumbnail's File row
    names the bucket and its removal takes the object with it, and the post keeps its local file."""
    try:
        if thumbnail is not None:
            image_place = written[-1]
            thumbnail.source_url = upload_file_to_s3(image_place, _s3_key(image_place), guess_mime_type(image_place))
            _drop_local_copy(post, image_place)
        post.url = upload_file_to_s3(path, _s3_key(path), media_of(post)['mime'])   # named before the unlink
    except _STORE_ERRORS as error:
        current_app.logger.warning(f'probe_media: post {post.id}: S3 refused it: {error!r}')
        raise MediaToolError('the file could not be stored') from error
    _drop_local_copy(post, path)


def _drop_local_copy(post, path) -> None:
    """Remove a local file now safe in the bucket. Failing to is logged, not a failure: the post names the object."""
    try:
        os.unlink(path)
    except OSError as error:
        current_app.logger.warning(f'probe_media: post {post.id}: uploaded, but the local copy stayed: {error!r}')


def _fail(post, thumbnail, written, reason: str) -> None:
    """A13.9: the post stays unpublished, its media record says why, and its author is told."""
    if thumbnail is not None:
        post.image = None
        thumbnail.delete_from_disk()
        db.session.delete(thumbnail)
    for image_place in written:   # one written before its File row existed, or left half-written
        _remove(image_place)
    with_media(post, status='failed', error=reason)
    author = post.author
    with force_locale(get_recipient_language(author.id)):
        title = gettext('Your upload could not be processed: %(reason)s', reason=reason)
    db.session.add(Notification(user_id=author.id, title=shorten_string(title, 150), url=f'/post/{post.id}',
                                author_id=author.id, notif_type=NOTIF_DEFAULT, subtype='media_failed',
                                targets={'post_id': post.id}))
    author.unread_notifications += 1
    db.session.commit()


def publish_refusal(post) -> str | None:
    """Why `post` may not be published now, or None. The upload was allowed when it was made, but probing takes
    time: the author, the community or the post can change before it is ready. The scheduled publisher re-checks
    can_create_post at publish time for the same reason (app/cli.py, F7). The rows are read again, so a change another
    transaction committed while probing is seen, and moderator status is read from its row, not from the cached
    Community.moderators list."""
    db.session.flush()
    for row in (post, post.author, post.community):
        db.session.refresh(row)
    author, community = post.author, post.community
    if post.deleted:
        return 'the post was deleted'
    if community.banned or community.is_gone():
        return 'the community is gone'
    # A moderator is an owner or a moderator, as in Community.moderators: an AP Remove can leave an owner unflagged
    still_moderates = db.session.query(CommunityMember).filter(
        CommunityMember.user_id == author.id, CommunityMember.community_id == community.id,
        or_(CommunityMember.is_moderator == True, CommunityMember.is_owner == True),
        CommunityMember.is_banned == False).first() is not None
    if author.deleted or author.banned or user_banned_from_community(author.id, community.id) or \
            not still_moderates or not can_create_post(author, community) or not can_upload_media(author, community):
        return 'you may no longer post this media in this community'
    return None


def publish_ready_post(post) -> None:
    """The scheduled-post publisher's sequence (app/cli.py publish_scheduled_posts): a scheduled upload waits for it."""
    if post.scheduled_for:
        post.status = POST_STATUS_SCHEDULED
        db.session.commit()
        return
    post.status = POST_STATUS_PUBLISHED
    post.posted_at = utcnow()
    db.session.commit()
    task_selector('make_post', post_id=post.id)
    notify_about_post(post)


def _stamp(value) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def requeue_stuck_media() -> None:
    """A13.9: the per-minute sweep for uploads a lost run left behind. Celery acks early, so a worker killed mid-probe
    (OOM, SIGKILL, a deploy restart) loses its task and the post would wait forever.

    A probe claimed more than STUCK_AFTER ago, and an upload not probed within STUCK_AFTER of its upload or of its last
    requeue, is put back to processing and queued again; one that has used MAX_ATTEMPTS fails and its author is told.
    Each move is the compare-and-set probe_media claims with, so a probe that is in fact alive keeps its upload."""
    cutoff = utcnow() - STUCK_AFTER
    for post in Post.query.filter(Post.status == POST_STATUS_PROCESSING, Post.deleted == False).all():
        media = media_of(post) or {}
        status = media.get('status')
        if status == 'probing':
            since = _stamp(media.get('probing_since'))
        elif status == 'processing':
            since = _stamp(media.get('requeued_at')) or post.created_at
        else:
            continue   # ready, or failed: nothing is waiting
        if since is not None and since > cutoff:
            continue
        if (media.get('attempts') or 0) >= MAX_ATTEMPTS:
            if _move_media_status(post.id, status, 'failed'):
                _fail(post, None, [], 'processing timed out')
        elif _move_media_status(post.id, status, 'processing', requeued_at=utcnow().isoformat()):
            queue_probe(post.id)

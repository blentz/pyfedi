"""probe_media (spec §5.2, A13): validate an uploaded file in a sandbox, thumbnail it, then publish its post. Then
make_renditions (W2.1b): a ready video's smaller copies."""
import json
import math
import os
from contextlib import suppress
from datetime import datetime, timedelta
from time import monotonic

from boto3.exceptions import Boto3Error
from botocore.exceptions import BotoCoreError, ClientError
from flask import current_app
from sqlalchemy import or_, select, text
from flask_babel import force_locale, gettext
from PIL import Image, ImageOps

from app import celery, db
from app.activitypub.util import make_image_sizes, notify_about_post
from app.community.util import notify_admins_of_post_needing_approval
from app.constants import (MEDIA_KIND_AUDIO, MEDIA_KIND_VIDEO, NOTIF_DEFAULT, POST_STATUS_PROCESSING, POST_TYPE_VIDEO,
                           POST_STATUS_PUBLISHED, POST_STATUS_REVIEWING, POST_STATUS_SCHEDULED)
from app.activitypub.serializers.peertube import uses_peertube_video
from app.models import (HLS_SETTING, RENDITION_SIDES, CommunityMember, File, Notification, Post, federation_base,
                        hls_directory, hls_names, rendition_name, renditions_of, s3_key_from_url, served_path, utcnow)
from app.shared.ffmpeg import (TRANSCODE_MAX_S, MediaToolError, extract_cover, extract_frame, hls_package, run_ffprobe,
                               transcode, transcode_timeout)
from app.shared.post import feature_published_post
from app.shared.media import MEDIA_DIRECTORY, can_upload_media, media_local_path, media_of, queue_probe, with_media
from app.shared.tasks import task_selector
from app.shared.tasks.maintenance import delete_from_s3
from app.utils import (can_create_post, download_from_s3, ensure_directory_exists, get_recipient_language, get_setting,
                       get_task_session, gibberish,
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
        fps = round(num / den) if den else 0
        return fps if fps > 0 else None
    except (AttributeError, ValueError, OverflowError):   # a 400-digit rate divides to more than a float holds
        return None


def _duration(probe: dict) -> float:
    try:
        duration = float(probe['format']['duration'])
    except (KeyError, TypeError, ValueError, OverflowError):   # a 309-digit JSON integer is an int float() refuses
        return 0.0
    return duration if math.isfinite(duration) else 0.0   # 'inf' parses, and math.ceil of it raises


def _is_cover_art(stream) -> bool:
    if not isinstance(stream, dict):   # ffprobe's streams are untrusted JSON: a number or a list is no cover art
        return False
    disposition = stream.get('disposition')
    return isinstance(disposition, dict) and bool(disposition.get('attached_pic'))


def _in(codec, allowed: set) -> bool:
    return isinstance(codec, str) and codec in allowed   # a list or dict codec_name is unhashable


def validate_probe(probe: dict, kind: str) -> dict:
    """The media record's metadata fields for ffprobe's `probe` of a `kind` file, or MediaToolError(reason)."""
    if not isinstance(probe, dict):
        raise MediaToolError('ffprobe output was unreadable')
    streams = probe.get('streams')
    if not isinstance(streams, list):
        raise MediaToolError('ffprobe output was unreadable')
    streams = [s for s in streams if isinstance(s, dict)]
    duration = _duration(probe)
    if not duration > 0:
        raise MediaToolError('the file has no duration')
    if kind == MEDIA_KIND_VIDEO:
        video = next((s for s in streams if s.get('codec_type') == 'video' and not _is_cover_art(s)), None)
        if video is None:
            raise MediaToolError('the file has no video stream')
        if not _in(video.get('codec_name'), VIDEO_CODECS):
            raise MediaToolError(f"video codec {video.get('codec_name')} is not h264, vp9 or av1")
        width, height = video.get('width') or 0, video.get('height') or 0
        if not all(type(side) is int and side > 0 for side in (width, height)):   # ffprobe writes integers
            raise MediaToolError('the video has no dimensions')
        meta = {'duration_s': math.ceil(duration), 'width': width, 'height': height, 'codec': video['codec_name']}
        fps = _fps(video.get('avg_frame_rate'))
        if fps:
            meta['fps'] = fps
        return meta
    audio = next((s for s in streams if s.get('codec_type') == 'audio'), None)
    if audio is None:
        raise MediaToolError('the file has no audio stream')
    if not _in(audio.get('codec_name'), AUDIO_CODECS):
        raise MediaToolError(f"audio codec {audio.get('codec_name')} is not aac, mp3, opus or vorbis")
    return {'duration_s': math.ceil(duration), 'codec': audio['codec_name']}


# One UPDATE moves the media status, only from `from_status` (and, given a token, only for the run that claimed with it)
# and only for an undeleted post still POST_STATUS_PROCESSING (so no move touches a ready, failed or published upload), and merges `fields` and the `counter` (attempts, or requeues) into the media record. Postgres re-reads the WHERE of a row another UPDATE changed meanwhile, so of
# two runs racing for one upload exactly one moves it: the compare-and-set is the statement, no read-check-write.
_MOVE_MEDIA_STATUS = text(
    "UPDATE post SET extensions = jsonb_set(extensions::jsonb, '{media}', (extensions::jsonb -> 'media')"
    " || CAST(:fields AS jsonb)"
    " || jsonb_build_object(CAST(:counter AS text),"
    " COALESCE((extensions -> 'media' ->> CAST(:counter AS text))::int, 0) + :bump))::json"
    " WHERE id = :post_id AND deleted = false AND status = :processing"
    " AND extensions -> 'media' ->> 'status' = :from_status"
    " AND (CAST(:token AS text) IS NULL OR extensions -> 'media' ->> 'probing_since' = CAST(:token AS text))")
STUCK_AFTER = timedelta(minutes=30)   # a probe is bounded by its tools' 60 s limits and one S3 upload
# An upload not yet claimed may only be waiting behind a long background queue: a lost task is assumed later
UNCLAIMED_AFTER = timedelta(hours=2)
MAX_ATTEMPTS = 3


def _move_media_status(post_id, from_status: str, to_status: str, bump: int = 0, token: str | None = None,
                       counter: str = 'attempts', **stamps) -> bool:
    moved = db.session.execute(_MOVE_MEDIA_STATUS, {
        'post_id': post_id, 'from_status': from_status, 'bump': bump, 'token': token, 'counter': counter,
        'processing': POST_STATUS_PROCESSING,
        'fields': json.dumps({'status': to_status, **stamps})})
    db.session.commit()
    return moved.rowcount == 1


def _release(post_id, token: str) -> None:
    """Put a claimed upload back to processing after an unexpected error, so no run leaves it claimed; only this run's
    claim, so a stale run never frees the live one's. If that fails too, it is logged and not raised: the caller
    re-raises the error that broke the run, and the sweep recovers it."""
    try:
        _move_media_status(post_id, 'probing', 'processing', token=token)
    except Exception as error:
        db.session.rollback()
        current_app.logger.error(f'probe_media: post {post_id}: could not release the claim: {error!r}')


@celery.task
def probe_media(send_async, post_id):
    session = get_task_session()
    try:
        with patch_db_session(session):
            # 'probing' while this run works: a second delivery of the task finds it claimed and leaves it alone. The
            # claim's probing_since is this run's token: if the sweep takes the upload back from a run it thinks lost
            # and another run claims it, this one sees a different token and writes nothing.
            token = utcnow().isoformat()
            if not _move_media_status(post_id, 'processing', 'probing', bump=1, probing_since=token):
                return
            try:
                _probe(post_id, token)
            except Exception:
                db.session.rollback()
                _release(post_id, token)
                raise
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _probe(post_id, token: str) -> None:
    """The claimed upload of an undeleted local post: ready and published, or failed with a reason, by the run that
    still holds the claim; a run that lost it removes its own files and writes nothing.

    Nothing of the post is written before the claim is checked: an UPDATE would lock its row through the S3 upload,
    and every other write to the post (an edit, a removal, a ban's bulk update) would wait on that upload."""
    post = db.session.get(Post, post_id)
    media = media_of(post)
    written = []        # every image file this run has started to write, so a failure can remove it
    thumbnail = None    # the thumbnail's File row, once it exists
    media_url = None    # the media's bucket URL; the local copy goes once post.url naming it is committed
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
            media_url = _move_to_s3(post, path, thumbnail, written)
    except MediaToolError as error:
        if not _holds_claim(post, token):
            _abandon(post, thumbnail, written)
            return
        _fail(post, thumbnail, written, str(error))
        return
    if not _holds_claim(post, token):
        _abandon(post, thumbnail, written)
        return
    if media_url is not None:
        post.url = media_url
    refusal = publish_refusal(post)
    published = False
    if refusal is not None:
        _fail(post, thumbnail, written, refusal)
    else:
        if thumbnail is not None:
            post.image = thumbnail
            meta['thumbnail_file_id'] = thumbnail.id
            meta['thumbnail_width'], meta['thumbnail_height'] = thumbnail.width, thumbnail.height
        with_media(post, status='ready', **meta)
        published = _commit_ready_post(post)
    if media_url is not None:   # committed: the post names the bucket, so nothing after this may keep the local copy
        _drop_local_copy(post, path)
    if refusal is None and thumbnail is not None:   # before the announce, so an error federating it cannot skip them
        make_image_sizes(thumbnail.id, 512, 1200, 'posts', post.community.low_quality)
    if refusal is None and wants_renditions(post):   # after the local copy went to the bucket: the post never waits
        task_selector('make_renditions', post_id=post.id)
    elif refusal is None and wants_hls(post):   # no renditions to wait for (H4)
        task_selector('make_hls', post_id=post.id)
    if published:
        _announce_post(post)


def _thumbnail(post, path, kind, probe, written) -> File | None:
    """The post's thumbnail as a File (A13.4), or None for audio with no cover art. Only the File row is written: the
    caller makes it post.image once it holds the claim, so the post's row is not locked meanwhile."""
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
        image_place, (width, height) = _save_thumbnail_image(frame, written)
    finally:
        _remove(frame)
    # on the post's domain, as its media is: a worker acts as whoever queued it, which need not be the author (C3)
    file = File(source_url=f"{federation_base(post)}{served_path(image_place)}", width=width, height=height,
                file_name=os.path.basename(image_place))   # our own file (File.wrote_its_source)
    db.session.add(file)
    db.session.flush()
    return file


def _save_thumbnail_image(frame, written) -> tuple:
    """Re-encode `frame` as edit_post does an uploaded image, into the media tree; returns where it went and its size."""
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
        size = img.size
    return image_place, size


def _remove(path: str) -> None:
    if os.path.exists(path):
        os.unlink(path)


def _s3_key(local_path: str) -> str:
    return 'posts/' + local_path.removeprefix(MEDIA_DIRECTORY + '/')   # posts/xx/yy/<name><ext>


def _move_to_s3(post, path, thumbnail, written) -> str:
    """The thumbnail, then the media, into the bucket; returns the media's URL, for the caller to write to post.url
    once it holds the claim. The media goes last: if it fails, the thumbnail's File row names the bucket and its
    removal takes the object with it, and the post keeps its local file."""
    try:
        if thumbnail is not None:
            image_place = written[-1]
            thumbnail.source_url = upload_file_to_s3(image_place, _s3_key(image_place), guess_mime_type(image_place))
            _drop_local_copy(post, image_place)
        return upload_file_to_s3(path, _s3_key(path), media_of(post)['mime'])
    except _STORE_ERRORS as error:
        current_app.logger.warning(f'probe_media: post {post.id}: S3 refused it: {error!r}')
        raise MediaToolError('the file could not be stored') from error


def _holds_claim(post, token: str) -> bool:
    """Whether this run still holds the claim, read under a row lock that the next commit releases: the sweep cannot
    take the upload back between this check and the run's own ready or failed write."""
    db.session.flush()
    # The lock is taken by a plain select of the row: Post's joined eager loads refuse FOR UPDATE on a refresh
    db.session.execute(select(Post.id).where(Post.id == post.id).with_for_update())
    db.session.refresh(post)
    media = media_of(post) or {}
    return media.get('status') == 'probing' and media.get('probing_since') == token


def _abandon(post, thumbnail, written) -> None:
    """A run that lost its claim: another run owns the upload now. Only this run's thumbnail and image files go. The
    media object in the bucket and the local media file stay: the winner uploads under the same key and may still
    be reading the file."""
    current_app.logger.warning(f'probe_media: post {post.id}: the claim was taken back; this run stops')
    if thumbnail is not None:
        thumbnail.delete_from_disk()
    db.session.rollback()
    for image_place in written:
        _remove(image_place)


def _drop_local_copy(post, path) -> None:
    """Remove a local file now safe in the bucket. Failing to is logged, not a failure: the post names the object."""
    try:
        os.unlink(path)
    except OSError as error:
        current_app.logger.warning(f'probe_media: post {post.id}: uploaded, but the local copy stayed: {error!r}')


def _fail(post, thumbnail, written, reason: str) -> None:
    """A13.9: the post stays unpublished, its media record says why, its author is told, and its file goes: no retry
    reads it, and keeping it would host bytes nobody can see."""
    if thumbnail is not None:   # never post.image: only a ready post gets its thumbnail
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
    _remove_media_file(post)


def _remove_media_file(post) -> None:
    """Remove a failed post's media, locally or in the bucket, as the owning post's purge does (A13.8): a post that
    only links to its URL is not a reference, and does not keep it."""
    local = media_local_path(post)
    if local is not None:
        with suppress(FileNotFoundError):
            os.unlink(local)
    s3_key = s3_key_from_url(post.url) if store_files_in_s3() else None
    if s3_key:
        if current_app.debug:
            delete_from_s3([s3_key])
        else:
            delete_from_s3.delay([s3_key])


def publish_refusal(post) -> str | None:
    """Why `post` may not be published now, or None. The upload was allowed when it was made, but probing takes
    time: the author, the community or the post can change before it is ready. The scheduled publisher re-checks
    can_create_post at publish time for the same reason (app/cli.py, F7). The rows are read again, so a change another
    transaction committed while probing is seen, and moderator status is read from its row, not from the cached
    Community.moderators list. The post itself the caller has just read again, under lock (_holds_claim)."""
    db.session.flush()
    for row in (post.author, post.community):
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
    if _commit_ready_post(post):
        _announce_post(post)


def _commit_ready_post(post) -> bool:
    """Commit the ready post as published, as held for review if its author's posts need approval, or as scheduled
    if it has a schedule; whether it was published now."""
    if post.author.ban_posts:
        # v1.8.10's approval queue: make_post holds such an author's other posts for review, and an upload is held
        # the same way when it is ready, whether the flag was set before the upload or while it was processing
        post.status = POST_STATUS_REVIEWING
        db.session.commit()
        notify_admins_of_post_needing_approval(post)
        return False
    if post.scheduled_for:
        post.status = POST_STATUS_SCHEDULED
        db.session.commit()
        return False
    post.status = POST_STATUS_PUBLISHED
    post.posted_at = utcnow()
    post.community.last_active = utcnow()   # a podcast feed's ETag is its last_active; make_post bumped it while the upload processed
    db.session.commit()
    return True


def _announce_post(post) -> None:
    """Federate a just-published post and tell its subscribers: the side effects after the publishing commit."""
    task_selector('make_post', post_id=post.id)
    feature_published_post(post)   # after the Create, so peers have the post the Add names
    notify_about_post(post)


def _stamp(value) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def requeue_stuck_media() -> None:
    """A13.9: the per-minute sweep for uploads a lost run left behind. Celery acks early, so a worker killed mid-probe
    (OOM, SIGKILL, a deploy restart) loses its task and the post would wait forever.

    A probe claimed more than STUCK_AFTER ago, and an upload not claimed within UNCLAIMED_AFTER of its upload or of its
    last requeue, is put back to processing and queued again. An unclaimed upload gets longer: it may only be waiting
    its turn behind a backlog on the background queue. Claims count `attempts` and requeues count `requeues`, so a
    task lost before it ever claims is capped too: at MAX_ATTEMPTS of either, the upload fails and its author is told.
    Each move is the compare-and-set probe_media claims with, so a probe that is in fact alive keeps its upload. One
    post the sweep cannot handle is logged and skipped: the sweep runs inside send-queue's minute, whose other work
    must still run."""
    now = utcnow()
    waiting = Post.extensions['media']['status'].as_string().in_(('processing', 'probing'))   # never a failed post
    for post in Post.query.filter(Post.status == POST_STATUS_PROCESSING, Post.deleted == False,
                                  waiting).order_by(Post.id).all():
        post_id = post.id   # read now: the rollback expires the post, and reloading it can fail too
        try:
            _sweep(post, now)
        except Exception as error:
            db.session.rollback()
            current_app.logger.error(f'requeue_stuck_media: post {post_id}: {error!r}')


def _sweep(post, now) -> None:
    media = media_of(post) or {}
    status = media.get('status')
    if status == 'probing':
        since, budget = _stamp(media.get('probing_since')), STUCK_AFTER
    elif status == 'processing':
        since, budget = _stamp(media.get('requeued_at')) or post.created_at, UNCLAIMED_AFTER
    else:
        return   # ready, failed or unknown: nothing is waiting. The query leaves these out, but the row was read earlier
    if since is not None and since > now - budget:
        return
    if max(media.get('attempts') or 0, media.get('requeues') or 0) >= MAX_ATTEMPTS:
        if _move_media_status(post.id, status, 'failed'):
            _fail(post, None, [], 'processing timed out')
    elif _move_media_status(post.id, status, 'processing', bump=1, counter='requeues',
                            requeued_at=utcnow().isoformat()):
        queue_probe(post.id)


# renditions (W2.1b, ruling V3)

def rendition_sides(media: dict) -> list[int]:
    """The rendition sizes a video of `media`'s dimensions gets: each of RENDITION_SIDES below its short side."""
    short = min(media.get('width') or 0, media.get('height') or 0)
    return [side for side in RENDITION_SIDES if side < short]


def wants_renditions(post) -> bool:
    """A ready local video upload with no renditions recorded yet, and room for one."""
    media = media_of(post) if post is not None else None
    return bool(media is not None and post.is_local() and media.get('status') == 'ready'
                and str(media.get('mime', '')).startswith('video/') and 'renditions' not in media
                and all(type(media.get(side)) is int for side in ('width', 'height')) and rendition_sides(media))


@celery.task
def make_renditions(send_async, post_id):
    session = get_task_session()
    try:
        with patch_db_session(session):
            _make_renditions(post_id)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _make_renditions(post_id) -> None:
    """Each size `rendition_sides` names, transcoded beside the source (into the bucket, with S3), then recorded under
    the post's row lock. A size that fails is logged and dropped; none of it touches the post's status or its file."""
    post = db.session.get(Post, post_id)
    if not wants_renditions(post):
        return
    media = media_of(post)
    sides, portrait = rendition_sides(media), media['height'] > media['width']
    timeout_s = transcode_timeout(media.get('duration_s') if type(media.get('duration_s')) is int else 0)
    local, key = media_local_path(post), None
    if local is None and store_files_in_s3():
        key = s3_key_from_url(post.url)
    db.session.commit()   # no transaction held open through the transcodes
    source = _rendition_source(post_id, local, key)
    if source is None:
        return
    made = []   # (side, width, height, bytes, where): a local path, or with S3 the object key
    deadline = monotonic() + TRANSCODE_MAX_S   # one budget for every size of this upload
    try:
        for side in sides:
            left = int(deadline - monotonic())
            if left <= 0:
                current_app.logger.warning(f'make_renditions: post {post_id}: no time left for {side}p or smaller')
                break
            out = rendition_name(local, side) if key is None else f'{TMP_DIRECTORY}/{gibberish(15)}_{side}p.mp4'
            try:
                transcode(source, out, side, portrait, min(timeout_s, left))
                width, height = _dimensions(out)
                size, where = os.path.getsize(out), out
                if key is not None:
                    where = rendition_name(key, side)
                    upload_file_to_s3(out, where, 'video/mp4')
                    _remove(out)
            except (MediaToolError, *_STORE_ERRORS) as error:
                current_app.logger.warning(f'make_renditions: post {post_id}: no {side}p: {error!r}')
                _remove(out)
                continue
            made.append((side, width, height, size, where))
    finally:
        if key is not None:
            _remove(source)
    _record_renditions(post_id, made, key is not None)


def _rendition_source(post_id, local, key) -> str | None:
    """The source file to transcode: the stored file, or with S3 a copy of the object in TMP_DIRECTORY."""
    if key is None:
        if local is None or not os.path.isfile(local):
            current_app.logger.warning(f'make_renditions: post {post_id}: the source file is missing')
            return None
        return local
    ensure_directory_exists(TMP_DIRECTORY)
    copy = f'{TMP_DIRECTORY}/{gibberish(15)}{os.path.splitext(key)[1]}'
    try:
        download_from_s3(key, copy)
    except _STORE_ERRORS as error:
        current_app.logger.warning(f'make_renditions: post {post_id}: the source could not be read: {error!r}')
        _remove(copy)
        return None
    return copy


def _dimensions(path) -> tuple[int, int]:
    video = next((stream for stream in run_ffprobe(path).get('streams', [])
                  if isinstance(stream, dict) and stream.get('codec_type') == 'video'), {})
    width, height = video.get('width'), video.get('height')
    if not (type(width) is int and type(height) is int and width > 0 and height > 0):
        raise MediaToolError('the rendition has no dimensions')
    return width, height


def _record_renditions(post_id, made, in_s3: bool) -> None:
    """Record `made` in the media record, under the post's row lock. A post purged meanwhile, or whose upload is no
    longer ready, keeps nothing: its renditions are removed here, since nothing will name them. One another run
    recorded first keeps that run's record; the files are the same names, so they stay."""
    locked = db.session.execute(select(Post.id).where(Post.id == post_id).with_for_update()).first()
    post = db.session.get(Post, post_id) if locked is not None else None
    if post is not None:
        db.session.refresh(post)
    media = media_of(post) if post is not None else None
    if media is not None and 'renditions' in media:
        db.session.commit()
        return
    if media is None or media.get('status') != 'ready':
        db.session.commit()
        _discard_renditions(post_id, made, in_s3)
        return
    with_media(post, renditions=[{'side': side, 'width': width, 'height': height, 'bytes': size}
                                 for side, width, height, size, _ in made])
    db.session.commit()
    if wants_hls(post):   # its variants made, the HLS copies can be (H4)
        task_selector('make_hls', post_id=post.id)
    if made and post.status == POST_STATUS_PUBLISHED and uses_peertube_video(post):
        task_selector('edit_post', post_id=post.id)   # peers that have the Video learn its new files


def _discard_renditions(post_id, made, in_s3: bool) -> None:
    current_app.logger.warning(f'make_renditions: post {post_id}: gone or no longer ready; its renditions go')
    if in_s3:
        if made:
            delete_from_s3([where for *_, where in made])
        return
    for *_, where in made:
        _remove(where)


# HLS (owner rulings H4, HL1-HL3): while the admin offers it, a ready video upload, once its renditions are made, is
# also offered as HLS: each MP4 it has (the upload itself when its codecs play in HLS, then each rendition)
# repackaged, never encoded again, into `<name>_hls/` beside it, with a master playlist naming them; into the bucket
# with S3. New uploads only: the admin's backfill makes them for the rest, and the admin's delete removes them all.

HLS_VIDEO_CODECS = {'h264'}
HLS_AUDIO_CODECS = {'aac', 'mp3'}
_PLAYLIST_TYPE = 'application/vnd.apple.mpegurl'


def hls_offered() -> bool:
    return bool(get_setting(HLS_SETTING, False))


def wants_hls(post) -> bool:
    """A ready local video upload, no HLS copies recorded yet, and no renditions still to make, while HLS is offered."""
    media = media_of(post) if post is not None else None
    return bool(media is not None and hls_offered() and post.is_local() and media.get('status') == 'ready'
                and str(media.get('mime', '')).startswith('video/') and 'hls' not in media and not wants_renditions(post))


@celery.task
def make_hls(send_async, post_id):
    session = get_task_session()
    try:
        with patch_db_session(session):
            _make_hls(post_id)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _plays_in_hls(path: str) -> bool:
    """Whether the upload's own streams can go into HLS as they are: H.264 video, and AAC or MP3 audio or none."""
    found = {stream.get('codec_type'): stream.get('codec_name') for stream in run_ffprobe(path).get('streams', [])
             if isinstance(stream, dict) and stream.get('codec_type') in ('video', 'audio')}
    return found.get('video') in HLS_VIDEO_CODECS and found.get('audio', 'aac') in HLS_AUDIO_CODECS


def _make_hls(post_id) -> None:
    """Each variant packaged into the HLS directory (a scratch one, uploaded after, with S3), then the master playlist,
    then recorded under the post's row lock. A variant that fails is logged and dropped."""
    post = db.session.get(Post, post_id)
    if not wants_hls(post):
        return
    media = media_of(post)
    duration_s = max(media['duration_s'] if type(media.get('duration_s')) is int else 0, 1)
    sources = [('source', None, media.get('width'), media.get('height'))] + [
        (f"{copy['side']}p", copy['side'], copy['width'], copy['height']) for copy in renditions_of(post)]
    local, key = media_local_path(post), None
    if local is None and store_files_in_s3():
        key = s3_key_from_url(post.url)
    db.session.commit()   # no transaction held open through the packaging
    if local is None and key is None:
        return
    directory = hls_directory(local) if key is None else f'{TMP_DIRECTORY}/{gibberish(15)}_hls'
    ensure_directory_exists(directory)
    made = []   # (name, width, height, bandwidth, bytes)
    deadline = monotonic() + TRANSCODE_MAX_S
    for name, side, width, height in sources:
        if not (type(width) is int and type(height) is int):
            continue
        source = _hls_source(post_id, local, key, side)
        if source is None:
            continue
        playlist, packaged = f'{directory}/{name}.m3u8', f'{directory}/{name}.mp4'
        try:
            if side is None and not _plays_in_hls(source):
                continue
            hls_package(source, playlist, packaged, max(int(deadline - monotonic()), 1))
            size = os.path.getsize(packaged)
            made.append((name, width, height, math.ceil(size * 8 / duration_s), size))
        except MediaToolError as error:
            current_app.logger.warning(f'make_hls: post {post_id}: no {name}: {error!r}')
            _remove(playlist)
            _remove(packaged)
        finally:
            if key is not None:
                _remove(source)
    if made:
        _write_master(f'{directory}/master.m3u8', made)
    if key is not None:
        made = _upload_hls(post_id, directory, key, made)
    _record_hls(post_id, made, key is not None, local)


def _hls_source(post_id, local, key, side) -> str | None:
    """The MP4 a variant packages: the upload or its rendition at `side`, or with S3 a scratch copy of the object."""
    if key is None:
        path = local if side is None else rendition_name(local, side)
        if os.path.isfile(path):
            return path
        current_app.logger.warning(f'make_hls: post {post_id}: {path} is missing')
        return None
    return _rendition_source(post_id, None, key if side is None else rendition_name(key, side))


def _write_master(path: str, made) -> None:
    lines = ['#EXTM3U', '#EXT-X-VERSION:7', '#EXT-X-INDEPENDENT-SEGMENTS']
    for name, width, height, bandwidth, _size in made:
        lines += [f'#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},RESOLUTION={width}x{height}', f'{name}.m3u8']
    with open(path, 'w') as master:
        master.write('\n'.join(lines) + '\n')


def _upload_hls(post_id, directory, key, made) -> list:
    """The scratch directory's files into the bucket under the upload's HLS prefix, then the directory removed. A
    failed upload loses the HLS copies: what went up is removed, and nothing is recorded."""
    prefix, sent = hls_directory(key), []
    try:
        for name in ([f'{variant}.{extension}' for variant, *_ in made for extension in ('m3u8', 'mp4')]
                     + (['master.m3u8'] if made else [])):
            upload_file_to_s3(f'{directory}/{name}', f'{prefix}/{name}',
                              _PLAYLIST_TYPE if name.endswith('.m3u8') else 'video/mp4')
            sent.append(f'{prefix}/{name}')
    except _STORE_ERRORS as error:
        current_app.logger.warning(f'make_hls: post {post_id}: the HLS copies could not be stored: {error!r}')
        if sent:
            delete_from_s3(sent)
        made = []
    finally:
        _remove_directory(directory)
    return made


def _remove_directory(directory: str) -> None:
    if os.path.isdir(directory):
        for name in os.listdir(directory):
            _remove(os.path.join(directory, name))
        with suppress(OSError):
            os.rmdir(directory)


def _record_hls(post_id, made, in_s3: bool, local) -> None:
    """Record `made` in the media record, under the post's row lock. A post purged meanwhile, or no longer ready,
    keeps nothing: its HLS copies are removed here. One another run recorded first keeps that run's record and files
    (the same names), and so does a run that made nothing: an empty record is not tried again."""
    locked = db.session.execute(select(Post.id).where(Post.id == post_id).with_for_update()).first()
    post = db.session.get(Post, post_id) if locked is not None else None
    if post is not None:
        db.session.refresh(post)
    media = media_of(post) if post is not None else None
    if media is not None and 'hls' in media:
        db.session.commit()
        return
    if media is None or media.get('status') != 'ready':
        db.session.commit()
        current_app.logger.warning(f'make_hls: post {post_id}: gone or no longer ready; its HLS copies go')
        _discard_hls(post_id, local, s3_key_from_url(post.url) if in_s3 and post is not None else None)
        return
    with_media(post, hls=[{'name': name, 'width': width, 'height': height, 'bandwidth': bandwidth, 'bytes': size}
                          for name, width, height, bandwidth, size in made])
    db.session.commit()


def _discard_hls(post_id, local, key) -> None:
    if key is not None:
        delete_from_s3(hls_names(key))
    elif local is not None:
        _remove_directory(hls_directory(local))


def _in_task_session(work) -> None:
    session = get_task_session()
    try:
        with patch_db_session(session):
            work()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _video_ids() -> list[int]:
    """Every video post's id: the candidates the admin's HLS buttons look at, each judged on its own record."""
    return [row.id for row in db.session.query(Post.id).filter(Post.type == POST_TYPE_VIDEO,
                                                               Post.extensions.isnot(None)).order_by(Post.id)]


@celery.task
def backfill_hls(send_async):
    """HL1: the admin's button. Every local video upload that HLS would be made for now, queued for it."""
    _in_task_session(_backfill_hls)


def _backfill_hls() -> None:
    queued = 0
    for post_id in _video_ids():
        if wants_hls(db.session.get(Post, post_id)):
            task_selector('make_hls', post_id=post_id)
            queued += 1
    current_app.logger.info(f'backfill_hls: queued {queued} videos')


@celery.task
def delete_all_hls(send_async):
    """HL2: the admin's button. Every recorded HLS copy removed, on disk and in the bucket, and its record with it, so
    a later backfill makes it again."""
    _in_task_session(_delete_all_hls)


def _delete_all_hls() -> None:
    removed = 0
    for post_id in _video_ids():
        post = db.session.get(Post, post_id)
        media = media_of(post)
        if media is None or 'hls' not in media:
            continue
        local = media_local_path(post)
        key = s3_key_from_url(post.url) if local is None and store_files_in_s3() else None
        extensions = dict(post.extensions)
        extensions['media'] = {name: value for name, value in media.items() if name != 'hls'}
        post.extensions = extensions
        db.session.commit()   # the record first: a file left by a failed removal is no longer offered
        _discard_hls(post_id, local, key)
        removed += 1
    current_app.logger.info(f'delete_all_hls: removed the HLS copies of {removed} videos')

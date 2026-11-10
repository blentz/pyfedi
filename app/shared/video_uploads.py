"""A video uploaded in parts (PeerTube's resumable upload; plan W2.3, spec 2026-11-10-peertube-upload-design §3): its
rules, where its parts are kept, and the post it becomes.

The parts are kept on the media volume, which the web process writes and the maintenance clears, under a name of 160
random bits that nothing shows. The post is made through make_post, as the web's form makes a video post, once the
details come: its visibility is fixed when it is made (Amendment B), and the app sends it after the last byte."""
import contextlib
import os
import re
import secrets
from datetime import timedelta

from flask import current_app
from werkzeug.datastructures import FileStorage

from app import db
from app.constants import MEDIA_KIND_VIDEO, POST_TYPE_VIDEO, SRC_API
from app.models import VideoUpload, utcnow
from app.shared import post as shared_post
from app.shared.media import upload_byte_limit
from app.utils import Authorised, ensure_directory_exists, site_language_id

MAX_IN_PROGRESS = 3   # uploads a person may have waiting for a post, finished or not (ruling U2)
KEPT = timedelta(hours=24)   # an upload with no post is removed this long after its last part (ruling U2)
PUBLIC, UNLISTED, PRIVATE, INTERNAL, PASSWORD = 1, 2, 3, 4, 5   # PeerTube's privacy ids
# Ruling U1: public, and unlisted made public (PieFed has no local unlisted post); internal is a local-only post
TAKEN_PRIVACY = (PUBLIC, UNLISTED, INTERNAL)
PENDING_UUID = re.compile(r'00000000-0000-4000-9000-([0-9a-f]{12})')


class Refused(Exception):
    """An upload's refusal: `status` and PeerTube's `code`, with the message the app shows."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code = status, code


def partial_directory() -> str:
    return current_app.config.get('VIDEO_UPLOAD_PARTIALS') or 'app/static/media/partial_uploads'


def pending_uuid(upload: VideoUpload) -> str:
    """The uuid an upload is known by before its post is made, and after: a namespace of its own (W2.1's video_uuid
    names posts)."""
    return f'00000000-0000-4000-9000-{upload.id:012x}'


def pending_upload(key: str, user) -> VideoUpload | None:
    """The upload `key` (a pending uuid) names, if it is `user`'s."""
    match = PENDING_UUID.fullmatch(key)
    if match is None:
        return None
    upload = db.session.get(VideoUpload, int(match[1], 16))
    return upload if upload is not None and upload.user_id == user.id else None


def checked_privacy(value) -> int:
    """PeerTube's privacy id as one PieFed keeps (U1), or Refused."""
    if isinstance(value, bool) or not isinstance(value, int) or value not in (PUBLIC, UNLISTED, PRIVATE, INTERNAL,
                                                                               PASSWORD):
        raise Refused(400, 'invalid_request', 'privacy is 1 (public), 2 (unlisted) or 4 (internal) here')
    if value not in TAKEN_PRIVACY:
        raise Refused(400, 'invalid_request', 'PieFed has no private or password-protected videos: choose public, '
                                              'unlisted (shown as public) or internal')
    return value


def start(user, community, filename: str, name: str, content_type: str, size: int, details: dict) -> VideoUpload:
    """A new upload of `size` bytes, after every check that needs no byte (ruling U2), or Refused."""
    limit = upload_byte_limit(MEDIA_KIND_VIDEO)
    if size > limit:
        raise Refused(413, 'max_file_size_reached', f'A video may be at most {limit // (1024 * 1024)} MB here')
    # every upload no post has come of holds its file until the sweep, finished or not: each counts (security review)
    waiting = VideoUpload.query.filter(VideoUpload.user_id == user.id, VideoUpload.post_id.is_(None),
                                       VideoUpload.updated_at > utcnow() - KEPT).all()
    if len(waiting) >= MAX_IN_PROGRESS:
        raise Refused(429, 'too_many_requests', f'At most {MAX_IN_PROGRESS} uploads may wait to be published at once')
    if sum(upload.size for upload in waiting) + size > 2 * limit:
        raise Refused(413, 'quota_reached', 'Your waiting uploads are too large together: publish or cancel one')
    if 'privacy' in details:
        checked_privacy(details['privacy'])
    directory = partial_directory()
    ensure_directory_exists(directory)
    path = os.path.join(directory, secrets.token_hex(20) + '.part')
    open(path, 'wb').close()
    upload = VideoUpload(upload_id=secrets.token_urlsafe(24), user_id=user.id, community_id=community.id,
                         filename=filename, name=name, content_type=content_type, size=size, path=path,
                         details=details or None)
    db.session.add(upload)
    db.session.commit()
    return upload


def receive(upload: VideoUpload, first: int, stream, length: int) -> None:
    """Write `length` bytes from `stream` at `first`, which is what `upload` holds so far; Refused if fewer came."""
    with open(upload.path, 'r+b') as out:
        out.seek(first)
        written = 0
        while written < length:
            block = stream.read(min(1024 * 1024, length - written))
            if not block:
                break
            out.write(block)
            written += len(block)
        if written != length:
            out.truncate(first)
            raise Refused(400, 'invalid_request', 'The part was shorter than its Content-Range')
    upload.received = first + length
    upload.updated_at = utcnow()
    if upload.received == upload.size:
        upload.finished_at = upload.updated_at
    db.session.commit()


def make_post(upload: VideoUpload, details: dict):
    """The video post `upload` becomes, made as the web's form makes one (make_post: its checks, its media pipeline,
    its federation), with `details` (PeerTube's names) at creation. The parts' file is removed once it is stored."""
    privacy = checked_privacy(details.get('privacy', PUBLIC))
    tags = details.get('tags')
    language_id = site_language_id()
    payload = {'title': (details.get('name') or upload.name)[:255], 'body': str(details.get('description') or ''),
               'url': None, 'nsfw': details.get('nsfw') is True, 'language_id': language_id, 'notify_author': True,
               'ai_generated': False, 'local_only': privacy == INTERNAL}
    if isinstance(tags, list):
        payload['tags'] = ', '.join(str(tag) for tag in tags)
    with open(upload.path, 'rb') as stream:
        uploaded = FileStorage(stream=stream, filename=upload.filename, content_type=upload.content_type)
        _user_id, post = shared_post.make_post(payload, upload.community, POST_TYPE_VIDEO, SRC_API,
                                               Authorised(upload.user), uploaded_file=uploaded)
    upload.post_id = post.id
    db.session.commit()
    with contextlib.suppress(FileNotFoundError):
        os.unlink(upload.path)
    return post


def remove(upload: VideoUpload) -> None:
    with contextlib.suppress(FileNotFoundError):
        os.unlink(upload.path)
    db.session.delete(upload)
    db.session.commit()


def sweep() -> int:
    """Remove the uploads no post came of, KEPT past their last part (ruling U2): unfinished, or finished with no
    details. Hourly, with the rest of the media's expiry (flask expire_old_media). How many went."""
    stale = VideoUpload.query.filter(VideoUpload.post_id.is_(None), VideoUpload.updated_at < utcnow() - KEPT).all()
    for upload in stale:
        remove(upload)
    return len(stale)

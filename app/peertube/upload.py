"""Uploading a video in PeerTube's API (plan W2.3, spec 2026-11-10-peertube-upload-design §2, §3): the resumable
upload PeerTube's own app uses, then the video's details, and deleting it. Who may upload where is the web's rule
(app.shared.media.can_upload_media); the post is made by app.shared.video_uploads, through make_post.

    POST /api/v1/videos/upload-resumable        start: {filename, name, channelId}, X-Upload-Content-Length -> 201
    PUT  /api/v1/videos/upload-resumable?upload_id=  a part (Content-Range) -> 308 with what is held, or 200 at the end
    DELETE /api/v1/videos/upload-resumable?upload_id=  cancel -> 204
    PUT  /api/v1/videos/<id>                     the details (JSON), or a thumbnail (multipart) -> 204
    DELETE /api/v1/videos/<id>                   -> 204
"""
import re

from flask import g, jsonify, make_response, request

from app import db
from app.constants import POST_STATUS_PROCESSING, POST_TYPE_VIDEO, SRC_API
from app.local_only import content_local_only
from app.models import Community, Post, VideoUpload
from app.peertube import bp
from app.peertube.auth import failure, problem, signed_in
from app.peertube.entities import post_id_of, video_uuid
from app.shared import post as shared_post
from app.shared import video_uploads
from app.shared.media import MediaRefused, can_upload_media
from app.shared.video_uploads import Refused
from app.utils import Authorised

UPLOAD_PATH = '/api/v1/videos/upload-resumable'
DETAILS = ('name', 'description', 'privacy', 'nsfw', 'tags')   # what PieFed keeps; PeerTube's others it has no use for
PART = re.compile(r'bytes (\d{1,15})-(\d{1,15})/(\d{1,15})')
WHOLE = re.compile(r'bytes \*/(\d{1,15})')


def _refusal(refused: Refused):
    return problem(refused.status, refused.code, str(refused))


def _json() -> dict:
    fields = request.get_json(silent=True)
    return fields if isinstance(fields, dict) else {}


def _details(fields: dict) -> dict:
    return {name: fields[name] for name in DETAILS if name in fields}


def _held(upload: VideoUpload):
    """308 with the bytes held, as PeerTube answers a part that is not the last, or a question how far it got."""
    response = make_response('', 308)
    if upload.received:
        response.headers['Range'] = f'bytes=0-{upload.received - 1}'
    return response


def _finished(upload: VideoUpload):
    post = db.session.get(Post, upload.post_id) if upload.post_id else None
    uuid = video_uuid(post) if post is not None else video_uploads.pending_uuid(upload)
    return jsonify(video={'id': post.id if post is not None else upload.id, 'shortUUID': uuid, 'uuid': uuid})


def _own_upload() -> VideoUpload | None:
    found = VideoUpload.query.filter_by(upload_id=request.args.get('upload_id', '')).first()
    return found if found is not None and found.user_id == g.oauth_user.id else None


def _no_upload():
    return problem(404, 'not_found', 'No such upload')


@bp.route(UPLOAD_PATH, methods=['POST'])
@signed_in('write')
def start_upload():
    fields = _json()
    size = request.headers.get('X-Upload-Content-Length', '')
    if not size.isdigit() or not 0 < int(size) < 10 ** 15:
        return problem(400, 'invalid_request', 'X-Upload-Content-Length is the file size in bytes')
    filename, name = str(fields.get('filename') or '').strip(), str(fields.get('name') or '').strip()
    if not filename or not name or len(filename) > 256 or len(name) > 256:
        return problem(400, 'invalid_request', 'filename and name are 1 to 256 characters')
    channel = fields.get('channelId')
    community = db.session.get(Community, channel) if isinstance(channel, int) and not isinstance(channel, bool) \
        and 0 < channel < 2 ** 31 else None
    if community is None or not can_upload_media(g.oauth_user, community):
        return problem(403, 'forbidden', 'You may not upload video to this channel')
    content_type = str(request.headers.get('X-Upload-Content-Type') or 'application/octet-stream')[:128]
    try:
        upload = video_uploads.start(g.oauth_user, community, filename, name, content_type, int(size),
                                     _details(fields))
    except Refused as refused:
        return _refusal(refused)
    response = make_response('', 201)
    response.headers['Location'] = f"{request.host_url.rstrip('/')}{UPLOAD_PATH}?upload_id={upload.upload_id}"
    return response


@bp.route(UPLOAD_PATH, methods=['PUT'])
@signed_in('write')
def upload_part():
    upload = _own_upload()
    if upload is None:
        return _no_upload()
    if upload.finished_at is not None:
        return _finished(upload)
    span = request.headers.get('Content-Range', '')
    if (whole := WHOLE.fullmatch(span)) is not None:
        if int(whole[1]) != upload.size:
            return problem(400, 'invalid_request', 'The size is not the one the upload began with')
        return _held(upload)
    part = PART.fullmatch(span)
    if part is None:
        return problem(400, 'invalid_request', 'Content-Range is bytes <first>-<last>/<size>')
    first, last, total = (int(value) for value in part.groups())
    if total != upload.size or last < first or last >= upload.size:
        return problem(400, 'invalid_request', 'The part is not within the upload')
    if first != upload.received:
        return _held(upload)   # a part from elsewhere than what is held: the app resumes from what this says
    if request.content_length != last - first + 1:
        return problem(400, 'invalid_request', 'Content-Length is the part\'s length')
    try:
        video_uploads.receive(upload, first, request.stream, last - first + 1)
        if upload.finished_at is None:
            return _held(upload)
        if upload.details and 'privacy' in upload.details:   # the start carried the details: made now
            video_uploads.make_post(upload, upload.details)
    except Refused as refused:
        return _refusal(refused)
    except MediaRefused as refused:   # the file's first bytes are no video, or it is too big
        return problem(400, 'invalid_request', str(refused))
    except Exception as error:   # make_post's other refusals: a ban, the community gone
        return failure(error)
    return _finished(upload)


@bp.route(UPLOAD_PATH, methods=['DELETE'])
@signed_in('write')
def cancel_upload():
    upload = _own_upload()
    if upload is None:
        return _no_upload()
    video_uploads.remove(upload)
    return '', 204


@bp.route('/api/v1/videos/<video_id>', methods=['PUT'])
@signed_in('write')
def update_video(video_id):
    """The details, which make the post of an upload that has none yet, or edit the post. A thumbnail (multipart) is
    taken and not kept: PieFed makes a video's own from its frames."""
    if request.files:
        return ('', 204) if _owned(video_id) is not None else _no_video()
    fields = _json()
    upload = video_uploads.pending_upload(video_id, g.oauth_user)
    try:
        if upload is not None and upload.post_id is None:
            if upload.finished_at is None:
                return problem(400, 'invalid_request', 'The upload is not finished')
            video_uploads.make_post(upload, {**(upload.details or {}), **_details(fields)})
            return '', 204
        post = _owned(video_id)
        if post is None:
            return _no_video()
        _edit(post, _details(fields))
    except Refused as refused:
        return _refusal(refused)
    except MediaRefused as refused:   # the file's first bytes are no video, or it is too big
        return problem(400, 'invalid_request', str(refused))
    except Exception as error:
        return failure(error)
    return '', 204


@bp.route('/api/v1/videos/<video_id>/chapters', methods=['PUT'])
@signed_in('write')
def update_chapters(video_id):
    """Taken and not kept: PieFed has no chapters. The app sends them with the details, and a refusal stopped it."""
    return ('', 204) if _owned(video_id) is not None else _no_video()


@bp.route('/api/v1/videos/<video_id>', methods=['DELETE'])
@signed_in('write')
def delete_video(video_id):
    upload = video_uploads.pending_upload(video_id, g.oauth_user)
    if upload is not None and upload.post_id is None:
        video_uploads.remove(upload)
        return '', 204
    post = _owned(video_id)
    if post is None:
        return _no_video()
    try:
        shared_post.delete_post(post.id, True, SRC_API, Authorised(g.oauth_user))
    except Exception as error:
        return failure(error)
    return '', 204


def _owned(video_id: str):
    """The video post `video_id` names (its id, its uuid or its upload's), if it is the signed-in person's: found
    while it is still processing too, which a reader's lookup (find_video) does not show."""
    upload = video_uploads.pending_upload(video_id, g.oauth_user)
    if upload is not None:
        post = db.session.get(Post, upload.post_id) if upload.post_id else None
    else:
        post = db.session.get(Post, post_id) if (post_id := post_id_of(video_id)) is not None else None
    return post if post is not None and post.type == POST_TYPE_VIDEO and post.user_id == g.oauth_user.id \
        and not post.deleted else None


def _no_video():
    return problem(404, 'not_found', 'No such video of yours')


def _edit(post, details: dict) -> None:
    """Edit the post's title, body, NSFW and tags as the alpha API's edit does. Its privacy may change while its media
    processes, since until then it is neither published nor federated (the app sends its defaults on its own as the
    upload ends, then the person's choice); once published it is fixed (Amendment B)."""
    if 'privacy' in details:
        internal = video_uploads.checked_privacy(details['privacy']) == video_uploads.INTERNAL
        if internal != bool(post.local_only):
            if post.status != POST_STATUS_PROCESSING:
                raise Refused(400, 'invalid_request', 'A video\'s privacy is fixed once it is published')
            # recomputed, never just cleared: a local-only domain's (the onion's) content stays stamped (review)
            post.local_only = internal or content_local_only(post.author, post.community)
    tags = details.get('tags')
    payload = {'title': str(details.get('name') or post.title)[:255],
               'body': str(details['description']) if details.get('description') is not None else (post.body or ''),
               'url': post.url, 'nsfw': details['nsfw'] is True if 'nsfw' in details else bool(post.nsfw),
               'language_id': post.language_id, 'notify_author': bool(post.notify_author),
               'ai_generated': bool(post.ai_generated)}
    if isinstance(tags, list):
        payload['tags'] = ', '.join(str(tag) for tag in tags)
    shared_post.edit_post(payload, post, post.type, SRC_API, auth=Authorised(g.oauth_user))


@bp.route('/api/v1/videos/privacies')
@signed_in(optional=True)
def privacies():
    """The privacies the upload form offers: those an upload keeps (ruling U1). Unlisted is taken but made public, so
    it is not offered as if it were kept."""
    return jsonify({str(video_uploads.PUBLIC): 'Public', str(video_uploads.INTERNAL): 'Internal'})


@bp.route('/api/v1/videos/categories')
@bp.route('/api/v1/videos/licences')
@bp.route('/api/v1/videos/languages')
@signed_in(optional=True)
def kept_nowhere():
    """The upload form's other lists: PieFed keeps no category, licence or language of a video, so it offers none."""
    return jsonify({})

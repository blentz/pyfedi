"""Media from a Mastodon app (plan W1.3; Tusky's contract in plan W1.0): POST /api/v2/media stores an image through
the shared upload (app.shared.upload.process_upload, which records the uploader in user_file) and answers it at once,
processed (200); GET and PUT /api/v1/media/:id read it and set its description. Only the uploader reaches an upload.
Images only, as the alpha API's uploads: PieFed takes video and audio with a post, from moderators of media communities
(spec A13.6)."""
from flask import g, jsonify, request
from sqlalchemy import text

from app import db
from app.mastodon import bp
from app.mastodon.errors import failure
from app.mastodon.statuses import attachment
from app.models import ALT_TEXT_MAX, File
from app.oauth_server.protect import require_oauth
from app.shared.upload import is_refused_upload, process_upload

_PG_INT_MAX = 2 ** 31 - 1


def own_uploads(user_id: int, file_ids) -> list[File]:
    """The files among `file_ids` that `user_id` uploaded, in the order asked; an id of anyone else's is left out."""
    wanted = [int(raw) for raw in file_ids if str(raw).isdigit() and 0 < int(raw) <= _PG_INT_MAX]
    if not wanted:
        return []
    owned = set(db.session.execute(text('SELECT file_id FROM "user_file" WHERE user_id = :user_id AND '
                                        'file_id IN :ids'), {'user_id': user_id, 'ids': tuple(wanted)}).scalars())
    files = {file.id: file for file in File.query.filter(File.id.in_(owned))}
    return [files[file_id] for file_id in dict.fromkeys(wanted) if file_id in files]


def _description(value) -> str | None:
    return value.strip()[:ALT_TEXT_MAX] or None if isinstance(value, str) else None


@bp.route('/api/v2/media', methods=['POST'])
@bp.route('/api/v1/media', methods=['POST'])
@require_oauth('write:media')
def upload_media():
    upload = request.files.get('file')
    if upload is None or not upload.filename:
        return jsonify({'error': 'Validation failed: File is missing'}), 422
    try:
        url = process_upload(upload, user=g.oauth_user)
    except Exception as error:   # the shared upload refuses a type it does not take (is_refused_upload)
        return failure(error, refusals=(is_refused_upload,))
    file = File.query.filter(File.source_url == url).order_by(File.id.desc()).first()
    file.alt_text = _description(request.form.get('description'))
    db.session.commit()
    return jsonify(attachment(file))


def _own(media_id: int) -> File | None:
    found = own_uploads(g.oauth_user.id, [media_id])
    return found[0] if found else None


@bp.route('/api/v1/media/<int:media_id>')
@require_oauth('write:media')
def get_media(media_id):
    file = _own(media_id)
    return (jsonify(attachment(file)), 200) if file is not None else (jsonify({'error': 'Record not found'}), 404)


@bp.route('/api/v1/media/<int:media_id>', methods=['PUT'])
@require_oauth('write:media')
def update_media(media_id):
    file = _own(media_id)
    if file is None:
        return jsonify({'error': 'Record not found'}), 404
    fields = request.get_json(silent=True) if request.is_json else request.form
    if isinstance(fields, dict) and 'description' in fields:
        file.alt_text = _description(fields.get('description'))
        db.session.commit()
    return jsonify(attachment(file))

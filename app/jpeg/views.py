"""GET /media/jpeg/<file id>.jpg: the JPEG of one of our uploads (app/jpeg), converted the first time it is asked for.

The File row is read on every request, cached or not, so a JPEG whose record is gone is never served, even when the
record went by some path other than File.delete_from_disk (which removes the cache file itself).
"""
import io
import os

from flask import abort, current_app, send_file
import pillow_avif  # noqa: F401  registers Pillow's AVIF plugin, for an AVIF upload
from PIL import Image, ImageOps
from pillow_heif import register_heif_opener

from app import db
from app.jpeg import needs_jpeg
from app.main import bp
from app.models import File, jpeg_rendition_path, s3_key_from_url
from app.utils import read_from_s3, save_image_without_metadata, store_files_in_s3

MAX_DIMENSION = 5000   # Pixelfed refuses an attachment wider or taller (Helpers.php:182 at v0.15.2)
JPEG_QUALITY = 90
CACHE_SECONDS = 30 * 24 * 3600   # one id is one upload for good; a delete purges the CDN copy


@bp.route('/media/jpeg/<int:file_id>.jpg')
def jpeg_rendition(file_id: int):
    path = jpeg_rendition_path(file_id)
    file = db.session.get(File, file_id)
    if not needs_jpeg(file):
        _forget(path)
        abort(404)
    if not os.path.isfile(path):
        original = _original_bytes(file)
        if original is None:
            abort(404)
        try:
            write_jpeg(original, path)
        except Exception as e:   # anything Pillow raises on a file it cannot read: the upload is not convertible
            current_app.logger.warning(f'jpeg_rendition: File {file_id} could not be converted: {e!r}')
            abort(404)
    return send_file(os.path.abspath(path), mimetype='image/jpeg', max_age=CACHE_SECONDS)   # send_file reads a relative path from app/


def _original_bytes(file: File) -> bytes | None:
    """The upload's own bytes: from our bucket, or from the media folders (local_path_for_url confines it there)."""
    if store_files_in_s3():
        key = s3_key_from_url(file.source_url)
        if key:
            try:
                return read_from_s3(key)
            except Exception as e:   # boto's errors have no common base worth naming; a missing object is a 404
                current_app.logger.warning(f'jpeg_rendition: File {file.id} has no object at {key}: {e!r}')
                return None
    local_path = File.local_path_for_url(file.source_url)
    if not local_path or not os.path.isfile(local_path):
        return None
    with open(local_path, 'rb') as f:
        return f.read()


def write_jpeg(original: bytes, path: str) -> None:
    """Convert `original` to a JPEG at `path`: upright, transparency flattened onto white, no side over MAX_DIMENSION,
    no metadata. Written beside `path` and renamed into place, so a concurrent request never reads half a file."""
    register_heif_opener()
    image = ImageOps.exif_transpose(Image.open(io.BytesIO(original)))
    if image.mode in ('RGBA', 'LA') or (image.mode == 'P' and 'transparency' in image.info):
        rgba = image.convert('RGBA')
        flat = Image.new('RGB', rgba.size, (255, 255, 255))
        flat.paste(rgba, mask=rgba.getchannel('A'))
        image = flat
    else:
        image = image.convert('RGB')
    image.thumbnail((MAX_DIMENSION, MAX_DIMENSION), resample=Image.LANCZOS)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    partial = f'{path}.{os.getpid()}.part'
    try:
        save_image_without_metadata(image, partial, format='JPEG', quality=JPEG_QUALITY, optimize=True)
        os.replace(partial, path)
    finally:
        _forget(partial)


def _forget(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass

"""A JPEG of one of our own uploads, converted on first request (Pixelfed interop R3, amended 2026-10-29).

Pixelfed takes only JPEG, PNG and GIF attachments by default and drops a whole post for one attachment it cannot take
(Helpers.php:164-190 at v0.15.2), so an outbound attachment of a WebP, AVIF or HEIC upload names the JPEG at
/media/jpeg/<file id>.jpg (app/jpeg/views.py) instead of the original. Receivers that take the original show the
same picture either way.

Only a file in our own media folders or our own bucket is converted (`_ours`), so the route reads nothing but our own
public files and is no proxy for a URL a peer names. Not `File.wrote_its_source`: an app uploads through /upload/image
and then posts the URL, and that post's File has no file_name. SVG is not converted (there is no rasteriser here), so
an SVG post image still goes out as itself.
"""
from app.models import File, s3_key_from_url
from app.utils import guess_mime_type, store_files_in_s3

PIXELFED_IMAGE_TYPES = frozenset({'image/jpeg', 'image/png', 'image/gif'})


def needs_jpeg(file) -> bool:
    """Whether `file`, a post image, goes out as its JPEG: our own upload of a raster type Pixelfed refuses."""
    if file is None or not file.source_url or not _ours(file.source_url):
        return False
    mime_type = guess_mime_type(file.source_url)
    return mime_type.startswith('image/') and mime_type not in PIXELFED_IMAGE_TYPES and mime_type != 'image/svg+xml'


def jpeg_url(file, base: str) -> str:
    """The JPEG of `file` on the domain `base` (the post's object base, plan C3)."""
    return f'{base}/media/jpeg/{file.id}.jpg'


def _ours(url: str) -> bool:
    """Whether `url` names a file in our media folders (local_path_for_url confines it there) or in our bucket."""
    return File.local_path_for_url(url) is not None or (store_files_in_s3() and s3_key_from_url(url) is not None)

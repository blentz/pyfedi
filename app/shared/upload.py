from __future__ import annotations

import os

import boto3
from PIL import Image, ImageOps
from flask import current_app
from pillow_heif import register_heif_opener
from sqlalchemy import text

from app import db
from app.models import File, User, user_file, served_path
from app.storefront import canonical_link
from app.utils import gibberish, ensure_directory_exists, is_video_url, store_files_in_s3, guess_mime_type, sanitize_svg, \
    save_image_without_metadata


def open_uploaded_image(path, allowed_extensions):
    """Image.open(path), decoded, or Exception('filetype not allowed') -- with the upload removed -- for an image
    Pillow reads as a format allowed_extensions does not name, or cannot decode. Pillow reports a .heic file as
    'HEIF' (D579).

    The upload is already on disk under the media root, so a refusal that left it there left the uploader's bytes
    served from this origin with nothing referring to them. And the decode happens here, inside the try, because
    what Pillow raises for a broken file is of no one type -- UnidentifiedImageError at open, OSError for data cut
    short, SyntaxError for a broken PNG chunk, RuntimeError from the AVIF plugin (tests/fuzz/targets/images.py) --
    and each used to escape as itself, its message naming the file's path on this server."""
    img = None
    try:
        img = Image.open(path)
        allowed = '.' + img.format.lower() in allowed_extensions or \
            (img.format == 'HEIF' and '.heic' in allowed_extensions)
        if allowed:
            img.load()
    except Exception:
        allowed = False
    if not allowed:
        if img is not None:
            img.close()
        os.unlink(path)
        raise Exception('filetype not allowed')
    return img


UPLOAD_REFUSED = 'filetype not allowed'
SVG_REFUSED = 'SVG file could not be sanitized'


def is_refused_upload(exc: Exception) -> bool:
    """Whether exc is process_upload's refusal of a file it will not store: an extension the list does not name,
    bytes Pillow cannot decode, reads as another format or finds over IMAGE_PIXEL_CAP, or an SVG sanitize_svg will
    not clean (SVG_REFUSED, its own message for the API). The upload is already gone, so a web form flashes it
    rather than letting it become a 500."""
    return str(exc) in (UPLOAD_REFUSED, SVG_REFUSED)


def process_upload(image_file, destination='posts', user: User | None = None):
    # should have errored earlier if no upload, but just to be paranoid
    if not image_file or image_file.filename == '':
        raise Exception('file not uploaded')

    # Images only: video and audio are uploaded with a post, by moderators of media communities (spec A13.6)
    allowed_extensions = ['.gif', '.jpg', '.jpeg', '.png', '.webp', '.heic', '.heif', '.mpo', '.avif', '.svg']
    file_ext = os.path.splitext(image_file.filename)[1]
    if file_ext.lower() not in allowed_extensions:
        raise Exception('filetype not allowed')

    new_filename = gibberish(15)
    # set up the storage directory
    if store_files_in_s3():
        directory = 'app/static/tmp'
    else:
        directory = 'app/static/media/' + destination + '/' + new_filename[0:2] + '/' + new_filename[2:4]
    ensure_directory_exists(directory)

    # save the file
    final_place = os.path.join(directory, new_filename + file_ext)
    image_file.seek(0)
    image_file.save(final_place)

    final_ext = file_ext.lower()  # track file extension for conversion

    # Sanitize SVG files to remove potentially dangerous elements. An SVG that
    # cannot be sanitized is rejected: the '.svg' branch below skips the Pillow
    # re-encode, so nothing downstream would repair or re-check the file.
    # sanitize_svg has already destroyed it by the time it returns False.
    if final_ext == '.svg':
        if not sanitize_svg(final_place):
            raise Exception(SVG_REFUSED)

    if file_ext.lower() in ('.heic', '.heif'):
        register_heif_opener()
    if file_ext.lower() == '.avif':
        import pillow_avif  # NOQA  # lazy: registers Pillow's AVIF plugin only on the AVIF path

    # Use environment variables to determine image max dimension, format, and quality
    image_max_dimension = current_app.config['MEDIA_IMAGE_MAX_DIMENSION']
    image_format = current_app.config['MEDIA_IMAGE_FORMAT']
    image_quality = current_app.config['MEDIA_IMAGE_QUALITY']

    if image_format == 'AVIF':
        import pillow_avif  # NOQA  # lazy: registers Pillow's AVIF plugin only on the AVIF path

    if not final_place.endswith('.svg') and not final_place.endswith('.gif') and not is_video_url(final_place):
        img = open_uploaded_image(final_place, allowed_extensions)
        img = ImageOps.exif_transpose(img)
        img = img.convert('RGB' if (image_format == 'JPEG' or final_ext in ['.jpg', '.jpeg', '.mpo']) else 'RGBA')
        img.thumbnail((image_max_dimension, image_max_dimension), resample=Image.LANCZOS)

        kwargs = {}
        if image_format:
            kwargs['format'] = image_format.upper()
            final_ext = '.' + image_format.lower()
            final_place = os.path.splitext(final_place)[0] + final_ext
        if image_quality:
            kwargs['quality'] = int(image_quality)

        save_image_without_metadata(img, final_place, optimize=True, **kwargs)
    file_size = os.path.getsize(final_place)
    url = f"{current_app.config['SERVER_URL']}{served_path(final_place)}"  # D1363

    # Move uploaded file to S3
    if store_files_in_s3():
        session = boto3.session.Session()
        extra_args = {'ContentType': guess_mime_type(final_place)}
        if current_app.config.get('S3_STORAGE_CLASS'):
            extra_args['StorageClass'] = current_app.config['S3_STORAGE_CLASS']
        if current_app.config.get('S3_PUBLIC_ACL'):
            extra_args['ACL'] = 'public-read'
        s3 = session.client(
            service_name='s3',
            region_name=current_app.config['S3_REGION'],
            endpoint_url=current_app.config['S3_ENDPOINT'],
            aws_access_key_id=current_app.config['S3_ACCESS_KEY'],
            aws_secret_access_key=current_app.config['S3_ACCESS_SECRET'],
        )
        s3.upload_file(final_place, current_app.config['S3_BUCKET'], destination + '/' +
                       new_filename[0:2] + '/' + new_filename[2:4] + '/' + new_filename + final_ext,
                       ExtraArgs=extra_args)
        url = f"https://{current_app.config['S3_PUBLIC_URL']}/{destination}/" + \
              new_filename[0:2] + '/' + new_filename[2:4] + '/' + new_filename + final_ext
        s3.close()
        os.unlink(final_place)

    # associate file with uploader. Only provide user_id to this function when the image is not being used for a community icon, user avatar, etc where there is some other way to associate the image with the user.
    if user:
        file = File(source_url=url)
        db.session.add(file)
        db.session.commit()
        db.session.execute(text('INSERT INTO "user_file" (file_id, user_id, size) VALUES (:file_id, :user_id, :size)'),
                           {'file_id': file.id, 'user_id': user.id, 'size': file_size})
        db.session.commit()

    if not url:
        raise Exception('unable to process upload')

    return url


def process_file_delete(url: str, user_id: int):
    url = canonical_link(url)   # the media library sends back the storefront form it was shown; source_url is canonical
    if user_id:
        file = db.session.query(File).join(user_file).filter(user_file.c.file_id == File.id, user_file.c.user_id == user_id)\
            .filter(File.source_url == url).first()
        if file:
            file.delete_from_disk()
            db.session.execute(text('DELETE FROM "user_file" WHERE file_id = :file_id'), {'file_id': file.id})
            db.session.delete(file)
            db.session.commit()

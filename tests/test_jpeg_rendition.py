"""A JPEG of our own WebP/AVIF/HEIC upload, converted on first request (Pixelfed interop R3, amended 2026-10-29).

Pixelfed takes only JPEG, PNG and GIF attachments by default and drops a whole post for one it cannot take, so an
outbound attachment of any other image type names /media/jpeg/<file id>.jpg instead of the original.
"""
import io
import os

import boto3
import pytest
from PIL import Image

from app import db
from app.activitypub.serializers.microblog import note_attachments
from app.constants import POST_TYPE_IMAGE
from app.jpeg import jpeg_url, needs_jpeg
from app.models import File, jpeg_rendition_path
from tests.factories import make_community, make_instance, make_post, make_user

pytestmark = pytest.mark.usefixtures('private_static_tree')

HOST = 'test.piefed.local'
NAME = 'abcdQWERTYUIOPA'


def _upload(ext='webp', size=(64, 48), mode='RGB', colour=(200, 30, 30), write=True, ours=True) -> File:
    """A File as our upload code records one (file_name set), with its original on disk unless `write` is False."""
    relative = f'static/media/posts/ab/cd/{NAME}.{ext}'
    if write:
        os.makedirs(os.path.dirname(f'app/{relative}'), exist_ok=True)
        Image.new(mode, size, colour).save(f'app/{relative}', {'jpg': 'JPEG'}.get(ext, ext.upper()))
    file = File(source_url=f'https://{HOST}/{relative}', file_name=f'{NAME}.{ext}' if ours else None,
                width=size[0], height=size[1], alt_text='alt')
    db.session.add(file)
    db.session.commit()
    return file


def _get(app, file_id):
    return app.test_client().get(f'/media/jpeg/{file_id}.jpg')


# --- which images need one ---

@pytest.mark.parametrize('ext,expected', [('webp', True), ('avif', True), ('heic', True),
                                          ('jpg', False), ('jpeg', False), ('png', False), ('gif', False),
                                          ('svg', False)])
def test_only_types_pixelfed_refuses_need_a_jpeg(app, db_session, ext, expected):
    assert needs_jpeg(_upload(ext, write=False)) is expected


def test_a_peers_image_never_does(app, db_session):
    """Only our upload code's records (file_name) are converted: anything else would make the route an image proxy."""
    assert needs_jpeg(_upload('webp', write=False, ours=False)) is False
    assert needs_jpeg(None) is False


# --- the outbound attachment ---

def _image_post(file):
    instance = make_instance(HOST, software='piefed')
    author = make_user(instance, 'alice', local=True)
    post = make_post(make_community('microblogs', host=HOST), author, f'https://{HOST}/post/1', microblog=True)
    post.type = POST_TYPE_IMAGE
    post.image = file
    db.session.commit()
    return post


def test_a_webp_post_image_goes_out_as_the_jpeg(app, db_session):
    file = _upload('webp', write=False)
    attachment = note_attachments(_image_post(file))[0]

    assert attachment['url'] == jpeg_url(file, f'https://{HOST}')
    assert attachment['url'].endswith(f'/media/jpeg/{file.id}.jpg')
    assert (attachment['mediaType'], attachment['name'], attachment['width']) == ('image/jpeg', 'alt', 64)


def test_a_jpeg_post_image_goes_out_as_itself(app, db_session):
    file = _upload('jpg', write=False)
    attachment = note_attachments(_image_post(file))[0]

    assert (attachment['url'], attachment['mediaType']) == (file.source_url, 'image/jpeg')


# --- the route ---

def test_the_route_converts_the_original_once(app, db_session, monkeypatch):
    file = _upload('webp')

    first = _get(app, file.id)
    assert first.status_code == 200 and first.mimetype == 'image/jpeg'
    image = Image.open(io.BytesIO(first.data))
    assert (image.format, image.size) == ('JPEG', (64, 48))
    assert os.path.isfile(jpeg_rendition_path(file.id))

    monkeypatch.setattr('app.jpeg.views.write_jpeg', lambda *a: (_ for _ in ()).throw(AssertionError('converted twice')))
    assert _get(app, file.id).data == first.data


def test_transparency_is_flattened_onto_white(app, db_session):
    file = _upload('webp', mode='RGBA', colour=(0, 0, 0, 0))

    image = Image.open(io.BytesIO(_get(app, file.id).data))

    assert image.getpixel((5, 5)) == (255, 255, 255)


def test_an_image_over_pixelfeds_limit_is_scaled_down(app, db_session):
    file = _upload('webp', size=(6000, 300))

    image = Image.open(io.BytesIO(_get(app, file.id).data))

    assert max(image.size) == 5000


@pytest.mark.parametrize('kind', ['peer', 'jpeg-already', 'no-record', 'original-gone', 'not-an-image'])
def test_the_route_refuses_what_it_must_not_convert(app, db_session, kind):
    if kind == 'peer':
        file_id = _upload('webp', ours=False).id
    elif kind == 'jpeg-already':
        file_id = _upload('jpg').id
    elif kind == 'no-record':
        file_id = 999999
    elif kind == 'original-gone':
        file_id = _upload('webp', write=False).id
    else:
        file_id = _upload('webp', write=False).id
        os.makedirs('app/static/media/posts/ab/cd', exist_ok=True)
        with open(f'app/static/media/posts/ab/cd/{NAME}.webp', 'wb') as f:
            f.write(b'not an image')

    assert _get(app, file_id).status_code == 404
    assert not os.path.exists(jpeg_rendition_path(file_id))


def test_a_cached_jpeg_whose_record_is_gone_is_not_served(app, db_session):
    file = _upload('webp')
    file_id = file.id
    assert _get(app, file_id).status_code == 200
    db.session.delete(file)   # not through delete_from_disk: the cache file is left behind
    db.session.commit()

    assert _get(app, file_id).status_code == 404
    assert not os.path.exists(jpeg_rendition_path(file_id))


def test_an_s3_original_is_read_from_the_bucket(app, db_session, monkeypatch):
    buf = io.BytesIO()
    Image.new('RGB', (20, 10), (1, 2, 3)).save(buf, 'WEBP')
    monkeypatch.setattr('app.jpeg.views.store_files_in_s3', lambda: True)
    monkeypatch.setattr('app.jpeg.views.s3_key_from_url', lambda url: 'posts/ab/cd/x.webp')
    keys = []
    monkeypatch.setattr('app.jpeg.views.read_from_s3', lambda key: keys.append(key) or buf.getvalue())
    file = _upload('webp', write=False)

    response = _get(app, file.id)

    assert response.status_code == 200 and keys == ['posts/ab/cd/x.webp']
    assert Image.open(io.BytesIO(response.data)).size == (20, 10)


def test_an_s3_original_round_trips_through_the_bucket(app, db_session, s3_bucket, monkeypatch):
    """Nothing stood in: the key comes from s3_key_from_url and the bytes from read_from_s3 (moto)."""
    for key, value in (('S3_ACCESS_KEY', 'key'), ('S3_ACCESS_SECRET', 'secret'),
                       ('S3_ENDPOINT', 'https://s3.us-east-1.amazonaws.com'), ('S3_REGION', 'us-east-1'),
                       ('S3_BUCKET', s3_bucket), ('S3_PUBLIC_URL', 'cdn.jpeg.test')):
        monkeypatch.setitem(app.config, key, value)
    buf = io.BytesIO()
    Image.new('RGB', (30, 20), (9, 9, 9)).save(buf, 'WEBP')
    boto3.client('s3', region_name='us-east-1').put_object(Bucket=s3_bucket, Key='posts/ab/cd/x.webp',
                                                           Body=buf.getvalue())
    file = File(source_url='https://cdn.jpeg.test/posts/ab/cd/x.webp', file_name='x.webp')
    db.session.add(file)
    db.session.commit()

    response = _get(app, file.id)

    assert response.status_code == 200
    assert Image.open(io.BytesIO(response.data)).size == (30, 20)


def test_an_upload_from_before_s3_is_read_from_disk(app, db_session, monkeypatch):
    """With S3 on, a URL that names no object in our bucket (an upload stored on disk before the switch) is read
    from the media folders as without S3."""
    monkeypatch.setattr('app.jpeg.views.store_files_in_s3', lambda: True)
    monkeypatch.setattr('app.jpeg.views.s3_key_from_url', lambda url: None)
    file = _upload('webp')

    assert _get(app, file.id).status_code == 200


def test_a_missing_bucket_object_is_a_404(app, db_session, monkeypatch):
    monkeypatch.setattr('app.jpeg.views.store_files_in_s3', lambda: True)
    monkeypatch.setattr('app.jpeg.views.s3_key_from_url', lambda url: 'posts/ab/cd/gone.webp')

    def missing(key):
        raise RuntimeError('NoSuchKey')
    monkeypatch.setattr('app.jpeg.views.read_from_s3', missing)
    file = _upload('webp', write=False)

    assert _get(app, file.id).status_code == 404
    assert not os.path.exists(jpeg_rendition_path(file.id))


# --- deletion ---

def test_deleting_the_file_removes_its_jpeg(app, db_session):
    file = _upload('webp')
    assert _get(app, file.id).status_code == 200

    file.delete_from_disk(cache_urls=[])

    assert not os.path.exists(jpeg_rendition_path(file.id))


def test_a_jpeg_gone_between_the_check_and_the_unlink_is_no_error(app, db_session, monkeypatch):
    """Another worker may remove the cached JPEG first: the race is no error (D1349's `isfile` then `unlink`)."""
    file = _upload('webp')
    assert _get(app, file.id).status_code == 200
    real_unlink = os.unlink

    def gone_first(path):
        if path == jpeg_rendition_path(file.id):
            real_unlink(path)
            raise FileNotFoundError(path)
        real_unlink(path)
    monkeypatch.setattr('app.models.os.unlink', gone_first)
    urls = []

    file.delete_from_disk(cache_urls=urls)

    assert f'/media/jpeg/{file.id}.jpg' in ' '.join(urls)
    assert not os.path.exists(jpeg_rendition_path(file.id))

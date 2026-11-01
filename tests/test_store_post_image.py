"""store_post_image: an image uploaded with a post (the post image, or a gallery's picture) as it is stored.

It refuses a type not allowed and an SVG it cannot sanitise, re-encodes the rest as MEDIA_IMAGE_FORMAT / QUALITY /
MAX_DIMENSION say without metadata (JPEG to sRGB), reads HEIC, marks the post AI-generated when the image's C2PA says
so, refuses an image whose hash is blocked, and moves the file to the bucket when there is one. It answers the url
(on the post's domain, or in the bucket), the name of the file it wrote and the hash.
"""
import io
import os

import boto3
import pytest
from flask import current_app
from PIL import Image
from pillow_heif import register_heif_opener
from werkzeug.datastructures import FileStorage

from app.shared.post import store_post_image
from tests.factories import make_community, make_instance, make_post, make_user

pytestmark = pytest.mark.usefixtures('private_static_tree')


@pytest.fixture
def post(app, db_session, monkeypatch):
    monkeypatch.setattr('app.shared.post.inspect_image_c2pa', lambda data, mimetype: {'c2pa': {'ai_generated': False}})
    for key, value in (('MEDIA_IMAGE_FORMAT', ''), ('MEDIA_IMAGE_QUALITY', 90), ('MEDIA_IMAGE_MAX_DIMENSION', 2000),
                       ('IMAGE_HASHING_ENDPOINT', '')):
        monkeypatch.setitem(current_app.config, key, value)
    author = make_user(make_instance('test.piefed.local', software='piefed'), 'alice', local=True)
    return make_post(make_community('pictures'), author, None)


def upload(name, image_format='PNG', mode='RGB', content=None, mimetype='image/png'):
    if content is None:
        buffer = io.BytesIO()
        Image.new(mode, (30, 20), (10, 20, 30) if mode == 'RGB' else (10, 20, 30, 40)).save(buffer, format=image_format)
        content = buffer.getvalue()
    return FileStorage(stream=io.BytesIO(content), filename=name, content_type=mimetype)


def on_disk(url):
    return 'app/static' + url.split('/static', 1)[1]


def test_an_image_is_stored_on_the_posts_domain(app, post):
    url, name, hash = store_post_image(post, upload('photo.PNG'))

    assert url.startswith(f"{app.config['SERVER_URL']}/static/media/posts/") and url.endswith('.png')
    assert name == os.path.basename(url) and hash is None
    with Image.open(on_disk(url)) as stored:
        assert stored.size == (30, 20)


def test_a_type_not_allowed_is_refused(app, post):
    with pytest.raises(Exception, match='filetype not allowed'):
        store_post_image(post, upload('notes.txt', content=b'hello'))


def test_the_configured_format_quality_and_size_are_applied(app, post, monkeypatch):
    monkeypatch.setitem(current_app.config, 'MEDIA_IMAGE_FORMAT', 'JPEG')
    monkeypatch.setitem(current_app.config, 'MEDIA_IMAGE_MAX_DIMENSION', 15)

    url, name, hash = store_post_image(post, upload('photo.png'))

    assert url.endswith('.jpeg')
    with Image.open(on_disk(url)) as stored:
        assert stored.format == 'JPEG' and max(stored.size) == 15
    assert [f for f in os.listdir(os.path.dirname(on_disk(url))) if f.endswith('.png')] == []   # the upload went


def test_a_jpeg_is_kept_as_one(app, post):
    url, name, hash = store_post_image(post, upload('photo.jpg', image_format='JPEG', mimetype='image/jpeg'))

    with Image.open(on_disk(url)) as stored:
        assert stored.format == 'JPEG' and url.endswith('.jpg')


def test_a_heic_upload_has_its_reader_registered(app, post, monkeypatch):
    registered = []

    def register():
        registered.append(True)
        register_heif_opener()   # the real one: it also lets Pillow write the .heic back out
    monkeypatch.setattr('app.shared.post.register_heif_opener', register)

    url, name, hash = store_post_image(post, upload('photo.heic'))

    assert registered == [True] and url.endswith('.heic')


def test_an_svg_that_cannot_be_sanitised_is_refused(app, post, monkeypatch):
    monkeypatch.setattr('app.shared.post.sanitize_svg', lambda path: False)

    with pytest.raises(Exception, match='SVG file could not be sanitized'):
        store_post_image(post, upload('drawing.svg', content=b'<svg xmlns="http://www.w3.org/2000/svg"/>',
                                      mimetype='image/svg+xml'))


def test_an_ai_generated_image_marks_the_post(app, post, monkeypatch):
    monkeypatch.setattr('app.shared.post.inspect_image_c2pa', lambda data, mimetype: {'c2pa': {'ai_generated': True}})

    store_post_image(post, upload('photo.png'))

    assert post.ai_generated is True


@pytest.mark.parametrize('blocked', [False, True])
def test_with_hashing_on_a_blocked_image_is_refused(app, post, monkeypatch, blocked):
    monkeypatch.setitem(current_app.config, 'IMAGE_HASHING_ENDPOINT', 'https://hash.example')
    monkeypatch.setattr('app.shared.post.retrieve_image_hash', lambda url: '1010')
    monkeypatch.setattr('app.shared.post.hash_matches_blocked_image', lambda hash: blocked)

    if blocked:
        with pytest.raises(Exception, match='This image is blocked'):
            store_post_image(post, upload('photo.png'))
    else:
        assert store_post_image(post, upload('photo.png'))[2] == '1010'


def test_with_a_bucket_the_image_is_uploaded_and_the_local_copy_removed(app, post, s3_bucket, monkeypatch):
    for key, value in (('S3_ACCESS_KEY', 'key'), ('S3_ACCESS_SECRET', 'secret'),
                       ('S3_ENDPOINT', 'https://s3.us-east-1.amazonaws.com'), ('S3_REGION', 'us-east-1'),
                       ('S3_BUCKET', s3_bucket), ('S3_PUBLIC_URL', 'cdn.example.test'),
                       ('S3_STORAGE_CLASS', 'STANDARD_IA'), ('S3_PUBLIC_ACL', True)):
        monkeypatch.setitem(current_app.config, key, value)

    url, name, hash = store_post_image(post, upload('photo.png'))

    assert url.startswith('https://cdn.example.test/posts/') and url.endswith('.png')
    head = boto3.client('s3', region_name='us-east-1').head_object(Bucket=s3_bucket, Key=url.split('.test/', 1)[1])
    assert head['ContentType'] == 'image/png' and head['StorageClass'] == 'STANDARD_IA'
    assert os.listdir('app/static/tmp') == []


def test_an_svg_that_sanitises_is_stored_as_it_is(app, post, monkeypatch):
    monkeypatch.setattr('app.shared.post.sanitize_svg', lambda path: True)

    url, name, hash = store_post_image(post, upload('drawing.svg', content=b'<svg xmlns="http://www.w3.org/2000/svg"/>',
                                                    mimetype='image/svg+xml'))

    assert url.endswith('.svg') and open(on_disk(url), 'rb').read() == b'<svg xmlns="http://www.w3.org/2000/svg"/>'


def test_without_a_quality_or_bucket_options_the_defaults_apply(app, post, s3_bucket, monkeypatch):
    for key, value in (('MEDIA_IMAGE_QUALITY', None), ('S3_ACCESS_KEY', 'key'), ('S3_ACCESS_SECRET', 'secret'),
                       ('S3_ENDPOINT', 'https://s3.us-east-1.amazonaws.com'), ('S3_REGION', 'us-east-1'),
                       ('S3_BUCKET', s3_bucket), ('S3_PUBLIC_URL', 'cdn.example.test'),
                       ('S3_STORAGE_CLASS', ''), ('S3_PUBLIC_ACL', '')):
        monkeypatch.setitem(current_app.config, key, value)

    url, name, hash = store_post_image(post, upload('photo.png'))

    head = boto3.client('s3', region_name='us-east-1').head_object(Bucket=s3_bucket, Key=url.split('.test/', 1)[1])
    assert head.get('StorageClass', 'STANDARD') == 'STANDARD'

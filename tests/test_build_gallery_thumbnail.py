"""build_gallery_thumbnail (v1.8.10, moved to app/utils.py): a gallery's composite thumbnail, stored as the post's
other images are -- under app/static/media/posts on its domain (federation_base), or in the bucket.

A picture is read from disk when the caller hands a path, else fetched; a fetch that fails or is not an image
leaves that picture out, and a gallery with nothing readable has no thumbnail.
"""
import io
import os

import boto3
import pytest
from flask import current_app
from PIL import Image

from app import db
from app.models import File
from app.utils import build_gallery_thumbnail
from tests.factories import make_community, make_instance, make_post, make_user

pytestmark = pytest.mark.usefixtures('private_static_tree')


@pytest.fixture
def post(app, db_session):
    author = make_user(make_instance('test.piefed.local', software='piefed'), 'alice', local=True)
    return make_post(make_community('pictures'), author, None)


def png(colour, size=(40, 30)) -> bytes:
    buffer = io.BytesIO()
    Image.new('RGB', size, colour).save(buffer, format='PNG')
    return buffer.getvalue()


def on_disk(name, colour):
    path = f'app/static/media/posts/{name}.png'
    with open(path, 'wb') as handle:
        handle.write(png(colour))
    return path


def stored(url):
    """The file a composite's url names, under the private static tree."""
    return 'app/static' + url.split('/static', 1)[1]


def test_pictures_on_disk_make_a_stored_composite(app, post):
    file_id = build_gallery_thumbnail([on_disk('a', (200, 0, 0)), on_disk('b', (0, 200, 0))], post)

    file = db.session.get(File, file_id)
    assert file.source_url.startswith(f"{app.config['SERVER_URL']}/static/media/posts/")
    assert file.source_url.endswith('.webp')
    with Image.open(stored(file.source_url)) as composite:
        assert composite.size == (1200, 1200)


def test_the_url_alone_is_answered_when_asked(app, post):
    url = build_gallery_thumbnail([on_disk('a', (200, 0, 0)), on_disk('b', (0, 200, 0))], post,
                                  return_file_id=False)

    assert isinstance(url, str) and os.path.isfile(stored(url))
    assert db.session.query(File).count() == 0


def test_fetched_pictures_count_only_when_they_are_images(app, post, http_mock, monkeypatch):
    http_mock.get('https://m.example/a.png').respond(200, headers={'Content-Type': 'image/png'},
                                                     content=png((200, 0, 0)))
    http_mock.get('https://m.example/b.png').respond(200, headers={'Content-Type': 'image/png'},
                                                     content=png((0, 200, 0)))
    http_mock.get('https://m.example/page').respond(200, headers={'Content-Type': 'text/html'}, content=b'<p>no</p>')
    http_mock.get('https://m.example/bare').respond(200, content=png((0, 0, 200)))
    http_mock.get('https://m.example/gone.png').respond(404)
    seen = []
    real = Image.open

    def counting_open(source, *args, **kwargs):
        image = real(source, *args, **kwargs)
        seen.append(image.getpixel((0, 0))[:3])
        return image
    monkeypatch.setattr('app.utils.Image.open', counting_open)

    url = build_gallery_thumbnail(['https://m.example/a.png', 'https://m.example/page', 'https://m.example/bare',
                                   'https://m.example/gone.png', 'https://m.example/b.png'], post,
                                  return_file_id=False)

    assert seen == [(200, 0, 0), (0, 200, 0)]   # the page, the untyped answer and the 404 are left out
    assert os.path.isfile(stored(url))


def test_nothing_readable_makes_no_thumbnail(app, post, http_mock):
    http_mock.get('https://m.example/gone.png').respond(404)

    assert build_gallery_thumbnail(['https://m.example/gone.png'], post) is None
    assert db.session.query(File).count() == 0


@pytest.fixture
def s3(app, s3_bucket, monkeypatch):
    for key, value in (('S3_ACCESS_KEY', 'key'), ('S3_ACCESS_SECRET', 'secret'),
                       ('S3_ENDPOINT', 'https://s3.us-east-1.amazonaws.com'), ('S3_REGION', 'us-east-1'),
                       ('S3_BUCKET', s3_bucket), ('S3_PUBLIC_URL', 'cdn.example.test'),
                       ('S3_STORAGE_CLASS', 'STANDARD_IA'), ('S3_PUBLIC_ACL', True)):
        monkeypatch.setitem(current_app.config, key, value)
    return s3_bucket


def test_with_a_bucket_the_composite_is_uploaded_and_the_local_copy_removed(app, post, s3):
    url = build_gallery_thumbnail([on_disk('a', (200, 0, 0)), on_disk('b', (0, 200, 0))], post,
                                  return_file_id=False)

    assert url.startswith('https://cdn.example.test/posts/') and url.endswith('.webp')
    key = url.split('cdn.example.test/', 1)[1]
    head = boto3.client('s3', region_name='us-east-1').head_object(Bucket=s3, Key=key)
    assert head['ContentType'] == 'image/webp' and head['StorageClass'] == 'STANDARD_IA'
    assert os.listdir('app/static/tmp') == []

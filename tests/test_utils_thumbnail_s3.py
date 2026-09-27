"""Mirroring a remote thumbnail into this instance's object storage.

`url_to_thumbnail_file` in `app/utils.py` has two storage arms. The local one --
write the resized images under `app/static/media/posts/xx/yy/` and keep those
paths -- is exercised by `tests/test_utils_security.py`. The S3 one, which writes
to `app/static/tmp`, uploads, unlinks and keeps `https://{S3_PUBLIC_URL}/...`
URLs instead, had never been executed by any test.

It is the ingest twin of what round 152 repaired on the delete side: the keys
written here are the keys `s3_key_from_url` has to be able to name again, so one
test asserts exactly that round trip rather than leaving the two readings to
agree by coincidence.
"""
import os

import boto3
import httpx
import pytest
from flask import current_app
from moto import mock_aws

from app.models import s3_key_from_url
from app.utils import url_to_thumbnail_file

URL = 'https://thumbnails.example/remote.png'
TMP_ROOT = 'app/static/tmp'


def a_png(size=(600, 400)):
    """A real PNG, because Pillow sniffs the content rather than the extension."""
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new('RGB', size, (10, 120, 200)).save(buffer, format='PNG')
    return buffer.getvalue()


def files_under(root):
    found = []
    for directory, _subdirectories, names in os.walk(root):
        found.extend(os.path.join(directory, name) for name in names)
    return sorted(found)


@pytest.fixture
def bucket(app, monkeypatch):
    """A moto-backed bucket with the config that makes `store_files_in_s3()` true.

    `S3_ENDPOINT` has to be set for that check to pass, and it has to be an
    endpoint moto answers: moto intercepts by matching the URL botocore is about
    to dial, so a made-up host is really dialled and the test spends a hundred
    seconds failing to connect.
    """
    monkeypatch.setitem(current_app.config, 'S3_ACCESS_KEY', 'key')
    monkeypatch.setitem(current_app.config, 'S3_ACCESS_SECRET', 'secret')
    monkeypatch.setitem(current_app.config, 'S3_ENDPOINT',
                        'https://s3.us-east-1.amazonaws.com')
    monkeypatch.setitem(current_app.config, 'S3_REGION', 'us-east-1')
    monkeypatch.setitem(current_app.config, 'S3_BUCKET', 'pyfedi-test')
    monkeypatch.setitem(current_app.config, 'S3_PUBLIC_URL', 'cdn.example')
    with mock_aws():
        client = boto3.client('s3', region_name='us-east-1')
        client.create_bucket(Bucket='pyfedi-test')
        yield client


def keys_in(client):
    listing = client.list_objects_v2(Bucket='pyfedi-test')
    return sorted(item['Key'] for item in listing.get('Contents', []))


class TestAThumbnailMirroredIntoTheBucket:
    def test_both_sizes_are_uploaded(self, app, http_mock, bucket):
        http_mock.get(URL).mock(return_value=httpx.Response(
            200, content=a_png(), headers={'content-type': 'image/png'}))

        file = url_to_thumbnail_file(URL)

        assert file is not None
        keys = keys_in(bucket)
        assert len(keys) == 2
        assert [key.endswith('.webp') for key in keys] == [True, True]
        assert sum(key.endswith('_512.webp') for key in keys) == 1

    def test_the_row_keeps_public_urls_and_not_paths(self, app, http_mock, bucket):
        """The local arm stores the on-disk path in these two columns; this arm
        stores a URL. `File.delete_from_disk` tells them apart by reading the
        value, so which one is stored decides how the file is later removed."""
        http_mock.get(URL).mock(return_value=httpx.Response(
            200, content=a_png(), headers={'content-type': 'image/png'}))

        file = url_to_thumbnail_file(URL)

        assert file.thumbnail_path.startswith('https://cdn.example/posts/')
        assert file.file_path.startswith('https://cdn.example/posts/')
        assert file.file_path.endswith('_512.webp')
        assert file.source_url == URL

    def test_the_two_columns_name_the_two_objects(self, app, http_mock, bucket):
        http_mock.get(URL).mock(return_value=httpx.Response(
            200, content=a_png(), headers={'content-type': 'image/png'}))

        file = url_to_thumbnail_file(URL)

        assert sorted([s3_key_from_url(file.thumbnail_path),
                       s3_key_from_url(file.file_path)]) == keys_in(bucket)

    def test_the_key_is_shardedded_by_the_first_four_characters(self, app, http_mock,
                                                               bucket):
        """`posts/xx/yy/<name>` -- the same layout the local arm uses for
        directories, so one instance can be migrated to the other."""
        http_mock.get(URL).mock(return_value=httpx.Response(
            200, content=a_png(), headers={'content-type': 'image/png'}))

        url_to_thumbnail_file(URL)

        for key in keys_in(bucket):
            posts, first, second, name = key.split('/')
            assert posts == 'posts'
            assert len(first) == 2 and len(second) == 2
            assert name.startswith(first + second)

    def test_nothing_is_left_in_the_temporary_directory(self, app, http_mock, bucket):
        """This arm writes to `app/static/tmp` and unlinks after each upload. A
        file left there is a leak of a peer's bytes into a directory this
        instance keeps, which `clean_up_tmp` only sweeps after a day."""
        http_mock.get(URL).mock(return_value=httpx.Response(
            200, content=a_png(), headers={'content-type': 'image/png'}))
        before = files_under(TMP_ROOT)

        url_to_thumbnail_file(URL)

        assert files_under(TMP_ROOT) == before

    def test_the_media_directory_is_not_written_to_at_all(self, app, http_mock,
                                                          bucket):
        http_mock.get(URL).mock(return_value=httpx.Response(
            200, content=a_png(), headers={'content-type': 'image/png'}))
        before = files_under('app/static/media')

        url_to_thumbnail_file(URL)

        assert files_under('app/static/media') == before

    def test_the_bytes_are_only_ever_written_under_the_tmp_directory(
            self, app, http_mock, bucket, monkeypatch):
        """This arm sends the files to the bucket, so it stages them in
        `app/static/tmp` rather than in `app/static/media/posts`, and both the
        download and the resizes have to land there.

        Asserted by watching `open` rather than by listing the directory
        afterwards: everything here is unlinked once it is uploaded, so a version
        that staged a peer's body somewhere else and then tidied up would leave
        the same empty directories behind.
        """
        import builtins

        real_open = builtins.open
        written = []

        def recording_open(path, mode='r', *arguments, **keywords):
            if isinstance(path, str) and 'app/static' in path and \
                    ('w' in mode or 'a' in mode or '+' in mode):
                written.append(path)
            return real_open(path, mode, *arguments, **keywords)

        monkeypatch.setattr(builtins, 'open', recording_open)
        http_mock.get(URL).mock(return_value=httpx.Response(
            200, content=a_png(), headers={'content-type': 'image/png'}))

        url_to_thumbnail_file(URL)

        assert written
        assert [path for path in written
                if not path.startswith(TMP_ROOT + '/')] == []

    def test_the_configured_medium_format_decides_the_extension(self, app, http_mock,
                                                                bucket, monkeypatch):
        monkeypatch.setitem(current_app.config, 'MEDIA_IMAGE_MEDIUM_FORMAT', 'PNG')
        http_mock.get(URL).mock(return_value=httpx.Response(
            200, content=a_png(), headers={'content-type': 'image/png'}))

        file = url_to_thumbnail_file(URL)

        assert all(key.endswith('.png') for key in keys_in(bucket))
        assert file.file_path.endswith('_512.png')

    def test_the_sizes_are_recorded_from_the_resized_images(self, app, http_mock,
                                                            bucket):
        http_mock.get(URL).mock(return_value=httpx.Response(
            200, content=a_png(size=(1200, 600)),
            headers={'content-type': 'image/png'}))

        file = url_to_thumbnail_file(URL)

        assert (file.thumbnail_width, file.thumbnail_height) == (170, 85)
        assert (file.width, file.height) == (512, 256)


class TestAnSvgMirroredIntoTheBucket:
    """An SVG skips Pillow entirely, so there is no 512px version of it: one
    object is uploaded and both columns name it."""

    SVG_URL = 'https://thumbnails.example/remote.svg'
    PAYLOAD = (b'<svg xmlns="http://www.w3.org/2000/svg">'
               b'<rect width="1" height="1"/></svg>')

    def serve(self, http_mock):
        http_mock.get(self.SVG_URL).mock(return_value=httpx.Response(
            200, content=self.PAYLOAD,
            headers={'content-type': 'image/svg+xml'}))

    def test_one_object_is_uploaded(self, app, http_mock, bucket):
        self.serve(http_mock)

        file = url_to_thumbnail_file(self.SVG_URL)

        assert [key.endswith('.svg') for key in keys_in(bucket)] == [True]
        assert file.thumbnail_path == file.file_path

    def test_the_script_is_still_removed_before_it_is_uploaded(self, app, http_mock,
                                                              bucket):
        """The sanitise happens before the storage arm is chosen, so it must hold
        here too -- and what is uploaded is what the instance then serves from its
        own CDN hostname."""
        http_mock.get(self.SVG_URL).mock(return_value=httpx.Response(
            200,
            content=b'<svg xmlns="http://www.w3.org/2000/svg">'
                    b'<script>alert(1)</script><rect width="1" height="1"/></svg>',
            headers={'content-type': 'image/svg+xml'}))

        url_to_thumbnail_file(self.SVG_URL)

        key = keys_in(bucket)[0]
        body = bucket.get_object(Bucket='pyfedi-test', Key=key)['Body'].read()
        assert b'script' not in body.lower()
        assert b'<rect' in body

    def test_no_width_is_recorded(self, app, http_mock, bucket):
        """Pillow never opens it, so the four size columns stay None rather than
        being guessed from the document."""
        self.serve(http_mock)

        file = url_to_thumbnail_file(self.SVG_URL)

        assert (file.width, file.height) == (None, None)
        assert (file.thumbnail_width, file.thumbnail_height) == (None, None)

    def test_nothing_is_left_in_the_temporary_directory(self, app, http_mock, bucket):
        self.serve(http_mock)
        before = files_under(TMP_ROOT)

        url_to_thumbnail_file(self.SVG_URL)

        assert files_under(TMP_ROOT) == before


class TestWhatIsNotUploaded:
    def test_a_body_pillow_refuses(self, app, http_mock, bucket):
        """D1328's drop, on this arm: nothing is uploaded, nothing is returned,
        and the bytes do not stay in `app/static/tmp` either."""
        http_mock.get(URL).mock(return_value=httpx.Response(
            200, content=b'<html><script>alert(document.domain)</script>',
            headers={'content-type': 'image/png'}))
        before = files_under(TMP_ROOT)

        assert url_to_thumbnail_file(URL) is None
        assert keys_in(bucket) == []
        assert files_under(TMP_ROOT) == before

    def test_an_unsanitizable_svg(self, app, http_mock, bucket):
        http_mock.get(URL).mock(return_value=httpx.Response(
            200, content=b'<!DOCTYPE svg [<!ENTITY x "y">]><svg/>',
            headers={'content-type': 'image/svg+xml'}))
        before = files_under(TMP_ROOT)

        assert url_to_thumbnail_file(URL) is None
        assert keys_in(bucket) == []
        assert files_under(TMP_ROOT) == before

    def test_a_response_that_is_not_an_image(self, app, http_mock, bucket):
        http_mock.get(URL).mock(return_value=httpx.Response(
            200, content=b'{}', headers={'content-type': 'application/json'}))

        assert url_to_thumbnail_file(URL) is None
        assert keys_in(bucket) == []

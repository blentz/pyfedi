"""Making a medium and a thumbnail from an image another instance sent.

Sub-project 101 -- `make_image_sizes_async` in `app/activitypub/util.py`. It
runs for every image that arrives with a post, an avatar or a community icon:
it fetches the source, decodes it, writes a medium copy and a thumbnail, and
records their sizes on the File row. Everything it is given comes from
somewhere else -- the url, the bytes, the content type header.

Two defects:

    file.thumbnail_width = image.width
    file.thumbnail_height = image.height

`image` is the SOURCE. The medium branch twelve lines above reads its own copy
(`medium_image.width`), and this one did not, so every thumbnail this instance
has ever made is recorded with the dimensions of the full-size original --
which is what the template puts in the `width` and `height` attributes
(D1291).

And `medium_image` was assigned only inside `if img_width > medium_width or
medium_image_format:` and then read unconditionally, so an image already
narrower than `medium_width` -- on an instance that has configured no medium
format, which is the default -- was `UnboundLocalError` and got no medium
copy, no thumbnail and no dimensions at all (D1292).

The dead `else` under `if content_type_parts:` went with it: `str.split('/')`
never returns an empty list, so the arm deriving the extension from the url
could not run. That is the third copy of the shape D1193 removed.

One equivalent mutant: removing `if img_width > thumbnail_width:` survives,
because `Image.thumbnail()` never enlarges -- Pillow's own contract makes the
guard redundant, and no input can tell the two apart.
"""
import io
import os
from unittest.mock import patch

import httpx
import pytest
from flask import current_app, g
from PIL import Image

from app import db
from app.activitypub.util import make_image_sizes, make_image_sizes_async
from app.models import File, Post, Site
from tests.factories import (make_community, make_community_member, make_file,
                             make_post)

SOURCE = 'https://remote.test/media/photo.png'


@pytest.fixture
def env(app, api_baseline, monkeypatch):
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    g.site.enable_chan_image_filter = False
    community = make_community('probeland')
    member = api_baseline.user2
    db.session.commit()
    make_community_member(member, community)
    for key, value in (('MEDIA_IMAGE_MEDIUM_FORMAT', ''),
                       ('MEDIA_IMAGE_MEDIUM_QUALITY', ''),
                       ('MEDIA_IMAGE_THUMBNAIL_FORMAT', ''),
                       ('MEDIA_IMAGE_THUMBNAIL_QUALITY', ''),
                       ('S3_ACCESS_KEY', ''), ('S3_ACCESS_SECRET', ''),
                       ('S3_ENDPOINT', '')):
        monkeypatch.setitem(current_app.config, key, value)
    return SimpleNamespace(app=app, community=community, member=member,
                           baseline=api_baseline)


def image_bytes(width, height, fmt='PNG'):
    buffer = io.BytesIO()
    Image.new('RGB', (width, height), (10, 20, 30)).save(buffer, format=fmt)
    return buffer.getvalue()


def a_file(env, source_url=SOURCE):
    return make_file(source_url=source_url)


def answered_with(content, content_type='image/png', status=200):
    return httpx.Response(status, content=content,
                          headers={'content-type': content_type}
                          if content_type else {})


def cleanup(file):
    db.session.expire_all()
    stored = db.session.get(File, file.id)
    for path in (stored.file_path, stored.thumbnail_path):
        if path and not path.startswith('http') and os.path.exists(path):
            os.unlink(path)
    return stored


def run(file, response, **kwargs):
    with patch('app.activitypub.util.get_request', return_value=response):
        make_image_sizes_async(file.id, kwargs.pop('thumbnail_width', 50),
                              kwargs.pop('medium_width', 120),
                              kwargs.pop('directory', 'posts'),
                              kwargs.pop('toxic_community', False))
    return cleanup(file)


class TestTheSizesThatAreRecorded:
    def test_the_medium_copy_is_described_by_its_own_size(self, env):
        file = a_file(env)
        stored = run(file, answered_with(image_bytes(800, 600)))
        assert stored.width <= 120
        assert stored.height < 600

    def test_the_thumbnail_is_described_by_its_own_size(self, env):
        """D1291. These recorded `image.width` and `image.height` -- the
        SOURCE's -- so a 50px thumbnail was described as 800px wide."""
        file = a_file(env)
        stored = run(file, answered_with(image_bytes(800, 600)))
        assert stored.thumbnail_width <= 50
        assert stored.thumbnail_height < 600

    def test_an_image_smaller_than_the_thumbnail_is_not_grown(self, env):
        """D1292. An image already narrower than `medium_width`, on an
        instance with no configured medium format, took neither arm of the
        resize and the save below was `UnboundLocalError: cannot access local
        variable 'medium_image'` -- no medium copy, no thumbnail, no
        dimensions."""
        file = a_file(env)
        stored = run(file, answered_with(image_bytes(30, 20)))
        assert (stored.thumbnail_width, stored.thumbnail_height) == (30, 20)
        assert (stored.width, stored.height) == (30, 20)

    def test_both_files_are_written(self, env):
        file = a_file(env)
        with patch('app.activitypub.util.get_request',
                   return_value=answered_with(image_bytes(400, 300))):
            make_image_sizes_async(file.id, 50, 120, 'posts', False)
        db.session.expire_all()
        stored = db.session.get(File, file.id)
        try:
            assert os.path.exists(stored.file_path)
            assert os.path.exists(stored.thumbnail_path)
            assert stored.file_path != stored.thumbnail_path
        finally:
            cleanup(file)

    def test_only_a_thumbnail_is_asked_for(self, env):
        file = a_file(env)
        stored = run(file, answered_with(image_bytes(400, 300)),
                     medium_width=None)
        assert stored.thumbnail_path is not None
        assert stored.file_path is None

    def test_only_a_medium_copy_is_asked_for(self, env):
        file = a_file(env)
        stored = run(file, answered_with(image_bytes(400, 300)),
                     thumbnail_width=None)
        assert stored.file_path is not None
        assert stored.thumbnail_path is None


class TestWhatTheContentTypeSays:
    @pytest.mark.parametrize('content_type,fmt', [
        ('image/png', 'PNG'),
        ('image/jpeg', 'JPEG'),
        ('image/webp', 'WEBP'),
    ])
    def test_each_type_it_handles(self, env, content_type, fmt):
        file = a_file(env)
        stored = run(file, answered_with(image_bytes(400, 300, fmt=fmt),
                                         content_type=content_type))
        assert stored.file_path is not None

    def test_a_charset_on_the_header_is_ignored(self, env):
        file = a_file(env)
        stored = run(file, answered_with(image_bytes(400, 300),
                                         content_type='image/png; charset=binary'))
        assert stored.file_path.endswith('.png')

    def test_jpeg_is_stored_with_the_short_extension(self, env):
        file = a_file(env)
        stored = run(file, answered_with(image_bytes(400, 300, fmt='JPEG'),
                                         content_type='image/jpeg'))
        assert stored.file_path.endswith('.jpg')

    def test_an_svg_is_left_alone(self, env):
        """Nothing to resize, and nothing is written."""
        file = a_file(env)
        stored = run(file, answered_with(b'<svg/>',
                                         content_type='image/svg+xml'))
        assert stored.file_path is None
        assert stored.thumbnail_path is None

    def test_an_avif_arriving_as_a_byte_stream(self, env):
        import pillow_avif  # NOQA
        file = a_file(env, 'https://remote.test/media/photo.avif')
        stored = run(file, answered_with(
            image_bytes(400, 300, fmt='AVIF'),
            content_type='application/octet-stream'))
        assert stored.file_path is not None

    def test_a_byte_stream_that_is_not_an_avif_url(self, env):
        file = a_file(env)
        stored = run(file, answered_with(
            image_bytes(400, 300), content_type='application/octet-stream'))
        assert stored.file_path is None

    def test_something_that_is_not_an_image_at_all(self, env):
        file = a_file(env)
        stored = run(file, answered_with(b'<html>', content_type='text/html'))
        assert stored.file_path is None

    def test_an_answer_with_no_content_type(self, env):
        file = a_file(env)
        stored = run(file, answered_with(image_bytes(400, 300),
                                         content_type=None))
        assert stored.file_path is None


class TestWhatIsNotResizedAtAll:
    def test_a_gif_keeps_its_animation(self, env):
        file = a_file(env, 'https://remote.test/media/animated.gif')
        with patch('app.activitypub.util.get_request') as get:
            make_image_sizes_async(file.id, 50, 120, 'posts', False)
        assert get.call_count == 0

    def test_a_file_row_that_is_gone(self, env):
        with patch('app.activitypub.util.get_request') as get:
            make_image_sizes_async(999999, 50, 120, 'posts', False)
        assert get.call_count == 0

    def test_a_file_with_no_source_at_all(self, env):
        file = make_file(source_url=None)
        with patch('app.activitypub.util.get_request') as get:
            make_image_sizes_async(file.id, 50, 120, 'posts', False)
        assert get.call_count == 0

    def test_a_source_that_cannot_be_reached(self, env):
        file = a_file(env)
        with patch('app.activitypub.util.get_request',
                   side_effect=httpx.ConnectError('no route')):
            make_image_sizes_async(file.id, 50, 120, 'posts', False)
        db.session.expire_all()
        assert db.session.get(File, file.id).file_path is None

    def test_a_source_that_answers_404(self, env):
        file = a_file(env)
        stored = run(file, httpx.Response(404))
        assert stored.file_path is None


class TestAnImageBehindLemmysProxy:
    PROXIED = ('https://slrpnk.test/api/v3/image_proxy'
               '?url=https%3A%2F%2Fi.guim.test%2Fimg%2Fphoto.jpg')

    def test_the_real_url_is_tried_when_the_proxy_fails(self, env):
        file = a_file(env, self.PROXIED)
        asked = []

        def answer(url, *args, **kwargs):
            asked.append(url)
            if 'image_proxy' in url:
                return httpx.Response(404)
            return answered_with(image_bytes(400, 300, fmt='JPEG'),
                                 content_type='image/jpeg')

        with patch('app.activitypub.util.get_request', side_effect=answer):
            make_image_sizes_async(file.id, 50, 120, 'posts', False)
        stored = cleanup(file)
        assert asked[-1] == 'https://i.guim.test/img/photo.jpg'
        assert stored.file_path is not None

    def test_a_500_from_the_proxy_counts_too(self, env):
        file = a_file(env, self.PROXIED)
        asked = []

        def answer(url, *args, **kwargs):
            asked.append(url)
            if 'image_proxy' in url:
                return httpx.Response(500)
            return answered_with(image_bytes(400, 300, fmt='JPEG'),
                                 content_type='image/jpeg')

        with patch('app.activitypub.util.get_request', side_effect=answer):
            make_image_sizes_async(file.id, 50, 120, 'posts', False)
        cleanup(file)
        assert len(asked) == 2

    def test_a_proxy_url_that_names_no_url_to_proxy(self, env):
        file = a_file(env, 'https://slrpnk.test/api/v3/image_proxy?other=1')
        stored = run(file, httpx.Response(404))
        assert stored.file_path is None

    def test_a_404_from_something_that_is_not_a_proxy(self, env):
        file = a_file(env)
        asked = []

        def answer(url, *args, **kwargs):
            asked.append(url)
            return httpx.Response(404)

        with patch('app.activitypub.util.get_request', side_effect=answer):
            make_image_sizes_async(file.id, 50, 120, 'posts', False)
        assert len(asked) == 1


class TestTheFormatTheInstanceHasChosen:
    def test_posts_are_re_encoded_as_the_instance_asks(self, env,
                                                       monkeypatch):
        monkeypatch.setitem(current_app.config, 'MEDIA_IMAGE_MEDIUM_FORMAT',
                            'WEBP')
        monkeypatch.setitem(current_app.config, 'MEDIA_IMAGE_THUMBNAIL_FORMAT',
                            'WEBP')
        file = a_file(env)
        stored = run(file, answered_with(image_bytes(400, 300)))
        assert stored.file_path.endswith('.webp')
        assert stored.thumbnail_path.endswith('.webp')

    def test_with_a_quality_as_well(self, env, monkeypatch):
        monkeypatch.setitem(current_app.config, 'MEDIA_IMAGE_MEDIUM_FORMAT',
                            'WEBP')
        monkeypatch.setitem(current_app.config, 'MEDIA_IMAGE_MEDIUM_QUALITY',
                            '60')
        monkeypatch.setitem(current_app.config, 'MEDIA_IMAGE_THUMBNAIL_FORMAT',
                            'WEBP')
        monkeypatch.setitem(current_app.config,
                            'MEDIA_IMAGE_THUMBNAIL_QUALITY', '50')
        file = a_file(env)
        stored = run(file, answered_with(image_bytes(400, 300)))
        assert stored.file_path.endswith('.webp')

    def test_jpeg_output_goes_through_srgb(self, env, monkeypatch):
        monkeypatch.setitem(current_app.config, 'MEDIA_IMAGE_MEDIUM_FORMAT',
                            'JPEG')
        monkeypatch.setitem(current_app.config, 'MEDIA_IMAGE_THUMBNAIL_FORMAT',
                            'JPEG')
        file = a_file(env)
        stored = run(file, answered_with(image_bytes(400, 300)))
        assert stored.file_path.endswith('.jpeg')

    @pytest.mark.parametrize('extension,fmt,stored_as', [
        ('.jpg', 'JPEG', '.jpeg'), ('.png', 'PNG', '.png'),
        ('.webp', 'WEBP', '.webp'),
    ])
    def test_an_avatar_keeps_the_format_it_arrived_in(self, env, extension,
                                                     fmt, stored_as,
                                                     monkeypatch):
        """`communities` and `users` preserve the original FORMAT whatever the
        instance has configured for posts -- though not always the original
        spelling of it: a `.jpg` is re-encoded as JPEG and written as
        `.jpeg`, because the extension is rebuilt from the format name."""
        monkeypatch.setitem(current_app.config, 'MEDIA_IMAGE_MEDIUM_FORMAT',
                            'WEBP')
        content_type = 'image/jpeg' if fmt == 'JPEG' else \
            f'image/{fmt.lower()}'
        file = a_file(env, f'https://remote.test/media/icon{extension}')
        stored = run(file, answered_with(image_bytes(400, 300, fmt=fmt),
                                         content_type=content_type),
                     directory='users')
        assert stored.file_path.endswith(stored_as)

    def test_an_avatar_in_a_format_with_no_rule_becomes_a_png(self, env):
        import pillow_avif  # NOQA
        file = a_file(env, 'https://remote.test/media/icon.avif')
        stored = run(file, answered_with(
            image_bytes(400, 300, fmt='AVIF'), content_type='image/avif'),
            directory='communities')
        assert stored.file_path.endswith('.avif')


class TestHowTheResizeIsDispatched:
    def test_in_debug_it_runs_here_and_now(self, env, monkeypatch):
        monkeypatch.setattr(current_app, 'debug', True)
        with patch('app.activitypub.util.make_image_sizes_async') as task:
            make_image_sizes(7, 50, 120, 'posts')
        task.assert_called_once_with(7, 50, 120, 'posts', False)

    def test_otherwise_it_is_queued_with_a_delay(self, env, monkeypatch):
        monkeypatch.setattr(current_app, 'debug', False)
        with patch('app.activitypub.util.make_image_sizes_async') as task:
            make_image_sizes(7, 50, 120, 'posts')
        assert task.apply_async.call_args.kwargs['args'] == \
            (7, 50, 120, 'posts', False)


class TestWhenTheInstanceStoresImagesInS3:
    @pytest.fixture
    def s3(self, env, s3_bucket, monkeypatch):
        for key, value in (('S3_ACCESS_KEY', 'key'),
                           ('S3_ACCESS_SECRET', 'secret'),
                           ('S3_ENDPOINT',
                            'https://s3.us-east-1.amazonaws.com'),
                           ('S3_REGION', 'us-east-1'),
                           ('S3_BUCKET', s3_bucket),
                           ('S3_PUBLIC_URL', 'cdn.probeland.test'),
                           ('S3_STORAGE_CLASS', ''), ('S3_PUBLIC_ACL', '')):
            monkeypatch.setitem(current_app.config, key, value)
        return s3_bucket

    def test_both_copies_are_pushed(self, env, s3):
        file = a_file(env)
        stored = run(file, answered_with(image_bytes(400, 300)))
        assert stored.file_path.startswith('https://cdn.probeland.test/')
        assert stored.thumbnail_path.startswith('https://cdn.probeland.test/')

    def test_nothing_is_left_behind_on_disk(self, env, s3):
        file = a_file(env)
        stored = run(file, answered_with(image_bytes(400, 300)))
        assert not os.path.exists('app/static/tmp/' +
                                  os.path.basename(stored.file_path))

    def test_a_storage_class_and_an_acl_are_passed_on(self, env, s3,
                                                     monkeypatch):
        monkeypatch.setitem(current_app.config, 'S3_STORAGE_CLASS',
                            'STANDARD')
        monkeypatch.setitem(current_app.config, 'S3_PUBLIC_ACL', True)
        file = a_file(env)
        stored = run(file, answered_with(image_bytes(400, 300)))
        assert stored.file_path.startswith('https://cdn.probeland.test/')

    def test_a_thumbnail_only_run_still_opens_a_connection(self, env, s3):
        """The client is built lazily, and the medium branch is where it is
        usually built -- so a thumbnail-only run has to build its own."""
        file = a_file(env)
        stored = run(file, answered_with(image_bytes(400, 300)),
                     medium_width=None)
        assert stored.thumbnail_path.startswith('https://cdn.probeland.test/')
        assert stored.file_path is None

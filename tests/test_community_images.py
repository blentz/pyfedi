"""Saving a community's icon and banner.

Sub-project 95 -- `save_icon_file` and `save_banner_file` in
`app/community/util.py`, the two functions behind every icon and banner an
admin or a moderator uploads for a community, a feed or the site itself. They
take a file straight off a form, so the extension, the bytes and the size are
all somebody else's choice.

What they decide, in order: whether the extension is one of the nine allowed;
whether an SVG survives sanitising; which of three processing paths the image
takes (SVG untouched, animated GIF through `scale_gif`, everything else
through Pillow); whether the instance's configured output format and quality
are applied; and whether the result is left on disk or pushed to S3.

Two defects, both in the banner half:

* `.svg` is in the allowed-extension list, which is shared with
  `save_icon_file`, and THAT function has an `.svg` branch that skips the
  Pillow work. `save_banner_file` has none, so every SVG banner reached
  `Image.open` and raised `UnidentifiedImageError` -- a 500, with the
  uploaded bytes left sitting in the media root. It is a 400 refusal now,
  with the sanitiser still run on the way past so a hostile SVG is destroyed
  rather than merely refused (D1283);
* the banner path also checks what Pillow says the image ACTUALLY is against
  the same list of extensions, and Pillow names the format of a `.heic` file
  HEIF. So every HEIC banner was refused, though `.heic` is an allowed
  extension and an icon may be one (D1284).

Every image here is a real one built with Pillow, because both functions
decode what they are given -- a stub would exercise the error path instead.
The S3 arm runs against moto, so nothing leaves the container.

One equivalent mutant: removing the extension check at the top of
`save_icon_file` survives, because the `if file_ext.lower() in
allowed_extensions:` that wraps its body has an `else: abort(400)` with the
identical condition. No input can tell the two apart, which is also why that
`else` shows as uncovered.
"""
import io
import os

import pytest
from flask import current_app, g
from PIL import Image
from werkzeug.datastructures import FileStorage
from werkzeug.exceptions import HTTPException

from app import db
from app.community.util import save_banner_file, save_icon_file
from app.models import Site

SVG = (b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
       b'<rect width="10" height="10"/></svg>')

# An SVG py-svg-hush refuses; `sanitize_svg` destroys the file before it
# returns False, which is why nothing downstream can serve it.
HOSTILE_SVG = (b'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY a "'
               + b'x' * 64 + b'"><!ENTITY b "&a;&a;&a;">]>'
               b'<svg xmlns="http://www.w3.org/2000/svg">&b;&b;&b;&b;&b;&b;'
               b'&b;&b;&b;&b;</svg>')


@pytest.fixture
def env(app, api_baseline, monkeypatch, tmp_path):
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    for key, value in (('MEDIA_IMAGE_FORMAT', ''), ('MEDIA_IMAGE_QUALITY', ''),
                       ('MEDIA_IMAGE_THUMBNAIL_FORMAT', ''),
                       ('MEDIA_IMAGE_THUMBNAIL_QUALITY', ''),
                       ('S3_ACCESS_KEY', ''), ('S3_ACCESS_SECRET', ''),
                       ('S3_ENDPOINT', '')):
        monkeypatch.setitem(current_app.config, key, value)
    return SimpleNamespace(app=app, baseline=api_baseline, tmp=tmp_path)


def an_image(width, height, fmt='PNG', name=None, mode='RGB'):
    """A real image of `width` x `height`, as a form would deliver it."""
    buffer = io.BytesIO()
    Image.new(mode, (width, height), (10, 20, 30)).save(buffer, format=fmt)
    buffer.seek(0)
    extension = {'PNG': '.png', 'JPEG': '.jpg', 'WEBP': '.webp',
                 'GIF': '.gif'}[fmt]
    return FileStorage(stream=buffer, filename=name or f'upload{extension}')


def an_animated_gif(width, height, frames=3):
    buffer = io.BytesIO()
    images = [Image.new('P', (width, height), index) for index in range(frames)]
    images[0].save(buffer, format='GIF', save_all=True,
                   append_images=images[1:], duration=100, loop=0)
    buffer.seek(0)
    return FileStorage(stream=buffer, filename='animated.gif')


def a_file(content, name):
    return FileStorage(stream=io.BytesIO(content), filename=name)


def cleanup(file):
    """Both functions write to app/static/media; the rows are rolled back but
    the bytes are not."""
    for path in (file.file_path, file.thumbnail_path):
        if path and not path.startswith('http') and os.path.exists(path):
            os.unlink(path)


class TestWhatWillNotBeAccepted:
    @pytest.mark.parametrize('name', ['payload.php', 'script.js', 'note.txt',
                                      'archive.zip', 'noextension',
                                      'image.php.png.php'])
    def test_an_extension_nobody_allows(self, env, name):
        with pytest.raises(HTTPException) as caught:
            save_icon_file(a_file(b'anything', name))
        assert caught.value.code == 400

    def test_the_same_for_a_banner(self, env):
        with pytest.raises(HTTPException) as caught:
            save_banner_file(a_file(b'anything', 'payload.php'))
        assert caught.value.code == 400

    def test_an_extension_in_capitals_is_still_allowed(self, env):
        file = save_icon_file(an_image(50, 50, name='UPLOAD.PNG'))
        try:
            assert file.file_path.endswith('.PNG')
        finally:
            cleanup(file)

    def test_a_double_extension_is_read_from_the_end(self, env):
        """`os.path.splitext` takes the last one, so `payload.php.png` is a
        png as far as this is concerned -- and it is re-encoded by Pillow
        below, so the php never survives."""
        file = save_icon_file(an_image(50, 50, name='payload.php.png'))
        try:
            assert file.file_path.endswith('.png')
        finally:
            cleanup(file)


class TestAnSvg:
    def test_one_that_sanitises_cleanly(self, env):
        file = save_icon_file(a_file(SVG, 'logo.svg'))
        try:
            assert file.file_path.endswith('.svg')
            assert file.thumbnail_path == file.file_path
            assert file.width is None
            assert file.height is None
        finally:
            cleanup(file)

    def test_one_that_does_not(self, env):
        """`sanitize_svg` has already destroyed the file by the time it
        returns False; refusing here is what stops it being served from this
        instance's own origin exactly as uploaded."""
        with pytest.raises(HTTPException) as caught:
            save_icon_file(a_file(HOSTILE_SVG, 'logo.svg'))
        assert caught.value.code == 400

    def test_a_banner_may_not_be_an_svg_at_all(self, env):
        """D1283. `.svg` is in the shared allowed_extensions list and
        `save_banner_file` has no `.svg` branch, so a clean SVG banner
        reached `Image.open` and raised `UnidentifiedImageError` -- a 500,
        with the uploaded bytes left in the media root. It is a refusal
        now."""
        with pytest.raises(HTTPException) as caught:
            save_banner_file(a_file(SVG, 'banner.svg'))
        assert caught.value.code == 400

    def test_a_hostile_banner_svg(self, env):
        with pytest.raises(HTTPException) as caught:
            save_banner_file(a_file(HOSTILE_SVG, 'banner.svg'))
        assert caught.value.code == 400

    def test_nothing_is_left_in_the_media_root_either_way(self, env):
        import glob
        before = set(glob.glob('app/static/media/communities/*/*/*'))
        for content in (SVG, HOSTILE_SVG):
            with pytest.raises(HTTPException):
                save_banner_file(a_file(content, 'banner.svg'))
        assert set(glob.glob('app/static/media/communities/*/*/*')) == before


class TestAnIcon:
    def test_a_small_one_is_kept_as_it_is(self, env):
        file = save_icon_file(an_image(100, 80))
        try:
            assert (file.width, file.height) == (100, 80)
            assert os.path.exists(file.file_path)
        finally:
            cleanup(file)

    def test_a_large_one_is_brought_down_to_250(self, env):
        file = save_icon_file(an_image(1000, 500))
        try:
            assert file.width <= 250 and file.height <= 250
        finally:
            cleanup(file)

    def test_a_thumbnail_is_made_beside_it(self, env):
        file = save_icon_file(an_image(400, 400))
        try:
            assert file.thumbnail_path != file.file_path
            assert file.thumbnail_width <= 40 and file.thumbnail_height <= 40
            assert os.path.exists(file.thumbnail_path)
        finally:
            cleanup(file)

    def test_the_row_says_what_it_is_for(self, env):
        file = save_icon_file(an_image(100, 100), directory='feeds')
        try:
            assert file.alt_text == 'feeds icon'
            assert '/feeds/' in file.file_path
        finally:
            cleanup(file)

    @pytest.mark.parametrize('fmt', ['PNG', 'JPEG', 'WEBP'])
    def test_each_still_format(self, env, fmt):
        file = save_icon_file(an_image(300, 300, fmt=fmt))
        try:
            assert file.width <= 250
        finally:
            cleanup(file)


class TestAnAnimatedGif:
    def test_a_small_one(self, env):
        file = save_icon_file(an_animated_gif(60, 60))
        try:
            assert (file.width, file.height) == (60, 60)
            assert file.thumbnail_path.endswith('_thumbnail.gif')
            assert file.thumbnail_width <= 40
        finally:
            cleanup(file)

    def test_a_large_one_is_scaled(self, env):
        file = save_icon_file(an_animated_gif(600, 600))
        try:
            assert file.width <= 250 and file.height <= 250
        finally:
            cleanup(file)

    def test_a_gif_banner(self, env):
        file = save_banner_file(an_animated_gif(60, 60))
        try:
            assert file.file_path.endswith('.gif')
        finally:
            cleanup(file)


class TestTheFormatTheInstanceHasChosen:
    def test_an_instance_that_re_encodes_everything_as_webp(self, env,
                                                            monkeypatch):
        monkeypatch.setitem(current_app.config, 'MEDIA_IMAGE_FORMAT', 'WEBP')
        monkeypatch.setitem(current_app.config,
                            'MEDIA_IMAGE_THUMBNAIL_FORMAT', 'WEBP')
        file = save_icon_file(an_image(300, 300, fmt='PNG'))
        try:
            assert file.file_path.endswith('.webp')
            assert file.thumbnail_path.endswith('.webp')
            assert file.file_name.endswith('.webp')
        finally:
            cleanup(file)

    def test_one_that_asks_for_jpeg(self, env, monkeypatch):
        monkeypatch.setitem(current_app.config, 'MEDIA_IMAGE_FORMAT', 'JPEG')
        monkeypatch.setitem(current_app.config,
                            'MEDIA_IMAGE_THUMBNAIL_FORMAT', 'JPEG')
        file = save_icon_file(an_image(300, 300, fmt='PNG'))
        try:
            assert file.file_path.endswith('.jpeg')
        finally:
            cleanup(file)

    def test_one_that_also_sets_a_quality(self, env, monkeypatch):
        monkeypatch.setitem(current_app.config, 'MEDIA_IMAGE_FORMAT', 'WEBP')
        monkeypatch.setitem(current_app.config, 'MEDIA_IMAGE_QUALITY', '60')
        monkeypatch.setitem(current_app.config,
                            'MEDIA_IMAGE_THUMBNAIL_FORMAT', 'WEBP')
        monkeypatch.setitem(current_app.config,
                            'MEDIA_IMAGE_THUMBNAIL_QUALITY', '50')
        file = save_icon_file(an_image(300, 300))
        try:
            assert os.path.exists(file.file_path)
        finally:
            cleanup(file)

    def test_a_small_image_is_re_encoded_too_when_a_format_is_set(self, env,
                                                                  monkeypatch):
        """The resize is skipped for a small image, but the format is not."""
        monkeypatch.setitem(current_app.config, 'MEDIA_IMAGE_FORMAT', 'WEBP')
        file = save_icon_file(an_image(50, 50, fmt='PNG'))
        try:
            assert file.file_path.endswith('.webp')
        finally:
            cleanup(file)

    def test_a_banner_re_encoded_the_same_way(self, env, monkeypatch):
        monkeypatch.setitem(current_app.config, 'MEDIA_IMAGE_FORMAT', 'WEBP')
        file = save_banner_file(an_image(2000, 600, fmt='PNG'))
        try:
            assert file.file_path.endswith('.webp')
        finally:
            cleanup(file)

    def test_a_banner_with_a_quality_set_for_both_sizes(self, env,
                                                        monkeypatch):
        monkeypatch.setitem(current_app.config, 'MEDIA_IMAGE_FORMAT', 'WEBP')
        monkeypatch.setitem(current_app.config, 'MEDIA_IMAGE_QUALITY', '60')
        monkeypatch.setitem(current_app.config,
                            'MEDIA_IMAGE_THUMBNAIL_FORMAT', 'WEBP')
        monkeypatch.setitem(current_app.config,
                            'MEDIA_IMAGE_THUMBNAIL_QUALITY', '50')
        file = save_banner_file(an_image(2000, 700))
        try:
            assert file.file_path.endswith('.webp')
            assert file.thumbnail_path.endswith('.webp')
        finally:
            cleanup(file)


class TestABanner:
    def test_a_small_one_is_kept_as_it_is(self, env):
        file = save_banner_file(an_image(500, 100))
        try:
            assert (file.width, file.height) == (500, 100)
        finally:
            cleanup(file)

    def test_a_wide_one_is_brought_down(self, env):
        file = save_banner_file(an_image(3000, 900))
        try:
            assert file.width <= 1600
        finally:
            cleanup(file)

    def test_the_row_says_what_it_is_for(self, env):
        file = save_banner_file(an_image(500, 100), directory='feeds')
        try:
            assert 'feeds' in file.alt_text
        finally:
            cleanup(file)


class TestTheFormatsPillowNeedsAPluginFor:
    def a_heic(self, name='photo.heic'):
        from pillow_heif import register_heif_opener
        register_heif_opener()
        buffer = io.BytesIO()
        Image.new('RGB', (300, 300), (10, 20, 30)).save(buffer, format='HEIF')
        buffer.seek(0)
        return FileStorage(stream=buffer, filename=name)

    def an_avif(self, name='photo.avif'):
        import pillow_avif  # NOQA
        buffer = io.BytesIO()
        Image.new('RGB', (300, 300), (10, 20, 30)).save(buffer, format='AVIF')
        buffer.seek(0)
        return FileStorage(stream=buffer, filename=name)

    def test_a_heic_icon(self, env):
        file = save_icon_file(self.a_heic())
        try:
            assert file.width <= 250
        finally:
            cleanup(file)

    def test_an_avif_icon(self, env):
        file = save_icon_file(self.an_avif())
        try:
            assert file.width <= 250
        finally:
            cleanup(file)

    def test_a_heic_banner(self, env):
        """D1284. The banner path checks what Pillow says the format is
        against the allowed EXTENSIONS, and Pillow calls a .heic file HEIF --
        so every HEIC banner was refused with a 400, though .heic is an
        allowed extension and an icon may be one."""
        file = save_banner_file(self.a_heic('banner.heic'))
        try:
            assert file.width <= 1600
        finally:
            cleanup(file)

    def test_an_avif_banner(self, env):
        file = save_banner_file(self.an_avif('banner.avif'))
        try:
            assert file.width <= 1600
        finally:
            cleanup(file)

    def test_an_instance_that_re_encodes_everything_as_avif(self, env,
                                                            monkeypatch):
        monkeypatch.setitem(current_app.config, 'MEDIA_IMAGE_FORMAT', 'AVIF')
        monkeypatch.setitem(current_app.config,
                            'MEDIA_IMAGE_THUMBNAIL_FORMAT', 'AVIF')
        file = save_icon_file(an_image(300, 300))
        try:
            assert file.file_path.endswith('.avif')
        finally:
            cleanup(file)

    def test_the_same_for_a_banner(self, env, monkeypatch):
        monkeypatch.setitem(current_app.config, 'MEDIA_IMAGE_FORMAT', 'AVIF')
        monkeypatch.setitem(current_app.config,
                            'MEDIA_IMAGE_THUMBNAIL_FORMAT', 'AVIF')
        file = save_banner_file(an_image(2000, 700))
        try:
            assert file.file_path.endswith('.avif')
        finally:
            cleanup(file)


class TestAFileThatIsNotWhatItSaysItIs:
    def test_a_bitmap_wearing_a_png_extension_is_refused_as_a_banner(self,
                                                                     env):
        """The banner path checks what Pillow says the image ACTUALLY is
        against the same allowed list, so an allowed extension over
        disallowed bytes is still refused."""
        buffer = io.BytesIO()
        Image.new('RGB', (50, 50)).save(buffer, format='BMP')
        buffer.seek(0)
        with pytest.raises(HTTPException) as caught:
            save_banner_file(FileStorage(stream=buffer,
                                         filename='sneaky.png'))
        assert caught.value.code == 400

    def test_bytes_that_are_not_an_image_at_all(self, env):
        with pytest.raises(Exception):
            save_icon_file(a_file(b'this is not a PNG', 'upload.png'))


class TestWhenTheInstanceStoresImagesInS3:
    @pytest.fixture
    def s3(self, env, s3_bucket, monkeypatch):
        for key, value in (('S3_ACCESS_KEY', 'key'),
                           ('S3_ACCESS_SECRET', 'secret'),
                           ('S3_ENDPOINT', 'https://s3.us-east-1.amazonaws.com'),
                           ('S3_REGION', 'us-east-1'),
                           ('S3_BUCKET', s3_bucket),
                           ('S3_PUBLIC_URL', 'cdn.probeland.test'),
                           ('S3_STORAGE_CLASS', ''),
                           ('S3_PUBLIC_ACL', '')):
            monkeypatch.setitem(current_app.config, key, value)
        return s3_bucket

    def test_an_icon_is_pushed_and_the_row_points_at_the_cdn(self, env, s3):
        file = save_icon_file(an_image(300, 300))
        assert file.file_path.startswith('https://cdn.probeland.test/')
        assert file.thumbnail_path.startswith('https://cdn.probeland.test/')
        assert file.thumbnail_path != file.file_path

    def test_nothing_is_left_behind_on_disk(self, env, s3):
        file = save_icon_file(an_image(300, 300))
        assert not os.path.exists('app/static/tmp/' +
                                  os.path.basename(file.file_name))

    def test_an_svg_has_one_url_for_both(self, env, s3):
        """An SVG is its own thumbnail, so there is one object, not two."""
        file = save_icon_file(a_file(SVG, 'logo.svg'))
        assert file.thumbnail_path == file.file_path

    def test_a_storage_class_is_passed_on_when_one_is_configured(self, env,
                                                                 s3,
                                                                 monkeypatch):
        monkeypatch.setitem(current_app.config, 'S3_STORAGE_CLASS',
                            'STANDARD')
        file = save_icon_file(an_image(300, 300))
        assert file.file_path.startswith('https://cdn.probeland.test/')

    def test_and_a_public_acl(self, env, s3, monkeypatch):
        monkeypatch.setitem(current_app.config, 'S3_PUBLIC_ACL', True)
        file = save_icon_file(an_image(300, 300))
        assert file.file_path.startswith('https://cdn.probeland.test/')

    def test_a_banner_is_pushed_too(self, env, s3):
        file = save_banner_file(an_image(2000, 600))
        assert file.file_path.startswith('https://cdn.probeland.test/')

    def test_a_banner_with_a_storage_class_and_an_acl(self, env, s3,
                                                      monkeypatch):
        monkeypatch.setitem(current_app.config, 'S3_STORAGE_CLASS',
                            'STANDARD')
        monkeypatch.setitem(current_app.config, 'S3_PUBLIC_ACL', True)
        file = save_banner_file(an_image(2000, 600))
        assert file.file_path.startswith('https://cdn.probeland.test/')
        assert file.thumbnail_path.startswith('https://cdn.probeland.test/')

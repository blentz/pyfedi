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
from app.constants import IMAGE_PIXEL_CAP
from app.models import Site
from tests.image_samples import MALFORMED_EXIF, assert_no_metadata, jpeg_with_gps_exif, jpeg_with_malformed_exif

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


def a_disguised_image(fmt, name):
    """A real image in `fmt`, uploaded under a name the extension check accepts."""
    buffer = io.BytesIO()
    Image.new('RGB', (300, 300), (10, 20, 30)).save(buffer, format=fmt)
    return FileStorage(stream=io.BytesIO(buffer.getvalue()), filename=name)


class TestAnIconInAFormatNobodyAllows:
    """An icon is decoded only if Pillow reads it as one of the formats the extension list names, as a banner and
    a post image already were. save_icon_file never asked, so a TGA, BMP, TIFF, PCX -- any of the forty-odd formats
    Pillow opens -- under a .png or .gif name went through that format's decoder, a crash surface PieFed never meant
    to expose (found by the icon_banner_decode fuzz target). The refusal is the function's own 400, and the upload is
    not left behind in the media root."""

    @pytest.mark.parametrize('fmt,name', [('TGA', 'icon.png'), ('BMP', 'icon.jpg'), ('TIFF', 'icon.webp'),
                                          ('PCX', 'icon.gif'), ('TGA', 'icon.gif')])
    def test_is_refused(self, env, fmt, name):
        import glob
        before = set(glob.glob('app/static/media/communities/*/*/*'))
        with pytest.raises(HTTPException) as caught:
            save_icon_file(a_disguised_image(fmt, name))
        assert caught.value.code == 400
        assert set(glob.glob('app/static/media/communities/*/*/*')) == before

    def test_an_allowed_format_under_another_allowed_name_is_still_taken(self, env):
        file = save_icon_file(a_disguised_image('JPEG', 'icon.png'))
        try:
            assert file.width <= 250
        finally:
            cleanup(file)


def truncated(data):
    return data[:len(data) * 2 // 3]


def png_bytes(width=300, height=300):
    buffer = io.BytesIO()
    Image.new('RGB', (width, height)).save(buffer, format='PNG', compress_level=0)
    return buffer.getvalue()


def gif_with_a_truncated_last_frame():
    """Three frames, the last cut short: Image.open and the first frame are fine, scale_gif's seek is not."""
    buffer = io.BytesIO()
    frames = [Image.effect_noise((300, 300), 64 + index).convert('P') for index in range(3)]
    frames[0].save(buffer, format='GIF', save_all=True, append_images=frames[1:])
    data = buffer.getvalue()
    return data[:len(data) - 2000]


class TestAnImagePillowCannotDecode:
    """Pillow refuses a file it cannot decode with an exception, and of no one type: UnidentifiedImageError at
    open, OSError for data cut short at load (and in scale_gif's seek through a GIF's frames), SyntaxError for a
    broken PNG chunk, RuntimeError from the AVIF plugin. Neither function caught any of them, so every one was a
    500 with the upload left in the media root -- where nothing refers to it and it is served from this instance's
    own origin. They are the functions' own 400 now, as the extension check's refusal is, and the upload goes."""

    @pytest.mark.parametrize('content,name', [
        (b'not an image at all', 'icon.png'),
        (truncated(png_bytes()), 'icon.png'),
        (truncated(png_bytes()), 'icon.gif'),
        (gif_with_a_truncated_last_frame(), 'icon.gif'),
    ], ids=['garbage', 'truncated_png', 'truncated_png_as_gif', 'gif_with_a_truncated_last_frame'])
    def test_an_icon_is_refused_and_not_left_behind(self, env, content, name):
        import glob
        before = set(glob.glob('app/static/media/communities/*/*/*'))
        with pytest.raises(HTTPException) as caught:
            save_icon_file(a_file(content, name))
        assert caught.value.code == 400
        assert set(glob.glob('app/static/media/communities/*/*/*')) == before

    @pytest.mark.parametrize('content,name', [
        (b'not an image at all', 'banner.png'),
        (truncated(png_bytes()), 'banner.png'),
        (a_disguised_image('TGA', 'banner.png').read(), 'banner.png'),
    ], ids=['garbage', 'truncated_png', 'tga'])
    def test_a_banner_is_refused_and_not_left_behind(self, env, content, name):
        import glob
        before = set(glob.glob('app/static/media/communities/*/*/*'))
        with pytest.raises(HTTPException) as caught:
            save_banner_file(a_file(content, name))
        assert caught.value.code == 400
        assert set(glob.glob('app/static/media/communities/*/*/*')) == before


def gif_of_identical_frames(width, height, delay_cs, frames=2):
    """`frames` copies of one frame, each with a delay of `delay_cs` hundredths of a second. Pillow's own writer
    merges identical frames, so the file is put together from one Pillow-encoded frame by hand."""
    buffer = io.BytesIO()
    Image.new('P', (width, height), 0).save(buffer, format='GIF')
    data = buffer.getvalue()
    packed = data[10]    # the logical screen descriptor's flags: a global colour table of 3 * 2**(n+1) bytes
    start = 13 + (3 * 2 ** ((packed & 7) + 1) if packed & 0x80 else 0)
    assert data[start] == 0x2c, 'Pillow wrote an extension before the image descriptor'
    header, frame = data[:start], data[start:-1]
    control = b'\x21\xf9\x04\x00' + delay_cs.to_bytes(2, 'little') + b'\x00\x00'
    return header + (control + frame) * frames + b'\x3b'


class TestAGifPillowCannotWriteBack:
    """scale_gif saves every frame with the first frame's duration, and Pillow's GIF writer merges identical
    consecutive frames by adding their durations. Past 65535 hundredths of a second the sum does not fit the
    format's 16-bit delay and Pillow raises struct.error: two identical frames of 327.68 s were a 500 out of
    save_icon_file, with the upload left in the media root. They are its 400 now, and the upload goes."""

    @pytest.mark.parametrize('size', [(2, 2), (300, 300)], ids=['thumbnail_only', 'scaled_to_250_first'])
    def test_an_icon_is_refused_and_not_left_behind(self, env, size):
        import glob
        before = set(glob.glob('app/static/media/communities/*/*/*'))
        with pytest.raises(HTTPException) as caught:
            save_icon_file(a_file(gif_of_identical_frames(*size, delay_cs=32768), 'icon.gif'))
        assert caught.value.code == 400
        assert set(glob.glob('app/static/media/communities/*/*/*')) == before

    def test_the_same_frames_just_under_the_limit_are_kept(self, env):
        file = save_icon_file(a_file(gif_of_identical_frames(2, 2, delay_cs=32767), 'icon.gif'))
        try:
            assert (file.width, file.height) == (2, 2)
        finally:
            cleanup(file)


def declaring(width, height, fmt='PNG'):
    """A valid one-bit image of width x height: a few KB, however many pixels it declares."""
    buffer = io.BytesIO()
    Image.new('1', (width, height)).save(buffer, format=fmt)
    return buffer.getvalue()


class TestAnImageOverThePixelCap:
    """IMAGE_PIXEL_CAP (50 MP). Pillow raised only above 178,956,970 pixels and warned above half that, so a few KB
    declaring 7072x7072 -- or 10000x10000, in Pillow's warning band -- was decoded at full size (for the GIF arm,
    every frame of it) and resized. Each is the functions' 400 now, refused at the header, and the upload goes."""

    @pytest.mark.parametrize('size', [(7072, 7072), (10000, 10000)], ids=['just_over_the_cap', 'pillow_warning_band'])
    @pytest.mark.parametrize('fmt,name', [('PNG', 'icon.png'), ('GIF', 'icon.gif')], ids=['still', 'gif_arm'])
    def test_an_icon_is_refused_and_not_left_behind(self, env, size, fmt, name):
        import glob
        assert size[0] * size[1] > IMAGE_PIXEL_CAP
        before = set(glob.glob('app/static/media/communities/*/*/*'))
        with pytest.raises(HTTPException) as caught:
            save_icon_file(a_file(declaring(*size, fmt=fmt), name))
        assert caught.value.code == 400
        assert set(glob.glob('app/static/media/communities/*/*/*')) == before

    @pytest.mark.parametrize('size', [(7072, 7072), (10000, 10000)], ids=['just_over_the_cap', 'pillow_warning_band'])
    def test_a_banner_is_refused_and_not_left_behind(self, env, size):
        import glob
        before = set(glob.glob('app/static/media/communities/*/*/*'))
        with pytest.raises(HTTPException) as caught:
            save_banner_file(a_file(declaring(*size), 'banner.png'))
        assert caught.value.code == 400
        assert set(glob.glob('app/static/media/communities/*/*/*')) == before


def an_mpo(width, height, name):
    buffer = io.BytesIO()
    Image.new('RGB', (width, height), (10, 20, 30)).save(buffer, format='MPO')
    return a_file(buffer.getvalue(), name)


class TestAnMpo:
    """'.mpo' is in the extension list and Pillow saves it as JPEG, but it was converted to RGBA like a PNG, so an
    icon or banner large enough to be resized, on an instance with no MEDIA_IMAGE_FORMAT, failed at the save with
    'cannot write mode RGBA as JPEG'. It is converted to RGB, as '.jpg' is (and one frame saved as MPO reads back
    as a JPEG)."""

    def test_an_icon(self, env):
        file = save_icon_file(an_mpo(300, 300, 'icon.mpo'))
        try:
            assert file.file_path.endswith('.mpo')
            with Image.open(file.file_path) as img:
                assert (img.format, img.mode, img.size) == ('JPEG', 'RGB', (250, 250))
        finally:
            cleanup(file)

    def test_a_banner(self, env):
        file = save_banner_file(an_mpo(1700, 100, 'banner.mpo'))
        try:
            assert file.file_path.endswith('.mpo')
            with Image.open(file.file_path) as img:
                assert (img.format, img.mode, img.width) == ('JPEG', 'RGB', 1600)
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


class TestAnAvifCarriesNoUploaderMetadata:
    """With MEDIA_IMAGE_FORMAT / MEDIA_IMAGE_THUMBNAIL_FORMAT = AVIF the pillow_avif writer copied the upload's EXIF
    and XMP from img.info into both stored files, GPS position and camera make included. A malformed EXIF block
    made the save raise 'Invalid Exif payload' (a 500), or wrote an AVIF Pillow cannot reopen. Neither file
    carries either now, and both reopen."""

    @pytest.mark.parametrize('save', [save_icon_file, save_banner_file], ids=['icon', 'banner'])
    @pytest.mark.parametrize('content', [jpeg_with_gps_exif((2000, 700))] +
                             [jpeg_with_malformed_exif(n) for n in MALFORMED_EXIF],
                             ids=['gps_exif_and_xmp', *MALFORMED_EXIF])
    def test_neither_stored_file_carries_it(self, env, monkeypatch, save, content):
        monkeypatch.setitem(current_app.config, 'MEDIA_IMAGE_FORMAT', 'AVIF')
        monkeypatch.setitem(current_app.config, 'MEDIA_IMAGE_THUMBNAIL_FORMAT', 'AVIF')
        file = save(a_file(content, 'photo.jpg'))
        try:
            assert_no_metadata(file.file_path, 'AVIF')
            assert_no_metadata(file.thumbnail_path, 'AVIF')
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

    def test_a_heif_named_icon_and_banner(self, env):
        """D579 residue, fixed (owner ruling): .heif is HEIC's other name, and the
        community icon and banner list accepts it and opens it as it does .heic,
        like the post and generic upload paths already do. It was refused with
        a 400."""
        icon = save_icon_file(self.a_heic('photo.heif'))
        banner = save_banner_file(self.a_heic('banner.heif'))
        try:
            assert icon.width <= 250
            assert banner.width <= 1600
        finally:
            cleanup(icon)
            cleanup(banner)

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

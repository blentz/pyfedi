"""Round 262: the Content-Type a peer sends a thumbnail under, and the SVG discard.

`url_to_thumbnail_file` fetches an image a peer named and writes it under
`app/static/media/posts`, which this instance serves. D1327 stopped the peer choosing the
extension; `tests/test_utils_security.py` holds that regression guard, and
`tests/test_utils_thumbnail_s3.py` holds the object-storage arm. What neither covered is how the
header is READ, and reading it wrong is what D1427 repaired here:

    RFC 9110 section 8.3.1 makes a media type and its subtype CASE-INSENSITIVE

and both readings were case-sensitive. `Content-Type: IMAGE/PNG` failed `startswith('image')`,
so the thumbnail was discarded without a word; `image/SVG+XML` failed `"svg" in content_type`,
so the SVG sanitiser was not reached and the extension fell through to `.img`. Neither was
exploitable -- Pillow refuses an SVG, so the unsanitised bytes were dropped rather than served --
but both are a peer's spelling deciding whether a fetch works.

D1428 then deleted the second `if file_extension == '.svg' and "svg" not in content_type`
sanitisation, which the case-fold makes unreachable: `file_extension` is `'.svg'` only when the
subtype is `svg`, and the subtype is a substring of the content type. The rows in
`TestEverySvgIsSanitisedExactlyOnce` are what says the deletion removed no defence.

Also here: the `;`-parameter strip, the AVIF import, the S3 storage class, and
`discard_unsanitized_svg`'s failure arm.
"""
import io
import os
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from flask import current_app
from PIL import Image as PILImage

from app.utils import discard_unsanitized_svg, url_to_thumbnail_file

MEDIA_ROOT = 'app/static'

CLEAN_SVG = (b'<svg xmlns="http://www.w3.org/2000/svg">'
             b'<script>alert(1)</script><rect width="1" height="1"/></svg>')


def a_png(size=(400, 300)):
    """A real PNG. Pillow sniffs the CONTENT, so the bytes have to be an image whatever the
    header says -- which is the whole reason a wrong extension was serveable."""
    buffer = io.BytesIO()
    PILImage.new('RGB', size, (10, 120, 200)).save(buffer, format='PNG')
    return buffer.getvalue()


def files_under(root):
    found = []
    for directory, _subdirectories, names in os.walk(root):
        found.extend(os.path.join(directory, name) for name in names)
    return sorted(found)


@pytest.fixture
def fetch(app, http_mock):
    """Fetch one thumbnail and clean up whatever it left on disk.

    `url_to_thumbnail_file` writes under the working directory rather than a tmpdir, so each row
    removes its own files (tests/README.md fact 3).
    """
    written = []

    def run(content, content_type, url=None):
        url = url or f'https://thumbnails.example/{len(written)}-probe'
        http_mock.get(url).mock(return_value=httpx.Response(
            200, content=content, headers={'content-type': content_type}))
        before = files_under(MEDIA_ROOT)
        result = url_to_thumbnail_file(url)
        written.extend(sorted(set(files_under(MEDIA_ROOT)) - set(before)))
        return result

    yield run
    for path in written:
        if os.path.isfile(path):
            os.unlink(path)


@pytest.fixture
def keeps_the_fetched_extension(monkeypatch):
    """Turn the resize's reformat off, so the name the peer's bytes were written under survives
    into the returned row.

    With `MEDIA_IMAGE_MEDIUM_FORMAT` set, the resize saves in that format and renames the path,
    so `file_path` reports the CONFIGURED extension and says nothing about what the header was
    read as. Empty is a supported setting -- `tests/test_ap_make_image_sizes.py` uses it -- and
    Pillow then infers the format from the extension, which is exactly the reading under test.
    """
    monkeypatch.setitem(current_app.config, 'MEDIA_IMAGE_MEDIUM_FORMAT', '')


# --------------------------------------------------------------------------
# How the header is read
# --------------------------------------------------------------------------


class TestTheContentTypeIsReadCaseInsensitively:
    """D1427. Every row here fails before the case-fold, and each is a spelling a peer is
    entitled to send.
    """

    def test_an_uppercase_media_type_is_still_an_image(self, fetch):
        """`startswith('image')`. The gate for the whole block, so this spelling was not a
        wrong extension -- it was no thumbnail at all, and no log line either."""
        assert fetch(a_png(), 'IMAGE/PNG') is not None

    def test_a_mixed_case_media_type_is_too(self, fetch):
        assert fetch(a_png(), 'Image/Png') is not None

    def test_an_uppercase_subtype_still_picks_the_right_extension(
            self, fetch, keeps_the_fetched_extension):
        """The second reading. `.PNG` is not in `allowed_thumbnail_extensions`, which is
        lowercase, so before the fold this became `.img` -- and with the reformat off Pillow
        cannot save to `.img`, so the thumbnail was dropped entirely."""
        stored = fetch(a_png(), 'IMAGE/PNG')

        assert stored.file_path.endswith('_512.png')

    def test_an_uppercase_jpeg_still_maps_to_jpg(self, fetch, keeps_the_fetched_extension):
        """The one rename in the map, reached through the fold: `JPEG` has to reach the
        `subtype == 'jpeg'` test to become `.jpg` rather than `.jpeg`."""
        stored = fetch(a_png(), 'IMAGE/JPEG')

        assert stored.file_path.endswith('_512.jpg')

    def test_the_bytes_are_written_under_an_allowlisted_extension(self, fetch):
        """`if file_extension not in allowed_thumbnail_extensions: '.img'` -- D1327's fallback,
        asserted HERE on the name the peer's bytes are actually written under.

        The returned row cannot say it: the resize renames the path to the configured format and
        unlinks the original, so by the time this function returns the peer-chosen name exists
        nowhere. `Image.open` is handed that name, so recording what it was given is what
        distinguishes the allowlist from no allowlist at all.
        """
        opened = []
        real_open = PILImage.open

        def record(path, *args, **kwargs):
            opened.append(path)
            return real_open(path, *args, **kwargs)

        with patch('app.utils.Image.open', side_effect=record):
            assert fetch(a_png(), 'image/bogus') is not None

        assert opened[0].endswith('.img')
        assert not opened[0].endswith('.bogus')

    def test_a_content_type_that_is_not_an_image_is_still_refused(self, fetch):
        """The control, in the direction that matters. The fold widens which spellings are
        accepted and must not widen WHAT is accepted -- `text/html` is refused in either
        case."""
        assert fetch(b'<html><script>alert(1)</script></html>', 'TEXT/HTML') is None

    def test_a_response_with_no_content_type_is_refused(self, fetch):
        """`if content_type:` guards the fold itself. A peer may send no header at all, and
        `None.lower()` would be an AttributeError out of a function `edit_post` does not wrap."""
        url = 'https://thumbnails.example/headerless'
        # respx sends no content-type when the body is passed as raw bytes with none set.
        with patch('app.utils.httpx_client') as client:
            client.get.return_value = SimpleNamespace(
                status_code=200, headers={}, content=a_png(), close=lambda: None)
            assert url_to_thumbnail_file(url) is None


class TestTheParametersAfterTheMediaType:

    def test_a_charset_parameter_is_stripped_before_the_subtype_is_read(
            self, fetch, keeps_the_fetched_extension):
        """`if ';' in content_type`. `image/png; charset=utf-8` is a legal header, and without
        the strip the subtype is `png; charset=utf-8`, so the extension is not in the allowlist
        and the file becomes `.img` -- which with the reformat off Pillow will not write."""
        stored = fetch(a_png(), 'image/png; charset=utf-8')

        assert stored is not None
        assert stored.file_path.endswith('_512.png')

    def test_the_parameter_is_stripped_whatever_its_case(self, fetch,
                                                        keeps_the_fetched_extension):
        """The two repairs together: the fold happens first, so the strip sees one spelling."""
        stored = fetch(a_png(), 'IMAGE/PNG; CHARSET=UTF-8')

        assert stored.file_path.endswith('_512.png')

    def test_a_header_with_no_parameter_is_unchanged(self, fetch,
                                                    keeps_the_fetched_extension):
        """The negative side of the same `if`, which is the common case."""
        stored = fetch(a_png(), 'image/png')

        assert stored.file_path.endswith('_512.png')


class TestEverySvgIsSanitisedExactlyOnce:
    """D1428 deleted the second sanitisation, so these rows carry what it used to guard: for
    each spelling that produces a `.svg`, the stored bytes have had their script removed.

    The deleted line is UNREACHABLE rather than redundant, and the difference matters: with the
    case-fold above, `'svg' in content_type` is true whenever the subtype is `svg`, because the
    subtype is a substring of the content type. `image/SVG` was its one live input.
    """

    @pytest.mark.parametrize('content_type', ['image/svg+xml', 'image/SVG+XML',
                                              'IMAGE/SVG+XML', 'image/SVG',
                                              'image/svg+xml; charset=utf-8'])
    def test_the_script_is_removed_whatever_the_header_spelling_is(self, fetch,
                                                                  content_type):
        stored = fetch(CLEAN_SVG, content_type)

        assert stored is not None
        assert stored.thumbnail_path.endswith('.svg')
        with open(stored.thumbnail_path, 'rb') as handle:
            content = handle.read()
        assert b'script' not in content.lower()
        assert b'<rect' in content

    def test_an_svg_that_cannot_be_sanitised_is_dropped_whatever_the_spelling(self, fetch):
        """The `except ValueError` arm, reached through the uppercase spelling that used to
        miss the sanitiser. `sanitize_svg_bytes` raises on an entity declaration, and the
        answer is no row -- returning one would mean serving the peer's bytes."""
        entity_svg = (b'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY x "y">]>'
                      b'<svg xmlns="http://www.w3.org/2000/svg">&x;</svg>')

        assert fetch(entity_svg, 'IMAGE/SVG+XML') is None

    def test_an_svg_is_not_resized(self, fetch):
        """`if file_extension != ".svg"` below. An SVG has no pixel dimensions to thumbnail, so
        both size columns stay NULL and the one sanitised file is served at both sizes."""
        stored = fetch(CLEAN_SVG, 'image/SVG+XML')

        assert stored.thumbnail_width is None
        assert stored.width is None
        assert stored.file_path == stored.thumbnail_path

    def test_an_ordinary_image_is_not_sanitised(self, fetch):
        """The control for the arm selection. `sanitize_svg_bytes` on a PNG raises, so a PNG
        reaching it would be dropped -- which is how a too-eager `svg` test would show up."""
        calls = []

        with patch('app.utils.sanitize_svg_bytes',
                   side_effect=lambda data: calls.append(data) or data):
            assert fetch(a_png(), 'image/png') is not None

        assert calls == []


class TestTheConfiguredMediumFormat:

    def test_avif_is_registered_with_pillow_before_the_save(self, fetch, monkeypatch):
        """`if medium_image_format == 'AVIF': import pillow_avif`. The plugin registers the AVIF
        codec as an import side effect, and without it an instance configured for AVIF drops
        every remote thumbnail at `img.save` -- which D1328's handler makes silent.

        REMOVING THE IMPORT IS AN EQUIVALENT MUTANT IN THIS ENVIRONMENT, and the reason is the
        installed Pillow rather than the code: Pillow 12.3 reads and writes AVIF natively
        (`PIL.features.check('avif')` is True), so the plugin is redundant here. `requirements.txt`
        pins no Pillow version, so a deployment on Pillow below 11.3 still needs the import --
        which is why the line stays, in this function and in the nine other copies of it under
        `app/`, one of them marked "do not remove". The assertion below is on the OUTPUT format,
        which is what the line exists to make possible.
        """
        monkeypatch.setitem(current_app.config, 'MEDIA_IMAGE_MEDIUM_FORMAT', 'AVIF')

        stored = fetch(a_png(), 'image/png')

        assert stored is not None
        assert stored.file_path.endswith('_512.avif')
        with PILImage.open(stored.file_path) as image:
            assert image.format == 'AVIF'

    def test_the_default_format_is_webp(self, fetch):
        """The `if`'s other side, and the configuration nearly every instance runs."""
        stored = fetch(a_png(), 'image/png')

        assert stored.file_path.endswith('_512.webp')


# --------------------------------------------------------------------------
# The object-storage arm's extra arguments
# --------------------------------------------------------------------------


class _Recorder:
    """A stand-in for the boto3 client, which records what each upload asked for."""

    def __init__(self):
        self.uploads = []

    def upload_file(self, local, bucket, key, ExtraArgs=None):
        self.uploads.append(SimpleNamespace(local=local, bucket=bucket, key=key,
                                           extra=ExtraArgs))


@pytest.fixture
def s3(app, monkeypatch):
    """The configuration that makes `store_files_in_s3()` true, with the client recorded rather
    than dialled. `tests/test_utils_thumbnail_s3.py` drives the same arm through moto for the
    keys and URLs; what is asserted here is the ExtraArgs, which moto does not report back for
    every storage class."""
    for key, value in (('S3_ACCESS_KEY', 'a key'),
                       ('S3_ACCESS_SECRET', 'a secret'),
                       ('S3_ENDPOINT', 'https://s3.example'),
                       ('S3_REGION', 'us-east-1'),
                       ('S3_BUCKET', 'a-bucket'),
                       ('S3_PUBLIC_URL', 'cdn.example')):
        monkeypatch.setitem(current_app.config, key, value)
    monkeypatch.setitem(current_app.config, 'S3_STORAGE_CLASS', '')
    monkeypatch.setitem(current_app.config, 'S3_PUBLIC_ACL', False)
    recorder = _Recorder()
    session = SimpleNamespace(client=lambda **kwargs: recorder)
    with patch('app.utils.boto3.session.Session', lambda: session):
        yield recorder


class TestWhatTheUploadAsksFor:

    @pytest.fixture(autouse=True)
    def clean_tmp(self):
        """This arm writes to `app/static/tmp` and unlinks after each upload, but a failing row
        must not leave the bytes there either -- `clean_up_tmp` sweeps only eight extensions."""
        before = files_under('app/static/tmp')
        yield
        for path in sorted(set(files_under('app/static/tmp')) - set(before)):
            os.unlink(path)

    def test_no_storage_class_is_sent_by_default(self, fetch, s3):
        """`if current_app.config.get('S3_STORAGE_CLASS')`. An empty setting must be OMITTED
        rather than sent as `StorageClass: ''`, which providers reject."""
        assert fetch(a_png(), 'image/png') is not None
        assert s3.uploads != []
        for upload in s3.uploads:
            assert 'StorageClass' not in upload.extra

    def test_a_configured_storage_class_is_sent_on_every_upload(self, fetch, s3,
                                                               monkeypatch):
        """Both sizes go through the same `extra_args`, so one dict covers two uploads -- and an
        instance paying for a cheaper class expects it on the 512px object too."""
        monkeypatch.setitem(current_app.config, 'S3_STORAGE_CLASS', 'GLACIER_IR')

        fetch(a_png(), 'image/png')

        assert len(s3.uploads) == 2
        for upload in s3.uploads:
            assert upload.extra['StorageClass'] == 'GLACIER_IR'

    def test_the_content_type_sent_is_the_one_guessed_from_the_written_file(self, fetch, s3):
        """`guess_mime_type(temp_file_path)`, not the peer's header. The object is served from
        the CDN with whatever is stored here, so taking the peer's value would hand it back the
        content type it chose -- after the extension was taken away from it."""
        fetch(a_png(), 'IMAGE/PNG')

        assert s3.uploads[0].extra['ContentType'] == 'image/webp'

    def test_no_acl_is_sent_by_default(self, fetch, s3):
        """The neighbouring `if`, and the security-relevant one: `ACL: public-read` makes the
        object world-readable, so an instance that configured nothing must not get it."""
        fetch(a_png(), 'image/png')

        for upload in s3.uploads:
            assert 'ACL' not in upload.extra

    def test_a_public_acl_is_sent_when_it_is_configured(self, fetch, s3, monkeypatch):
        monkeypatch.setitem(current_app.config, 'S3_PUBLIC_ACL', True)

        fetch(a_png(), 'image/png')

        assert s3.uploads[0].extra['ACL'] == 'public-read'


# --------------------------------------------------------------------------
# Destroying an SVG that could not be cleaned
# --------------------------------------------------------------------------


class TestDiscardingAnUnsanitizableSvg:
    """`discard_unsanitized_svg` is called when `sanitize_svg` fails on a file already written
    to disk under `app/static`. It truncates first and unlinks second, because truncation is
    what destroys the payload.
    """

    def test_the_file_is_removed(self, app, tmp_path):
        path = tmp_path / 'bad.svg'
        path.write_bytes(b'<svg onload="alert(1)"></svg>')

        discard_unsanitized_svg(str(path))

        assert not os.path.exists(path)

    def test_a_missing_path_is_not_created(self, app, tmp_path):
        """`if not os.path.isfile(filepath): return`. `open(path, 'wb')` CREATES the file, so
        without the guard a call for a path already cleaned would leave an empty file behind in
        the course of deleting one."""
        path = tmp_path / 'not-here.svg'

        discard_unsanitized_svg(str(path))

        assert not os.path.exists(path)

    def test_a_missing_path_is_not_an_error(self, app, tmp_path):
        """The same guard, asserted where it is OBSERVABLE.

        The row above cannot see it: without the guard, `open` creates the file and `os.remove`
        then deletes it, so the end state is identical. Put the path inside a directory that does
        not exist and the two differ -- the guard returns in silence, and no guard means
        `FileNotFoundError`, an `OSError`, caught and logged as a failure that did not happen.
        `sanitize_svg` calls this on every refusal, so a log line per refusal is noise in the one
        place an operator is reading.
        """
        path = tmp_path / 'gone' / 'bad.svg'
        logged = []

        with patch.object(current_app.logger, 'error',
                          side_effect=lambda message, *a, **k: logged.append(message)):
            discard_unsanitized_svg(str(path))

        assert logged == []
        assert not path.exists()

    def test_a_directory_is_left_alone(self, app, tmp_path):
        """The same guard on the other shape `os.path.isfile` excludes. The docstring says the
        failure cases stay side-effect-free apart from the one file it was asked to clean -- so
        neither its contents change nor is anything logged."""
        directory = tmp_path / 'adir'
        directory.mkdir()
        (directory / 'keep.txt').write_bytes(b'kept')
        logged = []

        with patch.object(current_app.logger, 'error',
                          side_effect=lambda message, *a, **k: logged.append(message)):
            discard_unsanitized_svg(str(directory))

        assert (directory / 'keep.txt').read_bytes() == b'kept'
        assert logged == []

    def test_a_file_that_cannot_be_unlinked_is_logged_rather_than_raised(self, app,
                                                                        tmp_path):
        """`except OSError`. The read-only directory the docstring names. This runs on
        `sanitize_svg`'s failure path, so raising here would replace a handled upload refusal
        with an unhandled exception -- and the truncation has already happened, which is the
        part that matters."""
        path = tmp_path / 'bad.svg'
        path.write_bytes(b'<svg onload="alert(1)"></svg>')
        logged = []

        with patch('app.utils.os.remove', side_effect=OSError('read-only file system')), \
                patch.object(current_app.logger, 'error',
                             side_effect=lambda message, *a, **k: logged.append(message)):
            discard_unsanitized_svg(str(path))

        assert logged != []
        assert 'read-only file system' in logged[0]
        # The payload is gone even though the file is not: truncation came first.
        assert path.read_bytes() == b''

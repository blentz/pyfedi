"""app/shared/upload.py, real images in, real files on disk.

This module was at 44.531% coverage (45 statements / 26 arcs missing) going
into this round. `tests/test_utils_security.py` is already part of this
module's coverage oracle: it imports `process_upload` at `:35` and drives the
SVG sanitize-or-reject paths (`:48-50`) for real around `:993-1049`
(`TestProcessUploadRejectsUnsanitizableSvg`), so this file does not duplicate
those cases -- any coverage run for this module must include
`tests/test_utils_security.py` alongside this file or it under-reports.

Every real upload here lands under `app/static/media/` (or, for the S3
branch, briefly under `app/static/tmp/` before being unlinked), which is
gitignored. Every test that writes a file cleans it up in a `finally`, using
the same `files_under`/before-after-comparison arrangement
`tests/test_utils_security.py:44-53` already uses.

`process_upload` opens and re-encodes real images with Pillow (`:68-82`), so
these use `_image()` below to build genuine, small, in-memory images rather
than stub file objects -- a stub would fail at `Image.open` rather than
exercising the branch it is meant to cover.
"""

import io
import os

import pillow_heif
import pytest
from PIL import Image
from sqlalchemy import text
from werkzeug.datastructures import FileStorage

from app import db
from app.models import File
from app.shared.upload import process_file_delete, process_upload
from tests.factories import make_instance, make_user

MEDIA_ROOT = 'app/static'


def files_under(root: str) -> list:
    """Every regular file below `root`, for before/after comparison.

    Copied from tests/test_utils_security.py:47-53 rather than imported, to
    keep this file's oracle self-contained -- see that file's `files_under`
    for the canonical version this mirrors.
    """
    found = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            found.append(os.path.join(dirpath, name))
    return found


def _image(fmt='PNG', size=(8, 8), filename=None):
    """A real, small image as an uploadable file object.

    Pillow actually opens and re-encodes this at app/shared/upload.py:68-82,
    so a stub file object would fail there rather than exercising the branch.
    8x8 keeps the thumbnail step at :72 cheap while staying a genuine image.
    """
    buffer = io.BytesIO()
    Image.new('RGB', size, color=(120, 120, 120)).save(buffer, format=fmt)
    buffer.seek(0)
    ext = {'PNG': '.png', 'GIF': '.gif', 'JPEG': '.jpg', 'WEBP': '.webp'}[fmt]
    return FileStorage(stream=buffer, filename=filename or ('probe' + ext))


def _stored_path(url: str) -> str:
    """process_upload returns SERVER_URL + '/' + final_place minus 'app/'."""
    return 'app/static/' + url.split('/static/', 1)[1]


def _seed_user_file(source_url: str, user_id: int) -> int:
    """A File row plus its `user_file` link, shaped as :112-118 builds them.

    Returns the File id. No disk file is created -- these rows exist so a
    deletion (or the absence of one) can be OBSERVED in the database.
    """
    file_row = File(source_url=source_url)
    db.session.add(file_row)
    db.session.commit()
    db.session.execute(
        text('INSERT INTO "user_file" (file_id, user_id, size) VALUES (:fid, :uid, :size)'),
        {'fid': file_row.id, 'uid': user_id, 'size': 123})
    db.session.commit()
    return file_row.id


def _user_file_row_exists(file_id: int) -> bool:
    return db.session.execute(
        text('SELECT 1 FROM "user_file" WHERE file_id = :fid'), {'fid': file_id}).first() is not None


def _cleanup(before, root=MEDIA_ROOT):
    """Remove every file that appeared under `root` since `before` was taken."""
    after = files_under(root)
    for path in sorted(set(after) - set(before)):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


class TestEmptyFileGuard:
    """`:18` -- `if not image_file or image_file.filename == '':`, both arms."""

    def test_none_file_raises(self, app):
        with app.app_context():
            with pytest.raises(Exception, match='file not uploaded'):
                process_upload(None)

    def test_empty_filename_raises(self, app):
        """`not image_file` is False here; `image_file.filename == ''` is what fires."""
        with app.app_context():
            empty = FileStorage(stream=io.BytesIO(b''), filename='')
            with pytest.raises(Exception, match='file not uploaded'):
                process_upload(empty)

    def test_a_real_image_does_not_raise_the_empty_file_guard(self, app):
        """False arm: a genuine upload passes :18 and proceeds."""
        before = files_under(MEDIA_ROOT)
        with app.app_context():
            url = process_upload(_image())
            try:
                assert url
            finally:
                _cleanup(before)


class TestExtensionRejection:
    """`:25` -- `if file_ext.lower() not in allowed_extensions: raise`."""

    def test_disallowed_extension_raises(self, app):
        with app.app_context():
            bad = FileStorage(stream=io.BytesIO(b'not an image'), filename='payload.exe')
            with pytest.raises(Exception, match='filetype not allowed'):
                process_upload(bad)


class TestCanUploadVideoGate:
    """`:22` -- `if user is not None and can_upload_video(user): allowed_extensions.extend(...)`.

    `can_upload_video` is imported by name into app.shared.upload's module
    globals (`from app.utils import can_upload_video, ...`), so patching
    `app.utils.can_upload_video` would not intercept the call this module
    makes -- rebinding must happen on `app.shared.upload.can_upload_video`
    (global-constraints.md's "Patching: rebind on the module that uses the
    name").
    """

    def test_video_extension_rejected_when_user_lacks_permission(self, app, db_session, monkeypatch):
        monkeypatch.setattr('app.shared.upload.can_upload_video', lambda user: False)
        instance = make_instance('novideo.test')
        user = make_user(instance, 'novideoperm', local=True)
        with app.app_context():
            video = FileStorage(stream=io.BytesIO(b'not really a video'), filename='clip.mp4')
            with pytest.raises(Exception, match='filetype not allowed'):
                process_upload(video, user=user)

    def test_video_extension_allowed_when_user_has_permission(self, app, db_session, monkeypatch):
        """True arm of :22, the VIDEO FALSE ARM of :67's gate (mp4 is recognised
        by app.utils.is_video_url, so `not is_video_url(...)` is False, the whole
        conjunction is False, and the Pillow re-encode is SKIPPED -- the same
        short-circuit TestPillowReencodeGate below names as the video False arm),
        and the True arm of :112's `if user:` (a real File/user_file row is
        written).
        """
        monkeypatch.setattr('app.shared.upload.can_upload_video', lambda user: True)
        instance = make_instance('yesvideo.test')
        user = make_user(instance, 'videoperm', local=True)
        before = files_under(MEDIA_ROOT)
        with app.app_context():
            video = FileStorage(stream=io.BytesIO(b'not really a video, but its extension is allowed'),
                                filename='clip.mp4')
            try:
                url = process_upload(video, user=user)
                assert url.endswith('.mp4')

                file_row = File.query.filter_by(source_url=url).first()
                assert file_row is not None
                link = db.session.execute(
                    text('SELECT size FROM "user_file" WHERE file_id = :fid AND user_id = :uid'),
                    {'fid': file_row.id, 'uid': user.id}).first()
                assert link is not None
            finally:
                _cleanup(before)


class TestPillowReencodeGate:
    """`:67` -- `if not final_place.endswith('.svg') and not final_place.endswith('.gif')
    and not is_video_url(final_place):` into the Pillow re-encode.

    The True arm (a plain image) is exercised throughout this file. This
    covers the '.gif' False arm specifically -- the SVG False arm is already
    covered by tests/test_utils_security.py and the video False arm by
    TestCanUploadVideoGate above, so each of the gate's three short-circuit
    reasons has its own witness rather than all three being exercised only in
    lockstep by one file type.
    """

    def test_a_gif_upload_skips_the_pillow_reencode(self, app):
        """Under the suite's default (empty) MEDIA_IMAGE_FORMAT, a skipped
        re-encode and a re-encode that happened to run are indistinguishable
        -- neither one would change the extension or the sniffed format, so
        the original assertions here held whether or not :67's '.gif' arm
        actually fired. MEDIA_IMAGE_FORMAT is set to 'WEBP' below precisely
        because that value WOULD visibly change the output (extension
        '.gif' -> '.webp', sniffed format GIF -> WEBP) if the re-encode ran
        -- the same technique TestImageFormatAndQualityKwargs uses to prove
        its kwargs are applied. Asserting the file stayed a GIF under that
        setting is what actually discriminates "skipped" from "ran".
        """
        before = files_under(MEDIA_ROOT)
        with app.app_context():
            original_format = app.config['MEDIA_IMAGE_FORMAT']
            app.config['MEDIA_IMAGE_FORMAT'] = 'WEBP'
            try:
                url = process_upload(_image(fmt='GIF'))
                assert url.endswith('.gif')
                stored = _stored_path(url)
                # Untouched by Pillow's re-encode: still a valid GIF on
                # disk, not converted to WEBP as MEDIA_IMAGE_FORMAT='WEBP'
                # would force if the '.gif' skip at :67 did not fire.
                with Image.open(stored) as img:
                    assert img.format == 'GIF'
            finally:
                app.config['MEDIA_IMAGE_FORMAT'] = original_format
                _cleanup(before)


class TestFormatVersusExtensionMismatch:
    """`:69`/`:84` -- `if '.' + img.format.lower() in allowed_extensions: ... else: raise`.

    The brief suggests "a GIF saved as .png" for this. That does NOT reach
    the raise: '.gif' is itself in `allowed_extensions`
    (app/shared/upload.py:21), so a real GIF's bytes named with a .png
    extension still passes the `'.' + img.format.lower() in allowed_extensions`
    check -- img.format sniffs the real GIF signature regardless of the
    filename, and '.gif' is allowed. Investigated at source before writing
    this test; using BMP instead, whose format string ('BMP') maps to
    '.bmp', which is NOT in allowed_extensions, so it genuinely reaches the
    raise while still passing the earlier `:25` extension check because the
    file is *named* '.png'.
    """

    def test_a_bmp_disguised_as_png_is_rejected_after_being_opened(self, app):
        before = files_under(MEDIA_ROOT)
        buffer = io.BytesIO()
        Image.new('RGB', (8, 8), color=(1, 2, 3)).save(buffer, format='BMP')
        buffer.seek(0)
        disguised = FileStorage(stream=buffer, filename='disguised.png')
        with app.app_context():
            try:
                with pytest.raises(Exception, match='filetype not allowed'):
                    process_upload(disguised)
            finally:
                # The BMP is saved to disk (:39) before the format check runs
                # (:68-84), so a rejected upload still leaves the raw bytes
                # behind under the media root -- clean them up like any other
                # test here.
                _cleanup(before)


class TestImageFormatAndQualityKwargs:
    """`:75`/`:79` -- the `image_format`/`image_quality` kwargs. Both arms of
    each are DRIVEN; only the format half is OBSERVED.

    Both read `current_app.config['MEDIA_IMAGE_FORMAT']` /
    `['MEDIA_IMAGE_QUALITY']`, and each test sets both explicitly rather than
    relying on whatever config.py's environment-derived default happens to be,
    so which arm each test takes is determined here rather than inherited.

    WHAT IS ASSERTED IS THE FORMAT HALF ONLY. The output extension and
    `Image.open(stored).format` distinguish :75's True arm from its False arm.
    Nothing in this file observes image quality at all, and at these settings
    it is unobservable in principle: `MEDIA_IMAGE_QUALITY = 80` is Pillow's own
    WebP default, so :79-80's `kwargs['quality'] = 80` produces the same output
    as omitting the kwarg entirely. :79/:80 are therefore entered but not
    discriminated. That gap is registered as D592(d) with its recipe (assert
    output bytes or size on a lossy format); no test is added for it here.
    """

    def test_a_configured_format_and_quality_are_both_applied(self, app):
        before = files_under(MEDIA_ROOT)
        with app.app_context():
            original_format = app.config['MEDIA_IMAGE_FORMAT']
            original_quality = app.config['MEDIA_IMAGE_QUALITY']
            app.config['MEDIA_IMAGE_FORMAT'] = 'WEBP'
            app.config['MEDIA_IMAGE_QUALITY'] = 80
            try:
                url = process_upload(_image(fmt='PNG'))
                assert url.endswith('.webp')
                stored = _stored_path(url)
                with Image.open(stored) as img:
                    assert img.format == 'WEBP'
            finally:
                app.config['MEDIA_IMAGE_FORMAT'] = original_format
                app.config['MEDIA_IMAGE_QUALITY'] = original_quality
                _cleanup(before)

    def test_no_configured_format_or_quality_leaves_kwargs_unset(self, app):
        before = files_under(MEDIA_ROOT)
        with app.app_context():
            original_format = app.config['MEDIA_IMAGE_FORMAT']
            original_quality = app.config['MEDIA_IMAGE_QUALITY']
            app.config['MEDIA_IMAGE_FORMAT'] = ''
            app.config['MEDIA_IMAGE_QUALITY'] = 0
            try:
                url = process_upload(_image(fmt='PNG'))
                # final_ext is untouched: still the original upload extension.
                assert url.endswith('.png')
            finally:
                app.config['MEDIA_IMAGE_FORMAT'] = original_format
                app.config['MEDIA_IMAGE_QUALITY'] = original_quality
                _cleanup(before)


class TestS3Branch:
    """`:89-109` -- store_files_in_s3() True: upload to S3, rebuild `url`, unlink the tmp file.

    Follows tests/conftest.py's s3_bucket fixture (`:522-527`), which wraps
    the test body in `mock_aws()`. store_files_in_s3() (app/utils.py:4317)
    requires S3_ACCESS_KEY, S3_ACCESS_SECRET and S3_ENDPOINT all non-empty.

    S3_ENDPOINT must be a real AWS-shaped endpoint for moto to intercept the
    request at all: a made-up custom endpoint (e.g. 'https://s3.example.test',
    the shape other test files in this repo use to stub `boto3` itself
    instead of letting the call reach botocore) makes moto's mock a no-op --
    verified experimentally against this repo's moto version -- and botocore
    then attempts a real DNS lookup and fails with
    EndpointConnectionError. Using the real 'https://s3.amazonaws.com'
    endpoint lets moto's interception match and the call succeeds against the
    in-memory fake S3.
    """

    def test_an_upload_is_moved_to_s3_and_the_tmp_copy_is_removed(self, app, s3_bucket):
        tmp_before = files_under('app/static/tmp') if os.path.isdir('app/static/tmp') else []
        with app.app_context():
            original = {k: app.config[k] for k in (
                'S3_ACCESS_KEY', 'S3_ACCESS_SECRET', 'S3_ENDPOINT', 'S3_REGION',
                'S3_BUCKET', 'S3_PUBLIC_URL', 'S3_STORAGE_CLASS', 'S3_PUBLIC_ACL')}
            app.config['S3_ACCESS_KEY'] = 'test-key'
            app.config['S3_ACCESS_SECRET'] = 'test-secret'
            app.config['S3_ENDPOINT'] = 'https://s3.amazonaws.com'
            app.config['S3_REGION'] = 'us-east-1'
            app.config['S3_BUCKET'] = s3_bucket
            app.config['S3_PUBLIC_URL'] = 'cdn.example.test'
            app.config['S3_STORAGE_CLASS'] = 'STANDARD'
            app.config['S3_PUBLIC_ACL'] = True
            try:
                url = process_upload(_image(fmt='PNG'))
                assert url.startswith('https://cdn.example.test/posts/')

                # The tmp copy (app/shared/upload.py:31, :109) is gone...
                tmp_after = files_under('app/static/tmp') if os.path.isdir('app/static/tmp') else []
                assert sorted(tmp_after) == sorted(tmp_before)

                # ...and it is genuinely IN the moto bucket, not just claimed to be.
                import boto3
                s3 = boto3.client('s3', region_name='us-east-1',
                                  endpoint_url='https://s3.amazonaws.com',
                                  aws_access_key_id='test-key', aws_secret_access_key='test-secret')
                key = url.split('https://cdn.example.test/', 1)[1]
                head = s3.head_object(Bucket=s3_bucket, Key=key)
                assert head['ContentType'] == 'image/png'
            finally:
                for k, v in original.items():
                    app.config[k] = v

    def test_an_upload_without_storage_class_or_public_acl_configured_still_works(self, app, s3_bucket):
        """False arms of `:92`'s `if current_app.config.get('S3_STORAGE_CLASS'):`
        and `:94`'s `if current_app.config.get('S3_PUBLIC_ACL'):` -- the positive
        control above sets both truthy, so this leaves them at their default
        falsy config values to reach the other side of each."""
        with app.app_context():
            original = {k: app.config[k] for k in (
                'S3_ACCESS_KEY', 'S3_ACCESS_SECRET', 'S3_ENDPOINT', 'S3_REGION',
                'S3_BUCKET', 'S3_PUBLIC_URL', 'S3_STORAGE_CLASS', 'S3_PUBLIC_ACL')}
            app.config['S3_ACCESS_KEY'] = 'test-key'
            app.config['S3_ACCESS_SECRET'] = 'test-secret'
            app.config['S3_ENDPOINT'] = 'https://s3.amazonaws.com'
            app.config['S3_REGION'] = 'us-east-1'
            app.config['S3_BUCKET'] = s3_bucket
            app.config['S3_PUBLIC_URL'] = 'cdn.example.test'
            app.config['S3_STORAGE_CLASS'] = ''
            app.config['S3_PUBLIC_ACL'] = False
            try:
                url = process_upload(_image(fmt='PNG'))
                assert url.startswith('https://cdn.example.test/posts/')
            finally:
                for k, v in original.items():
                    app.config[k] = v


class TestHeicAndAvifLazyImports:
    """`:52-53`/`:54-55`/`:64-65` -- the `.heic`/`.avif` lazy `import` statements.

    Not in the brief's explicit checklist, but reachable cheaply with real
    codecs already present in this environment (pillow_heif, pillow_avif),
    so covered here rather than left as residue: a module described as
    "closed" should not carry gaps that cost nothing to close.
    """

    def test_a_heic_upload_registers_the_heif_opener_then_hits_the_format_mismatch(self, app):
        """:52-53 -- `if file_ext.lower() == '.heic': register_heif_opener()`.

        FINDING, investigated at source rather than assumed: a genuine .heic
        upload can never succeed through this function as written. Once
        opened, Pillow/pillow_heif report `img.format == 'HEIF'` (verified
        directly against this environment's pillow_heif), so `:69`'s check
        (`'.' + img.format.lower() in allowed_extensions`) tests for '.heif',
        which is NOT in `allowed_extensions` -- only '.heic' is
        (app/shared/upload.py:21). Every real .heic upload therefore falls
        into `:84`'s `raise Exception('filetype not allowed')`, a second,
        independent route to the same mismatch TestFormatVersusExtensionMismatch
        covers with a disguised BMP. Registered, not fixed: app/ is out of
        scope for this task.
        """
        before = files_under(MEDIA_ROOT)
        buffer = io.BytesIO()
        pillow_heif.from_pillow(Image.new('RGB', (8, 8), color=(4, 5, 6))).save(buffer, format='HEIF')
        buffer.seek(0)
        heic = FileStorage(stream=buffer, filename='probe.heic')
        with app.app_context():
            try:
                with pytest.raises(Exception, match='filetype not allowed'):
                    process_upload(heic)
            finally:
                _cleanup(before)

    def test_an_avif_upload_imports_pillow_avif_and_succeeds(self, app):
        """:54-55 -- `if file_ext.lower() == '.avif': import pillow_avif`."""
        before = files_under(MEDIA_ROOT)
        buffer = io.BytesIO()
        import pillow_avif  # noqa: F401  -- registers the AVIF plugin for this test's encode
        Image.new('RGB', (8, 8), color=(7, 8, 9)).save(buffer, format='AVIF')
        buffer.seek(0)
        avif = FileStorage(stream=buffer, filename='probe.avif')
        with app.app_context():
            try:
                url = process_upload(avif)
                assert url
            finally:
                _cleanup(before)

    def test_configuring_avif_output_format_imports_pillow_avif(self, app):
        """:64-65 -- `if image_format == 'AVIF': import pillow_avif`, independent of
        the upload's own extension: a plain PNG upload with output format
        configured to AVIF."""
        before = files_under(MEDIA_ROOT)
        with app.app_context():
            original_format = app.config['MEDIA_IMAGE_FORMAT']
            app.config['MEDIA_IMAGE_FORMAT'] = 'AVIF'
            try:
                url = process_upload(_image(fmt='PNG'))
                assert url.endswith('.avif')
            finally:
                app.config['MEDIA_IMAGE_FORMAT'] = original_format
                _cleanup(before)


class TestProcessFileDelete:
    """`process_file_delete`'s two False arms (`:127`, `:130`)."""

    def test_falsy_user_id_skips_the_lookup_entirely(self, app, db_session):
        """:127 False arm: user_id=0 short-circuits before the query.

        ASSERTS STATE, NOT THE RETURN VALUE. `process_file_delete` has no
        `return` on any path (:126-134), so `... is None` is unconditionally
        true and passed identically with :127 deleted or inverted -- false
        witness mechanism (a). A real File and its `user_file` link are seeded
        first and asserted still present afterwards, so what is checked is
        what the call did rather than what Python returns from a procedure.

        This does NOT claim to kill a :127 mutant: with user_id=0 the query at
        :128-129 filters on `user_file.c.user_id == 0` and finds nothing on
        either arm. It claims only that nothing was deleted.
        """
        instance = make_instance('skiplookup.test')
        user = make_user(instance, 'skiplookupuser', local=True)
        with app.app_context():
            source_url = 'https://example.test/does-not-matter.png'
            file_id = _seed_user_file(source_url, user.id)

            process_file_delete(source_url, 0)

            assert File.query.get(file_id) is not None
            assert _user_file_row_exists(file_id)

    def test_user_id_with_no_matching_file_is_a_noop(self, app, db_session):
        """:130 False arm: the query runs (True at :127) but matches nothing.

        ASSERTS STATE, NOT THE RETURN VALUE, for the same reason as the test
        above. Here the assertion is genuinely falsifiable: the seeded row
        belongs to this user but sits at a DIFFERENT source_url, so dropping
        :129's `File.source_url == url` filter makes the query find it, :130
        True, and the row deleted -- which these assertions catch.
        """
        instance = make_instance('nodelete.test')
        user = make_user(instance, 'nodeleteuser', local=True)
        with app.app_context():
            file_id = _seed_user_file('https://example.test/a-different-file.png', user.id)

            process_file_delete('https://example.test/nonexistent.png', user.id)

            assert File.query.get(file_id) is not None
            assert _user_file_row_exists(file_id)

    def test_a_matching_file_is_deleted_from_db_and_disk(self, app, db_session):
        """:130 True arm, and a genuine disk deletion.

        `delete_from_disk` (app/models.py:421-434) acts on `File.file_path`,
        not `source_url`. A File row built with only `source_url` set -- as
        the original version of this test did, matching how process_upload
        itself builds the row at :113 -- has `file_path=None`, so :424's
        `if self.file_path:` never fires and no disk I/O happens at all; the
        `delete_from_disk` half of this test's name was previously vacuous.
        `file_path` is set here to a real file this test creates first, so
        the deletion is real, and the file's absence afterward is asserted
        directly rather than only the DB rows.
        """
        instance = make_instance('dodelete.test')
        user = make_user(instance, 'deleteuser', local=True)
        with app.app_context():
            before = files_under(MEDIA_ROOT)
            directory = 'app/static/media/posts/xx/yy'
            os.makedirs(directory, exist_ok=True)
            disk_path = os.path.join(directory, 'doesexist.png')
            with open(disk_path, 'wb') as fh:
                fh.write(b'not really a png, just needs to exist on disk')
            assert os.path.isfile(disk_path)

            source_url = f"{app.config['SERVER_URL']}/static/media/posts/xx/yy/doesexist.png"
            file_row = File(source_url=source_url, file_path=disk_path)
            db.session.add(file_row)
            db.session.commit()
            db.session.execute(
                text('INSERT INTO "user_file" (file_id, user_id, size) VALUES (:fid, :uid, :size)'),
                {'fid': file_row.id, 'uid': user.id, 'size': 123})
            db.session.commit()
            file_id = file_row.id

            try:
                process_file_delete(source_url, user.id)

                assert File.query.get(file_id) is None
                remaining = db.session.execute(
                    text('SELECT 1 FROM "user_file" WHERE file_id = :fid'), {'fid': file_id}).first()
                assert remaining is None
                assert not os.path.isfile(disk_path)
                assert files_under(MEDIA_ROOT) == before
            finally:
                _cleanup(before)


class TestUrlCannotBeFalsy:
    """`:120-121` -- `if not url: raise Exception('unable to process upload')`.

    RULING: unreachable by any input, and not reachable by any legitimate
    double either. Recorded here rather than assumed -- see this task's
    report for the full reasoning; the short version:

    `url` is built at `:86` as `f"{current_app.config['SERVER_URL']}/{...}"`.
    app/__init__.py:133/135 sets SERVER_URL to
    `f"https://{SERVER_NAME}"`/`f"{HTTP_PROTOCOL}://{SERVER_NAME}"` at app
    creation, unconditionally and before any request -- there is no config
    value that leaves it empty, and even if there were, the f-string still
    contains a literal '/' character, so `url`'s minimum possible length is 1
    regardless of what SERVER_URL or the path evaluate to. The same holds for
    `:106`'s reassignment (`f"https://{S3_PUBLIC_URL}/{...}"`). No fixture,
    config value, or crafted upload can make either f-string produce ''.

    A double could only reach it by monkeypatching Python's own str
    formatting/concatenation machinery, which would not be exercising this
    function at all -- fabricating a state production cannot reach, which is
    exactly what tests/README.md fact 75 warns against doing to manufacture a
    fake kill. No test is written for this line; this class exists only to
    keep the ruling colocated with the code it rules on.

    DELIBERATELY EMPTY: pytest collects ZERO items from this class. It is a
    `Test*`-named docstring, not a test that silently stopped running. The
    same ruling is recorded in the register at D587, which cites this class;
    it is kept here as well so a reader of `:120-121` meets the reasoning
    without having to know the register exists.
    """

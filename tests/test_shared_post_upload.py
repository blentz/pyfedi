"""`edit_post`'s file-upload block -- Group E1 of the `app/shared/post.py` campaign.

SCOPE. `app/shared/post.py:461-563`, 66 statements and 39 branch arcs. This is
the first block in the campaign that is not control flow over database rows: it
runs a real file through a real image pipeline -- seek, read, save, PIL open,
colour conversion, thumbnail, re-encode, optional hash, optional S3 upload.

THE HARNESS IS NEW, and three facts shape it. All three were probed in Task 1
rather than assumed.

  1. A REAL `FileStorage` IS REQUIRED. `:479` calls `.seek(0)`, `:480`
     `.read()` and `.mimetype`, `:486` `.seek(0)`, `:487` `.save(path)`, and
     `:514` opens the saved result with `PIL.Image.open`. Sub-project 37's
     `SimpleNamespace(filename=...)` stand-in worked only because
     `make_post:197-204` reads nothing but `.filename`.

  2. THE UPLOAD PATH IS RELATIVE TO THE WORKING DIRECTORY. `:475` is
     `'app/static/media/posts/' + new_filename[0:2] + '/' + new_filename[2:4]`,
     and the container's working directory is the bind-mounted repo root, so
     every test would otherwise write a real file into the source tree under a
     random 15-character name from `gibberish(15)` at `:470`. `.gitignore:162`
     and `:163` cover those directories, so nothing reaches a commit -- but the
     files accumulate and nothing tracks what to remove. `chdir_upload` moves
     the working directory into pytest's per-test `tmp_path`, which redirects
     every relative write and is cleaned up by pytest itself. PROBE A confirmed
     this works end to end: no exception from the redirect itself, and a
     `tmp_path/app/static/media/posts/XX/YY/<gibberish>.png` was found after
     the call. Confirmed separately that the repository's own
     `app/static/media/` gained no new file and no new directory across the
     probe run (directory and file counts identical before and after).

     A SURPRISE THE PLAN'S LITERAL PROBE A SCRIPT DID NOT ANTICIPATE: calling
     `edit_post` at all with `uploaded_file=` set reaches `:601`
     `is_image_url(url)` on the NEWLY BUILT url (`:535`,
     `f"{SERVER_URL}/{final_place...}"`) regardless of how narrow the test's
     intent is -- this is downstream of the upload block (:461-563) entirely,
     in the function's shared tail. Without an `http_mock` route the literal
     probe script (`db_session, chdir_upload` only) fails with
     `respx.models.AllMockedAssertionError` on that unmocked HEAD, never
     reaching the `chdir` assertion at all -- a fourth outcome the plan's three
     did not name (it is not "before the save": the save already happened; the
     built url proves it). Every test in this file that drives a full
     `edit_post` call therefore also needs `http_mock` with a route that
     matches on `url__regex` (the filename is `gibberish(15)`, unknown ahead of
     time) -- a plain `http_mock.head('https://...').respond(...)` as used
     throughout `test_shared_post_edit.py` cannot be used here. Registering a
     `GET` route for the same pattern is a mistake, not a safety margin. The
     HEAD mock reports `Content-Type: image/png`, so `is_image_url` returns
     `True` and `edit_post` takes `:601`'s `if is_image_url(url):` branch --
     the ONLY one of the four mutually exclusive `if`/`elif`/`elif`/`else` arms
     at `:601`/`:619`/`:630`/`:641` that does not call `opengraph_parse`
     (`:621`/`:632`/`:642`), which is what would issue a GET via `get_request`.
     Taking the `:601` branch makes the other three arms, and every GET they
     could cause, unreachable in the same call -- not merely unlikely, but
     structurally excluded by the `if`/`elif` chain. So no GET is ever
     attempted on this url in this test, independent of the host: a registered
     GET route would simply never be called, and `http_mock`'s
     `assert_all_called=True` would fail the test at teardown for that reason.
     (`test.piefed.local`'s `.local` suffix DOES make `get_request` skip with
     an "invalid get request" log per `app/utils.py:132-133` -- that mechanism
     is real, but it never engages on this path, since `get_request` is never
     called here at all. A later task whose post takes one of the other three
     arms would hit that mechanism directly and should cite it instead.)

  3. `c2pa` IS INSTALLED, so `:480` runs for real rather than needing a stub
     (`app/utils.py:5757` does `import c2pa` inside the function). PROBE C
     (below) found that a plain in-memory generated PNG comes back with
     `ai_generated: False` -- the same value the block's default (unset) path
     takes -- so `:481`'s true arm is NOT reachable with any image this harness
     can construct from `Image.new(...)`. Reaching it requires either a crafted
     C2PA manifest or a monkeypatch of `post_module.inspect_image_c2pa`; later
     tasks that need the true arm must monkeypatch rather than try to craft a
     real AI-generation signal.

`Image.MAX_IMAGE_PIXELS` IS MUTATED PROCESS-WIDE at `:503` and never restored.
Nothing here depends on the default, but a later test in another file might.

THE EXTENSION CHECK AT `:463-468` IS BYTE-IDENTICAL TO `make_post:197-204`,
registered as D455(d). Sub-project 37 covered that copy. These tests call
`edit_post` directly, so they exercise this one.

A RULING ON THE TWO IDENTICAL EXCEPTION MESSAGES, recorded here because two
later tasks depend on it. `:468` and `:533` both raise
`Exception('filetype not allowed')` -- byte for byte the same message. A bare
`pytest.raises(Exception, match='filetype not allowed')` cannot distinguish
which line fired: a test aimed at one could pass because the other fired
instead. The discriminator is the FILE, not the message: `:468` raises BEFORE
`:487` saves anything, so no file exists under `chdir_upload` afterward;
`:533` raises AFTER `:487`'s save, so a file DOES exist under `chdir_upload`
afterward. `:531`'s re-encode and `:533`'s raise are mutually exclusive arms
of the SAME `if`/`else` at `:515` -- `:531` runs only in the `if` branch,
`:533` only in the `else` -- so `:531` never runs before `:533` fires; the
file present when `:533` raises is solely the one `:487` originally saved,
untouched by `:531`. Every test in this file that asserts the 'filetype not allowed'
message must also assert whether a file exists under `chdir_upload` --
absent for `:468`, present for `:533`.
"""

import sys
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image
from werkzeug.datastructures import FileStorage

import app.shared.post as post_module
from app import db
from app.constants import POST_TYPE_ARTICLE, POST_TYPE_IMAGE, POST_TYPE_VIDEO, SRC_API
from app.shared.post import edit_post
from app.utils import get_setting, set_setting
from tests.test_shared_post_edit import _api_input, _seed


SVG_BYTES = (b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8">'
            b'<rect width="8" height="8" fill="#0a141e"/></svg>')
"""Real, minimal SVG/XML. SVG is text, not a raster format, so it needs no
image library at all -- see `make_upload`'s `fmt='SVG'` case.
"""


MALFORMED_SVG_BYTES = (b'<?xml version="1.0" encoding="UTF-8"?>'
                       b'<!DOCTYPE svg [<!ENTITY xxe "pwned">]>'
                       b'<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8">'
                       b'<rect width="8" height="8" fill="#0a141e"/></svg>')
"""An SVG whose internal DOCTYPE subset declares an XML entity.

`refuse_svg_entity_declarations` (`app/utils.py:5542`) scans for the literal
bytes `<!ENTITY` and raises `ValueError('SVG entity declarations are not
allowed')` before py-svg-hush ever parses the document. `sanitize_svg_bytes`
calls it first (`app/utils.py:5633` onward), so `sanitize_svg`'s `except
Exception` (`app/utils.py:5730`) catches that `ValueError`, logs it, destroys
the file via `discard_unsanitized_svg`, and returns False -- `:500`'s TRUE
arm. VERIFIED against the installed py-svg-hush/this scan directly:

    from app.utils import refuse_svg_entity_declarations
    refuse_svg_entity_declarations(MALFORMED_SVG_BYTES)
    # -> ValueError: SVG entity declarations are not allowed

`make_upload` has no `fmt` that produces this -- its own docstring says a
test wanting `:500`'s TRUE condition "needs a different, malformed payload"
-- so this is built directly as a `FileStorage`, not through that helper.
"""


class _StubS3Client:
    """A no-op stand-in for the boto3 S3 client `edit_post` builds at `:550`.

    `upload_file` and `close` do nothing and record nothing: this file owns
    only the directory fork at `:472` (arc `472->473`), not the S3 upload
    itself -- that is Task 6's arc, per this round's controller ruling, so
    there is deliberately nothing here to assert on.
    """

    def upload_file(self, *args, **kwargs):
        pass

    def close(self):
        pass


class _StubBoto3Session:
    """Stands in for `boto3.session.Session`.

    Callable so `boto3.session.Session()` at `:544` returns this instance,
    and `.client(...)` at `:550` returns the no-op client above -- both
    calls that would otherwise reach out to a real endpoint.
    """

    def __call__(self):
        return self

    def client(self, **kwargs):
        return _StubS3Client()


def make_upload(filename='pic.png', fmt='PNG', size=(8, 8), colour=(10, 20, 30),
                content_type=None):
    """A real `FileStorage`, genuine-content for some formats, renamed for others.

    Built in memory rather than from a fixture file, following
    tests/test_utils_images.py, which constructs every image it needs with
    `Image.new(...)` -- no binary assets live in this repository and none
    should be added. 8x8 keeps encode and thumbnail cost negligible.

    GENUINE CONTENT for `fmt` in `PNG`, `GIF`, `JPEG` -- Pillow encodes real
    bytes of that format via `Image.new(...).save(buf, format=fmt)`, and
    `fmt='SVG'` (case-insensitive) takes an entirely different path, returning
    `SVG_BYTES` (real SVG/XML text, since Pillow has no SVG writer and SVG is
    not a raster format at all). VERIFIED against `:500`'s real
    `sanitize_svg(final_place)`: saving `SVG_BYTES` to disk and calling
    `sanitize_svg` on it returns `True` (successful sanitization), rewriting
    the file with py-svg-hush's re-serialized, still-valid-and-equivalent SVG.
    So `:500`'s condition (`not sanitize_svg(final_place)`) is False for
    `SVG_BYTES` and `:501`'s raise is NOT taken -- `make_upload(fmt='SVG')`
    exercises the SUCCESSFUL-sanitization path through `:500`. A test wanting
    `:500`'s TRUE condition (`:501`'s raise, an SVG that FAILS sanitization)
    needs a different, malformed payload -- `SVG_BYTES` alone cannot produce
    that outcome.

    RENAMED RASTER BYTES ONLY for `fmt` in `HEIC`, `AVIF`, or any video-like
    extension via `filename` (e.g. `.mp4`): this helper always falls through to
    `Image.new(...).save(buf, format=fmt)` for anything other than `'SVG'`, and
    Pillow cannot encode HEIC without `register_heif_opener()` having already
    run, or AVIF without `pillow_avif` registered -- neither happens here. A
    call like `make_upload(filename='pic.heic', fmt='PNG')` therefore produces
    real PNG bytes wearing a `.heic` filename, not genuine HEIC content. That is
    sufficient for branches gated on the FILENAME alone (`:491`'s
    `if final_ext == '.heic':`, `:493`'s `.avif` check, and `:513`'s
    `is_video_url(final_place)`, all of which read the extension string, not
    the bytes), but it cannot exercise a real HEIC/AVIF decode, a genuine
    format-mismatch at `:515` for those formats, or any video-content-specific
    behaviour. A test that needs that would need a different helper.

    `content_type` becomes `.mimetype`, which `:480` passes to
    `inspect_image_c2pa`; it defaults to `image/<fmt lowered>`
    (`image/svg+xml` for `fmt='SVG'`).
    """
    if fmt.upper() == 'SVG':
        buf = BytesIO(SVG_BYTES)
        return FileStorage(stream=buf, filename=filename,
                           content_type=content_type or 'image/svg+xml')
    buf = BytesIO()
    Image.new('RGB', size, colour).save(buf, format=fmt)
    buf.seek(0)
    return FileStorage(stream=buf, filename=filename,
                       content_type=content_type or f'image/{fmt.lower()}')


@pytest.fixture
def chdir_upload(tmp_path, monkeypatch):
    """Redirect every relative write in the upload block into `tmp_path`.

    `:475`'s directory is relative, so changing the working directory moves the
    whole pipeline -- `ensure_directory_exists` at `:476`, the save at `:487`,
    the re-encode at `:531` and the unlink at `:563` -- into a directory pytest
    creates per test and removes afterwards. Nothing touches the repository.

    Returns `tmp_path` so a test can inspect what was written.
    """
    monkeypatch.chdir(tmp_path)
    return tmp_path


def seed_upload_context(**over):
    """Thin wrapper over `_seed()`, named for this file's upload tests.

    `_seed` already seeds instance/user/community/post with `url=None`; this
    exists only so later tasks in this file have a name that documents intent
    at the call site rather than importing `_seed` directly each time.
    """
    return _seed(**over)


def test_an_uploaded_image_is_saved_and_linked(db_session, chdir_upload, http_mock):
    """The default path end to end: `:461` true, through `:487`'s save and
    `:531`'s re-encode, to a `File` row linked on the post.

    Asserts on the file existing under the redirected directory AND on the
    `File` row, because either alone could be produced by a different block --
    `edit_post` sets `post.image_id` on paths that never touch an upload.

    `http_mock` registers a `url__regex` route rather than an exact url,
    because the saved path (and so the url built at `:535`) embeds
    `gibberish(15)` and is different on every run -- see the module docstring.
    """
    http_mock.head(url__regex=r'.*').respond(200, headers={'Content-Type': 'image/png'})
    s = seed_upload_context()
    edit_post(_api_input(), s.post, POST_TYPE_IMAGE, SRC_API, user=s.user,
              uploaded_file=make_upload())

    written = list(chdir_upload.rglob('app/static/media/posts/*/*/*'))
    assert len(written) == 1
    db.session.refresh(s.post)
    assert s.post.image_id is not None


def test_a_disallowed_extension_is_refused(db_session, chdir_upload):
    """`:461`'s TRUE arm (a non-empty `uploaded_file.filename`) and `:467`'s
    TRUE arm feeding `:468`'s raise.

    This is `edit_post`'s own copy of the check, byte-identical to
    `make_post:197-204` (D455(d)), which sub-project 37 covered. Calling
    `edit_post` directly is what makes this test exercise THIS copy: reaching
    it through `make_post` would hit that one first and prove nothing about
    this line.

    Positive control: `test_an_allowed_extension_passes` below. It differs in
    more than the extension -- `type` is `POST_TYPE_IMAGE` there rather than
    `POST_TYPE_ARTICLE` here (kept as the brief's literal example gave it,
    since `type` is irrelevant to `:467`'s check outside the `POST_TYPE_VIDEO`
    compound at `:464`) and it needs `http_mock` because it runs to
    completion. What makes it a control for THIS test is narrower: both
    reach `:467` with a non-empty, non-video-typed `uploaded_file`, and only
    the extension decides which side of `:467` each one takes.

    No `http_mock` is registered: the raise at `:468` happens before `:470`'s
    `gibberish(15)` and every step after it, so this call never reaches
    `:601`'s HEAD request at all.

    Per the module docstring's ruling on the two byte-identical
    'filetype not allowed' messages (`:468` vs `:533`), this test also
    confirms no file was written -- `:468` raises BEFORE `:487`'s save, so a
    file existing here would mean `:533` fired instead, not `:468`.
    """
    s = _seed()
    with pytest.raises(Exception, match='filetype not allowed'):
        edit_post(_api_input(), s.post, POST_TYPE_ARTICLE, SRC_API, user=s.user,
                  uploaded_file=make_upload(filename='payload.exe'))

    written = list(chdir_upload.rglob('app/static/media/posts/*/*/*'))
    assert written == []


def test_an_allowed_extension_passes(db_session, chdir_upload, http_mock):
    """`:467`'s FALSE arm, arc `467->470` -- the positive control for
    `test_a_disallowed_extension_is_refused` above.

    Without it, a `pytest.raises` that fired for an unrelated reason would
    look identical to one that fired for the right reason. As the control for
    a test that asserts NO file exists, this one asserts a file DOES exist --
    without that, a mutant deleting `:487`'s save outright would still pass
    here (`post.image_id` gets set by the later `:601`-`:608` block
    regardless), making this control weaker than the thing it controls for.

    Unlike the raising tests in this file, this call runs to completion and
    reaches `:601`'s `is_image_url` HEAD request in `edit_post`'s shared
    tail, so it needs `http_mock` with a `url__regex` route (see the module
    docstring) -- the saved filename is `gibberish(15)`, unknown ahead of
    time, so an exact-url mock cannot be used.
    """
    http_mock.head(url__regex=r'.*').respond(200, headers={'Content-Type': 'image/png'})
    s = _seed()
    edit_post(_api_input(), s.post, POST_TYPE_IMAGE, SRC_API, user=s.user,
              uploaded_file=make_upload(filename='pic.png'))

    written = list(chdir_upload.rglob('app/static/media/posts/*/*/*'))
    assert len(written) == 1
    db.session.refresh(s.post)
    assert s.post.image_id is not None


def test_a_video_upload_is_refused_when_video_uploads_are_off(db_session, chdir_upload):
    """`:464`'s SECOND conjunct taken FALSE with the first TRUE, arc
    `464->466`.

    `can_upload_video` returns False on the default (unset) setting, so
    `:465` never runs and `.mp4` stays out of `:463`'s list; `:467` then
    finds `.mp4` not allowed and `:468` raises. A regression dropping the
    `can_upload_video()` conjunct (e.g. `and` weakened so only `type` is
    checked) would let this through -- see
    `test_a_video_upload_is_accepted_when_video_uploads_are_enabled` below
    for the positive control showing `.mp4` IS accepted once the setting is
    on.

    No `http_mock`: the raise happens before any network call. No file
    exists afterward, confirming `:468` fired rather than `:533`.
    """
    s = _seed()
    with pytest.raises(Exception, match='filetype not allowed'):
        edit_post(_api_input(), s.post, POST_TYPE_VIDEO, SRC_API, user=s.user,
                  uploaded_file=make_upload(filename='clip.mp4'))

    written = list(chdir_upload.rglob('app/static/media/posts/*/*/*'))
    assert written == []


def test_a_non_video_post_ignores_can_upload_video_even_when_enabled(db_session, chdir_upload):
    """`:464`'s FIRST conjunct taken FALSE with the SECOND TRUE.

    `type` is `POST_TYPE_ARTICLE`, so Python's `and` short-circuits and never
    calls `can_upload_video()` -- yet the setting is forced to `'yes'`
    anyway. This is the only test in this group that would catch a mutation
    weakening `:464`'s `and` to `or`: neither
    `test_a_video_upload_is_refused_when_video_uploads_are_off` (setting
    already `'no'`) nor
    `test_a_video_upload_is_accepted_when_video_uploads_are_enabled` (`type`
    already `POST_TYPE_VIDEO`) can distinguish `and` from `or`, because both
    already take the same branch either way. Here, an `or` would wrongly
    extend `:463`'s list with `.mp4` despite the non-video `type`; the real
    `and` does not, so `.mp4` stays disallowed and `:468` raises.

    Uses the `set_setting`/`get_setting` mechanism established in
    sub-project 37 (`.superpowers/sdd/2026-09-12-coverage-post-d-37/task-4-report.md`)
    and already in use in this suite (`tests/test_utils_upload_video.py`),
    restoring the original value in `finally` -- defensive and conventional,
    not required for isolation, since `db_session`'s teardown already clears
    the `Settings` table and `CACHE_TYPE = 'NullCache'` makes `get_setting`'s
    memoize inert (both recorded in that report).

    No `http_mock`: the raise happens before any network call. No file
    exists afterward, confirming `:468` fired rather than `:533`.
    """
    s = _seed()
    original = get_setting('allow_video_file_uploads')
    try:
        set_setting('allow_video_file_uploads', 'yes')
        with pytest.raises(Exception, match='filetype not allowed'):
            edit_post(_api_input(), s.post, POST_TYPE_ARTICLE, SRC_API, user=s.user,
                      uploaded_file=make_upload(filename='clip.mp4'))
    finally:
        set_setting('allow_video_file_uploads', original)

    written = list(chdir_upload.rglob('app/static/media/posts/*/*/*'))
    assert written == []


def test_a_video_upload_is_accepted_when_video_uploads_are_enabled(db_session, chdir_upload, http_mock):
    """`:464`'s TRUE arm, arc `464->465` -- both conjuncts TRUE.

    `type` is `POST_TYPE_VIDEO` and the setting is forced to `'yes'`, so
    `.mp4` joins `:463`'s list and `:467`'s check passes; the call runs to
    completion instead of raising. Positive control for
    `test_a_video_upload_is_refused_when_video_uploads_are_off`; together
    the two separate `:464`'s two conjuncts, which coverage.py otherwise
    scores as a single arc pair.

    `.mp4` makes `:513`'s `is_video_url(final_place)` True, so the Pillow
    re-encode at `:514-533` is skipped entirely -- this call never reaches
    `:533`'s raise, so the file-presence check below is unambiguous: only
    `:487`'s original save could have produced it. Needs `http_mock` for the
    same reason as `test_an_allowed_extension_passes`: this call reaches
    `:601`'s HEAD request in `edit_post`'s shared tail.
    """
    http_mock.head(url__regex=r'.*').respond(200, headers={'Content-Type': 'image/png'})
    s = _seed()
    original = get_setting('allow_video_file_uploads')
    try:
        set_setting('allow_video_file_uploads', 'yes')
        edit_post(_api_input(), s.post, POST_TYPE_VIDEO, SRC_API, user=s.user,
                  uploaded_file=make_upload(filename='clip.mp4'))
    finally:
        set_setting('allow_video_file_uploads', original)

    written = list(chdir_upload.rglob('app/static/media/posts/*/*/*'))
    assert len(written) == 1
    db.session.refresh(s.post)
    assert s.post.image_id is not None


def test_an_upload_lands_in_the_per_post_media_directory(db_session, chdir_upload, http_mock):
    """`:472`'s FALSE arm, arc `472->475`.

    The default: no S3 configured (TestConfig's `S3_*` settings are all
    empty strings), so `store_files_in_s3()` is False and `:475` builds a
    directory from the first four characters of `gibberish(15)`. Asserting
    on the PATH SHAPE (`*/*/*` under `app/static/media/posts`) rather than
    the exact name, which is random by construction.

    Asserting the OTHER arm's directory ('app/static/tmp') was never even
    created is what makes this a witness of the FORK rather than a
    coincidence that a file merely landed somewhere -- see
    `test_an_upload_lands_in_the_s3_tmp_directory` below for the paired
    positive control showing the opposite once S3 IS configured.
    """
    http_mock.head(url__regex=r'.*').respond(200, headers={'Content-Type': 'image/png'})
    s = _seed()
    edit_post(_api_input(), s.post, POST_TYPE_IMAGE, SRC_API, user=s.user,
              uploaded_file=make_upload())

    written = list(chdir_upload.rglob('app/static/media/posts/*/*/*'))
    assert len(written) == 1
    assert not list(chdir_upload.rglob('app/static/tmp'))


def test_an_upload_lands_in_the_s3_tmp_directory(db_session, chdir_upload, http_mock, app, monkeypatch):
    """`:472`'s TRUE arm, arc `472->473` -- the positive control for
    `test_an_upload_lands_in_the_per_post_media_directory` above.

    `store_files_in_s3()` (`app/utils.py:4317`) is True only once all three
    of `S3_ACCESS_KEY`, `S3_ACCESS_SECRET` and `S3_ENDPOINT` are non-empty;
    setting them here is what flips `:472`. Per this round's controller
    ruling, that ALSO makes `:543`'s block run, which would otherwise build
    a real `boto3.session.Session()` (`:544`) and call `session.client(...)`
    (`:550`) -- a genuine network attempt. `post_module.boto3` is patched
    with the no-op `_StubBoto3Session`/`_StubS3Client` pair above so no
    network call is attempted. Task 6 owns the assertions about what
    `boto3` is called with (the S3 arc itself); this test asserts only on
    the DIRECTORY, never on the stub's calls, so the two tests fail for one
    reason each rather than both failing together for either.

    The witness is the DIRECTORY, not the file: `:563`'s `os.unlink` removes
    the uploaded file from 'app/static/tmp' after the (stubbed) S3 "upload"
    completes, so by the time this test can look, the file itself is gone --
    but `ensure_directory_exists` (`:476`) already created 'app/static/tmp'
    before that happened, and nothing removes the directory itself.
    Asserting the OTHER arm's directory tree ('app/static/media/posts') was
    never created at all is the other half of the witness: a mutant that
    always took `:475` instead would still create SOME directory, just the
    wrong one.

    `S3_PUBLIC_URL` is also set (to a syntactically valid host) because
    `:560-561` build the post's url from it once the S3 branch runs, and an
    empty value would leave an empty host in that url -- `:601`'s
    `is_image_url` still issues a HEAD request against whatever url comes
    out, so this needs `http_mock` exactly as every other full `edit_post`
    call in this file does (see the module docstring).
    """
    monkeypatch.setitem(app.config, 'S3_ACCESS_KEY', 'test-key')
    monkeypatch.setitem(app.config, 'S3_ACCESS_SECRET', 'test-secret')
    monkeypatch.setitem(app.config, 'S3_ENDPOINT', 'https://s3.example.test')
    monkeypatch.setitem(app.config, 'S3_PUBLIC_URL', 'cdn.example.test')
    monkeypatch.setattr(post_module, 'boto3',
                        SimpleNamespace(session=SimpleNamespace(Session=_StubBoto3Session())))
    http_mock.head(url__regex=r'.*').respond(200, headers={'Content-Type': 'image/png'})
    s = _seed()

    edit_post(_api_input(), s.post, POST_TYPE_IMAGE, SRC_API, user=s.user,
              uploaded_file=make_upload())

    assert (chdir_upload / 'app' / 'static' / 'tmp').is_dir()
    assert not list(chdir_upload.rglob('app/static/media/posts'))


def test_c2pa_flags_the_post_as_ai_generated_when_a_manifest_says_so(
        db_session, chdir_upload, http_mock, monkeypatch):
    """`:481`'s TRUE arm, arc `481->482`.

    Probe C (module docstring, fact 3) found that a plain
    `Image.new(...)`-generated PNG comes back from the REAL
    `inspect_image_c2pa` with `ai_generated: False` -- there is no manifest
    this harness can construct from first principles that flips it to True.
    This monkeypatches `post_module.inspect_image_c2pa` to return a dict
    with `ai_generated: True`, STANDING IN FOR A MANIFEST THIS HARNESS
    CANNOT BUILD, per the brief.

    `ai_generated=False` in the API input means `:391` sets
    `post.ai_generated = False` before the upload block runs at all, so a
    final value of True can only have come from `:482`'s override -- see
    `test_c2pa_leaves_the_post_unflagged_without_an_ai_generated_manifest`
    below for the paired control proving the override does NOT fire when
    c2pa reports no AI generation, which is what makes True here meaningful
    rather than some other unconditional default.
    """
    def fake_inspect_image_c2pa(data, mimetype):
        return {'c2pa': {'present': True, 'ai_generated': True,
                         'creator': None, 'software': None}}

    monkeypatch.setattr(post_module, 'inspect_image_c2pa', fake_inspect_image_c2pa)
    http_mock.head(url__regex=r'.*').respond(200, headers={'Content-Type': 'image/png'})
    s = _seed()

    edit_post(_api_input(ai_generated=False), s.post, POST_TYPE_IMAGE, SRC_API,
              user=s.user, uploaded_file=make_upload())

    db.session.refresh(s.post)
    assert s.post.ai_generated is True


def test_c2pa_leaves_the_post_unflagged_without_an_ai_generated_manifest(
        db_session, chdir_upload, http_mock):
    """`:481`'s FALSE arm, arc `481->485` -- the positive control for
    `test_c2pa_flags_the_post_as_ai_generated_when_a_manifest_says_so` above.

    No monkeypatch here: this drives the REAL `inspect_image_c2pa` against a
    genuine, freshly-generated PNG, which Probe C (module docstring)
    established comes back with `ai_generated: False`.

    `:391` already sets `post.ai_generated = False` from the API input
    before the upload block runs at all, so this assertion ALONE would not
    distinguish ":481 ran and took the FALSE arm" from ":481 never ran" --
    what makes it a real witness is what it RULES OUT: a mutant that always
    takes `:481`'s TRUE arm (the `if` inverted, or dropped so `:482` always
    runs) would force `post.ai_generated` to True regardless of what c2pa
    actually reports, and this assertion catches exactly that mutant. It
    does not, on its own, catch a mutant that deletes the c2pa check
    entirely -- that is a known, accepted limitation given Probe C's
    finding that no image this harness can build reaches the TRUE arm any
    other way.
    """
    http_mock.head(url__regex=r'.*').respond(200, headers={'Content-Type': 'image/png'})
    s = _seed()
    edit_post(_api_input(ai_generated=False), s.post, POST_TYPE_IMAGE, SRC_API,
              user=s.user, uploaded_file=make_upload())

    db.session.refresh(s.post)
    assert s.post.ai_generated is False


def test_heic_extension_registers_the_heif_opener(db_session, chdir_upload, http_mock, monkeypatch):
    """`:491`'s TRUE arm (arc `491->492`), and, on the SAME call, the FALSE
    arms of the two dispatch checks after it: `493->495` (the `.avif`
    check) and `495->503` (the `.svg` check) -- `.heic` matches neither, so
    both fall through by construction.

    RENAMED RASTER BYTES: `make_upload(filename='pic.heic', fmt='PNG')`
    produces genuine PNG bytes wearing a `.heic` filename (see the module
    docstring and `make_upload`'s own docstring) -- this pins the dispatch
    on the FILENAME, not a real HEIC decode.

    `register_heif_opener` is `app.shared.post`'s own imported name
    (`app/shared/post.py:13`), so it is patched there directly as
    `post_module.register_heif_opener` (never `app.utils`, which does not
    hold this name) -- a spy WRAPPING the real function, so `:531`'s later
    re-encode (which needs the HEIF opener registered to save to a
    `.heic`-extensioned path with no explicit `format=` kwarg) still
    succeeds. VERIFIED by hand against this container: opening genuine PNG
    bytes and calling `.save('x.heic')` raises `ValueError('unknown file
    extension: .heic')` when the opener has never been registered in this
    process, and succeeds once it has. That makes `heif_spy_calls` a real,
    load-bearing fact about THIS call, not a coincidence of some earlier
    test in this file having already registered it process-wide
    (registration is idempotent and is never un-registered, which is
    exactly why "the upload succeeded" alone would NOT have been a safe
    witness -- the spy sidesteps that entirely).

    `493->495`: any cached `pillow_avif` import is popped from
    `sys.modules` before the call (and restored after) and asserted ABSENT
    afterward -- this Pillow (12.3.0) already has NATIVE AVIF support
    (verified: `'.avif' in Image.registered_extensions()` is True with a
    bare `from PIL import Image`, before `pillow_avif` is ever imported),
    so a successful save proves nothing about whether `:494` ran; only
    `sys.modules` membership does. See
    `test_avif_extension_imports_pillow_avif` below for the paired TRUE-arm
    witness.

    `495->503`: `post_module.sanitize_svg` is spied and asserted uncalled --
    a `.heic` file must never reach the SVG sanitizer. Positive control:
    `test_svg_extension_is_sanitized_successfully` below spies the SAME
    name with the SAME call-recording technique and asserts a non-empty
    result for a `.svg` file -- an empty list here could otherwise mean "the
    skip is correct", "the spy never installed", or "the patch targeted the
    wrong name" indistinguishably; the paired test rules out the latter two.
    """
    heif_spy_calls = []
    real_register_heif_opener = post_module.register_heif_opener

    def heif_spy():
        heif_spy_calls.append(True)
        return real_register_heif_opener()

    svg_spy_calls = []
    monkeypatch.setattr(post_module, 'register_heif_opener', heif_spy)
    monkeypatch.setattr(post_module, 'sanitize_svg',
                        lambda path: (svg_spy_calls.append(path), True)[1])

    had_pillow_avif = sys.modules.pop('pillow_avif', None)
    try:
        http_mock.head(url__regex=r'.*').respond(200, headers={'Content-Type': 'image/png'})
        s = _seed()
        edit_post(_api_input(), s.post, POST_TYPE_IMAGE, SRC_API, user=s.user,
                  uploaded_file=make_upload(filename='pic.heic', fmt='PNG'))

        assert heif_spy_calls == [True]
        assert svg_spy_calls == []
        assert 'pillow_avif' not in sys.modules
        written = list(chdir_upload.rglob('app/static/media/posts/*/*/*'))
        assert len(written) == 1
    finally:
        if had_pillow_avif is not None:
            sys.modules['pillow_avif'] = had_pillow_avif
        else:
            sys.modules.pop('pillow_avif', None)


def test_avif_extension_imports_pillow_avif(db_session, chdir_upload, http_mock, app, monkeypatch):
    """`:493`'s TRUE arm (arc `493->494`), and, on the SAME call, `:491`'s
    FALSE arm (arc `491->493`) -- `.avif` does not match the `.heic` check
    ahead of it.

    RENAMED RASTER BYTES: `make_upload(filename='pic.avif', fmt='PNG')`
    (see the module docstring and `make_upload`'s own docstring) -- genuine
    PNG bytes wearing a `.avif` filename, pinning the dispatch on the
    FILENAME.

    `pillow_avif` IS IMPORTED INSIDE THE FUNCTION (`:494` and `:511`), so
    per the brief it CANNOT be patched as `post_module.pillow_avif` -- no
    such attribute exists until the `import` statement runs, and patching
    one in beforehand would not stop that statement from executing and
    rebinding it anyway. This test MANIPULATES `sys.modules` DIRECTLY
    instead of patching, exactly as the brief allows.

    VERIFIED by hand against this container that Pillow 12.3.0 already
    registers a native '.avif' extension BEFORE `pillow_avif` is ever
    imported (`'.avif' in Image.registered_extensions()` is True with a
    bare `from PIL import Image`), so a successful AVIF save is NOT
    evidence that `:494`'s `import pillow_avif` ran -- the save would
    succeed identically whether or not that line exists. That is exactly
    the fourth false-witness mechanism (an input taking the same path under
    both arms), which is why this does not assert on the save succeeding as
    its evidence for THIS arc. The only direct evidence is `sys.modules`
    membership: `pillow_avif` is removed from the import cache (if present)
    before the call, and its PRESENCE afterward is asserted -- proof this
    specific `import` statement executed during THIS call, not merely that
    AVIF encoding works in this environment. The original cache entry, if
    any, is restored afterward; if there was none, the entry this call
    added is removed again.

    `491->493`: `post_module.register_heif_opener` is spied and asserted
    uncalled -- an `.avif` file must never reach the HEIF registration.

    THIS TEST'S SOUNDNESS RESTS ON AN ENVIRONMENTAL FACT THAT IS NOW PINNED
    RATHER THAN ASSUMED: `:511` is a SECOND `import pillow_avif`, gated only
    on `current_app.config['MEDIA_IMAGE_FORMAT'] == 'AVIF'`, not on
    `final_ext` -- so it is entirely independent of the `:493`/`:494` pair
    this test targets. If that config were `'AVIF'`, `:511` would import
    `pillow_avif` regardless of whether `:494` ran at all, and a mutant that
    deleted `:494` outright would STILL leave `'pillow_avif' in sys.modules`
    True after this call -- the exact same false-witness shape as the
    AVIF-native-support problem this test already works around, just one
    level further out, and it would fail SILENTLY (wrong config some day)
    rather than loudly (a code review catching it). `config.py:141` defaults
    `MEDIA_IMAGE_FORMAT` to `''`, and nothing in `TestConfig` overrides it,
    so `:511` never fires today -- but nothing in the test SAID so before
    this fix. `monkeypatch.setitem` below pins it explicitly for this test
    regardless of what any future default becomes, and the assertion right
    after makes that pin itself verifiable rather than a silent setup step:
    if `:511`'s gate condition were ever satisfied here, `sys.modules`
    membership would no longer be evidence of `:494` alone, and this
    assertion is what would catch that BEFORE the `sys.modules` assertion
    could be misread as a clean pass.
    """
    heif_spy_calls = []
    monkeypatch.setattr(post_module, 'register_heif_opener',
                        lambda: heif_spy_calls.append(True))
    monkeypatch.setitem(app.config, 'MEDIA_IMAGE_FORMAT', '')
    assert app.config['MEDIA_IMAGE_FORMAT'] != 'AVIF'

    had_pillow_avif = sys.modules.pop('pillow_avif', None)
    try:
        http_mock.head(url__regex=r'.*').respond(200, headers={'Content-Type': 'image/png'})
        s = _seed()
        edit_post(_api_input(), s.post, POST_TYPE_IMAGE, SRC_API, user=s.user,
                  uploaded_file=make_upload(filename='pic.avif', fmt='PNG'))

        assert heif_spy_calls == []
        assert 'pillow_avif' in sys.modules
        written = list(chdir_upload.rglob('app/static/media/posts/*/*/*'))
        assert len(written) == 1
    finally:
        if had_pillow_avif is not None:
            sys.modules['pillow_avif'] = had_pillow_avif
        else:
            sys.modules.pop('pillow_avif', None)


def test_svg_extension_is_sanitized_successfully(db_session, chdir_upload, http_mock, monkeypatch):
    """`:495`'s TRUE arm (arc `495->500`) and `:500`'s FALSE arm (arc
    `500->503`) -- `sanitize_svg` SUCCEEDS on `make_upload`'s genuine
    `SVG_BYTES`, so `:501`'s raise is not taken and the call runs to
    completion.

    Verified in `make_upload`'s own docstring: saving `SVG_BYTES` to disk
    and calling the REAL `sanitize_svg` on it returns True, rewriting the
    file with py-svg-hush's re-serialized output. `post_module.sanitize_svg`
    is spied here (wrapping the real function, so behaviour is unchanged)
    for a second reason beyond this test's own arc: it is the POSITIVE
    CONTROL for `test_heic_extension_registers_the_heif_opener`'s
    `svg_spy_calls == []` assertion above -- same monkeypatch target, same
    call-recording technique, opposite extension, non-empty result. Without
    this, that emptiness assertion had no same-mechanism control: an empty
    list there was equally consistent with "the skip is correct", "the spy
    never installed", and "the patch targeted the wrong name". This test's
    non-empty `svg_spy_calls` rules out the latter two for that spy target.
    Unlike `test_a_malformed_svg_fails_sanitization_and_is_rejected` below
    (`:500`'s TRUE-arm positive control, needing no monkeypatch at all since
    it drives the real function to a real failure), this test's monkeypatch
    is there only to observe the real call, not to change its outcome.

    `:513`'s Pillow re-encode is skipped entirely for a `.svg` path
    (`final_place.endswith('.svg')` is True there), so the file this test
    finds is the sanitizer's rewritten output, never touched by
    `Image.open` -- the only upload test in this file where that is true.
    """
    svg_spy_calls = []
    real_sanitize_svg = post_module.sanitize_svg

    def svg_spy(path):
        svg_spy_calls.append(path)
        return real_sanitize_svg(path)

    monkeypatch.setattr(post_module, 'sanitize_svg', svg_spy)

    http_mock.head(url__regex=r'.*').respond(200, headers={'Content-Type': 'image/png'})
    s = _seed()
    edit_post(_api_input(), s.post, POST_TYPE_IMAGE, SRC_API, user=s.user,
              uploaded_file=make_upload(filename='pic.svg', fmt='SVG'))

    assert svg_spy_calls != []
    written = list(chdir_upload.rglob('app/static/media/posts/*/*/*'))
    assert len(written) == 1
    db.session.refresh(s.post)
    assert s.post.image_id is not None


def test_media_image_format_avif_imports_pillow_avif_a_second_time(
        db_session, chdir_upload, http_mock, app, monkeypatch):
    """`:510`'s TRUE arm, arc `510->511`.

    `:511` is a SECOND `import pillow_avif`, independent of `:493`/`:494`'s
    filename-driven import -- it is gated only on
    `current_app.config['MEDIA_IMAGE_FORMAT'] == 'AVIF'`. This test uses a
    `.png`-named upload (`final_ext` stays `.png`), so `:493`'s check is
    False and `:494` never runs -- the only way `pillow_avif` can land in
    `sys.modules` during this call is `:511`, isolating this arc from the
    filename-driven one `test_avif_extension_imports_pillow_avif` already
    covers.

    Per the module docstring's Pillow-12.3.0 trap (also hit by
    `test_avif_extension_imports_pillow_avif` above): this container's
    Pillow already has native AVIF support, so a successful AVIF-format save
    is not evidence this specific `import` statement ran -- only
    `sys.modules` membership is. `pillow_avif` is popped from the import
    cache (if present) before the call and its presence asserted after; the
    original entry, if any, is restored in `finally`.

    `MEDIA_IMAGE_FORMAT='AVIF'` also makes `:524`'s `if image_format:` True,
    renaming `final_place` to a `.avif` suffix and passing `format='AVIF'`
    to `:531`'s save -- asserting that suffix is incidental confirmation
    the config took effect, not itself evidence for `:511`.
    """
    monkeypatch.setitem(app.config, 'MEDIA_IMAGE_FORMAT', 'AVIF')
    assert app.config['MEDIA_IMAGE_FORMAT'] == 'AVIF'

    had_pillow_avif = sys.modules.pop('pillow_avif', None)
    try:
        http_mock.head(url__regex=r'.*').respond(200, headers={'Content-Type': 'image/png'})
        s = _seed()
        edit_post(_api_input(), s.post, POST_TYPE_IMAGE, SRC_API, user=s.user,
                  uploaded_file=make_upload(filename='pic.png', fmt='PNG'))

        assert 'pillow_avif' in sys.modules
        # `:527` renames `final_place` to a new `.avif` path for the
        # re-encoded save; the ORIGINAL `.png` `:487` saved is never
        # removed, so two files exist here, not one -- verified empirically
        # against this container (a first run asserting `== 1` failed with
        # `2 == 1`, listing both the `.png` and the `.avif` path).
        written = list(chdir_upload.rglob('app/static/media/posts/*/*/*'))
        assert len(written) == 2
        assert any(p.suffix == '.avif' for p in written)
    finally:
        if had_pillow_avif is not None:
            sys.modules['pillow_avif'] = had_pillow_avif
        else:
            sys.modules.pop('pillow_avif', None)


def test_default_media_image_format_skips_the_second_avif_import(
        db_session, chdir_upload, http_mock, app, monkeypatch):
    """`:510`'s FALSE arm, arc `510->513` -- the positive control for
    `test_media_image_format_avif_imports_pillow_avif_a_second_time` above.

    Per D451: this test's correctness depends on `MEDIA_IMAGE_FORMAT`
    defaulting to `''` (`config.py:141`), so that default is asserted
    explicitly here rather than assumed -- a future config change would
    otherwise make this pass for the wrong reason with no code change to
    trigger a review.

    `.png`-named upload again keeps `:493`/`:494` out of the picture, so
    `pillow_avif` absent from `sys.modules` afterward is solely evidence
    that neither `:494` nor `:511` ran -- and since `:494` is already
    structurally excluded by the filename, this isolates `:511`'s FALSE
    arm specifically.
    """
    assert app.config['MEDIA_IMAGE_FORMAT'] == ''

    had_pillow_avif = sys.modules.pop('pillow_avif', None)
    try:
        http_mock.head(url__regex=r'.*').respond(200, headers={'Content-Type': 'image/png'})
        s = _seed()
        edit_post(_api_input(), s.post, POST_TYPE_IMAGE, SRC_API, user=s.user,
                  uploaded_file=make_upload(filename='pic.png', fmt='PNG'))

        assert 'pillow_avif' not in sys.modules
        written = list(chdir_upload.rglob('app/static/media/posts/*/*/*'))
        assert len(written) == 1
    finally:
        if had_pillow_avif is not None:
            sys.modules['pillow_avif'] = had_pillow_avif
        else:
            sys.modules.pop('pillow_avif', None)


def test_a_gif_path_skips_the_pil_reencode_block(db_session, chdir_upload, http_mock, monkeypatch):
    """`:513`'s FALSE arm via its SECOND conjunct, arc `513->535`.

    `:513` is `if not X.endswith('.svg') and not X.endswith('.gif') and not
    is_video_url(X):` -- three conditions folded into one arc pair by
    coverage.py. `test_svg_extension_is_sanitized_successfully` above
    already witnesses the FIRST conjunct's false arm (an `.svg` path) and
    `test_a_video_upload_is_accepted_when_video_uploads_are_enabled` already
    witnesses the THIRD (an `.mp4` path taking `is_video_url` True) -- both
    named here rather than duplicated. This test supplies the missing
    SECOND: a genuine `.gif` upload.

    `post_module.Image.open` -- the ONLY call site for `Image.open` in this
    module (`:514`) -- is spied so the block's entry is witnessed directly
    rather than inferred from file state, which would be ambiguous here: a
    `.gif` is itself an ALLOWED extension (`:463`), so if a mutant weakened
    the `.gif` conjunct (e.g. dropped it, or `and` softened to `or` against
    a condition that's False for this path) and let this file INTO the
    block, `img.format` would be `'GIF'` and `'.gif' in allowed_extensions`
    is True -- `:515` would pass, `:531` would re-encode, and a file would
    still exist and `post.image_id` would still be set. Only the spy
    distinguishes "block skipped" from "block ran and happened to succeed
    anyway".

    Genuine GIF bytes (`make_upload`'s `fmt='GIF'` is real content, not a
    renamed raster) so this reaches `:513` with a filename AND payload that
    would both pass `:515` if the block ran -- the only thing that should
    stop it is `:513`'s own `.gif` check.
    """
    open_calls = []
    real_open = Image.open

    def spy_open(*a, **kw):
        open_calls.append(a)
        return real_open(*a, **kw)

    monkeypatch.setattr(post_module.Image, 'open', spy_open)

    http_mock.head(url__regex=r'.*').respond(200, headers={'Content-Type': 'image/gif'})
    s = _seed()
    edit_post(_api_input(), s.post, POST_TYPE_IMAGE, SRC_API, user=s.user,
              uploaded_file=make_upload(filename='pic.gif', fmt='GIF'))

    assert open_calls == []
    written = list(chdir_upload.rglob('app/static/media/posts/*/*/*'))
    assert len(written) == 1
    db.session.refresh(s.post)
    assert s.post.image_id is not None


def test_a_file_whose_bytes_disagree_with_its_name_is_refused(db_session, chdir_upload):
    """`:513`'s TRUE arm (arc `513->514`) and `:515`'s FALSE arm feeding
    `:533`'s raise (arc `515->533`).

    `:467` checked the FILENAME's extension and passed -- `.png` is
    allowed. `:515` checks the DECODED format and refuses: a file named
    `.png` whose bytes decode to a format absent from `allowed_extensions`
    is the input that separates them.

    A CORRECTION TO THE BRIEF'S WORKED EXAMPLE, verified mechanically
    rather than assumed. The brief's Step 3 draft used
    `make_upload(filename='pic.png', fmt='GIF')`, predicting `:533` fires
    because "`.gif` is not in `allowed_extensions` unless the name said
    so". Both halves of that prediction were checked directly against this
    container:

      1. `make_upload(fmt='GIF')` DOES produce `img.format == 'GIF'` after
         a decode round-trip -- the brief's prediction on THIS point holds.
      2. But `:463` is `allowed_extensions = ['.gif', '.jpg', '.jpeg',
         '.png', '.webp', '.heic', '.mpo', '.avif', '.svg']` --  `.gif` is
         in the list UNCONDITIONALLY, on the same line as `.png` and
         `.jpg`. Only `.mp4`/`.webm`/`.mov` are conditional on
         `POST_TYPE_VIDEO` (`:464-465`). So `'.' + 'gif' in
         allowed_extensions` is True, `:515` takes its TRUE arm, and
         `:531`'s re-encode runs -- the brief's example does NOT reach
         `:533` at all; it completes normally instead.

    This test therefore uses `fmt='BMP'` instead. VERIFIED directly:
    encoding an 8x8 image as BMP and reopening it gives `img.format ==
    'BMP'`, and `.bmp` has no entry anywhere in `allowed_extensions` (no
    post type adds it), so `:515`'s condition is False and `:533` raises.

    Per the module docstring's ruling on the two byte-identical 'filetype
    not allowed' messages (`:468` vs `:533`), this test also confirms a
    file DOES exist under `chdir_upload` -- unlike every raising test
    earlier in this file (which assert NO file exists, because `:468`
    fires before `:487`'s save), `:533` fires AFTER `:487` saves the raw
    BMP-content bytes under a `.png`-suffixed path, so this is the one
    test in this file that must find something there. If this asserted
    the opposite (no file), it would pass equally were `:468` to fire
    instead of `:533` -- which is exactly the ambiguity the docstring
    warns against.
    """
    s = _seed()
    with pytest.raises(Exception, match='filetype not allowed'):
        edit_post(_api_input(), s.post, POST_TYPE_IMAGE, SRC_API, user=s.user,
                  uploaded_file=make_upload(filename='pic.png', fmt='BMP'))

    written = list(chdir_upload.rglob('app/static/media/posts/*/*/*'))
    assert len(written) == 1


def test_a_jpeg_upload_takes_the_to_srgb_conversion_path(db_session, chdir_upload, http_mock, monkeypatch):
    """`:515`'s TRUE arm (arc `515->516`) and `:517`'s TRUE arm (arc
    `517->518`) -- a genuine JPEG upload takes `to_srgb`, not `convert`.

    `post_module.to_srgb` is spied, WRAPPING the real function so the save
    still succeeds, and asserted called exactly once. `final_ext` is
    `.jpg`, which is in `:517`'s `['.jpg', '.jpeg']` list regardless of
    `MEDIA_IMAGE_FORMAT` (default `''` here, so the first disjunct is
    False) -- this isolates the SECOND disjunct as what makes `:517` True
    for this call.

    Positive control for
    `test_a_non_jpeg_upload_takes_the_rgba_conversion_path` below, which
    spies the SAME name and asserts it uncalled for a `.png` upload -- an
    empty list there would otherwise be equally consistent with "the RGBA
    arm ran", "the spy never installed", or "the patch targeted the wrong
    name"; this test's non-empty call list rules out the latter two for
    this spy target.
    """
    calls = []
    real_to_srgb = post_module.to_srgb

    def spy_to_srgb(img, *a, **kw):
        calls.append(True)
        return real_to_srgb(img, *a, **kw)

    monkeypatch.setattr(post_module, 'to_srgb', spy_to_srgb)

    http_mock.head(url__regex=r'.*').respond(200, headers={'Content-Type': 'image/jpeg'})
    s = _seed()
    edit_post(_api_input(), s.post, POST_TYPE_IMAGE, SRC_API, user=s.user,
              uploaded_file=make_upload(filename='pic.jpg', fmt='JPEG'))

    assert calls == [True]
    written = list(chdir_upload.rglob('app/static/media/posts/*/*/*'))
    assert len(written) == 1
    db.session.refresh(s.post)
    assert s.post.image_id is not None


def test_a_non_jpeg_upload_takes_the_rgba_conversion_path(db_session, chdir_upload, http_mock, app, monkeypatch):
    """`:517`'s FALSE arm, arc `517->520` -- the positive control for
    `test_a_jpeg_upload_takes_the_to_srgb_conversion_path` above.

    `MEDIA_IMAGE_FORMAT` stays at its default `''` (asserted, per D451) and
    the upload is `.png`, so neither of `:517`'s disjuncts is True and
    `:520`'s `img.convert('RGBA')` runs instead of `to_srgb`.
    `post_module.to_srgb` is spied and asserted UNCALLED -- without the
    paired positive control above, an empty call list here would equally be
    consistent with "the RGBA arm correctly ran", "the spy never
    installed", or "the patch targeted the wrong name" (D451 mechanism 3);
    the other test's non-empty result for the same spy rules out the
    latter two.

    Asserting only on `to_srgb` non-invocation rather than on the save
    succeeding matters here (D451 mechanism 4): a PNG reaches `:531`'s save
    successfully whether `:517` took the `if` or the `else` -- both arms
    converge on the same `img.save(...)` call one line later, so "the file
    exists and `post.image_id` is set" would pass identically under either
    arm and would not, on its own, discriminate them.
    """
    assert app.config['MEDIA_IMAGE_FORMAT'] == ''

    calls = []
    monkeypatch.setattr(post_module, 'to_srgb', lambda *a, **kw: calls.append(True))

    http_mock.head(url__regex=r'.*').respond(200, headers={'Content-Type': 'image/png'})
    s = _seed()
    edit_post(_api_input(), s.post, POST_TYPE_IMAGE, SRC_API, user=s.user,
              uploaded_file=make_upload(filename='pic.png', fmt='PNG'))

    assert calls == []
    written = list(chdir_upload.rglob('app/static/media/posts/*/*/*'))
    assert len(written) == 1
    db.session.refresh(s.post)
    assert s.post.image_id is not None


def test_a_malformed_svg_fails_sanitization_and_is_rejected(db_session, chdir_upload):
    """`:500`'s TRUE arm, arc `500->501` -- `sanitize_svg` returns False and
    `:501` raises. Positive control for
    `test_svg_extension_is_sanitized_successfully` above, using
    `MALFORMED_SVG_BYTES` (see its own docstring for the verified
    `ValueError` this triggers inside `sanitize_svg`).

    `sanitize_svg` destroys the file before returning False (`:496-499`
    explains why -- `discard_unsanitized_svg` truncates then unlinks it),
    so this asserts on the RAISE, not the file: by the time this test could
    look, there is nothing left to inspect. The `rglob` assertion below
    confirms no file survives anywhere under `chdir_upload` -- consistent
    with `discard_unsanitized_svg` having unlinked the exact file `:487`
    saved, with nothing after `:501`'s raise getting a chance to write
    another one.

    No `http_mock`: the raise happens before `:535` builds a url, so this
    call never reaches `:601`'s HEAD request.
    """
    upload = FileStorage(stream=BytesIO(MALFORMED_SVG_BYTES), filename='evil.svg',
                         content_type='image/svg+xml')
    s = _seed()
    with pytest.raises(Exception, match='SVG file could not be sanitized'):
        edit_post(_api_input(), s.post, POST_TYPE_IMAGE, SRC_API, user=s.user,
                  uploaded_file=upload)

    assert not list(chdir_upload.rglob('app/static/media/posts/*/*/*'))

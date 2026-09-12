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
from app.models import File
from app.shared.post import edit_post
from app.utils import get_setting, set_setting
from tests.test_shared_post_edit import _api_input, _seed


class _RecordingS3Client:
    """Stands in for the boto3 S3 client `edit_post` builds at `:550`,
    recording every `upload_file` call's positional and keyword arguments.

    Task 3's `_StubS3Client` above deliberately records nothing -- that
    class belongs to the `:472` directory-fork arc, not the S3 upload
    itself. This class is Task 6's own: `:546`/`:548` build `extra_args`
    whose only observable effect is the `ExtraArgs` kwarg passed to
    `upload_file` at `:557-559`, so those two arcs need the call recorded,
    not silently discarded. `close` COUNTS its calls (Task 7's addition)
    rather than being a bare no-op -- a no-op made `:562`'s call
    undetectable by construction, not by any test's oversight, since
    nothing could ever tell "called" from "never called" either way.

    `download_file` is a DELIBERATE no-op, added for a mutation this class
    does not otherwise witness: `:557`'s `s3.upload_file(...)` swapped for
    `s3.download_file(...)` (Task 7 fix round 1, Major 2). Without this
    method the swap crashes with `AttributeError` before any test
    assertion runs, and Rule 1 of the mutation pass ("a crash kill is not
    a kill") requires checking for a non-crashing variant of the same
    fault rather than accepting the crash as evidence. With this no-op in
    place, that exact mutation instead leaves `upload_file_calls` empty
    and every test that calls `edit_post` with S3 configured fails at its
    own `assert len(calls) == 1` -- a plain `AssertionError`, not a crash.
    """

    def __init__(self):
        self.upload_file_calls = []
        self.close_calls = 0

    def upload_file(self, *args, **kwargs):
        self.upload_file_calls.append((args, kwargs))

    def download_file(self, *args, **kwargs):
        pass

    def close(self):
        self.close_calls += 1


class _RecordingBoto3Session:
    """Stands in for `boto3.session.Session`.

    Callable so `boto3.session.Session()` at `:544` returns this instance,
    and `.client(...)` at `:550` returns ONE shared `_RecordingS3Client` --
    shared, rather than a fresh instance per call, so a test can hold a
    reference to it before `edit_post` runs and inspect its recorded calls
    after.
    """

    def __init__(self):
        self.client_instance = _RecordingS3Client()

    def __call__(self):
        return self

    def client(self, **kwargs):
        return self.client_instance


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

    THE `url__regex=r'.*'` ROUTE ABOVE matches any string, so it cannot
    itself prove `:535`'s `.replace('app/', '')` actually ran -- a mutant
    that stripped a different, never-matching prefix (Task 7's mutation
    pass tried `'App/'`) would still match the same wildcard route and
    leave every prior assertion here green. The `File.source_url` check
    below reads the actual stored url and pins that the on-disk `app/`
    prefix is gone from it, closing that gap.
    """
    http_mock.head(url__regex=r'.*').respond(200, headers={'Content-Type': 'image/png'})
    s = seed_upload_context()
    edit_post(_api_input(), s.post, POST_TYPE_IMAGE, SRC_API, user=s.user,
              uploaded_file=make_upload())

    written = list(chdir_upload.rglob('app/static/media/posts/*/*/*'))
    assert len(written) == 1
    db.session.refresh(s.post)
    assert s.post.image_id is not None
    file = File.query.get(s.post.image_id)
    assert '/app/' not in file.source_url
    assert '/static/media/posts/' in file.source_url


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

    THE TWO PATH-SHAPE ASSERTIONS ABOVE cannot distinguish `:475`'s actual
    `new_filename[0:2]` / `new_filename[2:4]` split from a mutant widening
    either slice (e.g. `[0:3]`) -- both still glob-match `*/*/*` under
    `posts/`. Task 7's mutation pass found this: `[0:2]` -> `[0:3]` survived
    every existing assertion in this file. The two length/prefix checks
    below pin the exact split against the leaf filename itself (which is
    `new_filename` unmodified, since no format conversion runs for a
    default PNG upload), closing that gap.
    """
    http_mock.head(url__regex=r'.*').respond(200, headers={'Content-Type': 'image/png'})
    s = _seed()
    edit_post(_api_input(), s.post, POST_TYPE_IMAGE, SRC_API, user=s.user,
              uploaded_file=make_upload())

    written = list(chdir_upload.rglob('app/static/media/posts/*/*/*'))
    assert len(written) == 1
    assert not list(chdir_upload.rglob('app/static/tmp'))

    leaf = written[0]
    level2, level1 = leaf.parent.name, leaf.parent.parent.name
    assert len(level1) == 2 and len(level2) == 2
    assert leaf.stem[0:2] == level1
    assert leaf.stem[2:4] == level2


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

    Positive control: `test_a_jpeg_upload_takes_the_to_srgb_conversion_path`
    below spies the SAME `post_module.Image.open` target and asserts a
    NON-EMPTY call list for a file that DOES enter the block -- without it,
    this test's empty list would be equally consistent with "the guard
    worked", "the spy never installed", or "the patch targeted the wrong
    name" (D451 mechanism 3); that test's non-empty result for the same
    spy rules out the latter two.
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

    Positive control, named explicitly rather than left to inference:
    `test_a_jpeg_upload_takes_the_to_srgb_conversion_path` below drives a
    genuine, allowed-format upload through the SAME `:515` check and
    reaches its TRUE arm instead -- completing normally rather than
    raising. Without a same-format-decode input that goes the other way,
    a `pytest.raises` firing for the wrong reason (e.g. a mutant that made
    `:515` always False) would look identical to this test passing for the
    right one.
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

    ALSO the positive control for `test_a_gif_path_skips_the_pil_reencode_block`
    above: that test spies `post_module.Image.open` and asserts an EMPTY
    call list to prove `:513`'s guard skipped the block for a `.gif` path.
    An empty list there is, on its own, equally consistent with "the guard
    correctly skipped the block", "the spy never installed", or "the patch
    targeted the wrong name" (D451 mechanism 3). This test installs the
    SAME spy on the SAME target (`post_module.Image.open`, the sole call
    site at `:514`) and asserts a NON-EMPTY call list for a file that DOES
    enter the block, ruling out the latter two for that spy mechanism --
    the shape Task 3 used pairing its `sanitize_svg` spy across
    `test_heic_extension_registers_the_heif_opener` (empty) and
    `test_svg_extension_is_sanitized_successfully` (non-empty).
    """
    calls = []
    real_to_srgb = post_module.to_srgb

    def spy_to_srgb(img, *a, **kw):
        calls.append(True)
        return real_to_srgb(img, *a, **kw)

    monkeypatch.setattr(post_module, 'to_srgb', spy_to_srgb)

    open_calls = []
    real_open = Image.open

    def spy_open(*a, **kw):
        open_calls.append(a)
        return real_open(*a, **kw)

    monkeypatch.setattr(post_module.Image, 'open', spy_open)

    http_mock.head(url__regex=r'.*').respond(200, headers={'Content-Type': 'image/jpeg'})
    s = _seed()
    edit_post(_api_input(), s.post, POST_TYPE_IMAGE, SRC_API, user=s.user,
              uploaded_file=make_upload(filename='pic.jpg', fmt='JPEG'))

    assert calls == [True]
    assert len(open_calls) == 1
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

    NEITHER of the assertions above pins WHICH conversion `:520` performs --
    `to_srgb` being uncalled is consistent with `:520` running
    `img.convert('RGBA')` as written, but equally consistent with a mutant
    that instead ran `img.convert('RGB')` (Task 7's mutation pass tried
    this and it survived unnoticed). The saved file's mode, opened below,
    is the only place that distinguishes the two.
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
    with Image.open(written[0]) as saved:
        assert saved.mode == 'RGBA'


def test_the_configured_max_dimension_actually_shrinks_the_thumbnail(
        db_session, chdir_upload, http_mock, app, monkeypatch):
    """`:506`'s read of `MEDIA_IMAGE_MAX_DIMENSION` into `image_max_dimension`,
    fed to `:521`'s `img.thumbnail(...)`.

    Every OTHER test in this file uploads an 8x8 image against the
    default `MEDIA_IMAGE_MAX_DIMENSION` of `2000` (`config.py:139`) --
    already larger than the source in both dimensions, so
    `Image.thumbnail()` is a byte-for-byte no-op (VERIFIED: resizing an
    8x8 image toward a 2000x2000 target with `Image.thumbnail` leaves its
    bytes identical to the untouched original, independent of resample
    method). Task 7's mutation pass found that `:506` hardcoded to `1`
    survived every existing test for exactly this reason -- nothing forces
    a real resize to happen, so nothing could tell whether this line's
    value was even read.

    Configuring a `MEDIA_IMAGE_MAX_DIMENSION` smaller than the 8x8 source
    forces a genuine resize, so the saved file's pixel dimensions directly
    witness the configured value rather than merely being consistent with
    it.
    """
    assert app.config['MEDIA_IMAGE_FORMAT'] == ''
    monkeypatch.setitem(app.config, 'MEDIA_IMAGE_MAX_DIMENSION', 4)

    http_mock.head(url__regex=r'.*').respond(200, headers={'Content-Type': 'image/png'})
    s = _seed()
    edit_post(_api_input(), s.post, POST_TYPE_IMAGE, SRC_API, user=s.user,
              uploaded_file=make_upload(filename='pic.png', fmt='PNG', size=(8, 8)))

    written = list(chdir_upload.rglob('app/static/media/posts/*/*/*'))
    assert len(written) == 1
    with Image.open(written[0]) as saved:
        assert saved.size == (4, 4)


def test_exif_orientation_is_corrected_before_conversion(
        db_session, chdir_upload, http_mock, app):
    """`:516`'s `img = ImageOps.exif_transpose(img)`.

    No image `make_upload` can build carries EXIF orientation metadata --
    every one of them is a synthetic `Image.new(...)` with no EXIF block
    at all, so `:516` has no observable effect on any of them whether it
    runs or not. Task 7's mutation pass found that skipping this line
    outright (replacing it with `pass`) survived every existing test for
    exactly that reason.

    `make_upload`'s own docstring says content it cannot produce "would
    need a different helper" -- this test is that helper, built directly
    (bypassing `make_upload`, same as `MALFORMED_SVG_BYTES` above) rather
    than adding a binary asset to the repository. A real, non-square (8x4)
    JPEG is built in memory with Pillow and given an EXIF `Orientation`
    tag of `6` (VERIFIED: `ImageOps.exif_transpose` on an (8, 4) image
    carrying that tag returns a (4, 8) image -- width and height swapped).
    Whether `:516` ran is therefore visible directly in the saved file's
    dimensions: swapped if it did, unchanged if it did not.

    The final `saved.size == (4, 8)` assertion below is only valid because
    `:521`'s `img.thumbnail((image_max_dimension, image_max_dimension),
    ...)` is a no-op on an image this small -- which depends on
    `MEDIA_IMAGE_MAX_DIMENSION`'s DEFAULT (`config.py:139`) being >= 8.
    Per D451/the standing rule that a test depending on a config default
    must assert it, that default is asserted explicitly below, the same
    way `test_the_configured_max_dimension_actually_shrinks_the_thumbnail`
    above asserts `MEDIA_IMAGE_FORMAT`'s default.
    """
    assert app.config['MEDIA_IMAGE_MAX_DIMENSION'] == 2000

    buf = BytesIO()
    img = Image.new('RGB', (8, 4), (10, 20, 30))
    exif = img.getexif()
    exif[0x0112] = 6  # Orientation: needs a 270-degree rotation to display upright
    img.save(buf, format='JPEG', exif=exif)
    buf.seek(0)
    uploaded_file = FileStorage(stream=buf, filename='pic.jpg', content_type='image/jpeg')

    http_mock.head(url__regex=r'.*').respond(200, headers={'Content-Type': 'image/jpeg'})
    s = _seed()
    edit_post(_api_input(), s.post, POST_TYPE_IMAGE, SRC_API, user=s.user,
              uploaded_file=uploaded_file)

    written = list(chdir_upload.rglob('app/static/media/posts/*/*/*'))
    assert len(written) == 1
    with Image.open(written[0]) as saved:
        assert saved.size == (4, 8)


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


def test_a_configured_format_rewrites_the_saved_extension(
        db_session, chdir_upload, http_mock, app, monkeypatch):
    """`:524`'s TRUE arm, `:525`'s `kwargs['format']`, and `:526-527`'s
    rewrite of `final_ext`/`final_place`.

    `test_media_image_format_avif_imports_pillow_avif_a_second_time` above
    already takes this same arc incidentally -- its `MEDIA_IMAGE_FORMAT =
    'AVIF'` makes `:524` True too, and its own docstring says so explicitly
    ("also makes `:524`'s `if image_format:` True ... incidental
    confirmation the config took effect, not itself evidence for `:511`").
    But that test's PURPOSE is `:510`'s second `import pillow_avif`
    (`510->511`); this test is about the format kwarg on its own terms, so
    it uses `MEDIA_IMAGE_FORMAT = 'WEBP'` instead of `'AVIF'` -- Pillow's
    WEBP encoder is a native, always-available codec, so this test carries
    none of the module docstring's Pillow-12.3.0 native-AVIF-support
    complication (a successful WEBP save unambiguously required `:525`'s
    kwarg to reach `:531`'s `img.save`; there is no competing "it would have
    saved anyway" explanation the way there is for AVIF).

    THE OBSERVABLE DIFFERENCE IS THE SAVED FILE'S EXTENSION, NOT THE KWARGS
    DICT -- `kwargs` is local to `edit_post` and invisible from outside;
    `:527` replaces `final_place`'s extension, so a `.png` upload with WEBP
    configured lands on disk as `.webp`. That is what is asserted here.

    TWO FILES ARE EXPECTED, NOT ONE -- a REGISTERED DEFECT, not a test bug.
    `:487` saves the original upload to `<name>.png`; `:527` then rewrites
    `final_place` to `<name>.webp` and `:531` saves the re-encoded image
    there. Nothing between `:485` and `:563` unlinks the pre-rename `.png`
    path -- `:563`'s `os.unlink` runs only in the S3 branch, and only on the
    NEW path -- so both files survive on disk. Task 4's
    `test_media_image_format_avif_imports_pillow_avif_a_second_time` found
    this the same way (a first run asserting `== 1` failed with `2 == 1`)
    and pinned it there as a known defect rather than correct behaviour;
    this test pins the same defect for the WEBP arm rather than silently
    re-deriving the wrong (`== 1`) expectation.

    Positive control for the `== 2` count:
    `test_an_uploaded_image_is_saved_and_linked` above takes the DEFAULT
    (`MEDIA_IMAGE_FORMAT == ''`) path with an otherwise-identical `.png`
    upload and asserts `len(written) == 1` -- so the second file here is
    attributable to the configured format taking `:524`'s TRUE arm, not to
    some format-independent mechanism that always leaves two files behind.
    """
    monkeypatch.setitem(app.config, 'MEDIA_IMAGE_FORMAT', 'WEBP')
    assert app.config['MEDIA_IMAGE_FORMAT'] == 'WEBP'

    http_mock.head(url__regex=r'.*').respond(200, headers={'Content-Type': 'image/png'})
    s = _seed()
    edit_post(_api_input(), s.post, POST_TYPE_IMAGE, SRC_API, user=s.user,
              uploaded_file=make_upload(filename='pic.png', fmt='PNG'))

    written = list(chdir_upload.rglob('app/static/media/posts/*/*/*'))
    assert len(written) == 2
    assert any(p.suffix == '.webp' for p in written)
    assert any(p.suffix == '.png' for p in written)
    db.session.refresh(s.post)
    assert s.post.image_id is not None


def test_default_media_image_quality_passes_the_quality_kwarg(
        db_session, chdir_upload, http_mock, app, monkeypatch):
    """`:528`'s TRUE arm, arc `528->529` -- positive control for
    `test_falsy_media_image_quality_omits_the_quality_kwarg` below.

    Per D451: this test's correctness depends on `MEDIA_IMAGE_QUALITY`
    defaulting to `90` (`config.py:142`), so that default is asserted
    explicitly rather than assumed. It ALSO depends on `MEDIA_IMAGE_FORMAT`
    defaulting to `''` (`config.py:141`) for the `len(written) == 1`
    assertion below -- a non-empty default there would take `:524`'s TRUE
    arm too, rewrite `final_place` at `:527`, and leave the original `.png`
    behind unremoved (the same registered defect
    `test_a_configured_format_rewrites_the_saved_extension` pins
    deliberately), turning this into a 2-file case and failing the `== 1`
    assertion for a reason this docstring would not have explained. That
    default is asserted explicitly too, below -- this assertion is
    load-bearing, not decorative.

    `PIL.Image.Image.save` (the class method, not a particular instance) is
    spied so calls are seen regardless of which `Image` object ends up
    calling `.save` -- `img` is reassigned by `ImageOps.exif_transpose` and
    by `to_srgb`/`convert` before `:531`, so an instance-level spy installed
    on the object `Image.open` returns would miss the actual call. The spy
    WRAPS the real method (still saves for real) and records every call's
    kwargs. `make_upload`'s own internal `Image.new(...).save(buf,
    format=fmt)` call is also caught by this class-level patch, so calls
    are filtered down to the one carrying `optimize=True`.

    That filter is a COUNT-BASED discriminator, not a uniqueness claim --
    `optimize=True` is not exclusive to `:531` in this codebase.
    `app/activitypub/util.py:1872` and `:1919`, inside
    `make_image_sizes_async` (a near-verbatim copy of this block, reached
    from `edit_post` via `make_image_sizes` at `:614`/`:616`), pass the same
    literal kwarg to their own `img.save` calls. Neither fires in this test:
    `make_image_sizes_async` downloads `file.source_url` via `get_request`
    before it ever reaches its own kwargs-building block, and this suite's
    convention of registering no GET route (see the module docstring) makes
    that call fail first. So `reencode_calls` here is filtered by
    `optimize=True` alone, but it is `assert len(reencode_calls) == 1`
    immediately below -- not the filter's exclusivity -- that is what makes
    the subsequent `reencode_calls[0]` safe to index; a change that made one
    of those other call sites reachable under this harness would show up as
    that count assertion failing, not as a silent misattribution.

    Asserting the recorded `quality` kwarg equals the configured `90`
    (not just that the key is present) is the positive control for
    `test_falsy_media_image_quality_omits_the_quality_kwarg`'s absence
    assertion below: without a paired test showing the key present and
    correct when quality is truthy, an empty/absent result there would be
    equally consistent with "the falsy guard worked", "the spy never
    installed", or "the patch targeted the wrong name" (D451 mechanism 3).
    This test's non-empty, correct-valued result for the same spy target
    rules out the latter two.

    The `'optimize' in kw` FILTER above discriminates calls by KEY presence
    only, so it cannot tell `optimize=True` from `optimize=False` -- a
    mutant flipping that literal (Task 7's mutation pass tried it) still
    has exactly one call with an `'optimize'` key and a `quality` of `90`,
    surviving both assertions above. The value check below closes that gap.
    """
    assert app.config['MEDIA_IMAGE_QUALITY'] == 90
    assert app.config['MEDIA_IMAGE_FORMAT'] == ''

    save_calls = []
    real_save = Image.Image.save

    def spy_save(self, *a, **kw):
        save_calls.append(kw)
        return real_save(self, *a, **kw)

    monkeypatch.setattr(Image.Image, 'save', spy_save)

    http_mock.head(url__regex=r'.*').respond(200, headers={'Content-Type': 'image/png'})
    s = _seed()
    edit_post(_api_input(), s.post, POST_TYPE_IMAGE, SRC_API, user=s.user,
              uploaded_file=make_upload(filename='pic.png', fmt='PNG'))

    reencode_calls = [kw for kw in save_calls if 'optimize' in kw]
    assert len(reencode_calls) == 1
    assert reencode_calls[0].get('quality') == 90
    assert reencode_calls[0].get('optimize') is True

    written = list(chdir_upload.rglob('app/static/media/posts/*/*/*'))
    assert len(written) == 1
    db.session.refresh(s.post)
    assert s.post.image_id is not None


def test_falsy_media_image_quality_omits_the_quality_kwarg(
        db_session, chdir_upload, http_mock, app, monkeypatch):
    """`:528`'s FALSE arm, arc `528->531`.

    `MEDIA_IMAGE_QUALITY` is forced to `0` -- falsy, and, unlike `''`, safe
    even if something downstream ever re-ran `int(image_quality)` on it
    (`config.py:142` only applies that coercion once, at `Config` class
    definition time; `current_app.config['MEDIA_IMAGE_QUALITY']` at
    `:522` reads the already-int value straight back out of the config
    dict). `0` was verified to actually reach `:528` as falsy and to leave
    `kwargs` without a `'quality'` key -- see the assertions below.

    Per D451, `MEDIA_IMAGE_FORMAT` defaulting to `''` (`config.py:141`) is
    also load-bearing here, not merely assumed: this test asserts
    `len(written) == 1` below, and a non-empty default would take `:524`'s
    TRUE arm too, rewriting `final_place` at `:527` and leaving the original
    `.png` behind unremoved (the registered defect
    `test_a_configured_format_rewrites_the_saved_extension` pins
    deliberately) -- turning this into a 2-file case for a reason unrelated
    to the `:528` arm this test targets. Asserted explicitly below.

    Same spy mechanism as `test_default_media_image_quality_passes_the_quality_kwarg`
    above (`PIL.Image.Image.save` spied at the class level, calls filtered
    to the one carrying `optimize=True`) -- that test is this one's positive
    control: it shows the SAME spy, on the SAME filtered call, records a
    `quality` key with the configured value when `:528` is True. Without
    it, this test's absent-key assertion would be equally consistent with
    "the falsy guard worked", "the spy never installed", or "the patch
    targeted the wrong name" (D451 mechanism 3).

    The `optimize=True` filter discriminates `:531`'s call from
    `make_upload`'s own internal `Image.new(...).save(...)` call, but it is
    NOT a claim that `:531` is the only call site in the codebase passing
    that literal kwarg -- see
    `test_default_media_image_quality_passes_the_quality_kwarg`'s docstring
    for the two other call sites (`app/activitypub/util.py:1872`, `:1919`)
    and why they cannot fire under this harness's conventions. The
    `assert len(reencode_calls) == 1` below is what makes indexing
    `reencode_calls[0]` safe, not the filter's exclusivity.
    """
    monkeypatch.setitem(app.config, 'MEDIA_IMAGE_QUALITY', 0)
    assert app.config['MEDIA_IMAGE_QUALITY'] == 0
    assert app.config['MEDIA_IMAGE_FORMAT'] == ''

    save_calls = []
    real_save = Image.Image.save

    def spy_save(self, *a, **kw):
        save_calls.append(kw)
        return real_save(self, *a, **kw)

    monkeypatch.setattr(Image.Image, 'save', spy_save)

    http_mock.head(url__regex=r'.*').respond(200, headers={'Content-Type': 'image/png'})
    s = _seed()
    edit_post(_api_input(), s.post, POST_TYPE_IMAGE, SRC_API, user=s.user,
              uploaded_file=make_upload(filename='pic.png', fmt='PNG'))

    reencode_calls = [kw for kw in save_calls if 'optimize' in kw]
    assert len(reencode_calls) == 1
    assert 'quality' not in reencode_calls[0]

    written = list(chdir_upload.rglob('app/static/media/posts/*/*/*'))
    assert len(written) == 1
    db.session.refresh(s.post)
    assert s.post.image_id is not None


# ---------------------------------------------------------------------------
# Task 6: image hashing (`:537`-`:540`) and the S3 upload (`:543`-`:563`).
# ---------------------------------------------------------------------------


def test_image_hashing_endpoint_disabled_by_default_skips_hash_retrieval(
        db_session, chdir_upload, http_mock, app, monkeypatch):
    """`:537`'s FALSE arm via its FIRST conjunct, arc `537->543`.

    `:537` is `if current_app.config['IMAGE_HASHING_ENDPOINT'] and not
    is_video_url(final_place):` -- two conditions folded into one arc pair.
    Per the round's ruling on this compound, each conjunct needs its OWN
    false-arm witness: this test supplies the first (an empty endpoint);
    `test_a_video_upload_skips_image_hashing_even_when_the_endpoint_is_configured`
    below supplies the second (a video path with the endpoint SET).

    Per D451, `IMAGE_HASHING_ENDPOINT` defaulting to `''` (`config.py:127`)
    is asserted explicitly rather than assumed. `post_module.retrieve_image_hash`
    is spied and asserted UNCALLED -- `:538` would call it with the built
    url if `:537`'s guard failed to skip. Positive control:
    `test_image_hashing_retrieves_a_hash_for_a_configured_endpoint_and_non_video_upload`
    below shows the SAME spy called once when the endpoint IS configured;
    without it, an empty call list here would be equally consistent with
    "the guard correctly skipped the block", "the spy never installed", or
    "the patch targeted the wrong name" (D451 mechanism 3).
    """
    assert app.config['IMAGE_HASHING_ENDPOINT'] == ''

    hash_calls = []
    monkeypatch.setattr(post_module, 'retrieve_image_hash',
                        lambda url: hash_calls.append(url))

    http_mock.head(url__regex=r'.*').respond(200, headers={'Content-Type': 'image/png'})
    s = _seed()
    edit_post(_api_input(), s.post, POST_TYPE_IMAGE, SRC_API, user=s.user,
              uploaded_file=make_upload())

    assert hash_calls == []
    written = list(chdir_upload.rglob('app/static/media/posts/*/*/*'))
    assert len(written) == 1
    db.session.refresh(s.post)
    assert s.post.image_id is not None


def test_a_video_upload_skips_image_hashing_even_when_the_endpoint_is_configured(
        db_session, chdir_upload, http_mock, app, monkeypatch):
    """`:537`'s FALSE arm via its SECOND conjunct, arc `537->543` -- the
    other half of the compound's false-arm pair, alongside
    `test_image_hashing_endpoint_disabled_by_default_skips_hash_retrieval`
    above.

    `IMAGE_HASHING_ENDPOINT` is set to a non-empty value here -- the
    OPPOSITE of the other test's setup -- so this test isolates the SECOND
    conjunct (`not is_video_url(final_place)`) as what makes `:537` False:
    a `.mp4` upload makes `is_video_url(final_place)` True, so `not
    is_video_url(...)` is False regardless of the endpoint. Without a
    dedicated test for this conjunct, a mutation weakening `:537`'s `and`
    to `or` could still pass every other test in this file (which either
    leave the endpoint empty, or upload a non-video file), since neither
    condition alone would then matter for those inputs.

    Video uploads are enabled via the same `set_setting`/`finally`
    mechanism as `test_a_video_upload_is_accepted_when_video_uploads_are_enabled`
    above, restoring the original value afterward.

    `post_module.retrieve_image_hash` is spied and asserted UNCALLED.
    Positive control:
    `test_image_hashing_retrieves_a_hash_for_a_configured_endpoint_and_non_video_upload`
    below shows the SAME spy called once for a non-video upload with the
    SAME endpoint configured -- ruling out "the spy never installed" or
    "the patch targeted the wrong name" as explanations for the empty list
    here (D451 mechanism 3).
    """
    monkeypatch.setitem(app.config, 'IMAGE_HASHING_ENDPOINT', 'https://hash.example.test')

    hash_calls = []
    monkeypatch.setattr(post_module, 'retrieve_image_hash',
                        lambda url: hash_calls.append(url))

    http_mock.head(url__regex=r'.*').respond(200, headers={'Content-Type': 'image/png'})
    s = _seed()
    original = get_setting('allow_video_file_uploads')
    try:
        set_setting('allow_video_file_uploads', 'yes')
        edit_post(_api_input(), s.post, POST_TYPE_VIDEO, SRC_API, user=s.user,
                  uploaded_file=make_upload(filename='clip.mp4'))
    finally:
        set_setting('allow_video_file_uploads', original)

    assert hash_calls == []
    written = list(chdir_upload.rglob('app/static/media/posts/*/*/*'))
    assert len(written) == 1


def test_image_hashing_retrieves_a_hash_for_a_configured_endpoint_and_non_video_upload(
        db_session, chdir_upload, http_mock, app, monkeypatch):
    """`:537`'s TRUE arm, arc `537->538` -- positive control for both
    false-arm witnesses above. ALSO `:539`'s FALSE arm via its FIRST
    conjunct, arc `539->543`: `retrieve_image_hash` returns a falsy value
    here, so `hash and hash_matches_blocked_image(hash)` short-circuits
    without calling the second function.

    `post_module.retrieve_image_hash` is spied (returning `None`, standing
    in for "no hash available", rather than issuing a real HTTP request)
    and asserted called exactly once -- proof `:538` ran, which is what
    makes the empty call lists in the two tests above meaningful rather
    than a broken spy. `post_module.hash_matches_blocked_image` is ALSO
    spied here and asserted UNCALLED: if a mutation weakened `:539`'s `and`
    to `or`, Python would still need to evaluate the second operand to
    decide the truth of `hash or hash_matches_blocked_image(hash)` when
    `hash` is falsy -- so `hash_matches_blocked_image` WOULD be called
    under that mutant, and this assertion catches it.
    `test_a_blocked_image_hash_raises_and_rejects_the_post` below is the
    positive control for `hash_matches_blocked_image` being spied at all
    (it asserts the same spy IS called once, with a truthy hash).

    The call completes normally: a falsy hash never reaches `:540`'s raise.

    `assert len(hash_calls) == 1` proves `:538` ran but not what it passed --
    a mutant calling `retrieve_image_hash('')` instead of `retrieve_image_hash(url)`
    (Task 7's mutation pass tried this) still appends exactly one item.
    The value check below reads the recorded argument and pins that it is
    the real, non-empty built url rather than an empty placeholder.
    """
    monkeypatch.setitem(app.config, 'IMAGE_HASHING_ENDPOINT', 'https://hash.example.test')

    hash_calls = []
    monkeypatch.setattr(post_module, 'retrieve_image_hash',
                        lambda url: hash_calls.append(url) or None)

    blocked_calls = []
    monkeypatch.setattr(post_module, 'hash_matches_blocked_image',
                        lambda hash: blocked_calls.append(hash) or False)

    http_mock.head(url__regex=r'.*').respond(200, headers={'Content-Type': 'image/png'})
    s = _seed()
    edit_post(_api_input(), s.post, POST_TYPE_IMAGE, SRC_API, user=s.user,
              uploaded_file=make_upload())

    assert len(hash_calls) == 1
    assert hash_calls[0] != ''
    assert '/static/media/posts/' in hash_calls[0]
    assert blocked_calls == []
    written = list(chdir_upload.rglob('app/static/media/posts/*/*/*'))
    assert len(written) == 1
    db.session.refresh(s.post)
    assert s.post.image_id is not None


def test_a_blocked_image_hash_raises_and_rejects_the_post(
        db_session, chdir_upload, app, monkeypatch):
    """`:539`'s TRUE arm, arc `539->540` -- both conjuncts truthy, so `:540`
    raises `Exception('This image is blocked')`.

    `post_module.retrieve_image_hash` is spied to return a fixed, non-empty
    binary-looking string (standing in for a genuine PDQ hash this harness
    cannot produce without a real hashing endpoint) and
    `post_module.hash_matches_blocked_image` is spied to return `True`
    (standing in for a real Hamming-distance match against
    `blocked_image`, which would need a seeded row and a real hash to
    compute against). `hash_matches_blocked_image` is asserted called
    exactly once, with the hash `retrieve_image_hash` returned -- the
    positive control for
    `test_image_hashing_retrieves_a_hash_for_a_configured_endpoint_and_non_video_upload`
    above, which spies the SAME name and asserts it UNCALLED; without this
    test, that assertion would have no same-mechanism control (D451
    mechanism 3).

    This exception's message ('This image is blocked') is distinct from
    the two byte-identical 'filetype not allowed' messages the module
    docstring discusses, so there is no message-ambiguity here -- but the
    file-presence check is still asserted for consistency with this file's
    convention of asserting full state: `:540` fires AFTER `:487`'s save
    (and, for this PNG upload, after `:531`'s re-encode too), so a file
    DOES exist under `chdir_upload` when this raises.

    No `http_mock` fixture: `:535` builds the url, but `:540`'s raise
    propagates out of `edit_post` before that url ever reaches `:601`'s
    `is_image_url` HEAD check in the function's shared tail, so no HTTP
    request occurs on this path and there is no route for `http_mock` to
    register.
    """
    monkeypatch.setitem(app.config, 'IMAGE_HASHING_ENDPOINT', 'https://hash.example.test')
    monkeypatch.setattr(post_module, 'retrieve_image_hash', lambda url: '1' * 256)

    blocked_calls = []

    def fake_hash_matches_blocked_image(hash):
        blocked_calls.append(hash)
        return True

    monkeypatch.setattr(post_module, 'hash_matches_blocked_image', fake_hash_matches_blocked_image)

    s = _seed()
    with pytest.raises(Exception, match='This image is blocked'):
        edit_post(_api_input(), s.post, POST_TYPE_IMAGE, SRC_API, user=s.user,
                  uploaded_file=make_upload())

    assert blocked_calls == ['1' * 256]
    written = list(chdir_upload.rglob('app/static/media/posts/*/*/*'))
    assert len(written) == 1


def test_the_local_file_survives_when_s3_is_not_configured(db_session, chdir_upload, http_mock, app):
    """`:543`'s FALSE arm, arc `543->565` -- the positive control for
    `test_an_s3_upload_removes_the_local_file` below.

    Per D451, all three of `S3_ACCESS_KEY` (`config.py:106`),
    `S3_ACCESS_SECRET` (`:107`) and `S3_ENDPOINT` (`:104`) defaulting to
    `''` is asserted explicitly -- `store_files_in_s3()`
    (`app/utils.py:4317-4319`) is False only because all three are empty.
    No `post_module.boto3` patch is needed or installed: with `:543` False,
    `:544`'s `boto3.session.Session()` and `:550`'s `.client(...)` never
    run, so nothing would call out to the real module even unpatched.

    The witness is the file's CONTINUED PRESENCE under
    `app/static/media/posts` -- the same location and glob
    `test_an_uploaded_image_is_saved_and_linked` already asserts for the
    unrelated `:461` end-to-end case, but named here specifically as the
    paired control for `test_an_s3_upload_removes_the_local_file`'s
    absence assertion: without this test, that file being gone would be
    equally consistent with ":563 correctly ran" and "the file was never
    written in the first place" or "chdir_upload silently failed" -- this
    test rules those out by showing the same kind of file DOES survive
    when `:563` never runs.
    """
    assert app.config['S3_ACCESS_KEY'] == ''
    assert app.config['S3_ACCESS_SECRET'] == ''
    assert app.config['S3_ENDPOINT'] == ''

    http_mock.head(url__regex=r'.*').respond(200, headers={'Content-Type': 'image/png'})
    s = _seed()
    edit_post(_api_input(), s.post, POST_TYPE_IMAGE, SRC_API, user=s.user,
              uploaded_file=make_upload())

    written = list(chdir_upload.rglob('app/static/media/posts/*/*/*'))
    assert len(written) == 1


def test_an_s3_upload_removes_the_local_file(db_session, chdir_upload, http_mock, app, monkeypatch):
    """`:543`'s true arm through `:563`'s unlink.

    The local file being GONE is the observable difference between the two
    arms -- stronger than asserting on the mock's call list, which would
    pass even if `:563` were deleted. `boto3` is patched because `:544` and
    `:550` would otherwise open a real session and attempt a network call.

    All three of `S3_ACCESS_KEY`, `S3_ACCESS_SECRET` and `S3_ENDPOINT` are
    set non-empty, flipping `store_files_in_s3()` True; `S3_PUBLIC_URL` is
    also set (a syntactically valid host) because `:560-561` build the
    post's url from it once the S3 branch runs, and `:601`'s
    `is_image_url` still issues a HEAD request against whatever url comes
    out -- see the module docstring on why every full `edit_post` call in
    this file needs `http_mock`.

    `post_module.boto3` is patched with this file's own
    `_RecordingBoto3Session`/`_RecordingS3Client` pair, NOT Task 3's silent
    `_StubBoto3Session`/`_StubS3Client` -- this test needs the exact local
    path `edit_post` passed to `upload_file` at `:557` in order to check
    it afterward, which the silent stub never records.

    The FIRST positional argument to `upload_file` (`:557`) is
    `final_place`, the local path `:563` unlinks -- asserted absent both
    directly (`chdir_upload / local_path`) and via a directory-wide rglob
    under `app/static/tmp`, matching this file's preference for a
    full-state assertion over a single-file one: an rglob returning empty
    also rules out some OTHER file having been left behind by a mutant
    that unlinked the wrong path. Positive control:
    `test_the_local_file_survives_when_s3_is_not_configured` above shows
    the same kind of file surviving when `:543` is False and `:563` never
    runs.

    TWO FURTHER ARCS have no other witness in this file and are pinned
    here rather than in new tests, since this is already the S3-true-arm
    call with the recording double in place:

    `:562`'s `s3.close()` has no OTHER observable effect in this harness --
    `_RecordingS3Client.close` was, before this task, a bare no-op, so
    deleting the call outright was undetectable by construction, not by
    oversight (Task 7's mutation pass). `close_calls` below is the
    counter this task added to that fixture to make the call itself
    observable.

    `:560-561` build the public url as `https://{S3_PUBLIC_URL}/posts/...`;
    a mutant using `http://` (Task 7's mutation pass) is invisible to the
    `url__regex=r'.*'` HEAD route above (it matches either scheme) and to
    every assertion elsewhere in this test, which never reads the built
    url at all. The stored `File.source_url` is the only place it is
    checked.
    """
    session = _RecordingBoto3Session()
    monkeypatch.setattr(post_module, 'boto3',
                        SimpleNamespace(session=SimpleNamespace(Session=session)))
    monkeypatch.setitem(app.config, 'S3_ACCESS_KEY', 'test-key')
    monkeypatch.setitem(app.config, 'S3_ACCESS_SECRET', 'test-secret')
    monkeypatch.setitem(app.config, 'S3_ENDPOINT', 'https://s3.example.test')
    monkeypatch.setitem(app.config, 'S3_PUBLIC_URL', 'cdn.example.test')

    http_mock.head(url__regex=r'.*').respond(200, headers={'Content-Type': 'image/png'})
    s = _seed()
    edit_post(_api_input(), s.post, POST_TYPE_IMAGE, SRC_API, user=s.user,
              uploaded_file=make_upload())

    calls = session.client_instance.upload_file_calls
    assert len(calls) == 1
    local_path = calls[0][0][0]
    assert not (chdir_upload / local_path).exists()
    assert list(chdir_upload.rglob('app/static/tmp/*')) == []
    assert session.client_instance.close_calls == 1

    db.session.refresh(s.post)
    file = File.query.get(s.post.image_id)
    assert file.source_url.startswith('https://cdn.example.test/posts/')


def test_extra_args_include_storage_class_and_public_acl_when_configured(
        db_session, chdir_upload, http_mock, app, monkeypatch):
    """`:546`'s TRUE arm (arc `546->547`) and `:548`'s TRUE arm (arc
    `548->549`) -- the latter arc lands on `:549`'s
    `extra_args['ACL'] = 'public-read'`, whose literal VALUE (not just its
    presence) this test's `extra_args['ACL'] == 'public-read'` assertion
    below pins; a mutant changing that literal (Task 7's mutation pass
    tried `'private'`) is caught here.

    These pin ARGUMENTS, not behaviour: `:546` and `:548` are two
    independent config checks (not a compound condition -- each is its own
    `if`, not joined by `and`/`or`) whose only observable effect is a key
    added to the `extra_args` dict passed as `upload_file`'s `ExtraArgs`
    kwarg at `:559`. Nothing else in `edit_post` reads `extra_args`, so the
    dict recorded by `_RecordingS3Client` is the only place these arcs are
    observable at all.

    `S3_STORAGE_CLASS` is set to a non-empty string and `S3_PUBLIC_ACL` to
    `True` -- both config keys checked independently at `:546`/`:548`, so
    setting both together exercises both TRUE arms on the SAME call rather
    than needing two separate S3 uploads. Positive control (both the FALSE
    arms and the config defaults):
    `test_extra_args_omit_storage_class_and_public_acl_by_default` below
    leaves both at their defaults and asserts the corresponding keys
    ABSENT -- without it, the absence there would have no same-mechanism
    control (D451 mechanism 3).

    `post_module.boto3` is patched with `_RecordingBoto3Session` for the
    same network-avoidance reason as `test_an_s3_upload_removes_the_local_file`
    above.

    NEITHER this test nor
    `test_extra_args_omit_storage_class_and_public_acl_by_default` can
    catch a mutant that SWAPS which `if` gates which assignment, because
    both move `S3_STORAGE_CLASS` and `S3_PUBLIC_ACL` together (both
    truthy here, both falsy there) --
    `test_extra_args_distinguish_storage_class_from_public_acl_when_only_one_is_set`
    below closes that gap with the two flags set to DIFFERENT values.
    """
    session = _RecordingBoto3Session()
    monkeypatch.setattr(post_module, 'boto3',
                        SimpleNamespace(session=SimpleNamespace(Session=session)))
    monkeypatch.setitem(app.config, 'S3_ACCESS_KEY', 'test-key')
    monkeypatch.setitem(app.config, 'S3_ACCESS_SECRET', 'test-secret')
    monkeypatch.setitem(app.config, 'S3_ENDPOINT', 'https://s3.example.test')
    monkeypatch.setitem(app.config, 'S3_PUBLIC_URL', 'cdn.example.test')
    monkeypatch.setitem(app.config, 'S3_STORAGE_CLASS', 'GLACIER')
    monkeypatch.setitem(app.config, 'S3_PUBLIC_ACL', True)

    http_mock.head(url__regex=r'.*').respond(200, headers={'Content-Type': 'image/png'})
    s = _seed()
    edit_post(_api_input(), s.post, POST_TYPE_IMAGE, SRC_API, user=s.user,
              uploaded_file=make_upload())

    calls = session.client_instance.upload_file_calls
    assert len(calls) == 1
    extra_args = calls[0][1]['ExtraArgs']
    assert extra_args['StorageClass'] == 'GLACIER'
    assert extra_args['ACL'] == 'public-read'


def test_extra_args_omit_storage_class_and_public_acl_by_default(
        db_session, chdir_upload, http_mock, app, monkeypatch):
    """`:546`'s FALSE arm (arc `546->548`) and `:548`'s FALSE arm (arc
    `548->550`) -- the positive control for
    `test_extra_args_include_storage_class_and_public_acl_when_configured`
    above.

    These pin ARGUMENTS, not behaviour: the only observable effect of
    `:546`/`:548` taking their FALSE arms is that `StorageClass` and `ACL`
    are ABSENT from `extra_args` -- everything else about the call
    (`ContentType`, the bucket, the key) is set unconditionally by `:545`
    and `:557-559` regardless of these two checks, so this test's
    assertions are narrowly about the two conditional keys, not the call
    as a whole.

    Per D451, `S3_STORAGE_CLASS` defaulting to `''` (`config.py:110`) and
    `S3_PUBLIC_ACL` defaulting to `False` (`:109`) are asserted explicitly
    -- both are falsy, but by different Python values, which is worth
    pinning: a future change coercing `S3_PUBLIC_ACL`'s default to `''`
    or `None` would not change `:548`'s behaviour, but a silent change to
    a TRUTHY default (e.g. `'false'`, a non-empty string) would flip this
    arc without any code change here to trigger a review.

    Without the paired test above, `'StorageClass' not in extra_args` and
    `'ACL' not in extra_args` would be equally consistent with "the FALSE
    arm correctly omitted the key", "the spy never installed", or "the
    patch targeted the wrong name" (D451 mechanism 3); that test's
    non-empty result for the SAME recording mechanism rules out the latter
    two.

    NEITHER this test nor the paired test above can catch a mutant that
    SWAPS which `if` gates which assignment, since both move
    `S3_STORAGE_CLASS` and `S3_PUBLIC_ACL` together (both falsy here, both
    truthy there) --
    `test_extra_args_distinguish_storage_class_from_public_acl_when_only_one_is_set`
    below closes that gap.

    THE UNCONDITIONAL `ContentType` KEY, claimed above as the reason this
    test's assertions stay narrow, was never itself checked anywhere in
    this file -- a mutant typo'ing `:545`'s key (Task 7's mutation pass
    tried `'ContentTypeX'`) survived every existing assertion. This is the
    one test in the file guaranteed to build `extra_args` regardless of
    S3_STORAGE_CLASS/S3_PUBLIC_ACL, so the value check below verifies the
    docstring's own claim instead of just asserting it.
    """
    assert app.config['S3_STORAGE_CLASS'] == ''
    assert app.config['S3_PUBLIC_ACL'] is False

    session = _RecordingBoto3Session()
    monkeypatch.setattr(post_module, 'boto3',
                        SimpleNamespace(session=SimpleNamespace(Session=session)))
    monkeypatch.setitem(app.config, 'S3_ACCESS_KEY', 'test-key')
    monkeypatch.setitem(app.config, 'S3_ACCESS_SECRET', 'test-secret')
    monkeypatch.setitem(app.config, 'S3_ENDPOINT', 'https://s3.example.test')
    monkeypatch.setitem(app.config, 'S3_PUBLIC_URL', 'cdn.example.test')

    http_mock.head(url__regex=r'.*').respond(200, headers={'Content-Type': 'image/png'})
    s = _seed()
    edit_post(_api_input(), s.post, POST_TYPE_IMAGE, SRC_API, user=s.user,
              uploaded_file=make_upload())

    calls = session.client_instance.upload_file_calls
    assert len(calls) == 1
    extra_args = calls[0][1]['ExtraArgs']
    assert 'StorageClass' not in extra_args
    assert 'ACL' not in extra_args
    assert extra_args['ContentType'] == 'image/png'


def test_extra_args_distinguish_storage_class_from_public_acl_when_only_one_is_set(
        db_session, chdir_upload, http_mock, app, monkeypatch):
    """`:546` and `:548` set to DIFFERENT truth values -- closes a
    mutation-survival gap the two lockstep tests above cannot close
    (task-6-review.md Finding 1), regardless of how key-specific their
    assertions are.

    THE GAP: a mutant that SWAPS which `if` gates which assignment --
    `:546` testing `S3_PUBLIC_ACL` while still executing `:547`'s
    `extra_args['StorageClass'] = ...`, and `:548` testing
    `S3_STORAGE_CLASS` while still executing `:549`'s
    `extra_args['ACL'] = 'public-read'` -- survives BOTH
    `test_extra_args_include_storage_class_and_public_acl_when_configured`
    and `test_extra_args_omit_storage_class_and_public_acl_by_default`,
    because both of those fixtures move `S3_STORAGE_CLASS` and
    `S3_PUBLIC_ACL` in LOCKSTEP (both truthy, or both falsy). Under a
    swap, two independent `if`s gated on values that agree with each
    other still gate the exact same set of assignments, so `extra_args`
    comes out byte-identical to the unmutated code either way -- no
    assertion on `extra_args`'s contents, however key-specific, can tell
    the two apart when the inputs never disagree.

    THIS IS A DISTINCT FALSE-WITNESS SHAPE, not previously recorded in
    this campaign: two independent conditions exercised only IN LOCKSTEP
    cannot detect a swap between them, however precise the assertions.
    It is adjacent to the registered mechanism about an input taking the
    same path under both arms, but not the same mechanism -- there, a
    SINGLE input is ambiguous between two arms of ONE condition; here,
    TWO SEPARATE tests each use a perfectly unambiguous input, and the
    gap only exists because the two tests' inputs never differ FROM EACH
    OTHER on the one axis (which flag is on) that would expose a swap
    between the two conditions.

    THE FIX: `S3_STORAGE_CLASS` is set non-empty while `S3_PUBLIC_ACL` is
    left at its D451 default (`False`, `config.py:109`, asserted
    explicitly below) -- the two flags now disagree. Under the REAL code,
    `:546` is True (sets `StorageClass`) and `:548` is False (`ACL`
    absent). Under the SWAP mutant described above, `:546` would instead
    test the falsy `S3_PUBLIC_ACL` (so `StorageClass` would be ABSENT) and
    `:548` would instead test the truthy `S3_STORAGE_CLASS` (so `ACL`
    WOULD be present) -- the exact opposite of both assertions below, so
    the swap is caught.
    """
    assert app.config['S3_PUBLIC_ACL'] is False

    session = _RecordingBoto3Session()
    monkeypatch.setattr(post_module, 'boto3',
                        SimpleNamespace(session=SimpleNamespace(Session=session)))
    monkeypatch.setitem(app.config, 'S3_ACCESS_KEY', 'test-key')
    monkeypatch.setitem(app.config, 'S3_ACCESS_SECRET', 'test-secret')
    monkeypatch.setitem(app.config, 'S3_ENDPOINT', 'https://s3.example.test')
    monkeypatch.setitem(app.config, 'S3_PUBLIC_URL', 'cdn.example.test')
    monkeypatch.setitem(app.config, 'S3_STORAGE_CLASS', 'GLACIER')

    http_mock.head(url__regex=r'.*').respond(200, headers={'Content-Type': 'image/png'})
    s = _seed()
    edit_post(_api_input(), s.post, POST_TYPE_IMAGE, SRC_API, user=s.user,
              uploaded_file=make_upload())

    calls = session.client_instance.upload_file_calls
    assert len(calls) == 1
    extra_args = calls[0][1]['ExtraArgs']
    assert extra_args['StorageClass'] == 'GLACIER'
    assert 'ACL' not in extra_args

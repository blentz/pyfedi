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

from io import BytesIO

import pytest
from PIL import Image
from werkzeug.datastructures import FileStorage

from app import db
from app.constants import POST_TYPE_IMAGE, SRC_API
from app.shared.post import edit_post
from tests.test_shared_post_edit import _api_input, _seed


SVG_BYTES = (b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8">'
            b'<rect width="8" height="8" fill="#0a141e"/></svg>')
"""Real, minimal SVG/XML. SVG is text, not a raster format, so it needs no
image library at all -- see `make_upload`'s `fmt='SVG'` case.
"""


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

# Coverage sub-project 38 Implementation Plan: `edit_post`'s upload block

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Take `edit_post`'s file-upload block (`app/shared/post.py:461-563`) to zero missing statements and zero missing branch arcs — 66 statements and 39 arcs.

**Architecture:** A new `tests/test_shared_post_upload.py`. The existing `tests/test_shared_post_edit.py` is 66 tests and covers `edit_post`'s other regions; this block is a distinct sub-domain with its own harness (real image bytes, a redirected working directory, PIL, boto3) and belongs in its own file. Helpers come from `tests.test_shared_post_edit` as sub-project 37 did.

**Tech Stack:** pytest, Flask, SQLAlchemy 2.0.52, Pillow, werkzeug `FileStorage`, boto3, the `c2pa` library, coverage.py with branch measurement. Everything runs through `./run_tests.sh`.

**Spec:** `docs/superpowers/specs/2026-09-12-coverage-post-e1-38-design.md`

## Global Constraints

- **Delete nothing this plan did not create.** `git checkout -- app/` is permitted ONLY as a mutation-restore step.
- **There is NO host Python with flask or pytest.** Everything runs through `./run_tests.sh` = `podman compose exec -T test-runner pytest "$@"` (`run_tests.sh:85`).
- **To run python inside the container**: `podman-compose -f compose.test.yaml exec -T test-runner python ...` (`tests/README.md:403-405`). **`run_tests.sh` has no `--exec` flag** and rejects one with pytest exit 4.
- **Only the controller runs the full suite.**
- **`pytest` exits 1 on a session timeout and `run_tests.sh` propagates it.** A shell PIPELINE eats the status — read `${PIPESTATUS[0]}`, or do not pipe.
- **Coverage takes the dotted form** `--cov=app.shared.post`. A path form collects nothing, writes no JSON, exits 0.
- **Write coverage JSON outside the repository.** `/app` is bind-mounted.
- **Read `summary.percent_covered`**, not `percent_statements_covered`.
- Before believing any failure, run `./run_tests.sh --down` and retry once.
- **Test counts come from pytest's own collection output**, never from a number in this plan.
- **No test may request the `redis_double` fixture.**
- **SRC_API tests must NOT be wrapped in `web_ctx`; SRC_WEB tests that read `current_user` must be.**
- **Every monkeypatch must restore in a `finally`, or be a pytest `monkeypatch` fixture**, and must patch `post_module.<name>`. `boto3`, `Image`, `os`, `can_upload_video`, `store_files_in_s3`, `is_video_url`, `retrieve_image_hash`, `hash_matches_blocked_image`, `sanitize_svg`, `inspect_image_c2pa` are all module-level in `app/shared/post.py`. **Never patch `app.utils` directly.**
- **No duplicate test names.** Check with `grep -oE "^def (test_[a-z0-9_]+)" tests/test_shared_post_upload.py | sort | uniq -d` before every commit. **The character class must include `0-9`.** Earlier dispatches in this campaign used `[a-z_]+`, which stops at the first digit: `test_c2pa_flags_...` and `test_c2pa_leaves_...` both truncate to `test_c` and report as a false duplicate. The flaw is false-positive only — a genuine duplicate still truncates identically and is still caught — but a check that cries wolf is a check people learn to ignore.
- **Every line number re-derived** with `awk 'NR>=X && NR<=Y {printf "%d\t%s\n",NR,$0}' FILE`. Cite a statement's own line, not the `if` guarding it and not a neighbour.
- **VERIFY EVERY CITATION MECHANICALLY AND PASTE THE PROOF.** For every `file:line` you add or change, run `awk` over it and paste the numbered output into your report beside the claim. In sub-project 37, three consecutive tasks each shipped exactly one Major and all three were citations; the four tasks after this rule was added shipped none, and the rule's own first use found a fourth error three reviewers had missed.
- **Mutations:** one at a time, **line-scoped `sed`**, dry-run and read the line first, apply, run, restore, then assert empty `git diff -- app/` and `wc -l app/shared/post.py` = 1193. Restore before any point where you might stop.
- Commit with `git commit -F <file>`, never `-m`. Lowercase `type:` subject prefix, normal English prose.
- **Commit trailer, last two lines, in this order.** `Claude Opus 5` is a LITERAL CONSTANT, not a field describing the agent:
  ```
  Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01BF7kVN2spWD86NYKWyGrVn
  ```
- **Register numbering:** findings start at **D464**, `tests/README.md` facts at **229**.

### The four false-witness mechanisms (D451)

Before writing any assertion, ask: *would this value be different on the arm I am NOT testing?*

1. **State something else sets unconditionally.** The acute risk here: `edit_post` writes `post.title`, `post.url` and `post.image_id` on paths unrelated to this block. An assertion may witness a later block rather than the line under test.
2. **A fixture coincidence.** Sub-project 37 found two where `user.id == post.id == 1` on a fresh database let wrong-argument mutations pass.
3. **Emptiness with no positive control.**
4. **An input that takes the same path under both arms.**

And: **a mutation's non-failures are evidence.** A test naming a branch that stays green under that branch's mutation does not guard it.

### Three hazards specific to this block

- **`:503` mutates a PIL global**, `Image.MAX_IMAGE_PIXELS`, process-wide and never restored. Register it; do not fix it.
- **`:463-468` is byte-identical to `make_post:197-204`**, registered as D455(d). Sub-project 37 covered that copy. A test here must not be satisfiable by it — these tests call `edit_post` directly.
- **`:500`'s SVG rejection destroys the file.** `sanitize_svg` has already overwritten it by the time it returns False (`:496-499`). That arm cannot inspect the file afterwards.

---

## File Structure

**Create:** `tests/test_shared_post_upload.py` — Tasks 1 through 7.

**Modify:** `coverage_floors.ini`, `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`, `tests/README.md` (Task 8).

**Not modified:** `app/shared/post.py` (no production change planned), `tests/factories.py`, `tests/test_shared_post_edit.py`.

---

## Task 1: Probe the harness, open the file

**Files:**
- Create: `tests/test_shared_post_upload.py`

**Interfaces:**
- Produces: `make_upload(...)` returning a `FileStorage`; a `chdir_upload` fixture; `seed_upload_context(...)`. Every later task consumes all three.

This task is a **probe**, and the round's main risk lives here. Three unknowns must be settled before any test is written. Each probe asserts something expected to FAIL; the point is the observed value.

- [ ] **Step 1: Search the register before probing**

```bash
for term in FileStorage 'MAX_IMAGE_PIXELS' c2pa store_files_in_s3 sanitize_svg ensure_directory_exists chdir tmp_path; do
  echo "=== $term ==="
  grep -n "$term" docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md | head -4
done
grep -nE "FileStorage|tmp_path|Image\.new" tests/README.md | head -10
```

Report what you found before running anything. If an entry already answers a probe, cite it and frame the probe as confirming a citation. **This step exists because sub-project 36 re-derived a registered fact and registered a worse duplicate of it; in sub-project 37 it paid for itself on first use.**

- [ ] **Step 2: Re-derive the block**

```bash
awk 'NR>=461 && NR<=563 {printf "%d\t%s\n",NR,$0}' app/shared/post.py
```

Confirm `:461` is the guard, `:470` `gibberish(15)`, `:475` the local directory, `:480` the C2PA call, `:487` the save, `:503` the PIL global, `:543` the S3 guard.

- [ ] **Step 3: Write the header and helpers**

```python
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
     every relative write and is cleaned up by pytest itself.

  3. `c2pa` IS INSTALLED, so `:480` runs for real rather than needing a stub
     (`app/utils.py:5757` does `import c2pa` inside the function). Task 1's
     Probe C records what it returns for a plain generated image, which decides
     whether `:481`'s true arm is reachable without a crafted input.

`Image.MAX_IMAGE_PIXELS` IS MUTATED PROCESS-WIDE at `:503` and never restored.
Nothing here depends on the default, but a later test in another file might.

THE EXTENSION CHECK AT `:463-468` IS BYTE-IDENTICAL TO `make_post:197-204`,
registered as D455(d). Sub-project 37 covered that copy. These tests call
`edit_post` directly, so they exercise this one.
"""

import os
from io import BytesIO

import pytest
from PIL import Image
from werkzeug.datastructures import FileStorage

from app import db
from app.constants import (
    POST_TYPE_ARTICLE,
    POST_TYPE_IMAGE,
    POST_TYPE_VIDEO,
    SRC_API,
    SRC_WEB,
)
from app.models import File, Post
from app.shared.post import edit_post
from tests.test_shared_post_edit import _OMIT, _Field, _api_input, _web_form


def make_upload(filename='pic.png', fmt='PNG', size=(8, 8), colour=(10, 20, 30),
                content_type=None):
    """A real `FileStorage` carrying real image bytes.

    Built in memory rather than from a fixture file, following
    tests/test_utils_images.py, which constructs every image it needs with
    `Image.new(...)` -- no binary assets live in this repository and none
    should be added. 8x8 keeps encode and thumbnail cost negligible.

    `content_type` becomes `.mimetype`, which `:480` passes to
    `inspect_image_c2pa`; it defaults to `image/<fmt lowered>`.
    """
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
```

- [ ] **Step 4: Probe A — does `chdir` work, and does it break anything?**

This is the round's central risk. Write and run:

```python
def test_probe_a_chdir_redirects_the_upload(db_session, chdir_upload):
    from tests.test_shared_post_edit import _seed

    s = _seed()
    edit_post(_api_input(), s.post, POST_TYPE_IMAGE, SRC_API, user=s.user,
              uploaded_file=make_upload())

    assert list(chdir_upload.rglob('*.png')) == []
```

Run: `./run_tests.sh tests/test_shared_post_upload.py::test_probe_a_chdir_redirects_the_upload -v`

**Expected to FAIL on the assertion, with a file found under `tmp_path`.** Three outcomes and they mean different things:
- Fails on the assertion, listing a path under `tmp_path` → `chdir` works. Record the path shape.
- Fails with an exception naming a missing template, config file or relative import → `chdir` broke something. **Record it and say so** — the plan's fallback is writing to the real directory with cleanup, and the file's docstring must change.
- Fails somewhere in `edit_post` before the save → record where; a later probe may already answer it.

Also confirm afterwards that **no file appeared under the repo's `app/static/media/`** — that is the whole point.

- [ ] **Step 5: Probe B — does `make_upload` survive the pipeline?**

`FileStorage.save()` to a path, then `Image.open` on the result. If `make_upload` is wrong, the failure will be an `AttributeError` or a PIL error rather than an assertion. Record which, and fix `make_upload` in this task.

- [ ] **Step 6: Probe C — what does `c2pa` return for a plain generated image?**

`:481` branches on `ai_gen['c2pa']['ai_generated']`.

```python
def test_probe_c_c2pa_on_a_plain_image(db_session, chdir_upload):
    from app.utils import inspect_image_c2pa

    up = make_upload()
    result = inspect_image_c2pa(up.read(), up.mimetype)

    assert result['c2pa']['ai_generated'] is True
```

Expected to fail with `False`. **The question that matters: can any image this harness can construct set it True?** If not, `:481`'s true arm needs either a crafted C2PA manifest or a monkeypatch of `post_module.inspect_image_c2pa`, and Task 3 must know which. Record your answer and the reasoning.

- [ ] **Step 7: Delete every probe, write the first real test**

```python
def test_an_uploaded_image_is_saved_and_linked(db_session, chdir_upload):
    """The default path end to end: `:461` true, through `:487`'s save and
    `:531`'s re-encode, to a `File` row linked on the post.

    Asserts on the file existing under the redirected directory AND on the
    `File` row, because either alone could be produced by a different block --
    `edit_post` sets `post.image_id` on paths that never touch an upload.
    """
    from tests.test_shared_post_edit import _seed

    s = _seed()
    edit_post(_api_input(), s.post, POST_TYPE_IMAGE, SRC_API, user=s.user,
              uploaded_file=make_upload())

    written = list(chdir_upload.rglob('app/static/media/posts/*/*/*'))
    assert len(written) == 1
    db.session.refresh(s.post)
    assert s.post.image_id is not None
```

Adjust to whatever Probes A-C established; this test must pass.

- [ ] **Step 8: Run, report the collection line, write the probe report**

- [ ] **Step 9: Commit**

Subject: `test: open the upload file and probe its three harness unknowns`

---

## Task 2: The guard and the extension check

**Arcs:** `461->463`, `464->465`, `464->466`, `467->468`, `467->470` — **5 of 39.**

- [ ] **Step 1: Re-derive**

```bash
awk 'NR>=461 && NR<=470 {printf "%d\t%s\n",NR,$0}' app/shared/post.py
```

- [ ] **Step 2: Know what is already covered and what is not**

`461->466` — the false arm, no file — is ALREADY covered by the 66 tests in `tests/test_shared_post_edit.py`, which pass no `uploaded_file`. Only the true arm is missing. Do not write a no-file test; it witnesses nothing new.

`:464` is `if type == POST_TYPE_VIDEO and can_upload_video():` — **two conjuncts, one arc pair.** `can_upload_video` reads `get_setting('allow_video_file_uploads', 'no')` (`app/utils.py:2586`) and returns False on that default, which is what makes them separable. Sub-project 37 covered the identical line in `make_post` and its report records the `set_setting` mechanism — read `.superpowers/sdd/2026-09-12-coverage-post-d-37/task-4-report.md` before reinventing it.

- [ ] **Step 3: Write the tests**

```python
def test_a_disallowed_extension_is_refused(db_session, chdir_upload):
    """`:467`'s true arm and `:468`'s raise.

    This is `edit_post`'s own copy of the check, byte-identical to
    `make_post:197-204` (D455(d)) which sub-project 37 covered. Calling
    `edit_post` directly is what makes this test exercise THIS copy: reaching it
    through `make_post` would hit that one first and prove nothing about this
    line.
    """
    from tests.test_shared_post_edit import _seed

    s = _seed()
    with pytest.raises(Exception, match='filetype not allowed'):
        edit_post(_api_input(), s.post, POST_TYPE_ARTICLE, SRC_API, user=s.user,
                  uploaded_file=make_upload(filename='payload.exe'))


def test_an_allowed_extension_passes(db_session, chdir_upload):
    """`:467`'s false arm -- the positive control for the test above.

    Without it, a `pytest.raises` that fired for an unrelated reason would look
    identical to one that fired for the right reason.
    """
    from tests.test_shared_post_edit import _seed

    s = _seed()
    edit_post(_api_input(), s.post, POST_TYPE_IMAGE, SRC_API, user=s.user,
              uploaded_file=make_upload(filename='pic.png'))

    db.session.refresh(s.post)
    assert s.post.image_id is not None


def test_a_video_upload_is_refused_when_video_uploads_are_off(db_session, chdir_upload):
    """`:464`'s SECOND conjunct taken false, with the first TRUE.

    `can_upload_video` returns False on the default setting, so `:465` never
    runs and '.mp4' stays out of `:463`'s list. A regression dropping the
    `can_upload_video()` conjunct would let this through.
    """
    from tests.test_shared_post_edit import _seed

    s = _seed()
    with pytest.raises(Exception, match='filetype not allowed'):
        edit_post(_api_input(), s.post, POST_TYPE_VIDEO, SRC_API, user=s.user,
                  uploaded_file=make_upload(filename='clip.mp4'))
```

**`:464`'s FIRST conjunct taken false with the second TRUE** needs its own witness — a non-video type while `can_upload_video()` would return True. Use the `set_setting` mechanism sub-project 37 established. **And `464->465`'s true arm** needs a video upload with the setting enabled that does NOT raise. Write both.

- [ ] **Step 4: Run, report the collection line, state which conjunct each test witnesses**

- [ ] **Step 5: Commit**

Subject: `test: cover the upload guard and edit_post's extension check`

---

## Task 3: Directory selection, C2PA, and extension dispatch

**Arcs:** `472->473`, `472->475`, `481->482`, `481->485`, `491->492`, `491->493`, `493->494`, `493->495`, `495->500`, `495->503`, `500->501`, `500->503` — **12 of 39.**

- [ ] **Step 1: Re-derive**

```bash
awk 'NR>=470 && NR<=503 {printf "%d\t%s\n",NR,$0}' app/shared/post.py
```

- [ ] **Step 2: Know the shape**

`:472` picks `'app/static/tmp'` when S3 is configured and the per-post media directory otherwise. Both arms are observable through where the file lands under `chdir_upload`.

`:491`, `:493` and `:495` dispatch on `final_ext` — `.heic` registers the HEIF opener, `.avif` imports `pillow_avif`, `.svg` sanitises. Each needs a file whose extension matches. **A `.heic` or `.avif` file must still be openable by whatever runs after it** — if `make_upload` cannot produce one PIL will accept, say so and use the extension with a PNG payload, recording that the test pins the dispatch rather than real format handling.

`:500` is `if not sanitize_svg(final_place):` and `:501` raises. **`sanitize_svg` destroys the file before returning False** (`:496-499`), so that arm cannot inspect it afterwards — assert on the raise.

- [ ] **Step 3: Write the directory-fork tests**

```python
def test_an_upload_lands_in_the_per_post_media_directory(db_session, chdir_upload):
    """`:472`'s false arm and `:475`.

    The default: no S3 configured, so the file goes to a directory derived from
    the first four characters of `gibberish(15)`. Asserting on the PATH SHAPE
    rather than the exact name, which is random by construction.
    """
    from tests.test_shared_post_edit import _seed

    s = _seed()
    edit_post(_api_input(), s.post, POST_TYPE_IMAGE, SRC_API, user=s.user,
              uploaded_file=make_upload())

    written = list(chdir_upload.rglob('app/static/media/posts/*/*/*'))
    assert len(written) == 1
    assert not list(chdir_upload.rglob('app/static/tmp/*'))
```

The S3 arm needs `store_files_in_s3()` true, which needs all three `S3_*` config values non-empty — and that also makes `:543`'s block run, which Task 6 owns. **Coordinate: either stub `post_module.boto3` here too, or assert only on the directory and let the S3 upload fail in a way you catch.** Say in your report which you chose and why.

- [ ] **Step 4: Write the C2PA test**

Task 1's Probe C decides this. If a constructible image can set `ai_generated` True, use it. If not, monkeypatch `post_module.inspect_image_c2pa` to return a dict with it True, and **say in the docstring that the patch stands in for a manifest this harness cannot build**. Both arms need a witness either way.

- [ ] **Step 5: Write the extension-dispatch tests**

One per arm of `:491`, `:493`, `:495`, plus `:500`'s two arms. Five tests minimum. For the SVG arms, remember the file is gone after a failed sanitise.

- [ ] **Step 6: Run, report, commit**

Subject: `test: cover the upload directory fork, c2pa and extension dispatch`

---

## Task 4: The PIL conversion path

**Arcs:** `510->511`, `510->513`, `513->514`, `513->535`, `515->516`, `515->533`, `517->518`, `517->520` — **8 of 39.**

- [ ] **Step 1: Re-derive**

```bash
awk 'NR>=503 && NR<=535 {printf "%d\t%s\n",NR,$0}' app/shared/post.py
```

- [ ] **Step 2: Know the levers**

All three come from config (`config.py:139-142`), all default to the quiet value:

| Config | Default | Gates |
|---|---|---|
| `MEDIA_IMAGE_FORMAT` | `''` | `:510` AVIF import, `:524` format kwarg |
| `MEDIA_IMAGE_QUALITY` | `90` | `:528` quality kwarg |
| `MEDIA_IMAGE_MAX_DIMENSION` | `2000` | `:521` thumbnail |

`:513` is `if not final_place.endswith('.svg') and not final_place.endswith('.gif') and not is_video_url(final_place):` — **three conditions, one arc pair.** Each needs its own witness for the false arm: an SVG, a GIF, and something `is_video_url` accepts.

`:515` is `if '.' + img.format.lower() in allowed_extensions:` and `:533` raises when it is not. **This is a second extension check, on the DECODED format rather than the filename** — a file named `.png` whose bytes are actually a GIF takes `:533`. That is the test worth writing, because it is the only thing distinguishing `:515` from `:467`.

`:517` picks `to_srgb` for JPEG and `convert('RGBA')` otherwise.

- [ ] **Step 3: Write the tests**

Eight arcs, at least six tests. The `:515` false arm deserves the sharpest one:

```python
def test_a_file_whose_bytes_disagree_with_its_name_is_refused(db_session, chdir_upload):
    """`:515`'s false arm and `:533`'s raise.

    `:467` checked the FILENAME's extension and passed. `:515` checks the
    DECODED format and refuses. A GIF named '.png' is the input that separates
    them: it survives `:467` because the name is allowed, and dies at `:533`
    because `img.format` is 'GIF' and '.gif' is not in `allowed_extensions`
    unless the name said so.

    Catches a regression that trusts the filename -- which is the whole reason
    this second check exists.
    """
    from tests.test_shared_post_edit import _seed

    s = _seed()
    with pytest.raises(Exception, match='filetype not allowed'):
        edit_post(_api_input(), s.post, POST_TYPE_IMAGE, SRC_API, user=s.user,
                  uploaded_file=make_upload(filename='pic.png', fmt='GIF'))
```

**Verify that claim before relying on it** — `:513` skips the whole block for a file whose PATH ends `.gif`, and the path here ends `.png`, so it should reach `:515`. If `make_upload(fmt='GIF')` does not produce `img.format == 'GIF'`, report what it does produce.

- [ ] **Step 4: Run, report, commit**

Subject: `test: cover the upload block's pil conversion and format checks`

---

## Task 5: The save kwargs

**Arcs:** `524->525`, `524->528`, `528->529`, `528->531` — **4 of 39.**

- [ ] **Step 1: Re-derive**

```bash
awk 'NR>=523 && NR<=533 {printf "%d\t%s\n",NR,$0}' app/shared/post.py
```

- [ ] **Step 2: Write the tests**

`:524` and `:528` build the kwargs `:531` passes to `img.save`. The default config gives format `''` (false) and quality `90` (true), so the default path already takes `524->528` and `528->529`. **The missing arms are `524->525` (a format configured) and `528->531` (quality falsy).**

`:526-527` also rewrite `final_ext` and `final_place`, so a configured format changes the SAVED FILENAME's extension — that is the observable difference, and it is stronger than asserting the kwargs, which are not visible from outside.

```python
def test_a_configured_format_rewrites_the_saved_extension(db_session, chdir_upload, monkeypatch):
    """`:524`'s true arm, `:525`'s kwarg, and `:526-527`'s rewrite.

    Asserting on the written file's extension rather than on the kwargs dict,
    because the dict is local to the function and the extension is the part a
    later read would see. `:527` replaces the path's extension, so a '.png'
    upload with WEBP configured lands as '.webp'.
    """
```

Fill in the body. Use the app's config rather than `set_setting` — these are `current_app.config` values, not `Settings` rows. Find how this suite overrides config (`grep -rn "monkeypatch.setitem(app.config\|app.config\[" tests/ | head`) and use the established mechanism.

**`:528`'s false arm** needs `MEDIA_IMAGE_QUALITY` falsy — `0` or `''`. Note the config coerces with `int(...)` at `config.py:142`, so check what a falsy override actually produces.

- [ ] **Step 3: Run, report, commit**

Subject: `test: cover the upload block's save-format and quality kwargs`

---

## Task 6: Image hashing and the S3 upload

**Arcs:** `537->538`, `537->543`, `539->540`, `539->543`, `543->544`, `543->565`, `546->547`, `546->548`, `548->549`, `548->550` — **10 of 39.**

- [ ] **Step 1: Re-derive**

```bash
awk 'NR>=535 && NR<=565 {printf "%d\t%s\n",NR,$0}' app/shared/post.py
```

- [ ] **Step 2: Know the hazards**

`:537` is `if current_app.config['IMAGE_HASHING_ENDPOINT'] and not is_video_url(final_place):` — **two conditions, one arc pair.** Default `''` (`config.py:127`) makes the first false.

`:538`'s `retrieve_image_hash(url)` issues a real HTTP request. Patch `post_module.retrieve_image_hash`, and `post_module.hash_matches_blocked_image` for `:539`.

**`:543`'s block constructs a real `boto3.session.Session()` at `:544` and calls `session.client(...)` at `:550`.** Setting the three `S3_*` config values makes `store_files_in_s3()` true, and then the code will attempt a network call unless `post_module.boto3` is patched. **Patch it.** `:563` then does `os.unlink(final_place)`, so a test of the S3 arm must expect the local file to be GONE — that is the observable difference between the two arms of `:543`, and it is better than asserting on a mock's call list.

`:546` and `:548` are independent config checks building `extra_args`. Their observable effect is only in the argument passed to `upload_file`, so these two do need assertions on the patched client's recorded call. Record that in the docstrings: they pin arguments, not behaviour.

- [ ] **Step 3: Write the tests**

Ten arcs, at least eight tests. The `:543` pair:

```python
def test_an_s3_upload_removes_the_local_file(db_session, chdir_upload, monkeypatch):
    """`:543`'s true arm through `:563`'s unlink.

    The local file being GONE is the observable difference between the two arms
    -- stronger than asserting on the mock's call list, which would pass even if
    `:563` were deleted. `boto3` is patched because `:544` and `:550` would
    otherwise open a real session and attempt a network call.
    """
```

Fill in the bodies. The false arm's positive control is any test from Task 1 or 3 where the file remains.

- [ ] **Step 4: Run, report, commit**

Subject: `test: cover the upload block's image hashing and s3 upload`

---

## Task 7: Mutation pass

**Files:**
- Modify: `tests/test_shared_post_upload.py` (only to close a hole)
- Modify: `app/shared/post.py` TEMPORARILY, always restored

- [ ] **Step 1: Record the baseline**

```bash
git diff --stat -- app/
wc -l app/shared/post.py
```

Empty diff, 1193 lines.

- [ ] **Step 2: Build the site list from the STATEMENT list, not the arc table**

**This is the correction sub-project 37 earned the hard way.** Its pass was scoped by the arc table, missed every covered statement containing no branch, and left a real hole at `make_post:224` that only the final whole-branch review caught — and the corrected pass then closed 14 more holes, including two fresh fixture coincidences.

Derive every statement line in `:461-563` from the AST, subtract nothing, and mutate each. For compound conditions — `:464`, `:513`, `:515`, `:537` — mutate **each operand separately**; sub-project 37's totals error came from counting a compound as one mutation.

- [ ] **Step 3: The loop, per mutation**

```bash
sed -n '<LINE>p' app/shared/post.py
sed -i '<LINE>s/<old>/<new>/' app/shared/post.py
git diff -- app/shared/post.py
./run_tests.sh tests/test_shared_post_upload.py -q
git checkout -- app/shared/post.py
git diff -- app/; wc -l app/shared/post.py
```

**Line-scoped `sed` always.** `:463-468` is byte-identical to `make_post:197-204` — an unanchored pattern hits both and produces a result nobody is measuring.

**Restore before any point where you might stop.**

- [ ] **Step 4: Read results in both directions**

- **A crash kill is not a kill.** For any non-assertion failure, find a viable non-crashing variant or record why none exists. Sub-project 37 recorded a `NameError` cascade across 18 tests as a kill; a void mutation is worse than a survivor because it enters the record as evidence and 18 failures look convincing.
- **An operator can be structurally void.**
- **Non-failures are evidence.** Compare each mutation's failures against the tests whose docstrings name that line. Report mismatches in either direction.
- **An arc being equivalent does not make every mutation of its line equivalent.** State which claim you are making, quantified over the inputs that REACH the site.

- [ ] **Step 5: One mandatory mutation beyond the list**

Neutralise `:467`'s extension check so it never refuses. **Two consecutive rounds have found a real hole this way** — sub-project 36's `restore_post` authorisation survived all 46 tests, sub-project 37's `make_post:165` survived all 29, both because every test supplied a permitted input. If it survives, close it with a test asserting the specific refusal.

- [ ] **Step 6: Close every hole, then re-run the mutation to confirm the new test kills it**

- [ ] **Step 7: Final checks**

```bash
git diff -- app/
wc -l app/shared/post.py
grep -oE "^def (test_[a-z0-9_]+)" tests/test_shared_post_upload.py | sort | uniq -d
./run_tests.sh tests/test_shared_post_upload.py -q
```

- [ ] **Step 8: Commit any tests added.** If none, make no commit and say so.

Subject: `test: close the holes the upload mutation pass found`

---

## Task 8: Measure, raise the floor, and register

- [ ] **Step 1: Measure over all six post files**

```bash
./run_tests.sh tests/test_shared_post_upload.py tests/test_shared_post_make.py \
  tests/test_shared_post_lifecycle.py tests/test_shared_post_moderation.py \
  tests/test_shared_post_interactions.py tests/test_shared_post_edit.py \
  --cov=app.shared.post --cov-branch --cov-report=json:/tmp/post38.json -q
echo "exit=$?"
```

- [ ] **Step 2: Read `percent_covered` from the JSON inside the container**

- [ ] **Step 3: Confirm the block is closed.** Re-derive the boundaries; confirm zero missing statements and zero missing arcs in `:461-563`. **Test BOTH endpoints of every missing arc.** If any remain, report BEFORE touching the floor.

- [ ] **Step 4: Re-measure Group E's remainder** so sub-project 39 inherits a number, not an estimate.

- [ ] **Step 5: Raise the floor** to the measured `percent_covered` rounded DOWN. `coverage_floors.ini`'s entry is 85. The module is not closed — the url half remains.

- [ ] **Step 6: Register from D464.** Update the marker. Required:

1. **Whatever the harness probes established** — the `chdir` decision and what it redirects, the real-`FileStorage` requirement, and what `c2pa` returns for a constructed image.
2. **`:503` mutates `Image.MAX_IMAGE_PIXELS` process-wide and never restores it.**
3. **`:515` is a second extension check on the decoded format**, and the `.png`-named GIF is what distinguishes it from `:467`. Record whether that test worked as predicted.
4. **`:463-468` duplicating `make_post:197-204`** — cross-reference **D455(d)**, do not re-register.
5. **D463's reach**: `:468`, `:501`, `:533` and `:540` all raise, and every one of them triggers the registered rollback defect when reached through `make_post`. Record the connection; the count of raising arcs in this block is the measure of how often that defect fires.
6. Whatever the mutation pass found, including every survivor with its argument.

- [ ] **Step 7: `tests/README.md` facts from 229.**

- [ ] **Step 8: Commit.** Subject: `docs: raise post.py's floor and register sub-project 38's findings`

**The controller runs the full suite and the floors check** — not this task.

---

## Self-Review

**Spec coverage.** Every spec section maps to a task: the three harness unknowns to Task 1's probes; the register search to Task 1 Step 1; the 39 arcs to Tasks 2-6, which account for 5 + 12 + 8 + 4 + 10 = 39; the three hazards to Tasks 3, 4 and 6 respectively; the statement-scoped mutation pass to Task 7 Step 2; the mandatory extension mutation to Step 5; the register list to Task 8 Step 6.

**Placeholder scan.** Four steps ask the implementer to derive rather than transcribe — Task 2's `:464` first conjunct, Task 3's C2PA and S3-directory decisions, Task 4's GIF-named-PNG verification, and Task 5's config-override mechanism — and each says what to report if the derivation fails. Task 3's C2PA step is explicitly conditional on Probe C. Those are bounded questions, not placeholders. Every other step contains its code.

**Type consistency.** `make_upload(filename, fmt, size, colour, content_type)` and the `chdir_upload` fixture keep their Task 1 signatures throughout. `_seed`, `_api_input`, `_web_form`, `_Field` and `_OMIT` are imported once and used unchanged.

**Known risk, stated.** Task 1's Probe A may show `chdir` breaks something in the call path. If so the plan's harness changes shape — the fallback is the real directory with cleanup — and Tasks 2-7 inherit that. The probe exists to find out before six tasks are built on it, which is why it is the first thing Task 1 does after the register search.

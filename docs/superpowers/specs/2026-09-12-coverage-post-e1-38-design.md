# Coverage sub-project 38: `app/shared/post.py` Group E1 — `edit_post`'s upload block

**Date:** 2026-09-12
**Branch:** `blentz`
**Measured at:** `300a3c97`

## Goal

Take `edit_post`'s file-upload block (`app/shared/post.py:461-600`) to zero missing statements and zero missing branch arcs.

## Measurement

Taken with `--cov=app.shared.post --cov-branch` over the five post test files (274 passed, exit 0), read from `summary.percent_covered`:

| | statements | missing | branches | missing | partial | percent_covered |
|---|---|---|---|---|---|---|
| module | 788 | 107 | 438 | 75 | 21 | 85.155 |

**`edit_post` is now the module's only function with any missing coverage.** Groups A, B, C and D contribute zero. Its 107 statements and 75 arcs band as follows, computed from the coverage JSON rather than estimated:

| Region | Lines | Stmts | Arcs |
|---|---|---|---|
| preamble, form reads, type | `250-430` | 3 | 6 |
| old-file teardown on type change | `431-460` | 8 | 7 |
| **upload: ext, C2PA, PIL, hash, S3** | **`461-600`** | **66** | **39** |
| url: pixelfed / loops / opengraph | `601-667` | 30 | 19 |
| poll, scheduling, federation tail | `668-751` | 0 | 4 |
| | | **107** | **75** |

**This round takes the upload block alone — 66 statements and 39 arcs.** Sub-project 39 takes the url half plus the scattered remainder (41/36) and closes the module.

## Why this is one round rather than half of one

Group E is 107/75, the most arcs of any group in D392's decomposition, and it splits along a real seam rather than an arbitrary one. The upload block is larger on its own than all of Group D was (61/26), and it needs a harness nothing in this campaign has built.

Every prior group tested pure control flow over database rows. This one runs a real file through a real image pipeline: `uploaded_file.seek()`, `.read()`, `.save()`, `PIL.Image.open`, colour-space conversion, thumbnailing, re-encoding, optional hashing, optional S3 upload. Group D's `SimpleNamespace(filename=...)` stand-in worked because `make_post:197-204` reads only `.filename`; it will not survive contact with `:479`.

## What the harness must supply

**Established by reading, not assumed:**

**1. A real `FileStorage` with real image bytes.** `:479` calls `.seek(0)`, `:480` `.read()`, `:486` `.seek(0)` again, `:487` `.save(final_place)`, and `:514` opens the result with `PIL.Image.open`. The precedent is `tests/test_utils_images.py`, which builds images in memory with `Image.new('RGB', (8, 8), ...)` and writes to `tmp_path` when a file is needed — no binary assets live in the repo, and none should be added. That file already covers `to_srgb`, which `:518` calls.

**2. A redirected working directory.** `:475` is `directory = 'app/static/media/posts/' + new_filename[0:2] + '/' + new_filename[2:4]` — a path **relative to the working directory**, which is the bind-mounted repo root. Every upload test would otherwise write a real file into the source tree under a random 15-character name from `gibberish(15)` at `:470`.

`.gitignore:162-163` already covers `/app/static/media/` and `/app/static/tmp/`, so nothing would reach a commit — but the files accumulate on disk forever and nothing tracks what to remove.

**Decision: a fixture does `monkeypatch.chdir(tmp_path)`.** Because the paths are relative, that redirects every write into pytest's per-test temp directory, which pytest cleans up itself. Zero repo writes, each test isolated.

**Task 1 must probe this before the plan commits to it.** The risk is that something else in the call path depends on the real working directory — a template lookup, a config-relative path, an import. If the probe shows `chdir` breaks something, the fallback is writing to the real directory and cleaning up, and the plan changes shape.

**3. `c2pa` is installed.** `:480` calls `inspect_image_c2pa`, which does `import c2pa` (`app/utils.py:5757`). Verified present in the test container, so the call runs for real rather than needing a stub. What it returns for an 8×8 PNG with no provenance is a Task 1 probe: `:481` branches on `ai_gen['c2pa']['ai_generated']`, and if a plain generated image can never set that True, the arc needs a crafted input or an argued impossibility.

## The block is config-driven, which is what makes it tractable

Four levers, all read from `current_app.config`, all defaulting to the quiet value:

| Config | Default | Gates |
|---|---|---|
| `MEDIA_IMAGE_FORMAT` | `''` (`config.py:141`) | `:510` AVIF import, `:524` format kwarg |
| `MEDIA_IMAGE_QUALITY` | `90` (`:142`) | `:528` quality kwarg |
| `MEDIA_IMAGE_MAX_DIMENSION` | `2000` (`:139`) | `:521` thumbnail size |
| `S3_ACCESS_KEY`/`_SECRET`/`_ENDPOINT` | `''` (`:104-107`) | `store_files_in_s3()`, so `:472` and `:543` |
| `IMAGE_HASHING_ENDPOINT` | `''` (`:127`) | `:537` hash-and-block |

So the default path runs: extension check → C2PA → directory → save → PIL open → convert → thumbnail → quality kwarg, with no S3, no hashing and no format conversion. Each of those three arms needs exactly one config override, which `set_setting`/config monkeypatching can supply — sub-project 37 established the mechanism for the video-upload setting and recorded that `CACHE_TYPE = 'NullCache'` makes `get_setting`'s memoize inert.

## Hazards this round must handle

**`:503` mutates a PIL global.** `Image.MAX_IMAGE_PIXELS = 89478485` is process-wide and never restored. A test that changes it, or any later test depending on the default, is affected for the rest of the session. Register it; do not fix it.

**`:543-560`'s S3 block needs boto3 stubbed even with config set.** Setting the three `S3_*` values makes `store_files_in_s3()` true, which then constructs a real `boto3.session.Session()` and calls `session.client(...)`. The tests must not attempt a network call. Task 1 determines whether patching `post_module.boto3` is sufficient.

**The extension check at `:463-468` is byte-identical to `make_post:197-204`**, which sub-project 37 covered. That duplication is registered as D455(d). The two copies are reached differently — `make_post`'s runs before any row exists, `edit_post`'s on every edit — so both need their own tests, and a test here must not be satisfied by `make_post`'s copy.

**`:500`'s SVG rejection destroys the file.** `sanitize_svg` has already overwritten the file by the time it returns False, per the comment at `:496-499`. A test of that arm cannot inspect the file afterwards.

## Production changes

**None planned.** No defect in this block has been confirmed. If one surfaces it gets the treatment PC1 and PC2 got in earlier rounds — a pasted failing observation before the change, and a fix to the cause rather than the symptom. No defect will be manufactured to justify the round.

**One defect is already registered and explicitly out of scope:** D463, `make_post`'s rollback leaving both post counts inflated when `edit_post` raises. Several arcs in this block raise — `:468` filetype, `:501` SVG, `:540` blocked image — and each therefore triggers that defect when reached through `make_post`. This round covers the raises; it does not fix D463.

## The four false-witness mechanisms

Registered as D451, and all four apply:

1. **Asserting on state something else sets unconditionally.** The acute risk here: `edit_post` writes `post.title`, `post.url` and `post.image_id` on paths unrelated to the upload block, and `make_post` calls this function, so an assertion may witness the caller or a later block rather than the line under test.
2. **A fixture coincidence.** Sub-project 37 found two where `user.id == post.id == 1` on a fresh database let wrong-argument mutations pass.
3. **Emptiness with no positive control.**
4. **An input that takes the same path under both arms.**

And the reading rule: **a mutation's non-failures are evidence.** A test whose docstring names a branch and stays green under that branch's mutation does not guard it.

## Verification

A mutation pass over every site in the block. **Scoped by the STATEMENT list, not the arc table** — sub-project 37's pass was scoped by arcs, missed every covered statement containing no branch, and left a real hole at `make_post:224` that only the final whole-branch review caught. Derive the statement list from the function's AST lines and subtract nothing.

Standing rules: a crash kill is not a kill unless a viable non-crashing variant of the same fault also dies; an operator can be structurally void; an arc being equivalent does not make every mutation of its line equivalent.

**One mandatory mutation beyond the list:** neutralise `:467`'s extension check so it never refuses. Two consecutive rounds have found a real hole this way — sub-project 36's `restore_post` authorisation survived all 46 tests, sub-project 37's `make_post:165` survived all 29 — both because every test supplied a permitted input.

## Success criteria

- `:461-600` at zero missing statements and zero missing arcs, confirmed by re-measurement with both endpoints of every arc checked.
- The module floor raised to the measured `percent_covered`, rounded down, in `coverage_floors.ini`.
- Full suite green, run by the controller in the foreground, unpiped, with the floors check chained by `&&`.
- Findings registered from **D464**, with the marker updated.
- `tests/README.md` facts from **229**, recording at minimum: the `chdir` fixture and what it redirects; how a real `FileStorage` is built; what `c2pa` returns for a plain generated image; and the `Image.MAX_IMAGE_PIXELS` global.
- **Group E's remaining figures re-measured** so sub-project 39 inherits a number rather than an estimate.

## Out of scope, carried forward

- **D463** — `make_post`'s rollback leaves both post counts inflated. Registered, unfixed, and touched by every raising arc in this block.
- Two reachable unhandled 500s in `app/post/routes.py` (D421).
- `Site.admins()`'s divergence wherever `g.admin_ids` is unset (D442, open check).
- `report_post:875`'s guard, unreachable in production (D450).
- Group E's url half and remainder, 41/36 — sub-project 39, which closes the module.

# Coverage sub-project 39: `app/shared/post.py` Group E2 — closing the module

**Date:** 2026-09-12
**Branch:** `blentz`
**Measured at:** `544a7eb1` (full suite, 4663 passed, 3 skipped, all 20 floors met)

## Goal

Take `edit_post`'s remaining gaps to zero and **close `app/shared/post.py`** — the last 41 statements and 35 branch arcs in the module.

## Measurement

Read from `summary.percent_covered`, not `percent_statements_covered`:

| | statements | missing | branches | missing | percent_covered |
|---|---|---|---|---|---|
| module | 788 | 41 | 438 | 35 | 93.80097879282219 |

`edit_post` is the module's **only** function with any gap. Groups A, B, C, D and E1 contribute zero.

## The 41 statements and 35 arcs

Banded from the coverage JSON with boundaries re-derived. Sub-project 38's register initially mis-stated Region A as `:403-451`; the function's first missing arc is `[387, 389]`, so the corrected boundary is `:387-460`.

| Region | Lines | Stmts | Arcs | What |
|---|---|---|---|---|
| **A** | `:387-460` | 11 | 13 | permission compound, URL type dispatch, scheduled, old-file teardown |
| **B** | `:565-661` | 30 | 17 | the same dispatch again — thumbnails and `File` rows |
| **C** | `:662-703` | 0 | 5 | poll and event handling, arc-only |
| | | **41** | **35** | |

Region C has **zero missing statements**. Its lines all execute; five specific arms do not.

## The harness must invert sub-project 38's convention

**This is the round's central fact and the thing most likely to waste a task.**

Region B's gaps sit in the `elif` chain after `:601`'s `if is_image_url(url):`. Sub-project 38 established — and recorded as `tests/README.md` fact 229 — that every full `edit_post()` call needs `http_mock` with a `url__regex` HEAD route reporting `image/png` and **no GET route**, because `is_image_url` returning True makes `:601` the arm taken, structurally excluding the opengraph arms, so a registered GET route would go uncalled and fail respx's `assert_all_called=True`.

**Sub-project 39 needs exactly the opposite**: a HEAD reporting a **non-image** content type so `is_image_url` is False, plus **GET routes**, because `opengraph_parse` will now run at `:621`, `:632` and `:642`.

A round that follows fact 229 literally cannot reach its own target. **Fact 229 needs extending with the inverse case, not following.** Task 1 establishes the inverted fixture and Task 8 rewrites the fact to cover both directions.

## The pixelfed divergence: probed, and registered rather than fixed

The two dispatches match the same hosts with different strictness:

```
:404   post.url.startswith('https://pixelfed.social/') or post.url.startswith('https://pixelfed.uno/')
:619        url.startswith('https://pixelfed.social')  or      url.startswith('pixelfed.uno')
```

This was probed during scoping rather than assumed, and **the probe changed the finding twice.**

First: the scheme-less disjunct is **not dead**. `is_image_url('pixelfed.uno/p/1')` survives — `httpx.UnsupportedProtocol` is a subclass of `httpx.HTTPError` (verified), so `mime_type_using_head`'s handler at `app/utils.py:345` catches it and returns `''`; `:271` takes the else branch; `urlparse` puts the whole string in `path` with an empty `netloc`; and `:284`'s extension sniff returns False. So `:601` is False and the URL reaches `:619`, where `startswith('pixelfed.uno')` is True.

Second, and more important: **the two sites do not test the same value.** `:403` reads `post.url` — the *existing* post's URL — while `:619` reads `url`, the *newly submitted* one, and `post.url` is not reassigned until `:618` and later. So this is not one value tested twice with different strictness. It is two classifiers of the same *kind* of thing, applied to different values at different points, disagreeing about what counts as a pixelfed URL.

**Decision: pin both branches with tests and register the divergence. Do not change either.**

- `:619`'s disjunct is **reachable**, so altering it removes live behaviour rather than repairing dead code. That is the opposite of PC1 (sub-project 36), where the needle could never match its haystack and the fix restored an intent the WEB arm independently proved.
- The sites read different variables at different times, so "make them match" presumes they should agree — which is the product question, not its answer.
- Whether scheme-less input should be accepted at all is a decision this round has no standing to make. **D476 got the same treatment for the same reason** one round ago.

## Region C's five arcs, identified

Arc-only gaps, all reachable, none exotic:

- **`665->668`** — `if file:` false. `post.image` is set but `File.query.get(post.image_id)` returns None: an orphaned reference.
- **`673->682`** — `if 'choices' in poll_data:` false. **Probably unreachable, and this was found while writing the plan rather than the spec.** Both entry points build `poll_data` as a fresh dict that always carries the key: `:306` is `'choices': poll_data.get('choices', [])` on the API branch, `:352` is `'choices': poll_choices` on the web branch. If the plan's proof holds, the arm takes the repository's existing `# pragma: no branch -- see proof in <test file>` treatment (`app/shared/tasks/pages.py:270`, `:312`, `:333`, `app/shared/tasks/follows.py:188`) and is registered.
- **`675->674`** — the **loop-back** arc. A choice dict lacking `choice_text`, or whose text is whitespace, so the loop continues to the next iteration.
- **`683->686`** — `if poll is None:` **false**. Editing a post that already has a `Poll` row, rather than creating one.
- **`699->703`** — `if event is None:` **false**. The same for an existing `Event`.

The last two are the edit-an-existing case, which the create-path tests never reach.

## Hazards

**`:446` is a three-conjunct compound** — `if post.type == POST_TYPE_VIDEO and store_files_in_s3() and post.url.startswith(f'https://{...S3_PUBLIC_URL}')` — scored by coverage.py as a single arc pair. Each conjunct needs its own witness. Its body then does a **function-body import** at `:447`, `from app.shared.tasks.maintenance import delete_from_s3`, so `post_module.delete_from_s3` cannot be patched — that line re-imports on every call. And `:448` forks on `current_app.debug`, choosing between a direct call and `.delay()`.

**`:654` is a four-disjunct compound** — `is_video_url(url) or url.endswith('.mp4') or url.endswith('.webm') or is_video_hosting_site(embed_url)`. Its arcs are already covered, but the mutation pass must still split it per operand.

**`:675`'s loop-back arc needs a multi-choice poll.** A single bad choice gives the loop no next iteration to return to; the arc only appears when a bad choice is followed by another.

**D476 is live in this region.** `:654` tests `url.endswith('.mp4')` and `.webm` explicitly but not `.mov`, consistent with `is_video_url` — so the `.mov` inconsistency registered last round has a third site here. Record it; do not fix it.

## Production changes

**No behaviour change planned.** The pixelfed divergence is registered, not fixed, for the reasons above. The one edit in prospect is a `# pragma: no branch` comment on `:673` if its false arm is proven unreachable — the treatment four sites in this repository already carry, changing no behaviour and no line count. If a genuine defect surfaces it gets the treatment PC1 and PC2 got — a pasted failing observation before the change — but no defect will be manufactured to justify the round.

**Three registered defects are live in or adjacent to this code and stay unfixed:** D463 (`make_post`'s rollback leaving post counts inflated), D465 (the upload file-leak at `:527`), and D476 (`.mov` permitted but unrecognised).

## The five false-witness mechanisms

D451's four plus D469's fifth, all of which cost this campaign real tests:

1. **State something else sets unconditionally.** Acute here: `edit_post` writes `post.type` and `post.url` from several arms, so asserting either alone rarely witnesses the arm under test.
2. **A fixture coincidence.**
3. **Emptiness with no positive control**, named by test name.
4. **An input taking the same path under both arms.**
5. **Two independent conditions exercised only in lockstep** cannot detect a swap between them, however precise the assertions.

And: **a mutation's non-failures are evidence.**

## Verification

A mutation pass **scoped by the statement list, not the arc table** — sub-project 38's pass found 10 holes that way, every one a branchless statement, after all 39 arcs were witnessed and six tasks had passed review.

**The compound list is derived mechanically**, via `ast.walk` for `BoolOp` nodes with their operand counts, not by reading. Sub-project 38's controller hand-listed five compounds; the AST reported seven, and the hand list both included a non-compound and omitted two real ones.

Standing rules: a crash kill is not a kill unless a viable non-crashing variant also dies; an operator can be structurally void; an arc being equivalent does not make every mutation of its line equivalent.

## Success criteria

- **`app/shared/post.py` at zero missing statements and zero missing arcs** — the module closed, confirmed by re-measurement with both endpoints of every arc checked pairwise rather than by inspecting a global min and max. An arm proven unreachable from both entry points counts as closed only with a `# pragma: no branch` carrying its proof, never by lowering the target.
- The floor raised to the measured `percent_covered`, rounded down.
- Full suite green, run by the controller, with the floors check chained by `&&`.
- Findings registered from **D478**, marker updated.
- `tests/README.md` **fact 229 extended** to cover both HEAD directions, and new facts from **230**.

## Out of scope, carried forward

- **D463** — `make_post`'s rollback leaves both post counts inflated.
- **D465** — the original upload is never unlinked when a format is configured.
- **D476** — `.mov` permitted at `:465` but absent from `is_video_url`, reaching `Image.open` uncaught.
- **D421** — two reachable unhandled 500s in `app/post/routes.py`.
- **D442** — `Site.admins()`'s divergence wherever `g.admin_ids` is unset.
- **D450** — `report_post:875`'s guard, unreachable in production.

**After this round the module is closed and the campaign needs a new target.** The register's open findings are the natural candidate: six defects across five files, none owned by any scheduled round.

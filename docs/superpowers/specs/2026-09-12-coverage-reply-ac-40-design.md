# Coverage sub-project 40: `app/shared/reply.py` Groups A and C

**Date:** 2026-09-12
**Branch:** `blentz`
**Measured at:** `e3a4afa0` (full suite, 4719 passed, 3 skipped, all 20 floors met)

## Goal

Take `app/shared/reply.py`'s reader interactions and its delete/restore lifecycle to zero missing statements and zero missing branch arcs — 73 statements and 46 arcs — and add the module to the coverage ratchet.

## Why this module, and why now

`app/shared/post.py` closed at 100% in sub-project 39, ending six consecutive rounds on one file. For the first time in the campaign no scheduled round owns anything.

`app/shared/reply.py` is the highest-transfer target available, because it is `post.py`'s structural twin. Twelve of its sixteen functions mirror functions this campaign has already closed:

| `reply.py` | `post.py` | closed in |
|---|---|---|
| `vote_for_reply` `:18` | `vote_for_post` `:31` | SP34 |
| `bookmark_reply` `:57`, `remove_bookmark_reply` `:75` | `:77`, `:97` | SP34 |
| `subscribe_reply` `:93` | `subscribe_post` `:115` | SP34 |
| `extra_rate_limit_check` `:134` | `extra_rate_limit_check` `:155` | SP34 |
| `make_reply` `:142` | `make_post` `:163` | SP37 |
| `edit_reply` `:202` | `edit_post` `:250` | SP38-39 |
| `delete_reply` `:241`, `restore_reply` `:269` | `:755`, `:796` | SP36 |
| `report_reply` `:297` | `report_post` `:821` | SP36 |
| `mod_remove_reply` `:400`, `mod_restore_reply` `:436` | `:1045`, `:1088` | SP35 |

Four are reply-only and have no precedent: `lock_post_reply` `:472`, `set_collapse_post_reply` `:509`, `choose_answer` `:534`, `unchoose_answer` `:565`.

**The module is far smaller than its twin.** `reply.py` is 577 lines against `post.py`'s 1193, and its largest function is `report_reply` at 55 statements. `edit_reply` is 39 lines where `edit_post` was 505 — and `edit_post` alone consumed five of the last six rounds. Nothing in this module has that shape.

## Measurement

Read from `summary.percent_covered`, not `percent_statements_covered`:

| | statements | missing | branches | missing | percent_covered |
|---|---|---|---|---|---|
| module | 364 | 304 | 186 | 172 | 13.455 |

The module has **no entry in `coverage_floors.ini`**, so this round adds a ratchet entry rather than raising one.

Per function, boundaries from `grep -n "^def "`:

| Group | Functions | Stmts | Arcs |
|---|---|---|---|
| **A** reader interactions | `vote_for_reply` (24/16), `bookmark_reply` (4/4), `remove_bookmark_reply` (1/2), `subscribe_reply` (7/6), `extra_rate_limit_check` (1/0) | **37** | **28** |
| **B** create and edit | `make_reply` (42/22), `edit_reply` (29/12) | 71 | 34 |
| **C** lifecycle | `delete_reply` (18/8), `restore_reply` (18/10) | **36** | **18** |
| **D** report | `report_reply` (55/34) | 55 | 34 |
| **E** moderator verbs | `mod_remove_reply` (21/12), `mod_restore_reply` (21/12) | 42 | 24 |
| **F** reply-only verbs | `lock_post_reply` (24/14), `set_collapse_post_reply` (15/12), `choose_answer` (15/4), `unchoose_answer` (8/4) | 62 | 34 |
| | | **303** | **172** |

The six groups sum to 303 of the module's 304 missing statements; the residual one is outside every function boundary. Arcs sum exactly.

**This round takes A and C — 73 statements and 46 arcs.** That is deliberately under the campaign's demonstrated pace of roughly 110/56 per round, because round one also builds the reply harness. B, D, E and F follow as later sub-projects, re-measured rather than inheriting these figures.

A and C are chosen together because they have the strongest precedent: Group A is the twin of the group SP34 closed, and Group C is the twin of two functions SP36 closed. Every convention transfers.

## The harness

**Three factories already exist and need no work:** `make_post_reply` (`tests/factories.py:455`), `make_post_reply_bookmark` (`:477`), `make_post_reply_vote` (`:488`).

### What transfers from sub-project 34 verbatim

All of this was established and verified in `tests/test_shared_post_interactions.py` and must not be re-derived:

- **No `user=` escape hatch.** `edit_post` and `edit_reply` take `user=`; no Group A function does. `vote_for_reply:21`/`:28`, `bookmark_reply:58`, `remove_bookmark_reply:76` and `subscribe_reply:95` read `current_user` or call `authorise_api_user` with no way around it.
- **The SRC_API arm needs no request context.** `get_ip_address` (`app/__init__.py:68-77`) wraps its `request` read in `try/except RuntimeError` — its own comment names the case — and returns `''`. `user_ip_banned` then sees a falsy IP and returns `None`, so a context-free API call passes the guard.
- **What blocks an API test is an `ap_id`, not context.** `authorise_api_user` requires `ap_id is None`, `verified` true and `banned` false.
- **`web_ctx` is reserved for SRC_WEB arms**, which need `flash` and template rendering.

### What is new, and what Task 1 must probe

- **`subscribe_reply:94` joins `Post`** and filters `deleted=False` on **both** the reply and its parent post. `subscribe_post` had no join. A seeded reply needs a live parent.
- **`delete_reply:256-258` and `restore_reply:282-284` execute raw SQL** against `post_reply.child_count`, keyed on `tuple(reply.path[:-1])`. The `path` ARRAY column is `app/models.py:2898`. Whether `make_post_reply` populates it is **Task 1's first probe**; if it does not, reaching those arcs needs a seeded path, and a single-element path makes `reply.path[:-1]` an empty tuple, which is a different arc from a multi-element one.
- **Both web arms render templates** — `vote_for_reply:51` renders `post/_comment_voting_buttons.html` and `subscribe_reply:131` renders `post/_reply_notification_toggle.html`. SP34 solved template rendering for the post twins; confirm the convention transfers rather than assuming it.

## Two defects, found by reading and registered rather than fixed

### `restore_reply` does not mirror `delete_reply`

```
252    reply.post.reply_count -= 1                280    reply.post.reply_count += 1
253    reply.post.reply_count_cross_posted -= 1      (no counterpart)
254    reply.community.post_reply_count -= 1         (no counterpart)
255    reply.author.post_reply_count -= 1         281    reply.author.post_reply_count += 1
```

`reply_count_cross_posted` and `community.post_reply_count` are decremented on delete and never restored, so a delete-then-restore cycle leaves both permanently low.

This is D463's shape — a rollback that does not undo what it did. **Registered, not fixed.** The round pins today's behaviour on both paths so the divergence is witnessed by a test rather than asserted in a document.

### `vote_for_reply`'s permission check is API-only

`:22` and `:24` call `can_upvote`/`can_downvote` inside the `if src == SRC_API:` arm. The `else` at `:26-28` has no equivalent, and the web route that reaches it — `app/post/routes.py:555-561`, `comment_vote` — carries `@login_required`, `@validation_required` and `@approval_required` but no voting-permission check.

So the API arm refuses a voter the community has banned from voting and the web arm does not.

**Registered, not fixed, and the reasoning is the one that governed the pixelfed divergence in sub-project 39:** changing a permission check is a product decision, this round has no standing to make it, and `:26-28` is live behaviour rather than dead code. The round pins both arms as they behave and records the asymmetry with its route evidence.

**No production change is planned.** If a genuine defect surfaces that warrants one, it gets the treatment PC1 and PC2 got in sub-project 36 — a pasted failing observation before the change, and a fix to the cause rather than the symptom. No defect will be manufactured to justify the round.

## The five false-witness mechanisms

D451's four plus D469's fifth. Every test docstring names which branch it witnesses and why the assertion could only be produced by that branch.

1. **Asserting on state something else sets unconditionally.** Acute in Group C: `delete_reply:248-249` and `restore_reply:276-277` write `deleted` and `deleted_by` on every path through their function, so asserting `deleted` alone witnesses nothing about the counter arithmetic below it. Sub-project 36 shipped exactly that test against `delete_post` and had to replace it.
2. **A fixture coincidence making two arms produce the same value.**
3. **Emptiness with no same-mechanism positive control.**
4. **An input that takes the same path under both arms.**
5. **Two independent conditions exercised only in lockstep** cannot detect a swap between them.

And: **a mutation's non-failures are evidence.**

## Verification

A mutation pass **scoped by the statement list, not the arc table** — sub-project 38's statement-scoped pass found ten holes an arc-scoped pass would have missed, every one a branchless statement (D470).

**Both the statement list and the compound list are derived mechanically** via `ast.walk`, never by reading. Sub-project 39's controller predicted 11 `BoolOp` nodes and 26 operands; the AST reported 27 and 58 (D487).

**`git diff --quiet -- app/` is the load-bearing tree check, not `wc -l`.** A single-line replacement preserves the line count, and in sub-project 39 that left a live mutant in production code across two turn boundaries with the stated tripwire reading normal throughout (D488).

Standing rules: a crash kill is not a kill unless a viable non-crashing variant of the same fault also dies; an operator can be structurally void; an arc being equivalent does not make every mutation of its line equivalent.

**One mandatory mutation beyond the statement list:** neutralise `vote_for_reply:30`'s ban guard so it never refuses. Three consecutive rounds found a real hole this way, because every test supplied a permitted input.

## The documentation rule this round inherits

Sub-project 39 hit one defect seventeen times: **a claim left standing after a change invalidated its premise.** Its own diagnosis, in two shades — implementers searched for what they were *changing* rather than for what they were *invalidating*; and one graded a claim on whether a careful reader could reach the truth rather than on whether it is true where it stands.

**A claim is graded where it is read.** Every task in this round re-reads each docstring it touches in full, before committing, and asks what premise its change invalidated elsewhere in that docstring. That count of seventeen is a floor, not a tally — no exhaustive sweep was ever run.

## Success criteria

- Groups A and C at zero missing statements and zero missing arcs, confirmed by re-measurement with **every arc checked as a pair** rather than by inspecting a global minimum and maximum.
- A **new** `coverage_floors.ini` entry for `app/shared/reply.py` at the measured `percent_covered`, rounded down.
- Full suite green, run by the controller in the foreground, unpiped, with the floors check chained by `&&` and given **both** arguments — it takes the coverage JSON *and* the floors file, and fails closed on one.
- Findings registered from **D496**, marker updated.
- `tests/README.md` facts from **231**, recording at minimum: whether `make_post_reply` populates `path`, the `subscribe_reply` join requirement, and which SP34 harness facts transfer unchanged.
- **Groups B, D, E and F re-measured** so the next sub-project inherits a number rather than an estimate.

## Out of scope, carried forward

No round owns these. The register's open findings now span several files:

- **D421** — two reachable unhandled 500s in `app/post/routes.py`.
- **D442** — `Site.admins()`'s divergence wherever `g.admin_ids` is unset.
- **D450** — `report_post:875`'s guard, unreachable in production.
- **D463** — `make_post`'s rollback leaves both post counts inflated.
- **D465** — the upload file-leak at `edit_post:527`.
- **D476** — `.mov` permitted but unrecognised by `is_video_url`, across three sites.
- **D478** — the pixelfed host-matching divergence.
- **D490-D492** — three in-production type-implies-data `None` dereferences at `app/shared/tasks/pages.py:224`, `:233`, `:319`, and dead code in `edit_post`.
- **D495** — 32 open mutation holes in `app/shared/post.py` with recipes, five flagged not fully verifiable.

Groups B, D, E and F of this module — 230 statements and 126 arcs before this round's side effects — are sub-projects 41 onward.

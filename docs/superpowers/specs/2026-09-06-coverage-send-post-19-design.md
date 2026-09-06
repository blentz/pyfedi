# Sub-project 19 — `send_post`, and the two Page builders that disagree

**Date:** 2026-09-06
**Branch:** `blentz`
**Target:** `app/shared/tasks/pages.py`, function `send_post` (`:88-368`)
**Predecessor:** sub-project 18 (`docs/superpowers/specs/2026-09-05-coverage-shared-post-edit-18-design.md`), which took
`edit_post`'s source dispatch and suspicious-domain block to zero on both metrics and raised
`app/shared/post.py` from 9.03% to 40.55%.

---

## 1. Why this function

`app/shared/tasks/pages.py` is 51.38% covered, 176 uncovered, and **125 of those are in `send_post`**
(64 statements / 61 branch arms). No test in the repository calls `send_post` directly.

It is also the function sub-project 18 spent twenty lines avoiding. Ten of its tests set
`s.community.local_only = True` before calling `edit_post`, purely so that `app/shared/post.py:736`
would set `federate = False` and the eager-Celery path would not reach `send_post` and crash. Those
crashes were registered as **D298** and **D299**. This sub-project stops avoiding the function and
tests it.

---

## 2. The framing: two builders, and what they actually disagree about

PyFedi builds an ActivityPub `Page` in two places:

- **`post_to_page`** — `app/activitypub/util.py:132-219`. Reached from the outbox collection view
  (`app/activitypub/routes.py:2033`).
- **`send_post`** — `app/shared/tasks/pages.py:88-368`. Reached from the Celery task path
  (`app/shared/post.py:739-743` → `app/shared/tasks/pages.py:67`/`:80` → `send_post` at `:88`).

Sub-project 18's own reviews established that reachability twice, the second time correcting the
first: `post_to_activity` (`app/activitypub/util.py:100-131`) is **not** on the Celery path and has
exactly one caller. Both `:166-177` and `:195-201` are inside `post_to_page`, not `post_to_activity`.
That distinction is load-bearing here and is stated because it was got wrong before.

The two builders are near-twins. Their differences are the findings, and they are **not all the same
kind of difference** — which is the part the design first got wrong and this spec corrects.

### 2.1 D299 — a genuine divergence, plus an internal contradiction

| | |
|---|---|
| `app/activitypub/util.py:172-177` | `if post.image_id is not None:` wraps the image dereference |
| `app/shared/tasks/pages.py:181` | `elif post.type == POST_TYPE_IMAGE:` then `post.image.source_url`, **no `image_id` check** |

So the two copies disagree about whether a `POST_TYPE_IMAGE` post with no image is safe to serialise.

**The sharper argument is internal to `pages.py`, not comparative.** `:213` is `if post.image_id:`,
thirty-two lines below `:181`, in the same function. The file already knows the guarded idiom. `:181`
simply does not use it. A fix does not need the other file's permission — it needs only to be
consistent with the line below it.

### 2.2 D298 — NOT a divergence. Both builders have it, and the precedent is a sibling statement

This is the correction to the in-chat design, which called D298 a disagreement between the files.
It is not:

| Site | Field | Guarded? |
|---|---|---|
| `app/activitypub/util.py:195` | `poll.end_poll` | no |
| `app/activitypub/util.py:200`, `:201` | `event.start`, `event.end` | no |
| `app/shared/tasks/pages.py:224` | `poll.end_poll` | no |
| `app/shared/tasks/pages.py:232`, `:233` | `event.start`, `event.end` | no |

All six are unguarded. `ap_datetime` (`app/utils.py:2293-2294`) is
`return date_time.isoformat() + '+00:00'` with no `None` guard, and `Poll.end_poll`
(`app/models.py:3782`), `Event.start` (`:3841`) and `Event.end` (`:3842`) are all nullable and are
left `None` by `edit_post` whenever the input omits them (`app/shared/post.py:687`, `:705`).

**The precedent that settles the arbitration is `app/activitypub/util.py:168`** — a sibling statement
inside `post_to_page` itself: `if post.edited_at is not None:` guards the `updated` key by **omitting
it** rather than by emitting a null. The same function solves the same problem correctly for one
field and incorrectly for three.

### 2.3 A third difference, candidate D300

| | |
|---|---|
| `app/activitypub/util.py:170` | `if (post.type == POST_TYPE_LINK or POST_TYPE_VIDEO or POST_TYPE_EVENT) and post.url is not None:` |
| `app/shared/tasks/pages.py:178-179` | `if post.type == POST_TYPE_LINK or post.type == POST_TYPE_VIDEO:` then `{'href': post.url, ...}` |

Two disagreements in one statement: `pages.py` omits the `post.url is not None` check, and it omits
`POST_TYPE_EVENT`. `Post.url` is nullable (`app/models.py:1712`, `db.String(2048)` with no
`nullable=False`), and `edit_post` writes `None` into it on ordinary paths — `:326-327` for a
non-link type and `:613` for an event with a banner image. So `{'href': None}` is constructible.

**Register it; do not fix it in this sub-project.** Unlike D298 and D299, this one has no settled
answer: adding `POST_TYPE_EVENT` changes what peers receive for every event post on a live install,
and that is a behaviour change needing its own arbitration. The `None` half and the `EVENT` half must
be argued separately, and the plan must say so rather than bundling them.

---

## 3. Scope

**125 uncovered** in `send_post` (`:88-368`), 64 statements and 61 branch arms, to zero on both
metrics.

The function has five regions, and the plan should follow them as its task boundaries:

| Region | Lines | What it does |
|---|---|---|
| Mention extraction | `:96-123` | regex over `post.body`, `search_for_user` behind two bare `except:` clauses |
| Mention notification | `:125-174` | local recipients, `edit` vs create branches |
| The Page builder | `:175-246` | **holds D298, D299 and the D300 candidate**. Starts at `:175`, not `:176`: `language` is built there and consumed by the page dict at `:195` |
| Create/Announce + community delivery | `:247-305` | `send_post_request` gated on `community.local_only`, `community.is_local()`, instance software |
| Note amendment + follower delivery | `:306-368` | the Mastodon-friendly copy, then mention and follower fan-out |

### Explicitly out of scope

`make_post` (`:62-74`, 3 uncovered), `edit_post` (`:75-87`, 3), `move_post` (`:369-386`, 15) and
`move_object` (`:387-432`, 30). `move_object` is a distinct concern — moving a post between
communities — and folding it in would dilute the framing for 51 uncovered.

---

## 4. The two fixes

Both test-first, each in its own commit, under the campaign's mutation discipline: each conjunct
mutated separately, one at a time, each killed by a distinct named test, kills labelled
assertion-kill vs crash-kill and sole vs multi, `app/` restored and **verified** after every single
mutation.

**Mutation instrument, non-negotiable.** Apply each mutation with a targeted single-line edit
(`sed -i 'NNNs/old/new/'` or an Edit matching one unique line). Never rewrite the whole file. After
applying, `wc -l app/shared/tasks/pages.py` must print 432. After restoring, both `git diff -- app/`
empty and the line count unchanged, before the next mutation. Sub-project 18 lost a 1174-line
production module to a whole-file rewrite during this exact step.

### 4.1 D299 — the image dereference at `:181`

The fix must make `:181` agree with `:213` thirty-two lines below it and with
`app/activitypub/util.py:172`. The failing test comes first and must fail with `AttributeError` on a
`POST_TYPE_IMAGE` post whose `image_id` is `None` — a state `edit_post` reaches whenever the image
path at `app/shared/post.py:601-608` is not taken.

The plan must decide, and argue, whether the guard omits the attachment entirely or emits the `Link`
shape instead. The nearest precedent is `app/activitypub/util.py:172-177`, which omits.

### 4.2 D298 — three unguarded `ap_datetime` calls at `:224`, `:232`, `:233`

Guard the callers and **omit the key**, following `app/activitypub/util.py:168`. Do **not** change
`ap_datetime` itself: it has **29 call sites**, most passing non-nullable columns, and returning
`None` would put `"endTime": null` into the activity — worse than an absent key, because a peer
cannot tell a null from a missing value without knowing our schema.

**Only `app/shared/tasks/pages.py` is fixed.** The three copies at `app/activitypub/util.py:195`,
`:200` and `:201` are outside this sub-project's scoped file and stay registered, with this
arbitration attached so a later sub-project applies it mechanically. This is the same partial-closure
shape sub-project 18 used for D286.

### 4.3 What the fixes unblock, and what they do not

Sub-project 18's twenty `local_only = True` lines exist only to dodge these two crashes. Once both
are fixed those workarounds become deletable — **but not here**. `tests/test_shared_post_edit.py` is
not this sub-project's file, and removing them would require re-running that file's own mutation
tables. Register the availability; leave the deletion to whoever next owns that file.

---

## 5. Harness

New file: **`tests/test_shared_tasks_send_post.py`**. No test in the repository calls `send_post`
directly today.

**Entry.** `send_post(post_id, edit=False, session=None)` at `:88`. The `session` parameter has no
usable default — `:89` is `session.query(Post).get(post_id)` — so every test passes `db.session`
explicitly. `edit=False` and `edit=True` are two distinct paths through the builder (`:211-212`,
`:225`, `:228`) and both must be exercised.

**Stopping before the network.** Delivery runs through `send_post_request`
(`app/activitypub/signature.py:82`). Two levers, and they are not interchangeable:

- `community.local_only` short-circuits the whole community-delivery block at `:267`.
- `:336-338` `followers = ...; if not followers: return` is an early return that ends the function
  before follower fan-out.

A locally-seeded community with no followers and no remote instances exercises the entire builder and
reaches `:337`'s return without a single outbound request. Tests that need to observe delivery
register `http_mock` routes and assert on them; tests that only need the builder must register
nothing, because `http_mock` is `assert_all_called=True` (`tests/conftest.py:288-295`) and an
unreached registration fails the test.

**Standing harness facts that bind here.** `tests/conftest.py:143` truncates with `RESTART IDENTITY`,
so ids restart at 1 and id-valued assertions go silently vacuous on collision (fact 89) — seed
pairwise-distinct and assert `len({...}) == n`. Seeding **order** matters too, not just distinctness:
`make_community` hardcodes `instance_id=1`, so a peer instance built before the local one leaves the
community's FK pointing at the peer (fact 89's extension, added by sub-project 18).
`db.session.expire(obj)` before asserting is the campaign's instrument for pinning a commit, because
`autoflush=False` means a read through the writing Session cannot distinguish committed from pending.

**A trap this function carries.** `search_for_user` at `:106` and `:112` sits behind bare `except:`
clauses, so a mention that fails to resolve is silently skipped. A test asserting "the mention was
not delivered" cannot distinguish "correctly skipped" from "crashed and swallowed". Any such test
must pin the reason, not just the absence.

---

## 6. Findings register

New findings are numbered from **D300**, appended to
`docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`. D300 is reserved for §2.3's
`href`/`EVENT` divergence. Establish the next free number with a grep before writing it; sub-project
18 used the wrong number throughout for want of that check, and the register carries the note.

D298 and D299 move from *registered* to *fixed*, with D298's three `app/activitypub/util.py` copies
explicitly still open.

---

## 7. The coverage floor

`app/shared/tasks/pages.py` has **no entry** in `coverage_floors.ini`, so nothing defends it. It is
51.38% blended today. The plan must re-measure after the tests land and add
`app/shared/tasks/pages.py = <measured, floored to a whole percent>` in the file's existing ordering.
Floors only ever rise.

---

## 8. Global constraints

Copied verbatim into the plan; they bind every task.

- **Delete nothing the task did not create. `claude_test` in the repository root is not the campaign's.**
- Only the controller runs the full suite, one pytest session at a time. The controller supplies every
  coverage figure; no implementer reports one.
- Any run exceeding ~600s is erroneous — that is `session_timeout` in `pytest.ini:28`, and pytest
  **still exits 0** on a session timeout, so a truncated run reads as green. Check the test count and
  the coverage report's mtime, not the exit code. `./run_tests.sh --down`, then retry.
- Coverage command: `./run_tests.sh --cov=app --cov-branch --cov-report=json:scratch_full_cov.json -q`.
- Mutation discipline as §4 states it, including the line-count assertion after every restore.
- Every line number copied from anywhere must be re-derived against the current tree before it is
  written down (fact 99). Citation sweeps run in two passes: `file:line` first, then bare paths
  against `git ls-files` (fact 100).
- Enumerate conditional expressions by AST walk, not by grep (fact 94) — coverage.py emits no arc for
  one (fact 87), so zero on both metrics cannot see them. `sed -n 'A,Bp'` prints no line numbers
  (fact 95).
- Commit messages containing backticks are committed with `git commit -F <file>`, never `-m`.

---

## 9. Success criteria

1. `tests/test_shared_tasks_send_post.py` exists and calls `send_post` directly.
2. `send_post` (`:88-368`) is at zero uncovered statements and zero uncovered branch arms.
3. Both `edit=False` and `edit=True` are exercised throughout the builder.
4. Every conditional expression in `:88-368` has both arms exercised by named tests, reconciled by an
   AST walk rather than by the coverage number.
5. D299 is fixed at `:181` and proved by a test that fails with `AttributeError` before the fix.
6. D298 is fixed at `:224`, `:232` and `:233`, each proved by a test that fails with
   `AttributeError: 'NoneType' object has no attribute 'isoformat'` before the fix, and the fix omits
   the key rather than emitting a null.
7. `ap_datetime` (`app/utils.py:2293-2294`) is unchanged.
8. `coverage_floors.ini` gains an `app/shared/tasks/pages.py` entry at the measured floor.
9. The register carries D300 onward; D298 and D299 are marked fixed, with D298's three
   `app/activitypub/util.py` copies recorded as still open.
10. The full suite is green, with the pass/skip counts and the coverage report's mtime recorded.

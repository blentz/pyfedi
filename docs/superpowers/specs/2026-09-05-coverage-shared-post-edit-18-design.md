# Sub-project 18 — `edit_post`'s source dispatch and its suspicious-domain block

**Date:** 2026-09-05
**Branch:** `blentz`
**Target:** `app/shared/post.py`, function `edit_post` (`:250-754`; the next `def` is `delete_post` at `:755`)
**Predecessor:** sub-project 17 (`docs/superpowers/specs/2026-09-04-coverage-update-tails-17-design.md`), which took
`update_post_from_activity` to zero uncovered statements and raised `app/activitypub/util.py` from 72.5547% to 76.9025%.

---

## 1. Why this function, and why now

`app/shared/post.py` is 9.03% covered — the lowest of any file the campaign has touched. It has **no entry in
`coverage_floors.ini`**, so nothing currently defends it. `edit_post` alone is 564 uncovered (350 statements /
214 branch arms).

More importantly, sub-project 17 spent its whole length inside `update_post_from_activity`, the **federated** editor,
and two of its most significant findings are statements about how that function disagrees with `edit_post`, the
**local** editor:

- **D287** — `app/shared/post.py:582` calls `community_member.is_local()`. `CommunityMember`
  (`app/models.py:3499-3513`) defines no such method. It raises `AttributeError` for any moderator.
- **D288** — the sibling loop at `app/activitypub/util.py:3520-3527` has **no locality gate at all**. The two copies
  disagree about whether remote moderators are notified.
- **D292** — `Notification.targets` is a `db.JSON` column, and `app/shared/post.py:577` puts `post.domain`, an ORM
  object, into it. The register names this the **worst** of D292's four sites.

Every one of those was found by reading `edit_post` from inside `util.py` and never executing it. This sub-project
executes it. That is the difference between a claim and a measurement, and sub-project 17 cost three controller
claims that measurement disproved.

---

## 2. Scope

**175 uncovered** (107 statements / 68 branch arms), in two clusters.

### Cluster A — the source dispatch, `:251-383` (137)

| Region | Lines | Statements | Branch arms | Total |
|---|---|---|---|---|
| `SRC_API` branch | `:251-314` | 46 | 30 | 76 |
| `SRC_WEB` branch | `:315-383` | 41 | 20 | 61 |

The cluster's lower bound is the function's own comment at `:384`:

```
    # WARNING: beyond this point do not use the input variable as it can be either a dict or a form object!
```

That is the code declaring where its own dispatch ends. The scope boundary is the code's, not the author's.

### Cluster B — the domain and notify block, `:565-598` (38)

20 statements / 18 branch arms. This is the block that holds both authorised fixes and is the local copy of
`app/activitypub/util.py:3508-3545`.

### Explicitly out of scope

`:384-564` and `:599-754` — the shared post-dispatch body, the upload path, and the URL/thumbnail typing tail.
Together they are 389 uncovered and would double the sub-project. They stay for a later one.

---

## 3. What this sub-project must find out, not assume

### 3.1 The timestamp comparison — the framing, corrected

The design presented in chat claimed the API branch's `datetime.fromisoformat` calls *preserve* the instant while
D290 records the federated path *discarding* the offset. **Measurement shows that is half wrong**, and the corrected
form is sharper. The three parses in `edit_post`'s API branch:

```
290:  event_data['start']      = datetime.fromisoformat(event_data['start'].replace('Z', '+00:00')).replace(tzinfo=None)
295:  event_data['end']        = datetime.fromisoformat(event_data['end'].replace('Z', '+00:00')).replace(tzinfo=None)
310:  parsed_poll['end_poll']  = datetime.fromisoformat(poll_data['end_poll'].replace('Z', '+00:00'))
```

`:290` and `:295` strip `tzinfo` **after** parsing, so a peer offset is discarded without conversion — the wall-clock
digits survive, the instant does not. `:310` does not strip, so it yields an **aware** datetime.

The federated copies do the opposite in each case:

- `app/activitypub/util.py:3375-3376` — `event.start` / `event.end` via bare `fromisoformat`, **aware**, instant preserved.
- `app/activitypub/util.py:3338` — `poll.end_poll = request_json['object']['endTime']`, the **raw string**, unparsed. This is D290.

So for events the local path discards what the federated path keeps; for polls the local path parses what the
federated path does not. All three destination columns are `db.DateTime` — `TIMESTAMP WITHOUT TIME ZONE`
(`app/models.py:3782` `Poll.end_poll`, `:3841-3842` `Event.start` / `Event.end`), so `:310` writes an aware value into
a naive column.

**The plan must establish by execution, not by argument, what that write actually does** — silently drops the offset,
converts, or raises. Register the result as **D297**. Do not fix it in this sub-project: a timestamp semantics change
is a behaviour change for existing installs and belongs to its own arbitration, exactly as D290 does.

### 3.2 The conditional expression at `:578`

```
578:                             'author_user_name': user.ap_id if user.ap_id else user.user_name
```

Harness fact 87: coverage.py emits **no arc** for a conditional expression, so 100% statements and 100% branches can
both hold while one arm has never run. This is the same structure as D293, which sub-project 14's own criterion
structurally could not have caught. Both arms of `:578` must be exercised by named tests, and the plan must say so as
a requirement rather than leaving it to the coverage number.

---

## 4. The two fixes

Both are authorised. Both are test-first, each in its own commit, with the mutation discipline the campaign already
runs: each conjunct mutated separately, one at a time, each killed by a distinct named test, kills labelled
assertion-kill vs crash-kill and sole vs multi, `app/` restored and verified after **every single** mutation.

### 4.1 D287 — `:582`

```python
-                    if community_member.is_local():
+                    if community_member.user.is_local():
```

`CommunityMember.user` is a relationship at `app/models.py:3509` (`lazy='joined'`). `User.is_local` is a method at
`app/models.py:1251`. The failing test comes first and must fail with `AttributeError`, not with an assertion — that
is what proves the defect is live rather than latent, which is the distinction sub-project 16 had to reason its way
around when its scoped defect turned out to be unreachable.

Because `:582` gates the loop, D287 also means `:577` is currently reachable **only** through the admin loop at
`:590-598`. Fixing `:582` opens the moderator path to `:577`, which is D292's site. The two fixes therefore interact:
**land D287 first**, so that the D292 test can reach the crash through the path the fix just opened.

### 4.2 D292 — `:577`

```python
-                            'orig_post_domain': post.domain,
+                            'orig_post_domain': post.domain.name,
```

The register left this unfixed because the choice among `post.domain.name`, `post.domain_id`, and dropping the key was
unarbitrated. **This sub-project arbitrates it, and the argument is the deliverable, not the diff.**

Pinned choice: **`post.domain.name`**. Three reasons.

1. Its three sibling keys in the same dict — `orig_post_title` (`:575`), `orig_post_body` (`:576`),
   `author_user_name` (`:578`) — are all human-readable display values. `domain_id` would be the only opaque integer
   in a dict of display strings.
2. The key has four writers and zero readers. Dropping it changes the dict's **shape**, and shape is the thing a
   future reader would compare the four writers against; a serialisable value of the right kind costs nothing and
   keeps the four comparable.
3. `post.domain` is non-`None` at `:577`: `:566` computes `domain`, `:567` guards `if domain:`, and `:570` assigns
   `post.domain = domain` before the dict is built at `:573`. So `.name` cannot raise here.

The failing test must reproduce the register's stated consequence, which is worse than a crash. The session
boundaries inside `edit_post` make it concrete:

- `:459` — `db.session.commit()`. The dispatch's field assignments are already durable.
- `:588` / `:598` — `db.session.add(notify)`. The poisoned `Notification` is pending, and nothing raises yet.
- `:607` (or, if the `is_image_url(url)` branch at `:601` is not taken, `:691`, `:722` or `:734`) — the next
  `db.session.commit()`. This is where the flush raises `TypeError: Object of type Domain is not JSON serializable`.

So the edit is **half-applied**: the `:459` commit stands, everything after it is rolled back, and when the failing
flush is `:607` the `File` added at `:606` is orphaned. The test must pin the boundary — assert what survived `:459`
as well as that the later commit raised — because a test that only catches the `TypeError` does not distinguish this
from a clean abort.

**Only `app/shared/post.py:577` is fixed.** The other three D292 sites (`app/activitypub/util.py:3517` and `:3535`,
`app/models.py:2075`) are outside this sub-project's scoped file and stay registered. The arbitration argument above
now travels with them, so a later sub-project applies it mechanically instead of re-deciding it.

### 4.3 The constraint both fixes must respect

Both are one-line in-place edits. **Neither may shift a line number.** Seven test files cite `app/shared/post.py` by
line, and the cited lines bracket both fix sites:

| Citing file | Cites |
|---|---|
| `tests/factories.py:751` | `:211-214` |
| `tests/test_feed_sorts.py:45` | `:213` |
| `tests/test_unparseable_url_ingress.py:620` | `:192`, `:443`, `:566` |
| `tests/test_unparseable_url_ingress.py:35`, `:176` | `:654` |
| `tests/test_urlparse_valueerror_guards.py:321` | `:410`, `:601` |
| `tests/test_urlparse_valueerror_guards.py:621`, `:655` | `:654` |
| `tests/test_event_post_type_survives_update.py:16`, `:134` | `:613` |
| `tests/test_ap_update_post_tails.py:2889`, `:3332` | `:577` |

Every cited line resolves correctly today. An inserted line anywhere above `:654` stales citations in five files.
This is harness fact 100's third class — a citation into a file the same change is editing — and it binds here.

Note that `tests/test_ap_update_post_tails.py` cites `:577` **as D292's worst site**. Fixing `:577` makes that prose
wrong in the file sub-project 17 just finished. The plan must update it in the same commit as the fix. Harness fact 96:
a correction that only deletes a false claim will be re-derived; land the refutation where the claim lives.

---

## 5. Harness

New file: **`tests/test_shared_post_edit.py`**. It is the first test in the repository to drive this module directly —
no existing test imports `edit_post`.

Entry is a direct call. The three production callers are `app/api/alpha/utils/post.py:1521` (`SRC_API`),
`app/post/routes.py:1075` (`SRC_WEB`), and `app/shared/post.py:231` inside `make_post` (which passes
`from_scratch=True`). None of them is the harness's route in; the tests call `edit_post` themselves.

**Authorisation escape.** Both branches begin with the same two lines:

```
251:     if src == SRC_API:
252:         if not user:
253:            user = authorise_api_user(auth, return_type='model', id_match=post.user_id)
...
315:     else:
316:         if not user:
317:             user = current_user
```

Passing `user=<User>` therefore skips `authorise_api_user` on the API side **and** `current_user` on the web side. No
request context, no login, no auth token. This is simpler than the design first assumed — the escape is not API-only.

**Input shapes.** `SRC_API` takes a plain dict (`input['title']`, `input['body']`, `input['url']`, …). `SRC_WEB` takes
a WTForms-shaped object read through `.data` attributes (`input.title.data`, `input.body.data`, …). The file needs one
small double for the web shape; it should be a single named helper in the shared prelude, not an ad-hoc object per
test, following sub-project 17's file layout where the prelude holds a small fixed set of names and nothing else.

**Reaching cluster B.** `:565` is `if url and (from_scratch or url_changed):`. So a test must supply a truthy `url`
and either pass `from_scratch=True` or change the url from what the post already holds. Both routes in must be
exercised — they are two branch arms of the same guard.

**Standing harness constraints.** `tests/conftest.py:143` truncates with `RESTART IDENTITY`, so ids restart at 1 and
id-valued assertions go silently vacuous on PK collision (fact 89); seed pairwise-distinct and assert
`len({...}) == n`. `db.session.expire(obj)` before assertions is the campaign's standard instrument for pinning a
commit, because `autoflush=False` means a read through the writing Session cannot distinguish committed from pending.
`http_mock` (`tests/conftest.py:288-295`) uses `assert_all_called=True`.

---

## 6. The coverage floor

`app/shared/post.py` has **no entry in `coverage_floors.ini`**. The file is currently 9.03% blended.
`tests/check_coverage_floors.py` compares `summary['percent_covered']`, the blended figure, and a module with no entry
is ignored — so today nothing defends this file at all.

Adding the entry is a deliverable of this sub-project. The plan must:

1. Re-measure the blended figure from a full-suite coverage run **after** the tests land, since the pre-existing 9.03%
   is a measurement of the tree before this work.
2. Add `app/shared/post.py = <measured floor>` to `coverage_floors.ini`, placed in the file's existing ordering.
3. Set the floor at the measured value rounded **down** to a whole percent, matching how every existing entry was set.

The floor only ever rises. Lowering one to make a run pass defeats the ratchet.

---

## 7. Findings register

New findings are numbered from **D297**, appended to
`docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`. D297 is reserved for the timestamp result in
§3.1. The plan must write the next free number into every live note that carries one and leave frozen notes untouched.

D287 and D292 move from *registered* to *fixed*, with the caveat in §4.2 that D292's other three sites remain open.

---

## 8. Global constraints

These bind every task and are copied verbatim into the plan.

- **Delete nothing the task did not create. `claude_test` in the repository root is not the campaign's.**
- Only the controller runs the full suite, and only one pytest session at a time. The controller supplies every
  coverage figure; no implementer reports one.
- Any test run exceeding ~600s is erroneous and must be addressed directly: `./run_tests.sh --down`, then retry.
- Coverage command: `./run_tests.sh --cov=app --cov-branch --cov-report=json:scratch_full_cov.json -q`.
- Mutation discipline: one mutation at a time, `app/` restored and verified after each, never batched. A fix that
  changes a guard invalidates every mutation previously recorded for that guard; re-run them all in the same commit
  (fact 74).
- Every line number copied from anywhere must be re-derived against the current tree before it is written down
  (fact 99). Citation sweeps run in two passes: `file:line` first, then bare paths against `git ls-files` (fact 100).
- Enumerate conditional expressions by AST walk, not by grep (fact 94). `sed -n 'A,Bp'` prints no line numbers; use
  `grep -n`, `awk` on `NR`, or `cat -n` (fact 95).
- Commit messages containing backticks are committed with `git commit -F <file>`, never `-m`.

---

## 9. Success criteria

1. `tests/test_shared_post_edit.py` exists and drives `edit_post` directly.
2. Cluster A (`:251-383`) and cluster B (`:565-598`) are at zero uncovered statements and zero uncovered branch arms.
3. Both arms of the conditional expression at `:578` are exercised by distinct named tests.
4. D287 is fixed at `:582` and proved by a test that fails with `AttributeError` before the fix.
5. D292 is fixed at `:577` with the arbitration argued in the spec, and proved by a test that fails with
   `TypeError: Object of type Domain is not JSON serializable` before the fix.
6. No line number in `app/shared/post.py` moves. Every existing citation still resolves.
7. `tests/test_ap_update_post_tails.py`'s prose about `:577` is updated in the same commit as the fix.
8. `coverage_floors.ini` gains an `app/shared/post.py` entry at the measured floor.
9. The findings register carries D297 onward, and D287/D292 are marked fixed.
10. The full suite is green, with the pass/skip counts recorded.

---

## 10. Correction, dated 2026-09-05, appended by a later register round and NOT known to any task above

**The finding number is wrong throughout this document.** The `orig_post_domain` / `TypeError: Object of type Domain is not JSON serializable` defect that every "D292" above refers to (all eleven occurrences, §4.1, §4.2's heading, §4.2's body, and criteria 5 and 9) is **D286**, not D292. The number was read once from the spec draft and copied forward without anyone re-opening the register cell it named. **D292 is a different, still-open entry**: `Post.new`'s unguarded `choice_ap['name']` read at `app/models.py:2205-2209`, the create-path sibling of D284. Nothing in this design was wrong except the label — the fix, the tests and the arbitration this document argues for are all correct under the name D286. This document is left as written, since a design document is a record of what was designed; the corrected label and the six citing sites in `tests/test_shared_post_edit.py` are carried in `docs/superpowers/plans/2026-09-05-coverage-shared-post-edit-18.md`'s own SECOND CORRECTION note and in the findings register at `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`, where the closure is recorded against D286.

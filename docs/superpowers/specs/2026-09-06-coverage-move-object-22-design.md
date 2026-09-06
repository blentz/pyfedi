# Sub-project 22: close `app/shared/tasks/pages.py`

**Date:** 2026-09-06
**Branch:** `blentz`
**Predecessor:** sub-project 21 (`74b67335..c6ed020a`, 17 commits)

## 1. Goal

Take `app/shared/tasks/pages.py` from 84.91% to a full close: cover `move_object`,
`move_post` and the `make_post`/`edit_post` wrappers, land two production
changes, register what should not be fixed, and raise the module's floor.

This completes the `notes.py`/`pages.py` twin pair the campaign has worked
through since sub-project 19. `notes.py` closed at 99.09% in sub-project 21.

## 2. Extent

All extents are AST `FunctionDef.lineno`/`end_lineno`, not a convention.
Coverage is from `scratch_full_cov.json`, written 2026-09-06 15:57:53 by a full
`--cov=app --cov-branch` run of 3919 passed / 3 skipped / 6 subtests in 230.83s.

`pages.py` is **435 lines**, 243 statements, 41 missing, 128 branches, 3 partial.

| function | extent | missing statements | missing arms |
|---|---|---|---|
| `move_object` | `:390-435` | 20 | 10 |
| `move_post` | `:373-387` | 13 | 2 |
| `make_post` | `:63-72` | 3 | 0 |
| `edit_post` | `:76-85` | 3 | 0 |
| `send_post` | `:88-352` | 2 | 3 |

39 statements and 12 arms are in scope. `send_post`'s residual — statements
`:107-108` and arcs `(270, 310)`, `(312, 314)`, `(333, 339)` — was **proved
unreachable by sub-project 19** and stays. Do not chase it.

An AST walk over the four in-scope functions finds **no conditional
expressions**. The plan must re-run the walk and report its raw output rather
than repeating this line (fact 94); this sub-project's predecessor claimed a
function had no ternaries by inspection and the walk found one.

## 3. `move_object`'s control flow

Simpler than `send_answer`: there is no `is_undo`, so the 2×2 collapses. Three
decisions and a loop, and the ten missing arms are exactly these:

| line | decision | arms |
|---|---|---|
| `:393` | `isinstance(origin, Community) and isinstance(target, Community)` | `(393, 394)`, `(393, 396)` |
| `:398` | `community.local_only or not community.instance.online()` | `(398, 399)` return, `(398, 401)` continue |
| `:416` | `community.is_local()` — Announce vs. direct post | `(416, 417)`, `(416, 435)` |
| `:431-432` | the follower loop and its four-conjunct guard | `(431, -390)` never entered, `(431, 432)`, `(432, 431)` continue, `(432, 433)` deliver |

Two end-to-end paths, each with a different wire shape:

- **local** — `del move['@context']` at `:417`, then the `Announce` built at
  `:422-430` wrapping the `Move`, posted per follower instance at `:433`.
- **remote** — the bare `Move` posted to `community.ap_inbox_url` at `:435`.

`@context` is kept on whichever object goes on the wire outermost and deleted
from the one that gets nested, exactly as in `send_answer`. Assert both
directions per path; do not argue from which `del` appears to run.

### 3.1 `is_local()` at `:416` decides silently

`Community.is_local()` (`app/models.py:795`) is a disjunction whose
`profile_id()` falls back to a computed default, so a community whose `ap_id`
was never set is **silently local** and `:435` never runs. Seeding must set
`ap_id` **and** `ap_profile_id` for the remote case. This is harness fact 112.

### 3.2 The `isinstance` guard is a genuine branch, not a type annotation

`:393` requires **both** `origin` and `target` to be `Community`. Its false arm
raises at `:396`. Both arms need a test, and the false arm needs at least two —
a non-`Community` `origin` and a non-`Community` `target` — because a mutant
changing `and` to `or` is killed by neither alone.

## 4. `move_post`'s wrapper differs from its siblings

`move_post` (`:373-387`) is **not** shaped like `make_reply`/`edit_reply`:

- It wraps the whole body in `with current_app.app_context():` at `:374`.
- It carries an inner guard at `:379`, `if post and not post.deleted:`, whose
  two conditions are separate failure modes: a **missing** post and a
  **deleted** post. Both must be reached, and they are distinct tests — a
  single "no work happened" assertion cannot tell them apart.
- Its arcs `(379, 380)` and `(379, 387)` are the two the coverage report names.

`make_post` (`:63-72`) and `edit_post` (`:76-85`) are the ordinary shape: three
missing statements each, being the `except: rollback; raise` and `finally: close`
arms. Reach them by a **natural raise**, and note that `pages.py`'s lookup style
must be established by reading it — sub-project 21 lost a fix round to assuming
one function's lookup style applied to its neighbour, when `send_reply:81` uses
`.filter_by(...).one()` (raising `NoResultFound`) and `send_answer:246` uses
`.get()` (returning `None`). Ask the source which one `send_post` uses.

## 5. The two production changes

This sub-project lands **two**, which is a deliberate departure from the
one-guard discipline sub-projects 19-21 held. The risk was raised twice and the
scope confirmed twice. Sub-project 19's lesson stands as the reason for care:
one allowed guard became five when fixing one relocated the crash.

### 5.1 `:398` gains the `private` conjunct

```
if community.local_only or community.private or not community.instance.online():
```

D309's **third** closed site, of ten. `Community.private` (`app/models.py:611`)
is commented "only members can view. no federation."

Note the sibling guards differ and neither is a template to copy blindly:
`pages.py:153` is `local_only or private` with **no** `online()` check, while
`notes.py:248` is `local_only or private or not instance.online()`. The fixed
`:398` matches `notes.py:248`, because `:398` already tests `online()`.

This site is the one that went missing from D309's enumeration for two rounds,
because sub-project 19 declared this half of `pages.py` out of scope — and an
out-of-scope declaration is invisible to the next reader, who sees a list that
looks complete.

### 5.2 `:396` raises `TaskError` instead of a bare `Exception`

```
raise TaskError('Unsupported origin or target')
```

**`TaskError` is defined in `app/utils.py`, APPENDED AT THE END OF THE FILE**
(after `:5797`), not beside `get_task_session` (`:3673-3675`) and
`patch_db_session` (`:3679-3711`) where it thematically belongs.

That placement is forced by the same constraint that chose the file, and the
numbers decide it: `app/utils.py` carries **358 `utils.py:NNN` citations** in
tracked files, **135 of them at or after `:3673`**. Inserting beside the
task-session helpers would invalidate 135 citations — worse than the 96 in
`pages.py` this placement exists to protect. Appending after the last line
invalidates **zero**.

The cost is locality: the class sits at the end of a 5797-line module rather
than beside the machinery it serves. That is a real readability loss and it is
accepted deliberately, because this campaign's chronic defect is stale
citations and 0 against 135 is not a close call. Record the reason in a comment
at the definition so the next reader does not "tidy" it upward.

**The placement is chosen to avoid a mass citation invalidation, and that
reason is load-bearing.** `pages.py` already imports from `app/utils.py` on a
continuation line at `:10-11`, so `TaskError` is reached by **widening an
existing line**. A new import line would shift every line below it by one, and
**96 `pages.py:NNN` citations exist in tracked files** — a larger invalidation
than commit `ec98595c`'s 48, in a campaign whose chronic defect is stale
citations. `pages.py` must still be **435 lines** after both changes.

`move_object`'s only caller is `move_post:382`, whose handler is
`except Exception:`, so a narrower type changes nothing functionally today.
The change is to the error contract: a caller can now catch this failure
without catching everything.

`raise Exception(...)` is the **house idiom** across `app/shared/` — `site.py:17`,
`feed.py:146`, `:281`, `user.py:28`, `:37`, `:69`, `:105`, `:113`, `:120`, `:126`
and more. `:396` therefore becomes the first narrow raise among eleven. That
inconsistency is deliberate and must be recorded as a finding (§7) so the next
reader sees a stated direction rather than an oversight.

## 6. Testing approach

### 6.1 Capture mechanism

Unchanged and not negotiable: assert on **serialized outbound request bytes**
via `http_mock`/respx, never on an in-memory dict, because the code mutates
those dicts after delivery. `tests/test_shared_tasks_send_post.py` already
carries `_sent_activity` (`:263`), `_remote_inbox` (`:204`), `_seed` (`:78`),
`_key_id_of` (`:1218`) and `_community_follower` (`:1489`) — re-derive each
before use.

`ActivityPubLog.query.count()` is the early-return discriminator: `post_request`
(`app/activitypub/signature.py:103-105`) writes a row unconditionally before the
transport, so 0 proves `:399` returned and 1 proves it did not. Tests asserting
no delivery use the seeding half **without** registering a respx route, because
`http_mock` is built with `assert_all_called=True` and a registered-but-unfired
route fails the test for the wrong reason.

### 6.2 File layout

All tests go in the existing `tests/test_shared_tasks_send_post.py`, which holds
**60** tests and the prelude they need. `move_object` and `move_post` belong
beside `send_post` for the same reason `make_reply` belonged beside `send_reply`:
they are the same module's activity builders and reuse its seeding.

### 6.3 The wrapper error paths

Use `_recording_task_session`, ported from
`tests/test_shared_tasks_send_reply.py` — **copied, not imported across test
modules**, which is this campaign's deliberate pattern. It wraps a **genuine**
`Session`, recording only `rollback` and `close`, and the recorded **order** is
the assertion: `['rollback', 'close']` proves `finally` ran after `except`, and a
happy-path control asserting `['close']` alone proves `finally` runs without an
exception. Patch `get_task_session` in the **`pages` module namespace** — the
name is imported there, and patching `app.utils` would miss the binding the
functions actually call, leaving the tests green while observing nothing.

## 7. Findings to register, not fix

Next free number is **D314** — confirm against the register rather than
trusting this line. Facts end at **123**; append from **124**.

1. **`:432`'s redundant `instance.online()` conjunct** — a **sixth** instance of
   D302's shape. `Community.following_instances()` (`app/models.py:842-851`)
   already filters `Instance.dormant == False` at `:849` and
   `Instance.gone_forever == False` at `:850`, and `Instance.online()`
   (`app/models.py:118-119`) is exactly `not (self.dormant or self.gone_forever)`.
   D302's cell currently names **five** sites (`pages.py:295`, `:350`, `:351`,
   `notes.py:217`, `notes.py:300`); verify that count before writing "sixth", and
   append in place per the precedent sub-projects 20 and 21 set.
2. **`move_post` never calls `patch_db_session`** — or does, and the register
   must say which. D312 names nine other unpatched task functions sharing
   `send_answer`'s shape. Establish this one **by reading `:373-387`**, and
   record it at its measured strength.
3. **The bare-`Exception` idiom across `app/shared/`** — `:396` is now the first
   narrow raise among eleven sites. Record the remaining ten with the argument
   that a caller cannot distinguish one failure from another, and name `:396` as
   the stated direction for later migration. This is the entry that keeps §5.2's
   inconsistency from reading as an oversight.

## 8. Global constraints

- **Delete nothing the task did not create. `claude_test` in the repository root
  is not the campaign's.**
- Only the controller runs the full suite, one pytest session at a time, and
  **in the foreground**.
- **`pytest` exits 1 on a session timeout and `run_tests.sh` propagates it**
  (`pytest.ini:26-27`). The campaign's old "still exits 0" claim was measured
  false; the observed exit 0 was a shell **pipeline** eating the status. Read
  `${PIPESTATUS[0]}`, or do not pipe. Continue to check the test count and the
  coverage report's mtime, which catch more than a timeout.
- **A wrong `--cov` target fails silently and green** (fact 117): `--cov` takes a
  module path, so a file path collects nothing, writes no JSON, and exits 0.
- Coverage command: `./run_tests.sh --cov=app --cov-branch --cov-report=json:scratch_full_cov.json -q`.
- **A wedged podman stack reports failures that are not regressions.** Run
  `./run_tests.sh --down` and retry before believing any failure.
- **Mutation instrument.** One mutation at a time, applied as a targeted
  single-line `sed`, never a whole-file rewrite. **Dry-run every substitution
  without `-i` and confirm the produced line before trusting it** — sub-project
  21 recorded a mutation that applied cleanly and changed nothing, and a
  nine-test kill that mislabelled which conjunct it negated. A mutation's kill
  count says nothing about whether the mutation was the one you meant.
  `pages.py` is **435 lines** before and after both production changes; assert
  the line count and an empty `git diff -- app/` after every apply and restore.
- **Neither production change may shift an existing line number.** Both edits to
  `pages.py` extend existing lines, and `TaskError` is appended after
  `app/utils.py`'s last line. After the changes, `wc -l app/shared/tasks/pages.py`
  is still 435, and every pre-existing `utils.py:NNN` citation still resolves —
  only `app/utils.py`'s own line count grows, from the end.
- Mutation record: the mutation, the test that killed it, **assertion-kill or
  crash-kill**, **sole or multi**. A mutant that applies but does not change the
  program is a **no-op substitution** — not a survivor, and not an equivalent
  mutant.
- **Commit the production changes before running the mutations**: the clean-tree
  assertion is impossible while a fix is uncommitted, because
  `git checkout -- app/` would discard it.
- **Use `ast.parse` and `FunctionDef.end_lineno` for extents, never a
  convention.**
- Every line number must be re-derived against the current tree before it is
  written down (fact 99). Citation sweeps run in two passes, `file:line` then
  bare paths against `git ls-files` (fact 100), and **read the first pass's
  output to the end**.
- **When a file shifts, sweep by the cause — the moved file — not by the topic
  that made you notice.**
- **Fix a citation's symbol, not just its number**, and the converse: a corrected
  symbol beside a stale number is still a wrong citation, and the correction
  makes the stale number look freshly checked.
- Enumerate conditional expressions by AST walk, not grep (fact 94) —
  coverage.py emits no arc for one (fact 87).
- Commit messages containing backticks are committed with `git commit -F <file>`,
  never `-m`.

## 9. Success criteria

1. `move_object`, `move_post`, `make_post` and `edit_post` are at zero uncovered
   statements and zero uncovered branch arms, except any proved unreachable with
   a written argument **naming its establisher**.
2. Both end-to-end paths — local Announce and remote direct post — are asserted
   on **serialized outbound bytes**, with `@context` asserted present on the
   outermost object and absent on the nested one.
3. `:393`'s false arm is covered by **two** tests, a non-`Community` `origin` and
   a non-`Community` `target`, so an `and`→`or` mutant dies.
4. `move_post`'s missing-post and deleted-post cases are **distinct named tests**.
5. Both wrapper arms are covered for `make_post` and `edit_post`, the error arm
   reached by a natural raise and witnessed by a recording `Session`.
6. Exactly two production changes land — the `private` conjunct at `:398` and
   `TaskError` at `:396` — each proved by a test that fails before it, with
   `pages.py` still **435 lines**, all 96 existing `pages.py` citations still
   valid, and all 358 existing `utils.py` citations still valid.
7. The register carries D314 onward, with `:432` handled per D302's precedent and
   the bare-`Exception` idiom recorded across its eleven sites.
8. `coverage_floors.ini`'s `app/shared/tasks/pages.py` entry is raised from 84 to
   the measured blended figure, floored to a whole percent.

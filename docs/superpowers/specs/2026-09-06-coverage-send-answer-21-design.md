# Sub-project 21: close `app/shared/tasks/notes.py`

**Date:** 2026-09-06
**Branch:** `blentz`
**Predecessor:** sub-project 20 (`636e3126..d081e810`, 17 commits)

## 1. Goal

Take `app/shared/tasks/notes.py` from 70.32% to a full close: cover
`send_answer` and the module's four Celery wrappers, land one production guard,
register what should not be fixed, and raise the module's floor.

Sub-project 20 covered `send_reply` (`:80-229`) and left it at 2 uncovered
statements and zero uncovered branch arms, both proved unreachable. Everything
else in the module is untouched.

## 2. Extent

All five extents below are AST `FunctionDef.lineno`/`end_lineno`, not a
convention. Coverage figures are from `scratch_full_cov.json`, written
2026-09-06 11:06:42 by a full `--cov=app --cov-branch` run of 3898 passed / 3
skipped / 6 subtests in 228.46s.

| function | extent | statements | missing | missing arms |
|---|---|---|---|---|
| `send_answer` | `:242-310` | 34 | 33 | 12 |
| `make_reply` | `:55-64` | 9 | 8 | 0 |
| `edit_reply` | `:68-77` | 9 | 8 | 0 |
| `choose_answer` | `:233-234` | 2 | 1 | 0 |
| `unchoose_answer` | `:238-239` | 2 | 1 | 0 |

51 statements and 12 branch arms. The module is 310 lines with 159 statements
and 53 missing; `send_reply` accounts for 2 of those, so these five account for
the other 51 exactly.

## 3. `send_answer`'s control flow

The twelve missing arms are these five decisions, and no others:

| line | decision | arms |
|---|---|---|
| `:248` | `post_reply.community.local_only or not post_reply.community.instance.online()` | `(248, 249)` return, `(248, 251)` continue |
| `:265` | `if is_undo:` — builds the `Undo` envelope at `:268-277` | `(265, 266)`, `(265, 279)` |
| `:279` | `if post_reply.community.is_local():` — Announce vs. direct post | `(279, 280)`, `(279, 303)` |
| `:280` | `if is_undo:` again, inside the local branch | `(280, 281)`, `(280, 284)` |
| `:299-300` | the follower loop and its inner four-conjunct guard | `(299, 300)`, `(299, 310)`, `(300, 299)`, `(300, 301)` |

`:265` and `:279` are independent, so there are **four end-to-end paths**, each
with a different wire shape:

- **local, choose** — `Announce` at `:290-298` wrapping the `ChooseAnswer`,
  posted per follower instance at `:301`.
- **local, undo** — `Announce` wrapping the `Undo` wrapping the `ChooseAnswer`.
- **remote, choose** — the bare `ChooseAnswer` posted to
  `post_reply.community.ap_inbox_url` at `:304`.
- **remote, undo** — the bare `Undo` posted to the same.

### 3.1 The `@context` asymmetry is the load-bearing assertion

`@context` is deleted from whichever object becomes an inner object, and kept on
the outermost one:

- `:266` deletes it from `lock` when `is_undo`, because `lock` is about to be
  nested inside `undo` at `:272`.
- `:281` deletes it from `undo`, and `:284` deletes it from `lock`, because
  whichever one it is is about to be nested inside `announce` at `:294`.
- The outermost object keeps the `@context` it was built with (`:259`, `:273`,
  `:295`).

Each of the four paths must be asserted for `@context` **present on the
outermost object and absent on every nested one**. Sub-project 19 got a claim
about `@context` wrong by reasoning about which deletes run rather than
measuring, and the amendment asserting an arm was unreachable was false because
`del create['@context']` ran on the ordinary path. Assert; do not argue.

### 3.2 The ternary at `:303`

An AST walk over the `send_answer` `FunctionDef` finds **exactly one**
conditional expression:

```
:303  undo if is_undo else lock
```

The four wrappers have none. This matters because coverage.py emits **no arc**
for a conditional expression (fact 87), so `:303` will show 0 missing arms
whether or not both arms run. Both arms need a named test asserting a value the
other arm could not produce. Per fact 94, this enumeration came from the walk,
not from grep, and the plan must re-run the walk and report its raw output
rather than confirming this expectation — sub-project 19's controller named
three ternaries where the walk found eight.

### 3.3 `is_local()` at `:279` decides silently

`Community.is_local()` (`app/models.py:795`) is a disjunction: `ap_id is None or
profile_id().startswith(SERVER_URL)`, and `profile_id()` falls back to a
computed default. So a community whose `ap_id` was never set is **silently
local**, and the remote path at `:302-304` is never taken. This is harness fact
112. Seeding must set `ap_id` deliberately for the remote cases, or two of the
four paths will go uncovered while the tests still pass.

## 4. Testing approach

### 4.1 Capture mechanism

Unchanged from sub-projects 19 and 20, and not negotiable: assert on the
**serialized outbound request bytes** via `http_mock`/respx, never on an
in-memory dict. The twin mutates the dict after delivery, so a dict assertion
can pass against bytes that never went out. `_sent_activity(route)` in
`tests/test_shared_tasks_send_reply.py:325` is the established reader.

Two supporting facts carry over. The autouse `socket.getaddrinfo` stub for
`.example` hosts is required because delivery reaches a real resolver at
`app/utils.py:5520` and fails open. `ActivityPubLog.query.count()` is the
early-return discriminator, because `post_request`
(`app/activitypub/signature.py:103-105`) writes a failure row unconditionally
before the transport — so a count of 0 proves the early return at `:249` fired,
and a count of 1 proves it did not.

### 4.2 File layout

Two files, split by what each wrapper wraps:

- **`tests/test_shared_tasks_send_answer.py`** (new) — `send_answer`,
  `choose_answer`, `unchoose_answer`.
- **`tests/test_shared_tasks_send_reply.py`** (existing, 41 tests) — gains
  `make_reply` and `edit_reply` tests, because both delegate to `send_reply`
  and can reuse that file's `_seed`/`_send` prelude rather than rebuilding
  reply seeding in a file about answers.

### 4.3 Wrapper error paths

Each of the four wrappers is `get_task_session()` then `try/except: rollback;
raise` / `finally: close`. `get_task_session` (`app/utils.py:3673-3675`) returns
`Session(bind=db.engine)` — an independent session, not `db.session`.

Cover both arms:

- **Happy path** — the wrapper's callee runs and the activity reaches the wire.
- **Error path by natural raise** — pass an id with no row, so
  `session.query(...).get()` returns `None` and the real callee raises
  `AttributeError` unaided. No faked exception: a monkeypatched sentinel would
  prove the wrapper handles a fake failure, and a refactor that stopped raising
  would leave the test green.

To witness `rollback()` and `close()` rather than assume them, wrap
`get_task_session` so it returns a **genuine** `Session` that records those two
calls. The session stays real; only the observation is added.

`patch_db_session` (`app/utils.py:3679`) no-ops inside a Flask request context
and patches otherwise. The `app` fixture pushes an **app** context, not a
request context, so `has_request_context()` is False and the patch is live in
tests. The wrappers' patched arm is therefore the one under test.

`choose_answer` (`:233-234`) and `unchoose_answer` (`:238-239`) differ only in
the `is_undo` argument they pass, so one test each proving the flag reaches the
wire is sufficient and complete.

## 5. The one production change

**At most one guard lands**, at `:248`, adding the `private` conjunct:

```
if post_reply.community.local_only or post_reply.community.private or not post_reply.community.instance.online():
```

This is the same fix sub-project 20 landed at `:143`, in the same file, for the
same finding. `Community.private` (`app/models.py:611`) is commented "only
members can view. no federation."

It must be **proved by a test that fails before the guard exists** — write the
test, watch it fail, then land the line. Committing the guard before running the
mutations is required (sub-project 19's Ruling 7): the clean-tree assertion is
impossible while the fix is uncommitted, because `git checkout -- app/` would
discard it.

Landing it takes D309's omitting sites from ten to **nine**, and the register
entry must be updated to say so rather than left claiming ten.

## 6. Findings to register, not fix

Next free number is **D312** — confirm against the register rather than trusting
this line.

1. **`:300`'s redundant `instance.online()` conjunct** — a **fifth** instance of
   D302's shape. `Community.following_instances()` (`app/models.py:842-851`)
   already filters `Instance.dormant == False` at `:849` and
   `Instance.gone_forever == False` at `:850`, and `Instance.online()`
   (`app/models.py:118-119`) is exactly `not (self.dormant or
   self.gone_forever)`. Verify the instance count in D302's cell before writing
   "fifth" — sub-project 20 appended the fourth in place rather than taking a
   new number, and this should follow that precedent unless the register says
   otherwise.

2. **`send_answer` never calls `patch_db_session`.** `make_reply` (`:58`) and
   `edit_reply` (`:71`) both wrap their callee in it; `send_answer` opens a task
   session at `:243` and uses it directly. Anything it reaches that touches
   `db.session` therefore gets the request-scoped session rather than the task
   session it just opened, so writes made through one are invisible to the
   other. Establish the severity by execution, not by argument, and record it at
   its measured strength.

3. **`object=` at `:282` and `:285` shadows the builtin.** Minor; recorded
   because the register tracks reading-level findings and this one costs a
   reader a double-take in a function that already has two `is_undo` branches.

## 7. Global constraints

- **Delete nothing the task did not create. `claude_test` in the repository root
  is not the campaign's.**
- Only the controller runs the full suite, one pytest session at a time, and
  **in the foreground**.
- **`pytest` exits 1 on a session timeout and `run_tests.sh` propagates it**
  (`pytest.ini:26-27`). The campaign's long-standing "still exits 0" claim was
  measured false on 2026-09-06 and is corrected in fact 118; the observed exit 0
  was a shell **pipeline** eating the status. Read `${PIPESTATUS[0]}`, or do not
  pipe. Continue to check the test count and the coverage report's mtime, which
  catch more than a timeout.
- **A wrong `--cov` target fails silently and green** (fact 117, still true and a
  different trap): `--cov` takes a module path, so
  `--cov=app/shared/tasks/notes.py` warns `module-not-imported`, collects
  nothing, writes no JSON, and exits 0. The dotted form works.
- Coverage command: `./run_tests.sh --cov=app --cov-branch --cov-report=json:scratch_full_cov.json -q`.
- **A wedged podman stack reports failures that are not regressions.** Run
  `./run_tests.sh --down` and retry before believing any failure.
- **Mutation instrument.** One mutation at a time, applied as a targeted
  single-line edit (`sed -i 'NNNs/old/new/'`), never a whole-file rewrite.
  `app/shared/tasks/notes.py` is **310 lines** before the guard and **310
  lines** after it, because the guard extends an existing line rather than
  adding one; assert the line count and an empty `git diff -- app/` after every
  apply and every restore. A sub-project-18 implementer truncated a 1174-line
  production module at this step.
- Mutation record: the mutation, the single test that killed it,
  **assertion-kill or crash-kill**, **sole or multi**.
- **Use `ast.parse` and `FunctionDef.end_lineno` for extents, never a
  convention.**
- Every line number copied from anywhere must be re-derived against the current
  tree before it is written down (fact 99). Citation sweeps run in two passes,
  `file:line` then bare paths against `git ls-files` (fact 100), and **read the
  first pass's output to the end** — `run_tests.sh:21` was missed in
  sub-project 20 because a two-hit grep was read as one.
- **When a file shifts, sweep by the cause (the moved file), not by the topic
  that made you notice.** Sub-project 20's Ruling 6 scoped a sweep by subject
  and missed ~48 citations; its reversal is the rule.
- **Fix a citation's symbol, not just its number.** Sub-project 20 found a cell
  naming `notify_about_post_task` where the line was inside
  `notify_about_post_reply`; renumbering alone would have left it wrong with
  fresh-looking numbers.
- Enumerate conditional expressions by AST walk, not grep (fact 94) —
  coverage.py emits no arc for one (fact 87).
- Commit messages containing backticks are committed with `git commit -F <file>`,
  never `-m`.

## 8. Success criteria

1. `tests/test_shared_tasks_send_answer.py` exists and calls `send_answer`
   directly; `tests/test_shared_tasks_send_reply.py` gains `make_reply` and
   `edit_reply` coverage.
2. All five functions at zero uncovered statements and zero uncovered branch
   arms, except any proved unreachable with a written argument **naming its
   establisher**.
3. All four `is_undo` × `is_local` paths asserted on **serialized outbound
   bytes**, with `@context` asserted present on the outermost object and absent
   on every nested one, per path.
4. `:303`'s ternary has both arms exercised by named tests, reconciled by an AST
   walk rather than by the coverage number.
5. The follower loop's three arms — never entered, guard-continue, guard-send —
   each covered by a named test.
6. Both wrapper arms covered for all four wrappers, the error arm reached by a
   natural raise and witnessed by a recording `Session`.
7. At most one production guard lands, at `:248`, proved by a test that fails
   before it; D309's site count updated from ten to nine.
8. The register carries D312 onward, with `:300` handled per D302's precedent.
9. `coverage_floors.ini`'s `app/shared/tasks/notes.py` entry is raised from 70
   to the measured blended figure, floored to a whole percent.

# Sub-project 23: close `app/shared/tasks/adds.py` and `removes.py`

**Date:** 2026-09-07
**Branch:** `blentz`
**Predecessor:** sub-project 22 (`c6ed020a..401505d4`, 20 commits)

## 1. Goal

Close both twin modules: cover `add_object`/`remove_object` and their four Celery
wrappers, land the `private` conjunct in each, register what should not be
fixed, and add two floors.

This is the campaign's first sub-project to take **two modules at once**, and its
first to **add** floors rather than raise existing ones.

## 2. Extent, and the equivalence that makes it one design

All extents are AST `FunctionDef.lineno`/`end_lineno`. Coverage is from
`scratch_full_cov.json`, written 2026-09-06 21:07:39 by a full
`--cov=app --cov-branch` run of 3941 passed / 3 skipped / 6 subtests in 293.83s.

Both files are **100 lines, 51 statements, 40 missing, 18.03%**.

| function | extent | missing | arms |
|---|---|---|---|
| `sticky_post` / `unsticky_post` | `:27-38` | 10 | 0 |
| `add_mod` / `remove_mod` | `:42-53` | 10 | 0 |
| `add_object` / `remove_object` | `:56-100` | 20 | 10 |

**The twins are structurally identical.** After normalising names, the only
textual difference is one docstring word. Their coverage residuals are the same
lines and the same arcs, function for function:

```
missing  [57, 58, 59, 61, 63, 64, 66, 67, 68, 69, 81, 82, 84, 85, 86, 87, 96, 97, 98, 100]
arms     [58,59] [58,61] [63,64] [63,66] [81,82] [81,100] [96,-56] [96,97] [97,96] [97,98]
```

That equivalence is what makes one design cover two modules — and it is itself
worth asserting, so that a future divergence is a detectable event rather than a
later discovery. **Re-derive the residuals before relying on them**; this spec's
figures are a measurement, not a guarantee.

## 3. The builder's control flow

`add_object(session, user_id, object, community_id=None)` is `move_object`'s
shape with one decision added. The ten arms are exactly:

| line | decision | arms |
|---|---|---|
| `:58` | `if not community_id:` — community from the object (`:59`) or looked up (`:61`) | `(58, 59)`, `(58, 61)` |
| `:63` | `community.local_only or not community.instance.online()` | `(63, 64)` return, `(63, 66)` continue |
| `:81` | `community.is_local()` — Announce vs. direct post | `(81, 82)`, `(81, 100)` |
| `:96-97` | follower loop and its four-conjunct guard | `(96, -56)` never entered, `(96, 97)`, `(97, 96)` continue, `(97, 98)` deliver |

Two end-to-end paths: **local** deletes `add['@context']` at `:82` and posts the
`Announce` built at `:87-95` per follower at `:98`; **remote** posts the bare
`Add` to `community.ap_inbox_url` at `:100`.

`:61` uses `.filter_by(id=community_id).one()`, so a bad `community_id` raises
`NoResultFound` — it does not return `None`.

### 3.1 The ternary at `:74` pairs with the wrappers

An AST walk over both files finds **exactly one** conditional expression each,
at `:74`:

```
community.ap_moderators_url if community_id else community.ap_featured_url
```

coverage.py emits **no arc** for a conditional expression (fact 87), so `:74`
reports zero missing arms whether or not both arms run. Both need named tests.

The pairing is the elegant part and the plan should exploit it: `sticky_post`
calls `add_object(session, user_id, post)` with **no** `community_id`, taking
`:58`'s true arm and `:74`'s false arm (`ap_featured_url`, `app/models.py:604`);
`add_mod` calls with one, taking `:58`'s false arm and `:74`'s true arm
(`ap_moderators_url`, `:605`). The `target` field on the wire witnesses both at
once.

The plan must still **re-run the walk and report its raw output** rather than
repeating this (fact 94). Sub-project 21's spec claimed a function had no
ternaries by inspection and the walk found one.

### 3.2 `is_local()` at `:81` decides silently

`Community.is_local()` (`app/models.py:795`) is a disjunction whose
`profile_id()` falls back to a computed default, so a community whose `ap_id`
was never set is **silently local** and `:100` never runs. Seeding must set
`ap_id` **and** `ap_profile_id` for the remote case (fact 112).

## 4. Two lookup styles, twelve lines apart, in one file

Both twins carry both styles, at the same lines:

- `:32` — `session.query(Post).get(post_id)` returns `None` for a missing id, so
  `add_object` then reads `object.community` at `:59` and raises
  **`AttributeError`**.
- `:47` — `session.query(User).filter_by(id=mod_id).one()` raises
  **`NoResultFound`**.

This campaign has now met this trap in three modules. Sub-project 21 lost a fix
round assuming one function's style applied to its neighbour; sub-project 22's
plan told the implementer to read rather than inherit, and it did. Here the two
styles are in **the same file, twelve lines apart**, so the plan must require
each wrapper's error mechanic to be established separately, by reading.

## 5. The two production changes

The `private` conjunct at `adds.py:63` and `removes.py:63`:

```
if community.local_only or community.private or not community.instance.online():
```

**This is one fix applied twice, not two independent changes.** The twins are
structurally identical; guarding one and not the other would manufacture exactly
the kind of divergence this campaign hunts for. Each must be proved by a test
that fails before it — write the test, watch it fail, then land the line.

`Community.private` (`app/models.py:611`) is commented "only members can view.
no federation." The shape matches `notes.py:248` and `pages.py:398`, both of
which already test `online()`; it does **not** match `pages.py:153`, which is
`local_only or private` with no `online()` check.

Landing both takes D309 from eight open sites to **six**: `blocks.py:104`,
`deletes.py:127`+`:130`, `flags.py:57`, `groups.py:59`, `likes.py:60`,
`locks.py:89`.

Both edits extend existing lines. **Neither file may change its line count** —
both stay at **100 lines** — so no citation shifts.

## 6. Testing approach

### 6.1 Capture mechanism

Unchanged and not negotiable: assert on **serialized outbound request bytes**
via `http_mock`/respx, never on an in-memory dict, because the code mutates
those dicts after delivery (`:82`'s `del`). `ActivityPubLog.query.count()` is the
early-return discriminator — `post_request`
(`app/activitypub/signature.py:103-105`) writes a row unconditionally before the
transport, so 0 proves `:64` returned and 1 proves it did not. Tests asserting no
delivery must seed a deliverable community **without** registering a respx route,
because `http_mock` is built with `assert_all_called=True`.

### 6.2 File layout

**One file, `tests/test_shared_tasks_add_remove.py`**, with a shared prelude and
each test **written out separately** for adds and removes — not parametrised
across the twins.

Written out, because a parametrised failure names the parameter rather than the
function, and an arm covered under only one parameter is invisible in the
failure output. That is the same reasoning that kept `make_post`'s and
`edit_post`'s error tests separate in sub-project 22.

One file rather than two, because the twins' equivalence is the thing being
asserted and a duplicated ~250-line prelude would be the largest copy-paste the
campaign has accepted. This deviates from the one-file-per-module convention the
`send_post`/`send_reply`/`send_answer` files established; the deviation is
deliberate and should be recorded as such.

### 6.3 The wrapper error paths

Use `_recording_task_session`, ported from an existing test file — **copied, not
imported across test modules**, which is this campaign's deliberate pattern. It
wraps a **genuine** `Session`, recording only `rollback` and `close`, and the
recorded **order** is the assertion: `['rollback', 'close']` proves `finally` ran
after `except`, and a happy-path control asserting `['close']` alone proves
`finally` runs without an exception.

**It must patch `get_task_session` in each twin's own module namespace** — both
files import the name into their own namespace, so patching `app.utils` would
miss the binding the wrappers actually call and leave the tests green while
observing nothing. Two patch targets, one per twin.

Error paths use a **natural raise** — a real bad id, not a monkeypatched
sentinel, which would prove the handler catches a fake and stay green through a
refactor that stopped raising.

## 7. Findings to register, not fix

Next free number is **D316** — confirm against the register. Facts end at
**128**; append from **129**.

1. **`:97` in both twins** — D302's **seventh and eighth** instances.
   `Community.following_instances()` (`app/models.py:842-851`) already filters
   `Instance.dormant == False` at `:849` and `Instance.gone_forever == False` at
   `:850`, and `Instance.online()` (`:118-119`) is exactly
   `not (self.dormant or self.gone_forever)`. D302's cell currently names **six**
   (`pages.py:295`, `:350`, `:351`, `notes.py:217`, `:300`, `pages.py:432`) —
   **verify that count** before writing "seventh", and append in place per the
   precedent sub-projects 20-22 set.
2. **`post_request` is imported and never used, in both twins** (`:2` of each).
   Only `send_post_request` is called. A reading-level finding, and evidence for
   the equivalence claim: the twins share even their dead imports.
3. **The two lookup styles twelve lines apart**, `:32` against `:47`, in both
   twins — with the argument that a test reaching an error path by "pass a
   missing id" must ask which style the function uses, and that this is the third
   module in which the campaign has met it.
4. **The twins' exact structural equivalence**, recorded so a future divergence
   is detectable. Name what was compared and how.

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
- **Test counts come from collection, never from `grep -c '^def test_'`** — the
  campaign has been bitten by that gap three times.
- **A wedged podman stack reports failures that are not regressions.** Run
  `./run_tests.sh --down` and retry before believing any failure.
- **Mutation instrument.** One mutation at a time, applied as a targeted
  single-line `sed`, never a whole-file rewrite. **Dry-run every substitution
  without `-i` and confirm the produced line before trusting it** — a plan's
  `sed` is untested code, and three written across sub-projects 21-22 were
  defective, one of which did not parse. **Every mutation must be run against
  BOTH twins**: the files are identical, so a mutation that kills in one and
  survives in the other is a divergence in the tests, not in the code, and it is
  the only signal that would catch a test-design bug replicated across both.
  Each file is **100 lines** before and after; assert the line count and an empty
  `git diff -- app/` after every apply and restore.
- Mutation record: the mutation, the test that killed it, **assertion-kill or
  crash-kill**, **sole or multi**, and **as-of which commit**. A mutant that
  applies but does not change the program is a **no-op substitution** — not a
  survivor and not an equivalent mutant; one that does not parse is neither.
- **Commit the production changes before running the mutations**: the clean-tree
  assertion is impossible while a fix is uncommitted.
- **Use `ast.parse` and `FunctionDef.end_lineno` for extents, never a
  convention.**
- Every line number must be re-derived against the current tree before it is
  written down (fact 99). Citation sweeps run in two passes, `file:line` then
  bare paths against `git ls-files` (fact 100), and **read the first pass's
  output to the end**.
- **When a file shifts, sweep by the cause — the moved file — not by the topic
  that made you notice.**
- **Fix a citation's symbol, not just its number**, and the converse: a corrected
  symbol beside a stale number is still a wrong citation.
- **Where a total drifts, lead with the invariant.** A `--stat` total over a
  range ending at `HEAD` moves whenever anything is appended; a per-file
  `--numstat` of modified-only lines is a property of the change itself.
- **Anchor every `HEAD`-relative number to the commit it was true at.**
  Sub-project 22 spent two fix rounds removing un-anchored numbers and then
  planted one nine lines from its own edits.
- Enumerate conditional expressions by AST walk, not grep (fact 94) —
  coverage.py emits no arc for one (fact 87).
- Commit messages containing backticks are committed with `git commit -F <file>`,
  never `-m`.

## 9. Success criteria

1. All six functions across the two twins are at zero uncovered statements and
   zero uncovered branch arms, except any proved unreachable with a written
   argument **naming its establisher**.
2. Both end-to-end paths — local `Announce` and remote direct post — are asserted
   on **serialized outbound bytes** for **each** twin, with `@context` asserted
   present on the outermost object and absent on the nested one.
3. `:74`'s ternary has both arms exercised by named tests in each twin,
   reconciled by an AST walk rather than by the coverage number, and the `target`
   field asserted as `ap_featured_url` on one arm and `ap_moderators_url` on the
   other.
4. `:58`'s two arms are covered, and the `NoResultFound` from `:61` is reached by
   a natural raise.
5. Each wrapper's error mechanic is established **by reading** and asserted
   accordingly — `AttributeError` for the `.get()` path, `NoResultFound` for the
   `.one()` path — with both arms witnessed by a recording `Session`.
6. Exactly two production changes land, the `private` conjunct at `adds.py:63`
   and `removes.py:63`, each proved by a test that fails before it, with both
   files still **100 lines**.
7. Every mutation is run against **both** twins, and any kill/survive asymmetry
   between them is reported as a finding.
8. The register carries D316 onward, with `:97` handled per D302's precedent and
   the twins' equivalence recorded.
9. `coverage_floors.ini` gains entries for both modules at their measured
   figures, floored to whole percents.

---

## Appended 2026-09-07 by sub-project 23's register round (Task 8) -- one figure in this document is superseded

**This document says the twins' only textual difference, after normalising names,
is "one docstring word". That is short of the truth in both directions, and the
corrected form is in the register (`docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`,
sub-project 23's section, subsection 3) and in the module docstring of
`tests/test_shared_tasks_add_remove.py`.** Measured rather than summarised:
`diff app/shared/tasks/adds.py app/shared/tasks/removes.py` reports exactly
**eight** hunks, all of them name substitutions, and **two of them are string
literals** (`'Add'`/`'Remove'` and the `/activities/add/`-vs-`/activities/remove/`
path segment) rather than a docstring word. Conversely, once the docstring word
`Add:`/`Remove:` is normalised along with everything else, the two files are
**byte-identical** -- there is no residual difference at all. The original claim
was written by reading the diff and summarising it, and the summary lost two
hunks; registering the equivalence as **D318** is what forced it to be counted.

**A second figure is dated rather than wrong**: this document's "coverage
residuals are the same lines and the same arcs" was true when written, and both
twins now measure **100.0000%** with zero missing statements and zero partial
branches, so the residual is empty in both. The ten arcs it lists are the ten
**executed** arcs at this commit.

**Appended rather than rewritten**, in D275's two-way-pointer form and per
sub-project 22's precedent: no line above this annotation moved, so every
citation into this document still resolves, and a reader who opens this document
first is sent to the register.

# Sub-project 20 — `send_reply`, and what it does not do that its twin does

**Date:** 2026-09-06
**Branch:** `blentz`
**Target:** `app/shared/tasks/notes.py`, function `send_reply` (`:80-229`)
**Predecessor:** sub-project 19 (`docs/superpowers/specs/2026-09-06-coverage-send-post-19-design.md`), which took
`send_post` to two unreachable statements and three unreachable arms and raised `app/shared/tasks/pages.py`
from 51.38% to 84.91%.

---

## 1. Why this function

`app/shared/tasks/notes.py` is 10.05% covered. `send_reply` (`:80-229`) is **134 uncovered** — 86 statements
and 48 branch arms — which is near-exact parity with the 125 sub-project 19 closed in `send_post`.

It is also `send_post`'s structural twin: the same mention scan, the same four-ish early returns, the same
Create/Announce construction, the same per-instance fan-out. Sub-project 19 mapped one of the pair
exhaustively. **This sub-project tests the other, and the differences between them are the findings.**

Five divergences are already visible. Two are candidate defects, two are simplifications that make this
function easier to test than its twin, and one is a hazard sub-project 19 proved and this file repeats.

---

## 2. The five divergences

### 2.1 `reply.body` is scanned unguarded — the strongest candidate, and it must be measured before it is claimed

| | |
|---|---|
| `app/shared/tasks/pages.py:97` | `if post.body:` guards the mention scan |
| `app/shared/tasks/notes.py:92` | `re.finditer(pattern, reply.body)` — **no guard** |

`PostReply.body` is `db.Column(db.Text)` (`app/models.py:2901`) with no `nullable=False`, so `None` is a
storable value, and `re.finditer` against `None` raises
`TypeError: expected string or bytes-like object, got 'NoneType'`.

**Whether a `None`-bodied reply can actually reach `send_reply` is the plan's first job, and it must be
established by execution, not by argument.** The local path assigns
`body=piefed_markdown_to_lemmy_markdown(content)` (`app/shared/reply.py:182`); the federated path runs through
`PostReply.new` (`app/models.py:2995`). Either could plausibly produce `None`, and neither is proved to.

If it is reachable, this is a live crash and the sub-project's headline fix. If it is not, it is a latent
hazard and gets registered as one. **Do not write the spec's conclusion in advance of the measurement** — this
campaign has twice registered a severity it had to correct.

### 2.2 The private gate — nine senders of ten omit it, and the leak is latent

`Community.private` (`app/models.py:611`) carries the comment **"only members can view. no federation."**

`app/shared/tasks/pages.py:153` is `if community.local_only or community.private: return`. It is the **only**
one of the ten senders in `app/shared/tasks/` that tests `private`. `notes.py:143` is
`if community.local_only or not community.instance.online(): return` — the flag is absent, and the same is
true of `adds.py:63`, `blocks.py:104`, `flags.py:57`, `groups.py:59`, `likes.py:60`, `locks.py:89`,
`removes.py:63` and `notes.py:248` (`send_answer`).

**The leak is latent, and the reason matters.** Both writers of `Community.private` couple it to `local_only`
in the view layer: creation sets `form.local_only.data = True` at `app/community/routes.py:103-104` and passes
it at `:121`; the edit path does the same at `:1230-1231` and `:1245`. So no current UI path produces
`private=True, local_only=False`, and `:143`'s `local_only` test already catches every private community that
exists. Nothing at the model or database level enforces that coupling.

So `pages.py:153`'s extra conjunct is **defence in depth that nine siblings lack**, not a live leak. A test can
seed the uncoupled state directly and prove a guard works, but it tests a state no UI path currently produces,
and it must be labelled that way. This is sub-project 16's latent-versus-live problem; its precedent is an
equivalence argument plus a re-run of the accumulated mutations rather than a claim of a live fix.

### 2.3 `:217`'s `instance.online()` is redundant — the shape sub-project 19 registered

`:216` calls `community.following_instances()` with the default `include_dormant=False`, so
`app/models.py:849-850` filters `Instance.dormant == False` and `Instance.gone_forever == False` in SQL, while
`Instance.online()` (`:118-119`) is exactly `not (dormant or gone_forever)`. The conjunct cannot be False.

Sub-project 19 registered three instances of this shape with one structural cause. This is a fourth, in a
second file, which strengthens the finding rather than repeating it — the plan should record it that way.

### 2.4 The `@context` `del`/re-add pair repeats a proved equivalent mutant

`notes.py:203` is `del create['@context']` and `:225` is `if '@context' not in create:`. Sub-project 19
established that the matching pair in `send_post` yields an **equivalent mutant**: `post_request`
(`app/activitypub/signature.py:100-101`) re-adds `@context` when a body lacks it, with the same value in the
same trailing position, before serialization — so neither arm of the re-add is observable.

The plan must not spend a fix round rediscovering this. Assert **key order** as sub-project 19 did, and say in
the docstring what cannot be pinned.

### 2.5 Two simplifications that make this function easier to test than its twin

- **No amendment block.** `send_post` rewrites its Page into a Note at `:309-330`, which is where its aliasing
  hazard lives. `send_reply` is already a Note, so `:225-229` only re-adds `@context` and fans out. Sub-project
  19's aliasing finding **cannot arise here**, and its capture-on-serialized-bytes decision is therefore less
  load-bearing — though the plan should keep it, for consistency and because `:203`'s `del` still mutates.
- **No `if not community.local_only:` re-check.** `send_post` re-tests the flag at `:270` and `:333`, both of
  which are dead because `:153` already returned. `send_reply` has no such re-check, so **it does not inherit
  those two unreachable arms**.

---

## 3. Scope

**134 uncovered** in `send_reply` (`:80-229`), 86 statements and 48 branch arms, to zero on both metrics
except arms proved unreachable.

Three conditional expressions live in the function — `:129`, `:185`, `:187` — enumerated by AST walk.
coverage.py emits no arc for one (`tests/README.md` fact 87), so the zero above cannot see them and each needs
both arms exercised by named tests.

### Explicitly out of scope

`send_answer` (`:242-310`, 45 uncovered) is poll-answer federation — a distinct concern that would dilute the
twin comparison. `make_reply`, `edit_reply`, `choose_answer` and `unchoose_answer` are thin Celery wrappers
totalling 18. All five stay for a later sub-project, and `send_answer` carries the same private-gate omission
at `:248`, which the register must note.

---

## 4. The fix

**One fix, and which one depends on §2.1's measurement.**

If a `None`-bodied reply can reach `send_reply`, guard `:92` to match `pages.py:97` — test-first, with the
failing test producing the exact `TypeError`. If it cannot, register it as latent and instead land the private
gate at `:143`, on the equivalence-argument footing §2.2 describes.

**Land at most one production change.** The campaign's pattern is that a fix earns its place by a failing test;
a second speculative guard in the same commit dilutes what the mutations prove.

**Only `app/shared/tasks/notes.py` is touched.** The other eight senders missing the private gate stay
registered with the arbitration attached, exactly as sub-project 18 did for D286's remaining sites and
sub-project 19 for D298's.

**Mutation discipline**, unchanged and non-negotiable: each conjunct mutated separately, one at a time, each
killed by a distinct named test, kills labelled assertion-kill or crash-kill and sole or multi. Apply each
mutation with a targeted single-line edit — never a whole-file rewrite. `app/shared/tasks/notes.py` is
**310 lines**; assert that after every apply and every restore, together with an empty `git diff -- app/`.
**Commit the fix before running the mutations** — sub-project 19's Ruling 7 established that the clean-tree
assertion is impossible while the fix is uncommitted.

---

## 5. Harness

New file: **`tests/test_shared_tasks_send_reply.py`**. No test drives `send_reply` today.

Nearly everything transfers from `tests/test_shared_tasks_send_post.py`, and the plan should say so rather than
re-deriving it:

- **Entry.** `send_reply(reply_id, parent_id, edit=False, session=None)` at `:80`. `session` has no usable
  default — `:81` dereferences it immediately. `parent_id` selects the parent: truthy loads a `PostReply`
  (`:84`), falsy uses `reply.post` (`:86`). Both are branch arms.
- **`recipients` is seeded, not empty.** `:90` is `recipients = [parent.author]`, where `send_post` starts from
  `[]`. So the parent's author is always a recipient, and `:120`'s
  `if recipient.is_local() and recipient.id != parent.author.id:` then excludes them from the mention
  notification. That interaction has no analogue in the twin and needs its own tests.
- **Stopping before the network** and the capture mechanism follow sub-project 19: a locally-seeded community
  with no following instances runs the whole builder; `_remote_inbox`-style helpers assert on **serialized
  request bytes**; an autouse fixture stubs `socket.getaddrinfo` for `.example` hosts because the delivery path
  reaches a real resolver at `app/utils.py:5520`, failing open.
- **`http_mock` is `assert_all_called=True`** (`tests/conftest.py:288-295`): a registered route never reached
  fails the test.
- **Seeding order is load-bearing.** `make_community` hardcodes `instance_id=1` and `tests/conftest.py:143`
  truncates with `RESTART IDENTITY`, so the local instance must be created before any peer (fact 89's
  extension). `Community.is_local()` is a **disjunction** — `ap_id is None` **or** the `profile_id()` prefix —
  so making a community remote means setting `ap_profile_id` too, not `ap_id` alone.

---

## 6. Findings register

New findings are numbered from **D309**, appended to
`docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`. Establish the next free number with a grep
before writing it; sub-project 18 used the wrong number throughout for want of that check.

The register must record the private-gate asymmetry once, listing all nine omitting sites, with its severity
stated as latent and the view-layer coupling named as the reason.

---

## 7. The coverage floor

`app/shared/tasks/notes.py` has **no entry** in `coverage_floors.ini`. It is 10.0457% blended today. The plan
must re-measure after the tests land and add `app/shared/tasks/notes.py = <measured, floored>` in the file's
existing ordering. Floors only ever rise.

---

## 8. Global constraints

Copied verbatim into the plan; they bind every task.

- **Delete nothing the task did not create. `claude_test` in the repository root is not the campaign's.**
- Only the controller runs the full suite, one pytest session at a time, and **in the foreground**. Sub-project
  19's Ruling 19: five background full-suite runs were killed by the harness's background-task memory guard
  while the system had 18Gi free and zero swap in use; every foreground run succeeded. The 407s suite fits
  inside the 600s tool timeout.
- **A wedged podman stack reports test failures that are not regressions.** Sub-project 19 saw an 8-file probe
  report "6 failed, 65 passed, 1 error in 618.53s" where the same files on a reset stack give 629 passed in
  29.07s — the wedged run had collected 72 tests where the files hold 629. Before believing any failure, run
  `./run_tests.sh --down` and retry.
- Any run at or over ~600s is erroneous — that is `session_timeout` in `pytest.ini:28`, and pytest **still
  exits 0** on a session timeout, so a truncated run reads as green. Check the test count and the coverage
  report's mtime, never the exit code.
  **[CORRECTED 2026-09-06 by sub-project 20's final review, marked here rather than rewritten because a design/plan is a dated record of what implementers were dispatched with: "still exits 0" IS FALSE. pytest exits 1 on a session timeout and `run_tests.sh` propagates it, exactly as `pytest.ini:26-27` says. The observed exit 0 came from PIPING pytest through `grep`/`tail`, which makes `$?` the pipe's last element; `${PIPESTATUS[0]}` recovers it. Keep checking the count and the mtime -- but check the exit code TOO. Measured four ways in `tests/README.md` fact 118.]**
- Coverage command: `./run_tests.sh --cov=app --cov-branch --cov-report=json:scratch_full_cov.json -q`.
- Mutation discipline as §4 states it, including the line-count assertion after every restore.
- **Use `ast.parse` and `FunctionDef.end_lineno` for a function's extent, never a convention.** Sub-project 19
  committed three different extents for one function across its own documents because three different rules
  were applied, and every document was internally consistent.
- Every line number copied from anywhere must be re-derived against the current tree before it is written down
  (fact 99). Citation sweeps run in two passes, `file:line` then bare paths against `git ls-files` (fact 100),
  and **grep for the value being corrected, not for the text you are editing**.
- Enumerate conditional expressions by AST walk, not by grep (fact 94).
- Commit messages containing backticks are committed with `git commit -F <file>`, never `-m`.

---

## 9. Success criteria

1. `tests/test_shared_tasks_send_reply.py` exists and calls `send_reply` directly.
2. `send_reply` (`:80-229`) is at zero uncovered statements and zero uncovered branch arms, except any arm
   proved unreachable with a written argument naming its establisher.
3. Both arms of `:83`'s `parent_id` test are exercised, and the `recipients = [parent.author]` seeding's
   interaction with `:120`'s exclusion is covered by named tests.
4. All three conditional expressions — `:129`, `:185`, `:187` — have both arms exercised by named tests,
   reconciled by an AST walk rather than by the coverage number.
5. §2.1's reachability is established by execution and the finding's severity recorded accordingly.
6. At most one production guard lands, proved by a test that fails before it.
7. The register carries D309 onward, with the private-gate asymmetry recorded once across all nine omitting
   sites and its severity stated as latent.
8. `coverage_floors.ini` gains an `app/shared/tasks/notes.py` entry at the measured floor.
9. The full suite is green, run in the foreground, with the pass/skip counts and the coverage report's mtime
   recorded.

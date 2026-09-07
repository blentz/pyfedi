# Sub-project 25: closing `app/shared/tasks/locks.py` and `app/shared/tasks/likes.py`

Design document. Branch `blentz`. Follows sub-project 24, which closed
`app/shared/tasks/flags.py` and `app/shared/tasks/users.py` to 100.0% each
(25 commits, `896150f1..5f90fe63`).

## 1. Scope, and the phase boundary that defines it

Two modules, ~199 statements and 54 branches — roughly double sub-project 24.
The user chose the pairing after being shown that size, and chose a **phased**
structure rather than a flat one. That structure is load-bearing, not
cosmetic:

**Phase A closes `locks.py`, measures it, and sets its floor. Phase B does not
begin until Phase A's floor is in `coverage_floors.ini`.**

The reason is that `likes.py` is materially harder than its statement count
suggests, and the checkpoint converts "a half-finished pair" into "one module
closed and floored, plus a known remainder" if it proves worse than it reads.
A plan that interleaves the two modules has no such stopping point.

Phase A first is also the right order pedagogically. `locks.py` is the sixth
instance of the two-wrappers-plus-shared-handler shape this campaign has closed
five times, so its harness transfers. `likes.py` shares that shape only at the
surface.

## 2. Baselines

Measured from `scratch_full_cov.json` at `5f90fe63` (mtime 2026-09-07 13:45:42).
Re-derive before starting.

| Module | Lines | Statements | Branches | Partial | Missing | Percent |
|---|---|---|---|---|---|---|
| `app/shared/tasks/locks.py` | 145 | 82 | 12 | 0 | 67 | 15.9574% |
| `app/shared/tasks/likes.py` | 236 | 117 | 42 | 0 | 103 | 8.8050% |

Target: zero missing statements and zero partial branches in both, or a residual
proved unreachable with the proof documented.

Coverage takes the **dotted module form** (`--cov=app.shared.tasks.locks`). A
path form collects nothing, writes no JSON, and exits 0. The JSON lands *inside*
the `pyfedi_test-runner` container; retrieve it with `podman cp` (fact 137).

## 3. Phase A — `locks.py`

Four `@celery.task` wrappers delegating to one handler:

```
:26  lock_post(send_async, user_id, post_id)              :31  .get(post_id)
:41  unlock_post(...)                    is_undo=True     :46  .get(post_id)
:56  lock_post_reply(...)                                 :61  .filter_by(id=...).one()
:71  unlock_post_reply(...)              is_undo=True     :76  .filter_by(id=...).one()
:85  lock_object(session, user_id, object, is_undo=False)
:89      if community.local_only or not community.instance.online(): return
:95-104  the Lock envelope
:106-118 if is_undo: del lock['@context']; wrap in an Undo
:120     if community.is_local():  -> Announce, delivered per following instance
:140-142     for instance in community.following_instances():
:141             if instance.inbox and instance.online()
                    and not user.has_blocked_instance(instance.id)
                    and not instance_banned(instance.domain):
:143-145 else: send the Lock or Undo straight to community.ap_inbox_url
```

### 3.1 What transfers, and what is new

All four wrappers use `patch_db_session` and share the identical
`except`/`finally` tail, so `_recording_task_session` applies — as a **sixth**
copy. D324 registered that count at five; this makes six, and the entry must be
updated rather than left stale.

**The `is_undo` dimension is new.** Every path through `lock_object` exists in
two variants, and the two are not symmetric: `is_undo` deletes `@context` from
the Lock at `:107` *before* nesting it, then deletes it again from the Undo at
`:122`. The non-undo local path deletes from the Lock at `:125`. So the same
key is removed at three different sites depending on the path taken.

**`locks.py` mutates its dicts IN PLACE** (`del lock['@context']`), where
`likes.py` copies first (`vote_public.copy()` then `del`). That difference is
worth stating in both phases' test files, because it is exactly why this
campaign asserts on **serialized outbound bytes** rather than on an in-memory
dict: a recorder holding `lock` would be read back after the `del`.

### 3.2 Fact 136's meaningful half, finally exercised

`flags.py` had no Announce wrapper, so any `@context` assertion on its payload
was vacuous — `signature.py:100-101` reinjects top-level only, which for an
unwrapped activity is exactly where the builder already wrote it.

`locks.py` **does** wrap. When `community.is_local()`, the delivered object is an
Announce whose `object` is a Lock or Undo with `@context` deleted. So
**"no `@context` on the nested object" is a real, discriminating assertion here**,
and it is the first chance this campaign has had to assert it since sub-project
23. Assert it on all four local paths. State in the test file why the same
assertion would have been worthless one sub-project ago.

### 3.3 The gate at `:89` — D309's site

`if community.local_only or not community.instance.online():` omits
`community.private`. Production change 1 adds it. **Place `private` before the
`online()` call**, matching the precedent set in `flags.py`: `Community.instance_id`
is a nullable FK, so the ordering means a private community with no instance row
returns at the guard rather than raising `AttributeError` on `None.online()`.

D309's count drops from five files to four.

### 3.4 `:141` is the richest guard the campaign has met

Four conjuncts: `instance.inbox`, `instance.online()`, `not
user.has_blocked_instance(instance.id)`, `not instance_banned(instance.domain)`.

**THREE of the four are independently falsifiable; the second is not** — see the
D302 bullet below, which establishes that `following_instances()` cannot return
an instance for which `online()` is False. So the skip tests cover `inbox`,
`has_blocked_instance` and `instance_banned`, and each must ALSO show the loop
**continuing** past that kind of skip, which needs two instances — one skipped,
one delivered — per falsifiable conjunct. Do not write a fourth skip test for
`online()`; it would be asserting an unreachable state.

Two hazards, both already paid for by earlier rounds:

- `following_instances()` returns rows through the **task** session in
  planner-chosen order, so every assertion over delivered inboxes is set-based
  or sorted. Never a list. Sub-project 23 spent a ruling removing the last two
  ordered assertions from this suite; sub-project 24 nearly shipped a
  continues-past-a-skip test that could not fail because the deliverable
  instance was created first and so came back first. **Create the skipped
  instance first**, and say in the docstring which direction the residual
  assumption fails.
- `instance.online()` at `:141` is D302's shape, and its consequence is
  precise rather than vague. `following_instances()` (`app/models.py:842-851`)
  filters `Instance.dormant == False` at `:849` and
  `Instance.gone_forever == False` at `:850`, and `Instance.online()`
  (`:118-119`) is exactly `not (self.dormant or self.gone_forever)`. **So every
  instance the loop receives already satisfies the conjunct**, and no test can
  make it False through this path.

  **This does NOT block 100% branch coverage, and the reason is worth stating
  because it is easy to get backwards.** `coverage.py` does not decompose a
  conjunction: `if a and b and c and d:` has exactly one arc pair, taken and
  not-taken, so an instance failing ANY conjunct covers the False arc. That is
  how sub-project 23's twins closed at 100% carrying this identical line. Do not
  budget a residual for it.

  **What it does mean is that a mutation deleting the `online()` conjunct will
  SURVIVE — and that survival is the correct result.** It is evidence of the
  redundancy, not a gap in the tests, because no reachable input distinguishes
  the mutant from the original. Fact 138 says a surviving mutation is
  information about the test; this is the case where it is information about the
  **code** instead. Record the survival with that reading rather than
  strengthening a test until it dies, which here would mean asserting something
  untrue. This is a new D302 site; append it in place.

### 3.5 The lookup asymmetry, fifth module

`:31`/`:46` use `.get()` and return `None`, so the failure arrives at `:87`'s
`object.community` as `AttributeError`. `:61`/`:76` use `.filter_by().one()` and
raise `NoResultFound` at the lookup. Same post-versus-reply assignment as
`flags.py`. Extend D317 in place; do not allocate.

### 3.6 Phase A checkpoint

Phase A ends with `app/shared/tasks/locks.py` measured, its residual closed or
proved unreachable, its mutations killed, and its floor written to
`coverage_floors.ini` and proved to bite by inversion with an isolation control.
**Phase B does not start until that floor is committed.**

## 4. Phase B — `likes.py`

```
:24  vote_for_post(...)   :29 .get(post_id)              :30 if federate:
:40  vote_for_reply(...)  :45 .filter_by(id=...).one()   :46 if federate:
:55  send_vote(user_id, object, vote_to_undo, vote_direction, emoji)
:56      session = get_task_session()        <- a SECOND task session
:60      if community.local_only or not community.instance.online(): return
:63-65   a CommunityBan on this user in this community -> return
:66-68   remote community: has_blocked_instance or instance_banned -> return
:70-73   type = vote_to_undo, else 'Like'/'Dislike' by direction
:88-89   optional emoji content
:92-105  undo payload, built from vote_public.copy() with @context deleted
:107     if community.is_local():  -> Announce, THREE delivery mechanisms
:160     else: direct send to community.ap_inbox_url
:168     except:            <- BARE
:176 vote_for_poll(send_async, user_id, post_id, choice_text)
:177     session = get_task_session(), never patched, NO app_context
:181     if post:           <- the only guard; NO federation gate at all
:232     except:            <- BARE
```

### 4.1 Three delivery mechanisms in one branch

For a local community, `:135`'s loop chooses per instance:

1. `:141` — `instance.software` is `piefed` or `pylova`: write an `ActivityBatch`
   row and `session.commit()`, **inside the loop**, once per such instance.
2. `:145` — `current_app.config['NOTIF_SERVER']` is truthy: append a signed
   request to `send_async` and, after the loop, publish them to redis at `:157`.
   `NOTIF_SERVER` defaults to `''` (`config.py:131`), so this arm needs a config
   override to reach.
3. `:152` — otherwise: `send_post_request` directly.

All three need covering, and the third is the only one any sibling module has.
Mechanism 2's payload assembly at `:157-159` takes `urls` and `headers` from
every element but `data` from `send_async[0][2]` alone — correct only because
the Announce is identical for every instance. Assert on what is published.

### 4.2 The session picture, and why it is D314's sharpest carrier

`send_vote` opens its own task session at `:56` while its caller is already
inside `patch_db_session` with one open at `:26`/`:42`. Three sessions are live
in one call:

- the **outer** wrapper's task session, which `patch_db_session` has installed
  as `db.session`;
- **`send_vote`'s own** at `:56`, which it never patches;
- one **per call** to `instance_banned`, which opens its own at
  `app/utils.py:2336`.

And the objects mix: `object` arrives from the outer session, `user` at `:58` is
loaded from the inner one, and `user.has_blocked_instance()`
(`app/models.py:1467-1471`) reads through `db.session` — the outer one. So `:67`
compares an inner-session `user` against outer-session state.

Nothing observed misbehaves because of this, and the spec claims nothing more.
**Record it as a mechanism, measured, with no consequence asserted** — the
discipline D311, D312 and D314's cells already state. It is not fixed here: the
user scoped three production changes and this is a behavioural change with a
blast radius across every `db.session` read in the vote path.

`vote_for_poll` is D314's second carrier in this module: its own session at
`:177`, never patched, and unlike every other wrapper in the package it does not
open `with current_app.app_context()`.

### 4.3 The bare `except:` clauses

`:168` and `:232` are bare `except:`, not `except Exception:`. A bare except
catches `BaseException`, so `KeyboardInterrupt` and `SystemExit` are swallowed
into a rollback-and-re-raise path. **Registered, not fixed** — the user held
production scope at three and these are the fourth and fifth sites. Distinct
from D315's `raise Exception(...)` family, which is about what is *raised*
rather than what is *caught*; say so in the entry so the two are not merged.

### 4.4 `vote_for_poll` has no federation gate — production change 3

Every other sender in this package gates on the community before federating.
`vote_for_poll` checks only `if post:` at `:181`, then federates a poll vote out
of a local-only or private community with no check whatsoever.

This is not a D309 omission — those cells describe guards that exist and are
missing a conjunct. This is the guard's **absence**, and it gets its own number.

Production change 3 adds, inside `if post:` and before the envelope is built:

```python
            community = post.community
            if community.local_only or community.private or not community.instance.online():
                return
```

**This is three lines, not one**, and it is the largest of the three production
changes. It is also the only one whose absence has a user-visible consequence
that needs no inference: a private community's poll votes federate today.

Re-derive the insertion point and indentation from the tree; the line numbers
above are from `5f90fe63` and Phase A does not touch this file.

### 4.5 The `federate` parameter

`vote_for_post:30` and `vote_for_reply:46` gate the call to `send_vote` on a
`federate` keyword defaulting to `True`. No other module in this package has a
wrapper-level federation switch. Both arms need covering, and the `False` arm is
the cheapest genuine early-return in either module: the row is loaded and
nothing is sent.

## 5. Production changes — three

| # | Site | Change | Register |
|---|---|---|---|
| 1 | `locks.py:89` | add the `community.private` conjunct, before the `online()` call | D309: five files → four |
| 2 | `likes.py:60` | add the `community.private` conjunct, before the `online()` call | D309: four files → three |
| 3 | `vote_for_poll`, inside `if post:` | add the full three-way gate | **new number** |

Each is test-first: write the test, watch it fail **for the stated reason**, then
change the code. Changes 1 and 2 are one line each; change 3 is three.

`app/`'s numstat for the whole sub-project must be stated exactly in the final
report, and every mutation restore must assert the expected `wc -l` — 145 for
`locks.py` throughout, and 236 → 239 for `likes.py` after change 3.

## 6. Findings to register

Next free number is **D325** — confirm against the register. Facts end at
**140**; append from **141**.

New numbers:

1. **`send_vote`'s nested unpatched session** (§4.2). Mechanism measured, no
   consequence claimed.
2. **The two bare `except:` clauses** at `likes.py:168` and `:232`. Say
   explicitly how this differs from D315.
3. **`ActivityBatch.source_type` and `source_id` are written by nobody and read
   by nobody.** `likes.py:142` and `app/activitypub/routes.py:1992` are the only
   writers and both omit them; `app/cli.py:1132` reads batches by
   `instance_id` + `community_id` only. The column comment at
   `app/models.py:4337` describes an undo-by-`source_id` lookup that is
   implemented nowhere. **The consequence is benign and the entry must say so**:
   the comment's own fallback ("if not found, federate the undo") is what always
   happens, so batched votes degrade to the correct behaviour rather than
   breaking. This is a dead design, not a live defect.
4. **`vote_for_poll`'s missing gate** — fixed by production change 3.

In-place edits:

- **D309** drops from five files to three, `locks.py:89` and `likes.py:60`
  closed. State the counting unit; `deletes.py` contributes two lines at one
  site.
- **D302** gains `locks.py:141` and `likes.py:136`/`:222` as sites where
  `instance.online()` is redundant against `following_instances()`' own filter.
  Re-derive the existing count before writing a new one.
- **D314** gains `likes.py`'s two carriers, described at the strength §4.2 sets.
- **D317** gains `locks.py` and `likes.py` as the fifth and sixth modules
  carrying the `.get()`/`.one()` asymmetry.
- **D324** becomes six copies of `_recording_task_session`, not five.

## 7. Verification

- **Mutations**: one at a time, targeted single-line `sed`, **dry-run without
  `-i` and read the produced line before applying**, restore with
  `git checkout -- app/`, then assert both an empty `git diff -- app/` and the
  expected `wc -l`. Every mutation killed by a named test, and the kill shown —
  pasted failure output, not narration. A mutation that applies cleanly and
  changes nothing is defective, not surviving. **A mutation that survives is
  information about the test** (fact 138): find a real discriminator and
  strengthen the test, rather than declaring the mutation bad.
- **Floors**: `locks.py` at the Phase A checkpoint, `likes.py` at the end. Floors
  only rise. Prove each bites by inversion — hold floors fixed, regress a *copy*
  of the report — with an isolation control.
- **Full suite** in the foreground, **unpiped**; `pytest` exits 1 on a session
  timeout and a pipeline eats the status.
- Test counts from **collection**, never `grep -c '^def test_'`.
- Before believing any failure, `./run_tests.sh --down`.
- Every citation re-derived against the current tree, **and citations into a
  file your own diff touches re-derived after the diff is final** (fact 139.3).

## 8. Risks

1. **`likes.py`'s three delivery mechanisms triple the local-community
   surface.** Mechanism 2 needs a `NOTIF_SERVER` config override and asserts on
   a redis publish rather than an HTTP request, so it needs a different capture
   than anything the campaign owns. Design that capture before writing the test,
   and confirm it can fail.
2. **The `@context` deletion sites differ between the modules** — in-place in
   `locks.py`, copy-first in `likes.py`. A test asserting on an in-memory dict
   would read `locks.py`'s payload post-`del` and `likes.py`'s pre-`del`. Assert
   on serialized bytes in both.
3. **`:141`'s three falsifiable conjuncts need two instances each to prove
   continuation**, which is six instances' worth of setup if done naively. Look
   for a shape that shares fixtures without making one test's failure ambiguous
   about which conjunct broke.
4. **D302's redundancy is settled, not open** (§3.4): `online()`'s False arm is
   unreachable through `following_instances()`, it costs no coverage because
   `coverage.py` does not decompose conjunctions, and the mutation that deletes
   it is EXPECTED to survive. The risk is that an implementer meets that
   survival, reads fact 138's "a surviving mutation is information about the
   test", and strengthens a test until it dies — which here means asserting a
   state that cannot occur. §3.4 exists to pre-empt exactly that.
5. **The phase boundary is a real gate.** If Phase B overruns, the correct
   outcome is `locks.py` closed and floored with `likes.py` deferred — not both
   half-done.

# Coverage sub-project 42: `app/shared/reply.py` Groups B and D — closing the module

**Date:** 2026-09-13
**Branch:** `blentz`
**Measured at:** `15d7e132` (full suite, `PYTEST_EXIT=0`, 4815 passed, 3 skipped, all 21 floors met)

## Goal

Take `make_reply`, `edit_reply` and `report_reply` to zero missing statements and zero missing branch arcs — **126 statements and 68 arcs** — and **close `app/shared/reply.py`**, the campaign's second module after `app/shared/post.py`.

Then fix **D523 and D522 together**: the counter drift an author's delete-then-restore leaves behind, and the disagreement between the author pair and the moderator pair about which counters a removal moves.

## Measurement

Read from the full-suite JSON at `15d7e132`, and from coverage.py's own per-function table rather than from any figure in a document:

| | statements | missing | branches | missing | percent_covered |
|---|---|---|---|---|---|
| module | 375 | 126 | 198 | 68 | 66.14310645724258 |

| Group | Function | Stmts | Missing | Arcs | Missing |
|---|---|---|---|---|---|
| **B** | `make_reply` `:156` | 42 | 42 | 22 | 22 |
| **B** | `edit_reply` `:216` | 29 | 29 | 12 | 12 |
| **D** | `report_reply` `:311` | 55 | 55 | 34 | 34 |
| | **this round** | | **126** | | **68** |

**All three are at zero coverage** — `missing == total` for every one. No test in the suite executes a single line of any of them.

126/68 is slightly over the campaign's demonstrated pace of roughly 110/56. That is accepted deliberately, because the alternative is a seventh round on this module to close one function.

**Every line number in this spec was re-derived at `15d7e132`.** Sub-project 41 registered D532 after finding that a production fix silently invalidates every citation below it, and that nothing in this campaign's instrument set detects it — no missing arc, no failing test, no surviving mutant. That entry has been corrected four times. This round's own production change will shift lines in `app/shared/reply.py`, so **Tasks after the fix must re-derive rather than inherit.**

## The harness

### What transfers

- `tests/test_shared_reply_interactions.py` (Groups A and C) and `tests/test_shared_reply_moderation.py` (Groups E and F) carry the module's established harness: the seed helpers, `bearer`, `web_ctx(app, user, query_string='')`, `recording_task_selector`, and the federation lever `private=True` that stops eager Celery task bodies at their first guard.
- **`web_ctx` takes the app fixture first.** Sub-project 41's plan wrote `web_ctx(user)` five times and every occurrence was wrong.
- **Do not cargo-cult `make_site()`.** It is needed where a template renders or `can_downvote` reads `Site.query.get(1)`. Determine it per function and write the real reason.
- **No `Language` row is needed**: `make_user` leaves `language_id` and `interface_language` as `None`, so `get_recipient_language` takes its `'en'` branch and `Language.query.get(...)` is structurally unreachable there.
- The precedents are `tests/test_shared_post_make.py` (`make_post` `:175`, `edit_post` `:262`) and `tests/test_shared_post_moderation.py` (`report_post` `:833`).

### What is new, and what Task 1 must probe

- **`make_reply`'s weight is `PostReply.new`, not itself.** The function is plain control flow over rows — no upload pipeline, no URL classification. `PostReply.new` runs the gif-reaction and low-effort filters, builds the `path` array, increments three counters and recomputes `reply_count_cross_posted`, and raises `PostReplyValidationError` from several points. **What a factory post and user need to reach each raise is Task 1's first probe.**
- **`report_reply` is the module's largest function and its shape is a loop, not a fork.** `:359-376` iterates `reply.community.moderators()` and branches per moderator on local/remote, with `report_remote` gating the remote set. Witnessing both arms needs at least one local and one remote moderator on the same community.
- **`report_reply:379` calls `Site.admins()`** — D442's registered divergence, live in this round's target. Whether `g.admin_ids` is set differs between a request context and a bare call.
- **`notify_admins` is a substring test over two lists** (`:318-319`), so `'dox'` matches any word containing it. Record the behaviour; do not fix it.

## The production change

**One change, six lines across three functions, and its direction is settled by evidence rather than preference.**

Today only `delete_reply` moves all four counters:

| function | `post.reply_count` | `reply_count_cross_posted` | `community.post_reply_count` | `author.post_reply_count` |
|---|---|---|---|---|
| `delete_reply` `:265-269` | −1 | **−1** | **−1** | −1 |
| `restore_reply` `:293-295` | +1 | — | — | +1 |
| `mod_remove_reply` `:427-429` | −1 | — | — | −1 |
| `mod_restore_reply` `:462-464` | +1 | — | — | +1 |

**`app/shared/tasks/maintenance.py:314` is the authority**, because it recomputes the counter from scratch:

```sql
SELECT COUNT(*) FROM post_reply WHERE deleted is false and community_id = :community_id
```

The truth is *the number of non-deleted replies*. A delete must decrement it and a restore must increment it. So the three functions that do neither are wrong and `delete_reply` is right — the majority is not the model.

**And `reply_count_cross_posted` is a DERIVED value, which is why hand-decrementing it is nonetheless correct.** `app/models.py:3099-3108` recomputes it on creation as `sum(reply_count)` across the cross-post set, or sets it equal to `post.reply_count` when there are none. Decrementing by one preserves that invariant in both cases.

**The fix:** `restore_reply`, `mod_remove_reply` and `mod_restore_reply` each gain the two movements `delete_reply` already has, inside their existing `if not reply.author.bot:` guard. All four functions then agree with the maintenance job.

It lands **after** tests pin today's behaviour, with the failing observation pasted before the change, and the pins inverted in the same commit — the sequence sub-projects 40 and 41 both used.

**`delete_reply` and `restore_reply` are already at zero missing**, so the fix is cheap to verify and the round must re-measure them afterwards rather than assume they stayed closed. Sub-project 41 learned that the hard way: a production change mid-round created two new uncovered arcs that went unnoticed for two tasks.

## Four findings by reading, registered rather than fixed

### 1. `report_reply:391` compares a community id against a set of instance ids

```
389    if report_remote:
390        if not reply.community.is_local():
391            if reply.community_id not in remote_instance_ids:
392                remote_instance_ids.add(reply.community.instance_id)
```

The guard tests `reply.community_id`; the value added is `reply.community.instance_id`. Different id spaces, so the guard cannot do what it is written to do. The `suspect_user` block at `:393-395` gets the identical pattern right. **Registered, not fixed** — this round's production budget is the counter fix, and a duplicate Flag to one instance is a different blast radius from a permanently drifting counter.

### 2. `edit_reply` checks one permission twice, in two spellings, and the web arm fails silently

`:224` is `not is_moderator and not is_owner and not is_staff() and not is_admin()`. `:239` is `is_moderator or is_owner or is_admin_or_staff()`. `is_admin_or_staff()` is exactly `is_admin() or is_staff()` (`app/models.py:1274-1275`), so the two are De Morgan twins over the same set.

The API arm **raises** at `:225`; the web arm has no equivalent, so `:239` silently declines to apply `distinguished` and the caller is told nothing. That is the shape sub-project 41 fixed twice. **Registered, not fixed**, and pinned in both directions.

### 3. `app/activitypub/util.py:2276` decrements a counter it never increments for bots

`app/models.py:3090-3092` increments `post.community.post_reply_count` **inside** `if not user.bot:`. `app/activitypub/util.py:2276` decrements it **outside** the bot guard. So a bot's reply decrements a community counter its creation never incremented. Cross-references the counter family this round is repairing.

### 4. `Site.admins()` at `report_reply:379` is D442, live

Registered since sub-project 12 and reached by this round's target. Record the interaction; do not fix.

## The five false-witness mechanisms

1. **Asserting on state something else sets unconditionally.** Acute in `edit_reply`, which writes six attributes on every path before any branch.
2. **A fixture coincidence.** **D533 is live and unrepaired:** sub-project 41's fixture leaves `community.id == post.id == author.id == 1`, which hid at least four mutants because `add_to_modlog` resolves objects to ids and wrong-object-right-id is invisible. **Any new fixture in this round must offset its sequences**, and the round should say whether it fixes D533 or inherits it.
3. **Emptiness with no same-mechanism positive control.** Acute in `report_reply`, where "no remote instance was notified" is both the correct outcome and the signature of a broken seed.
4. **An input taking the same path under both arms.**
5. **Two independent conditions exercised only in lockstep.** `User.is_admin()` short-circuits True for user id 1.

And: **a mutation's non-failures are evidence.**

## Verification

A mutation pass **scoped by the statement list, not the arc table**, with the statement and compound lists derived mechanically via `ast.walk`.

**CORRECTED AT TASK 9 — THE SENTENCE THAT STOOD HERE WAS AMBIGUOUS, AND FALSE UNDER THE READING THAT MATTERS.** It read: *"An `ast.walk` over a `FunctionDef` counts the `def` line where coverage.py's `num_statements` does not, so the list runs one longer per function — that is expected and must not be 'reconciled' away."* **Two different numbers wear the name `num_statements`.** The FILE-level `files[f]['summary']['num_statements']` **includes** every `def` line — measured at `0cf503a6` over `app/shared/reply.py`, all sixteen `def` linenos are in `PythonParser(...).statements`, the parser total is 381 and the JSON file total is 381. The PER-FUNCTION `files[f]['functions'][name]['summary']['num_statements']` **excludes** it, billing it to the `<module>` entry (26 statements = 16 `def` lines + 10 module-level statements). Per function, `ast.walk` gives `43 / 30 / 56` and coverage.py's file-level statement set over the same spans gives `43 / 30 / 56`, with empty differences both ways; the per-function summaries give `42 / 29 / 55`. So `129` and `126` are both right about different fields, and **the `def` lines are covered statements coverage.py measures — not lines it cannot see.** The operative instruction (expect 129, do not reconcile down) is correct and Task 8 reached 129 by measuring rather than obeying; the REASON was wrong, and its danger is that it invites treating `def` lines as unmeasured. They are measured and already covered; the real objection to their mutants is that parameter-order swaps on functions every test calls positionally are **free kills**, which is a different point (see D528). The only genuine `ast`-versus-coverage difference on this file runs the other way: `:149`, a docstring. Registered as **D542**. The surviving rule: **say which `num_statements` you mean, derive the list, publish the command and its raw output, and let a measurement overrule a brief.**

**Publish the derivation command and its raw output beside any count.** Sub-project 41's pass silently excluded eight `add_to_modlog` continuation lines, reported 12 where 20 existed, and every one of the eight survived. The narrowing was found only because a reviewer re-derived the denominator instead of reading it.

**One mandatory mutation beyond the list:** neutralise `edit_reply:224`'s moderator guard so it never refuses. Five consecutive rounds have found a real hole this way.

**`git diff --quiet -- app/` is the tree check, not `wc -l`.** **`git checkout -- app/` is BANNED** while the round holds an uncommitted production change — HEAD is not the baseline then, and that idiom silently reverted a real fix in sub-project 40. Reverse edits only, with the restored line re-read.

Standing rules: a crash kill is not a kill unless a viable non-crashing variant also dies; an operator can be structurally void; **fix-catching is not a unique kill**; a test closing no arc and no statement earns its place by a unique kill against a fault-direction mutant, or by pinning a registered defect **with the fix-edit obligation stated in the test itself**.

## Success criteria

- `make_reply`, `edit_reply` and `report_reply` at zero missing statements and zero missing arcs, checked **as lists**.
- **`app/shared/reply.py` CLOSED** — the whole module at `missing_lines []` and `missing_branches []`, re-measured after the production change rather than before it.
- The floor raised to the measured `percent_covered`, rounded down.
- D523 and D522 **fixed**, their pins inverted, and their register entries marked fixed — a silently repaired defect is invisible to the register, which is worse than one never found.
- Full suite green, run by the controller in the foreground, unpiped, floors check chained by `&&` with **both** arguments. Note that the suite currently needs `-o session_timeout=1800` on a loaded machine; that is a command-line ini override and `pytest.ini` is not to be edited.
- Findings registered from **D538**, marker updated.
- `tests/README.md` facts from **237**.

## Out of scope, carried forward

- **D532** — the stale-citation sweep: ~25 `models.py` files, 10 `reply.py` sites, 56 `post.py` sites across 16 files, `cli.py` unenumerated.
- **D533** — the fixture id collision, and the fourteen modlog mutants that must be re-run after it is fixed.
- **D537** — 400-vs-403 and the per-denial log and Sentry volume.
- **D521, D526** — two unpinned permission disjuncts.
- **D495** — 32 open `post.py` mutation holes with recipes; plus 17 open survivors from sub-project 40 and 13 from sub-project 41.
- **D421, D442, D450, D463, D465, D476, D478, D490-D492.**

**After this round `app/shared/reply.py` is closed and the campaign needs a new target.** The register's open findings and the three modules still under 100 — `app/utils.py` (73, 607/362 missing), `app/activitypub/util.py` (76, 622/409), `app/activitypub/routes.py` (90, 151/100) — are the candidates.

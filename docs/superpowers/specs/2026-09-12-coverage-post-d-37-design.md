# Coverage sub-project 37: `app/shared/post.py` Group D — `make_post`

**Date:** 2026-09-12
**Branch:** `blentz`
**Measured at:** `3f885e0f` (module re-measured at `a6b6c49e`, unchanged — the intervening commits touch docs only)

## Goal

Take `make_post` (`app/shared/post.py:163-249`) to zero missing statements and zero missing branch arcs.

## Measurement

Taken with `--cov=app.shared.post --cov-branch` over the four post test files (239 passed, exit 0), read from `summary.percent_covered`:

| | statements | missing | branches | missing | partial | percent_covered |
|---|---|---|---|---|---|---|
| module | 788 | 171 | 438 | 103 | 21 | 77.651 |

Per function, boundaries from `grep -n "^def "`:

| Group | Function | Range | Missing stmts / arcs | Covered |
|---|---|---|---|---|
| **D** | `make_post` | `:163-249` | **61 / 26** | **0.0%** |
| E | `edit_post` | `:250-754` | 110 / 77 | 66.8% |

These sum to exactly 171/103 — the module's entire remaining gap. Groups A, B and C contribute zero. D392's five-group decomposition has now matched the measurement for the fourth consecutive round.

**This round takes Group D alone.** Group E follows as sub-project 38, re-measured, because this round will close some of its arcs as a side effect (see below).

## Why `make_post` is at 0.0%, which is the whole design problem

No test has ever called it. Three collaborators block a first call, and each has to be solved before any test can assert anything. **These were established by reading, not assumed:**

**1. `can_create_post` requires a keyed local user.** `app/utils.py:2504-2506`:

```python
    if user.is_local():
        if user.verified is False or user.private_key is None:
            return False
```

`make_user(..., local=True)` (`tests/factories.py:41-67`) leaves `private_key=None` unless `with_keys=True`, and its own docstring records that keypair generation "costs roughly a second". So `make_post:187` raises `'You are not permitted to make posts in this community'` before reaching anything, for every user `seed_post_context` currently produces.

**2. `g.site` is never set for a local author.** `make_post:219` is `community.last_active = g.site.last_active = utcnow()`. `can_create_post` has a `g.site` fallback at `:2508-2509`, but it sits on the **remote**-user branch, so a local author never reaches it. `conftest.py:205`'s `site` fixture creates the Site **row**; `g.site` is a separate thing, populated in production by the `before_request` at `app/request_hooks.py:79` — which this harness never dispatches, because `web_ctx` uses `test_request_context`.

**3. `notify_about_post` fires and runs inline.** `:238` guards on `post.status == POST_STATUS_PUBLISHED`, which is the column default, so the call at `:239` happens on the ordinary path. `app/activitypub/util.py:2796-2800` dispatches `notify_about_post_task`, and `conftest.py:106` sets `task_always_eager=True`, so the task body executes rather than queueing.

**Task 1 settles all three before any other task starts.** Each probe asserts something expected to fail, so the real value is recorded rather than predicted. A surprising answer changes the file's shape, and Task 1 owns the correction.

## A new required step: search the register before probing

**D295 already recorded what sub-project 36 spent a probe, a Major review finding, five documentation sites and a final-review correction re-deriving.** It states that `Site.admins()` reads `g.admin_ids` when present (`app/models.py:3996-3997`), that `conftest.py` clears `g`, and therefore that every test takes the INNER-join arm. `tests/test_shared_post_edit.py:219`'s `_make_admin` helper has implemented it for rounds, citing D295 in its docstring. Sub-project 36 registered a worse duplicate as D442, which never cited D295 until this round cross-linked it at `a6b6c49e`.

The register is ~453 entries. **Task 1 greps it for each collaborator it is about to probe** — `can_create_post`, `g.site`, `notify_about_post`, `generate_ap_id`, `scale_by` — and reports what it found before running anything. A round that probes for a fact it could have grepped pays twice: once to run the probe, once to correct the conclusion drawn without the older entry's context.

## The `edit_post` delegation

`make_post:230-236`:

```python
    try:    # federation is done in edit_post
        post = edit_post(input, post, type, src, user, auth, uploaded_file, from_scratch=True)
    except Exception as e:
        db.session.delete(vote)
        db.session.delete(post)
        db.session.commit()
        raise e
```

Groups D and E are coupled: `make_post` cannot execute without executing all of `edit_post`. **Decision: tests let `edit_post` really run, and monkeypatch it only for the tests that must make it raise.** The plan fixes that count from the four rollback statements; this spec deliberately does not guess it.

Consequences, both intended:

- `make_post` is exercised end to end, so the tests prove the two functions actually compose rather than only that `make_post` calls something.
- `edit_post`'s six `from_scratch` sites — `:324`, `:421`, `:565`, `:739`, `:746`, `:750` — have their True arms reached for the first time. Every guard *line* is already covered, because existing tests call `edit_post` directly and take the False arm; only `make_post` passes `from_scratch=True`. So **Group E's arc count will fall as a side effect of this round**, and sub-project 38 must re-measure rather than inherit 110/77.

The accepted cost: a defect in `edit_post` surfaces as a `make_post` failure. Task 1's probe report should note anything that looks like it originates below `:231`.

## The 26 arcs

All enumerated from the coverage JSON, not inferred from structure:

```
164 -> 165   164 -> 172      source fork
166 -> 167   166 -> 168      rate limit
174 -> 175   174 -> 176      POST_TYPE_LINK
176 -> 177   176 -> 179      POST_TYPE_VIDEO / neither
187 -> 188   187 -> 190      permission guard
190 -> 191   190 -> 197      if url
193 -> 194   193 -> 197      if domain
194 -> 195   194 -> 197      banned or .pages.dev
197 -> 199   197 -> 206      uploaded_file present
200 -> 201   200 -> 202      video and can_upload_video
203 -> 204   203 -> 206      extension not allowed
238 -> 239   238 -> 241      status published
243 -> 244   243 -> 246      return fork
```

**Two are compound conditions that coverage.py scores as a single arc pair each.** Taking each true once and false once satisfies coverage while leaving an operand untested:

- `:194` — `if domain.banned or domain.name.endswith('.pages.dev')`, two **disjuncts**.
- `:200` — `if type == POST_TYPE_VIDEO and can_upload_video()`, two **conjuncts**.

Each operand needs its own witness.

**There is no arc for the `try`/`except` at `:230-236`.** coverage.py does not model exception handlers as branches, so the rollback is statement coverage only — `:233`, `:234`, `:235`, `:236`. Nothing in the arc list reveals whether it is tested, which is exactly the kind of gap a coverage percentage hides. The rollback gets its own explicit step rather than riding along with the delegation tests.

## Task decomposition

Seven tasks, against sub-project 36's eleven; the round is 61/26 rather than 101/56.

| Task | Covers | Arcs |
|---|---|---|
| 1 | Register search, the three probes, file open, one real test | — |
| 2 | Source fork `:164`, rate limit `:166`, the three url arms `:174`/`:176`, return fork `:243` | 10 |
| 3 | Permission `:187`, url `:190`, domain `:193`, banned/`.pages.dev` `:194` (both disjuncts) | 8 |
| 4 | Upload `:197`, video+permission `:200` (both conjuncts), extension `:203` | 6 |
| 5 | Creation and state mutations `:206-228`, delegation, rollback statements, notify `:238` | 2 |
| 6 | Mutation pass | — |
| 7 | Measure, raise floor, register | — |

Task 5 carries the least arc weight and the most risk: the rollback is invisible to the arc count, and it owns the `edit_post` monkeypatch.

## Production changes

**None planned.** Unlike sub-projects 35 and 36, no defect in `make_post` has been confirmed going in. If one surfaces it gets the same treatment as PC1 and PC2 — a pasted failing observation before the change, and a fix to the cause rather than the symptom. No defect will be manufactured to justify the round.

## The four false-witness mechanisms

Registered as D451 after sub-project 36 shipped six tests that could not fail for their stated reason. All four apply here, and every task's dispatch carries them:

1. **Asserting on state the function sets unconditionally.** In `make_post`, `:206-228` runs on every non-raising path — the `Post` row, the vote, the counters. None of it witnesses anything about the guards above it.
2. **A fixture coincidence making two arms produce the same value.** `s.author.id == 1` collided with a literal `1` in sub-project 36 and made two arms of a source fork indistinguishable.
3. **Asserting emptiness with no positive control.** A raise-count of zero, or an empty call list, is what you get from a correct refusal, a broken fixture, and a monkeypatch that never installed — alike.
4. **Choosing an input that takes the same path under both arms.** A test of a filter must use an input the filter would reject.

And the reading rule that found the worst of them: **a mutation's non-failures are evidence.** A test whose docstring names a branch and stays green under that branch's mutation does not guard it.

## Verification

Task 6 mutates every site in the arc table above, one at a time, line-scoped, with the tree restored and asserted clean between each. The standing rules apply: a crash kill is not a kill unless a viable non-crashing variant of the same fault also dies; an operator can be structurally void; and an arc being equivalent does not make every mutation of its line equivalent — state which claim you are making.

**Two mutations are mandatory beyond the arc table**, because sub-project 36 found a security-relevant hole that only a non-crashing variant revealed. Replace `:165`'s `user = authorise_api_user(auth, return_type='model')` with a value that skips authorisation, and separately neutralise `:187`'s guard. If either survives, no test asserts that the check actually happened — only that the path which would have performed it was taken.

## Success criteria

- `make_post` at zero missing statements and zero missing arcs, confirmed by re-measurement with both endpoints of every arc checked.
- The module floor raised to the measured `percent_covered`, rounded down, in `coverage_floors.ini`.
- Full suite green, run by the controller in the foreground, unpiped, with the floors check chained by `&&`.
- Findings registered from **D454**, with the marker updated.
- `tests/README.md` facts from **224**, recording at minimum: the `with_keys` requirement and its cost, how `g.site` is supplied, and what `notify_about_post` does under eager Celery.

## Out of scope, carried forward

No round owns these:

- Two reachable unhandled 500s in `app/post/routes.py` (D421).
- `Site.admins()`'s divergence wherever `g.admin_ids` is unset, with four `app/activitypub/util.py` call sites possibly reached from `/inbox` — the one path the `before_request` hook skips (D442, open check).
- `report_post:875`'s `if moderator:` guard, unreachable in production because both hard-delete paths remove `CommunityMember` rows first (D450).
- Group E, `edit_post`, 110/77 before this round's side effects — sub-project 38.

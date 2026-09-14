# Coverage sub-project 43: the `reply_count_cross_posted` backfill, and `app/shared/user.py`

**Date:** 2026-09-14
**Branch:** `blentz`
**Measured at:** `01845836` (full suite, `PYTEST_EXIT=0`, 4880 passed, 3 skipped, all 21 floors met)

## Goal

Two parts, in this order.

**Part 1 — repair a live 500 this campaign introduced.** Backfill `post.reply_count_cross_posted`, whose `NULL` values now crash four code paths that sub-project 42 wrote.

**Part 2 — close `app/shared/user.py`**: 131 missing statements and 66 missing arcs, 35.831% to 100%.

Part 1 comes first because it is a production defect in shipped behaviour and Part 2 is not.

## Part 1: the backfill

### The defect, established at source

`migrations/versions/6814385881d3_cross_posted_reply_count.py` adds the column:

```python
batch_op.add_column(sa.Column('reply_count_cross_posted', sa.Integer(), nullable=True))
```

**No `server_default`, no backfill.** Every `post` row predating that migration (2026-01-25) holds `NULL`. `app/models.py:1723`'s `default=0` is a *Python-side* default: it applies only to rows the ORM inserts, never to rows already on disk.

Six sites now do arithmetic on that column. Only one guards it:

```
app/activitypub/util.py:2272        if to_delete.post.reply_count_cross_posted:   <- the guard
app/activitypub/util.py:2273            to_delete.post.reply_count_cross_posted -= 1
app/activitypub/util.py:2315        to_restore.post.reply_count_cross_posted += 1
app/shared/reply.py:267             reply.post.reply_count_cross_posted -= 1
app/shared/reply.py:295             reply.post.reply_count_cross_posted += 1
app/shared/reply.py:431             reply.post.reply_count_cross_posted -= 1
app/shared/reply.py:468             reply.post.reply_count_cross_posted += 1
```

`None - 1` raises `TypeError`; Flask renders that as a 500.

**Five of the six unguarded sites are this campaign's work, and four of them were added by sub-project 42's own counter repair.** `reply.py:267` predates it. So `mod_remove_reply` now fails on a pre-2026 row where it previously succeeded — the repair widened the crash surface from one site to five while fixing a different defect, and the register is currently the only artifact that says so.

### The fix

A new Alembic revision on `7b8bf43fa079` — **the single real head**, confirmed with `flask db heads` rather than by pattern-matching the files. A shell sweep for revisions nobody names as a `down_revision` reports three candidates; two are artifacts (one revision id is quoted differently, one file alembic does not treat as a head). **Ask the tool, not the inputs.**

```sql
UPDATE post SET reply_count_cross_posted = reply_count
 WHERE reply_count_cross_posted IS NULL;
```

`reply_count` is the right value, not zero, and the authority is the model's own definition: `app/models.py:3108` is `post.reply_count_cross_posted = post.reply_count` for a post with no cross-posts, and `:3100-3104` sums `reply_count` across the cross-post set when there are. A backfill to 0 would satisfy the type and violate the invariant.

Then `ALTER COLUMN ... SET DEFAULT 0` so a future insert outside the ORM cannot reintroduce `NULL`.

**`downgrade()` drops only the server default.** It must not re-`NULL` the data: a downgrade that destroys rows is worse than the defect it reverses.

### What Part 1 must prove

The failing observation comes first, as in sub-projects 40, 41 and 42: a `post` row with `reply_count_cross_posted` set to `NULL` directly, driven through `mod_remove_reply`, **pasted raising `TypeError` before the migration exists**. Then the same row after the backfill.

**A migration is not a mutation target** — the pass in Part 2 does not cover it. Its test is the before-and-after pair, and that pair is the whole evidence.

## Part 2: `app/shared/user.py`

### Measurement

From the full-suite JSON at `01845836`, per coverage.py's own function table:

| | statements | missing | branches | missing | percent_covered |
|---|---|---|---|---|---|
| module | 211 | 131 | 96 | 66 | 35.8306 |

| Function | Stmts | Missing | Arcs | Missing |
|---|---|---|---|---|
| `ban_user` `:141` | 51 | **51** | 26 | **26** |
| `block_another_user` `:20` | 24 | **24** | 14 | **14** |
| `bot_challenge_user` `:303` | 23 | **23** | 8 | **8** |
| `unblock_another_user` `:61` | 16 | **16** | 10 | **10** |
| `unban_user` `:213` | 10 | **10** | 2 | **2** |
| `follow_user` `:231` | 23 | 3 | 10 | 2 |
| `subscribe_user` `:89` | 34 | 2 | 22 | 2 |
| `unfollow_user` `:278` | 12 | 2 | 4 | 2 |
| `<module>` | 18 | 0 | 0 | 0 |
| | | **131** | | **66** |

**The split is natural rather than arbitrary.** Five functions are at literally zero — `missing == total` — and account for 124 statements and 60 arcs. Three are nearly closed, contributing 7 statements and 6 arcs between them, presumably reached incidentally by API tests elsewhere.

### Grouping

- **Group A — the block pair and the bot challenge**: `block_another_user`, `unblock_another_user`, `bot_challenge_user`. 63 statements, 32 arcs.
- **Group B — the ban pair**: `ban_user`, `unban_user`. 61 statements, 28 arcs. `ban_user` is the module's largest function by a wide margin.
- **Group C — the remainder**: the 7 statements and 6 arcs left in `follow_user`, `subscribe_user`, `unfollow_user`.

All three groups fit one round at the campaign's pace, with Part 1 taking the first task.

### What transfers, and what does not

`app/shared/post.py` and `app/shared/reply.py` are both closed, and this is their sibling. **The harness transfers**: `bearer`, `web_ctx(app, user, query_string='')`, the `SRC_API`/`SRC_WEB` source fork, `recording_task_selector`, and `private=True` as the federation lever.

**The traps transfer too, and they are not hypotheses — every one cost this campaign a test:**

- **`app/models.py:1259-1261` is `if self.id == 1: return True` in `is_admin`**, and `tests/conftest.py:131` resets every sequence after each test, so the first-minted user is silently an admin in **every** test, deterministically. `ban_user` is a permission-gated function; this trap is live for it. `_burn_a_seed()` in `tests/test_shared_reply_make.py` is the established remedy.
- **Identical exception messages defeat `pytest.raises`.** Check the messages before relying on one.
- **A correction can create a false witness** — fixing a value to dodge a database error can silently turn a live assertion into a restatement of a column default.
- **`Site.admins()` and `User.is_admin()` disagree** (D442, and the join-versus-name divergence found in sub-project 42): `Site.admins()` inner-joins `user_role` and filters on the role **id**, while `is_admin()` matches the role **name** and short-circuits on id 1.

**What does not transfer is the assumption that it does.** Sub-project 42 found that `report_reply` needed neither a `Site` row nor keyed users while `make_reply` needed both — the opposite of what copying the sibling's helper would have given. **Probe per function.**

## Verification

A mutation pass **scoped by the statement list, not the arc table**, with the statement and compound lists derived mechanically via `ast.walk`, and **the derivation command and its raw output published beside every count**. Sub-project 41 silently excluded eight lines and reported 12 where 20 existed; sub-project 42's pass reported three different numbers for one quantity and reconciled them only under review.

**An equivalence claim needs a proof of unkillability, never a failure to kill.** Sub-project 42 retracted D546 for exactly that error: a surviving mutant means either "no test *can* kill this" or "no test *does*", and resolving the ambiguity by assumption is how `:160` stayed uncovered for a whole round.

**One mandatory mutation beyond the list:** neutralise `ban_user`'s permission guard so it never refuses. Six consecutive rounds have found a real hole this way.

Standing rules: a crash kill is not a kill unless a viable non-crashing variant of the same fault also dies; fix-catching is not a unique kill; **a count is a claim — re-derive it like a line number.**

## Production changes

**One, and it is Part 1's migration.** No other production change is planned. If a genuine defect surfaces in Part 2 it is registered rather than folded in — sub-projects 40, 41 and 42 each ended up making production changes, and each one was named in its spec before the round began.

## Success criteria

- The backfill migration applied, with the `TypeError` pasted before it and the same row passing after.
- `app/shared/user.py` at zero missing statements and zero missing arcs, checked **as lists**.
- A **new** `coverage_floors.ini` entry at `floor(percent_covered)` — the module has none.
- Full suite green, run by the controller, floors check chained by `&&` with **both** arguments. It currently needs `-o session_timeout=1800` on a loaded machine; **`pytest.ini` is not to be edited.**
- Findings registered from **D554**, marker updated.
- `tests/README.md` facts from **242**.

## Out of scope, carried forward

- **The D545 citation sweep** — ~90 citations in `tests/test_shared_reply_interactions.py`, plus D522-D537's `app/shared/reply.py` citations stale by a four-region offset. And the **verified-versus-inherited convention**: five register rows carry `RE-DERIVED BY CONTENT` markers; across 553 entries nothing says an unmarked citation is unverified.
- **D553** — `report_reply`'s two `Instance` lookups disagree; D440 is the same defect in `report_post`. A product question with a third candidate meaning at `app/models.py:3766`.
- **D443's reply twin**, and the `notify_admins` policy divergence between the twins.
- **D421, D442, D450, D463, D465, D476, D478, D490-D492, D495, D499, D501, D518, D521, D526, D532, D533, D537, D548, D552.**

**`app/shared/community.py` (410/193) and `app/shared/feed.py` (334/153) are the next large targets** and each needs decomposing into groups before it can be scoped, exactly as `post.py` and `reply.py` were.

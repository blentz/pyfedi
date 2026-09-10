# Coverage sub-project 33: `maintenance.py` Group C, the identity phases

**Status:** approved, not yet implemented
**Branch:** `blentz`
**Predecessor:** sub-project 32, 19 commits `a0be51be..e114a63e`, Group C's first half closed, module floor 66 → 89

## Goal

Take `monitor_healthy_instances`' two identity phases to zero missing
statements, and fix three production defects found while reading them.

This is the last uncovered part of `app/shared/tasks/maintenance.py`. When it
closes, the module is done.

## Scope

54 statements and 35 missing branch arcs, measured against the current
1216-line tree:

| Block | Lines | Missing stmts | Missing arcs | What it does |
|---|---|---|---|---|
| Lemmy/PieFed admin roles and custom emoji | `:637-691` | 28 | 18 | reads `/api/v3/site`, reconciles `InstanceRole`, refreshes `Emoji` |
| MBIN admin roles | `:698-733` | 23 | 17 | reads `/api/users/admins`, reconciles `InstanceRole` only |
| the task's own outer handler | `:736-738` | 3 | 0 | `except Exception: session.rollback(); raise` |

Counts are **decorator-inclusive**, this campaign's convention throughout.

Everything else in the module is already at zero missing statements and zero
missing arcs.

**The outer handler is in scope and is easy to overlook.** `:739`'s `finally`
and `:740`'s `session.close()` are already covered — every call reaches them —
but nothing in the suite makes `monitor_healthy_instances` raise all the way
out, so `:736-738` has never run. Those three statements are the module's last.
Reaching them needs a failure raised outside the per-instance `try` blocks,
because everything inside them is caught locally; the selection query at
`:540-544` and the loop machinery are the candidates.

### Entry is gated on `software`, which is why this half was deferred

`:637` requires `instance.online()` and `instance.software` in
`{'lemmy', 'piefed', 'pylova'}`. `:698` requires `instance.online()` and
`'mbin'`. Sub-project 32's fixtures used `make_instance`'s `'mastodon'`
default, so neither block ran and both `if` statements were covered only on
their false arms.

This round inverts that: fixtures set the matching `software` values.
`instance.online()` is `not (dormant or gone_forever)` (`app/models.py:118-119`)
and `_seed_instance` leaves both False, so online-ness needs no arranging —
but it does mean a test that sets `software` and forgets to think about it
enters these blocks whether it meant to or not. Sub-project 32 learned that
the hard way when a version-comparison test entered the Lemmy block by
accident and crashed on DC1's defect.

## Production changes

Three. Each lands as its own commit with its own failing observation.

**Every one must be observed failing before it is fixed.** A production change
with no failing observation behind it is the same error as a test that cannot
fail, one level up.

### DC1 — `response` is unbound in both `finally` blocks

`:689` and `:731` each read `if response:` inside a `finally`, while
`response` is assigned inside the `try` at `:639` and `:700`. `get_request`
**raises** — unlike `get_request_instance`, which catches everything and
returns a synthetic 500 — so a transport failure leaves the name unbound, the
`finally` raises `UnboundLocalError`, and the `except Exception` at `:685` /
`:727` has already run and cannot catch it. The error escapes to `:736`,
rolls back, and re-raises, ending the sweep for every remaining instance.

There is a second effect on later iterations. Python function scope means
`response` survives from the previous instance, so instead of raising, the
`finally` closes the **previous** instance's response and leaks the current
one.

**This is the reachable twin of the defect sub-project 32 fixed at two other
sites in this same function**, and it is already confirmed by observation:
that round hit it by accident and recorded the verbatim
`UnboundLocalError: cannot access local variable 'response'`.

Bind `response = None` before each `try` and guard both closes. **Use
`is not None`, not the truthiness test the surrounding code uses** — sub-project
32 chose that deliberately at its two sites and this change should match it
rather than copying the weaker local idiom.

### DC2 — `:684` invalidates the emoji cache on every response

`cache.delete_memoized(get_emoji_replacements)` sits inside the `try` but
**outside** the `if response and response.status_code == 200:` guard at `:640`.
A 404 or 503 from a Lemmy instance therefore invalidates the whole site's
emoji replacements, discarding a cache the run had no reason to touch.

Move it inside the guard.

### DC3 — `:658` deletes from the query it is iterating

`for instance_admin in session.query(InstanceRole).filter_by(instance_id=...)`
iterates a query, and `:660-664` issues a bulk `Query.delete()` against rows
that result set contains — mutating a collection mid-iteration and
desynchronizing the session's identity map from the database in the same
stroke.

Collect the rows to remove first, then delete them after the loop.

**The ordering is the risk, and this spec says so rather than treating the
change as routine.** The adds at `:650-655` are still pending in the session
when `:658` runs, so the removal's view of "current admin roles" depends on
whether those pending adds are visible to that query. A fix that collects
first must not change which rows the removal considers. **The implementer must
demonstrate that the set of rows deleted before and after the change is
identical**, on a fixture with both a surviving admin and a departing one, or
the change is not made. Getting this wrong deletes a legitimate admin role,
which is worse than the defect being repaired.

## Test design

New file: `tests/test_shared_tasks_maintenance_identity.py`.

### No respx

Every helper these blocks call is imported at module scope:
`find_actor_or_create` at `maintenance.py:13`, `get_request` and
`instance_banned` at `:19`, `get_emoji_replacements` at `:21`, `cache` at
`:12`. Tests replace them by name and hand back constructed `httpx.Response`
objects.

This avoids `get_request`'s 3-to-10-second retry sleep
(`app/utils.py:158-162`, `:173-177`) entirely, because `get_request` itself is
replaced rather than driven.

### The probe, and why it inverts the previous rounds' trap

Sub-project 31 established that an unmatched request inside
`refresh_instance_chooser` is **swallowed** by a bare `except Exception` into a
failure branch, so a test that forgot a route silently tested the wrong path.
Sub-project 32 established the same shape for `get_request_instance`, which
returns a synthetic 500 rather than raising.

**Here the opposite should hold.** The `finally`'s `if response:` raises
`UnboundLocalError` *before* the `except Exception` can absorb anything, so an
unmatched or raising request should **kill the task** rather than route it
anywhere. That makes a forgotten patch loud instead of silent — the friendlier
failure mode, and the reverse of what the last two rounds trained on.

**Task 1 must confirm this by observation and record the result. No later task
may assume either behaviour**, and the file's docstring states whichever the
probe finds.

Note that DC1 changes this: once `response` is bound and guarded, a raising
`get_request` is caught by `except Exception` at `:685` / `:727` and becomes a
failure increment. The docstring must distinguish the pre-fix and post-fix
behaviour rather than describing one as though it were both.

### Harness facts carried from sub-project 32

- **`_seed_instance`, not `make_instance`.** The `db_session` teardown runs
  `SELECT setval(c.oid, 1, false)` (`tests/conftest.py:131`), so the first
  `Instance` a test seeds becomes id 1 — which `:543` excludes. The reserved
  row is excluded by **state**, not by id, so it stays invisible wherever the
  sequence lands.
- **No ordered assertions over rows a query planner returned.** `:658` and
  `:720` both iterate queries; `:540` returns the instance list. Compare sets.
- **An oracle over a field several arms write pins nothing.** `failures` is
  written from six arms in this function once these blocks are counted. Every
  assertion names the arm it pins.

### Model facts the tests depend on

- **`InstanceRole` has a composite primary key** `(instance_id, user_id)`
  (`app/models.py:167-168`), so adding a role for a pair that already has one
  raises rather than duplicating. `:649`'s `user_is_admin` guard is what
  prevents that, and it checks `role == 'admin'` specifically
  (`app/models.py:121-123`) — a row with a different role value would slip past
  it and collide.
- **`Emoji` has no unique constraint** on `(instance_id, token)`
  (`app/models.py:4378-4384`), so `:671`'s lookup-then-branch is the only thing
  preventing duplicates.
- **`User.profile_id()` does not lowercase** (`app/models.py:1454-1456`),
  unlike `Community.profile_id()` (`:787-789`) which does. `:647` stores
  `.lower()`ed values and `:659` compares against them. This is **not** a live
  defect — `ap_profile_id` is stored lowercased at creation
  (`app/activitypub/util.py:1233`) — but it is a latent fragility worth
  registering, and a test must not accidentally depend on the asymmetry.

### Coverage per block

**Lemmy/PieFed (`:637-691`)** — the `software` fork at `:637`; the response
guard at `:640`; the admin loop at `:644` with `:646`'s scheme check on both
arms; `:649`'s create-or-skip; the removal loop at `:658` with `:659` on both
arms; `:667`'s `instance_banned` gate on both arms; the emoji loop at `:668`
with `:673`'s update-or-create fork; and the handler at `:685-687`.

**MBIN (`:698-733`)** — the `software` fork at `:698`; the response guard at
`:701`; `:706`'s username extraction with the key absent and present;
`:707`'s admin-or-moderator compound, which needs each conjunct exercised
separately because coverage sees one arc pair; `:709`'s user-found fork;
`:711`'s create-or-skip; the removal loop at `:720`; and the handler at
`:727-729`.

### Every test must be able to fail

Sub-project 32 shipped six tests and docstrings that claimed more than they
proved, and its final review found two further gaps that were **missing seeds
rather than weak assertions** — a status range no test produced, and a
boundary mutated only in the direction already caught.

Each test here names the single-line regression it catches **and the arm**.
Two specific traps this round inherits:

- **An assertion that a value stayed at its seeded default is only as strong as
  the path the mock forces the code down.** A mock that returns where
  production raises removes the signal.
- **A compound condition is one arc pair to coverage.py.** `:646`'s `or` and
  `:707`'s `and` each need their conjuncts exercised separately; mutation is
  the only instrument that sees inside them.

## Verification

- **Mutation pass** over the branch points, one at a time: dry-run without
  `-i` and read the produced line, apply, run, restore, then assert an empty
  `git diff -- app/` and the expected `wc -l`. Restore before any point where
  the work might stop — a process that dies mid-probe cannot restore, and this
  campaign has had one do exactly that. **Enumerate every site in the table and
  give each a row**; sub-project 32's pass listed a site in its own prose and
  then never mutated it, and the final review found the hole.
- **Mutate boundaries in both directions.** That round's other missed hole was
  a cutoff mutated only the way the existing tests already caught.
- **Coverage** measured with the dotted form `--cov=app.shared.tasks.maintenance`
  and `--cov-branch`, across all five maintenance test files. A path form
  collects nothing, writes no JSON and still exits 0. Read
  `summary.percent_covered`, not `percent_statements_covered`. Write the JSON
  outside the repository — `/app` is bind-mounted.
- **Floor** raised from 89 to the measured integer, rounded down.
- **Full suite** run by the controller alone, foreground and unpiped, with
  floor enforcement as the separate chained step at `tests/README.md:403-405`.

## Register

Findings from D383, facts from 198.

- The three production changes, with DC3's ordering demonstration recorded.
- **`:683`'s `session.commit()` inside the per-emoji loop** — one commit per
  emoji rather than per instance, on a path iterating an unbounded list from a
  remote server. The fifth in-loop commit this campaign has registered.
  Registered, not fixed: outside this round's approved scope.
- **`User.profile_id()`'s missing `.lower()`** against `Community.profile_id()`'s
  presence of it — latent, not live, and why.
- **`:649`'s `user_is_admin` guard checks `role == 'admin'` specifically**, so a
  row with a different role value slips past it into a composite-PK collision.
- **The module is complete.** Record the final floor and what the campaign's
  next target should be, since this is the last round in
  `app/shared/tasks/maintenance.py` and the next sub-project starts somewhere
  new. That entry matters more than usual.
- **`check_instance_health`'s batched commit remains outstanding** and
  nominated for a future round's production scope — it is not in this range,
  and this round does not fix it.

## Risks carried deliberately

- **DC3 is the least obvious of the three.** Its correctness depends on
  pending-add visibility, and a wrong fix deletes a legitimate admin role. The
  plan requires a before-and-after demonstration rather than an assertion.
- **The probe may contradict the expectation.** If an unmatched request turns
  out to be swallowed rather than fatal, later tasks change shape and the
  finding is worth more than the prediction was.
- **DC1 changes the harness's own failure mode**, so the file's docstring must
  describe pre-fix and post-fix behaviour separately.

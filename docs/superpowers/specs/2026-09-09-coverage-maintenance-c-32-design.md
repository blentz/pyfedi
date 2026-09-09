# Coverage sub-project 32: `maintenance.py` Group C, the instance-health tasks (first half)

**Status:** approved, not yet implemented
**Branch:** `blentz`
**Predecessor:** sub-project 31, 19 commits `e10d8787..01aca9b9`, Group D closed, module floor 47 → 66

## Goal

Take the first half of `app/shared/tasks/maintenance.py`'s Group C to zero
missing statements, and fix four production defects found while reading it.

Group C is the last group in this module. It is 196 statements and 47 branch
points, all 94 branch arcs currently missing — no test in the suite reaches any
of it. That is too much for one round, so it splits in two. This spec covers
the first half.

## Scope

In scope, 131 statements and 54 missing arcs measured against the current tree:

| Function | Lines | Statements | Missing arcs |
|---|---|---|---|
| `sync_defederation_subscriptions` | `:408-425` | 12 | 2 |
| `check_instance_health` | `:426-507` | 49 | 18 |
| `monitor_healthy_instances`, HTTP half | `:508-603` | 70 | 34 |

Counts are **decorator-inclusive**: each `@celery.task` line is a statement to
coverage.py, and that is the convention this campaign uses throughout. Counting
from `def` lines instead gives 193 for Group C rather than 196. Sub-project 31's
Group C entry records both numbers and why 196 is the right one, because that
round mistakenly "corrected" 196 to 193 and had to restore it.

Out of scope, deferred to sub-project 33: `monitor_healthy_instances`' identity
half — Lemmy/PieFed admin roles and custom emoji at `:606-660`, MBIN admin roles
at `:667-702` — 65 statements and 40 missing arcs.

### The split is mechanical, not conventional

`:606` requires `instance.online() and instance.software` in
`{'lemmy', 'piefed', 'pylova'}`. `:667` requires `instance.online() and
instance.software == 'mbin'`. This round's fixtures set `software='mastodon'`,
so neither body executes and the deferred half stays genuinely untouched.

Both `if` statements still evaluate, so this round covers their **false** arms.
Sub-project 33 owns the true arms. A later round measuring missing arcs should
expect those two false arcs already closed and not treat that as drift.

## Production changes

Four, more than any prior round in this campaign, and three of them touch the
same function. Each lands as its own commit with its own failing observation, so
a reviewer can reject one without unwinding the others.

**Every one must be observed failing before it is fixed.** A production change
with no failing observation behind it is the same error as a test that cannot
fail, one level up.

### DC1 — `monitor_healthy_instances` never wraps its body in `patch_db_session`

`get_request_instance` (`app/utils.py:189-196`) catches every exception from the
underlying request and, at `:193-195`, does `instance.failures += 1`,
`instance.update_dormant_gone()` and `db.session.commit()` before returning a
synthetic `httpx.Response(status_code=500)`.

`check_instance_health` wraps its body in `patch_db_session(session)` at `:431`,
so those writes land on the task's own session. `monitor_healthy_instances` does
not. Its `instance` objects are loaded from `session` at `:515`, while
`get_request_instance` mutates and commits them through Flask-SQLAlchemy's
`db.session` — a different session for the same rows.

`update_dormant_gone` (`app/models.py:146-150`) sets `dormant` and
`gone_forever`, so the split affects instance lifecycle state, not only a
failure counter.

This is the shape sub-project 30 found in `pwn_bots`. Its discriminator applies
directly: a `before_cursor_execute` listener on `db.engine` recording
`id(conn.connection)` per statement should show two distinct checkouts before
the fix and one after.

**Precondition, from sub-project 31's PC2.** `patch_db_session` short-circuits
when `has_request_context()` (`app/utils.py:3685`, returning at `:3688`). The
`app` fixture pushes an app context, not a request context, so the guard is
False and the wrapper takes effect. Confirm that before trusting either result —
without it, a null result is ambiguous between "no defect" and "the instrument
is blind".

**If the discriminator shows one checkout either way, do not make this change.**
Sub-project 31's PC2 was investigated and correctly not made. The round then
lands three production changes, and DC1 becomes a register-only finding.

### DC2 — `nodeinfo` and `node` can be unbound in their `finally` blocks

`:560-561` closes `nodeinfo`, assigned at `:533`. `:591-592` closes `node`,
assigned at `:566`. If the assigning call raises, the name was never bound and
the `finally` raises `UnboundLocalError`, which the `except` at `:557`/`:582`
cannot catch because it has already run. The error escapes to `:705`, rolls back
and re-raises, and one instance's failure ends the entire sweep.

Bind both to `None` before their `try` blocks and guard the close.

**This justification is weaker than the round's usual bar, and this spec says so
rather than dressing it up.** In production the path needs `get_request_instance`'s
own handler to fail at `:193-195` — its bare `except:` swallows everything the
request itself raises. The failing observation is therefore produced by patching
that helper to raise, which tests robustness against a dependency's contract
rather than a defect a user sees today. That is the same class of justification
as sub-project 31's PC1, and it is recorded the same way.

The genuinely reachable instances of this shape are at `:658` and `:700`, which
call `get_request` directly — and `get_request` does raise. Those are in sub-project
33's range and are registered, not fixed, here.

### DC3 — `:553` counts one failure per non-matching link

`:541`'s loop walks a nodeinfo document's `links`. `:552-553`'s `else` arm
increments `instance.failures` for every link whose `rel` is not one of the three
recognised schema URLs. A document carrying five unrelated links records five
failures, and `update_dormant_gone`'s thresholds — dormant above 2, gone above 7
— are crossed by document shape rather than by reachability.

Move the increment out of the loop so an unmatched document counts once.

### DC4 — `:527` compares versions as strings

`:526-528` reads `instance.version >= '0.19.4'` to decide whether a Lemmy
instance's `nodeinfo_href` should be discarded and rediscovered. String
comparison puts `'0.19.10'` **below** `'0.19.4'`, so exactly the newer instances
the check exists to catch take the wrong path.

Replace with a comparison that orders numerically. The module has no version
helper today, so one arrives with this change; keep it to what this site needs.

## Test design

New file: `tests/test_shared_tasks_maintenance_health.py`.

### No respx

Every helper these tasks call is imported at module scope (`maintenance.py:13`
and `:19`), so `app.shared.tasks.maintenance.get_request_instance`,
`.get_request`, `.instance_banned` and `.download_defeds` are all reachable by
the namespace idiom. Tests return constructed `httpx.Response` objects.

This avoids two traps the previous round spent effort on. Sub-project 31's
central hazard — an unmatched respx request being swallowed by a bare
`except Exception` and silently routing a test into a failure branch — cannot
arise, because no request escapes to a transport. And `get_request`'s 3-10
second retry sleep (`app/utils.py:158-162` and `:173-177`) is never entered,
because `get_request` itself is replaced rather than driven.

A test that needs a helper to fail patches it to raise. A test that needs a
non-200 returns `httpx.Response(status_code=...)`.

### Harness facts

- **Seeded instances must have `id != 1`.** `:449` and `:518` both exclude it,
  and the conftest fixtures reserve `instance_id=1`.
- **`:436`, `:446` and `:515` return planner-ordered lists.** Assertions over
  more than one instance compare sets. The campaign's standing rule against
  asserting an order a query planner chose applies unchanged.
- **`instance_banned` and the literal `'flipboard.com'`** gate `:453` and `:522`.
  Fixtures either patch `instance_banned` or pick a domain that is not banned.
- `Instance.online()` (`app/models.py:118-119`) is `not (dormant or gone_forever)`.

### Coverage per function

**`sync_defederation_subscriptions`** — seed `DefederationSubscription` rows
(the model carries only `id` and `domain`, `app/models.py:79-81`), patch
`download_defeds`, and assert the `DELETE FROM banned_instances WHERE
subscription_id is not null` at `:413` removed subscription-sourced bans while
leaving admin-placed ones — `BannedInstances.subscription_id` is None for those
(`app/models.py:70`). Assert each subscription was handed to `download_defeds`
with its id and domain. Cover the `except` arm at `:419-421` by making
`download_defeds` raise, and assert the task re-raises rather than swallowing.

**`check_instance_health`** — two loops. The first marks dormant instances
`gone_forever` once `start_trying_again` is more than five days past (`:435-443`).
The second re-checks dormant instances that are not yet gone (`:446-497`), with
four paths: an instance skipped by `:453`; a `nodeinfo_href` present and
returning 200 with `software` (`:458-470`); no `nodeinfo_href`, so discovery
walks `links` (`:471-493`); and the handler at `:494-497` incrementing failures.
Note `:469` and `:492` are `finally` blocks that close the response — assert
they run.

**`monitor_healthy_instances`, HTTP half** — the discovery block at `:531-562`
and the fetch-and-escalate block at `:564-603`. The escalation thresholds at
`:579-581`, `:586-590` and `:598-602` are boundaries, so each needs a test on
both sides of it rather than one comfortably past. `:595`'s else arm — no
`nodeinfo_href` after discovery — is a distinct path from `:564`'s true arm.

### Every test must be able to fail

Sub-project 31 shipped four tests that could not, and each was caught by a
different instrument: one by a reviewer simulating the regression, one by a
reviewer reading a docstring's claim against the code, one by asking what else
could make an assertion hold, and one by a reviewer refusing a colleague's
"unobservable" verdict. Each test here names the single-line regression it
catches, and that claim is checked rather than asserted.

Two specific traps this round inherits:

- **An assertion that holds trivially proves nothing.** Sub-project 31's
  `:1043` test asserted no row existed where no row was ever seeded. Assert
  something that only holds if the path ran.
- **Name the mechanism you actually checked.** Two docstrings last round
  credited the wrong protection, in opposite directions. A docstring stating a
  mechanism the code does not use is a defect here, because the register is a
  deliverable and future rounds reason from it.

## Verification

- **Mutation pass** over the branch points, one at a time: dry-run without `-i`
  and read the produced line, apply, run, restore, then assert an empty
  `git diff -- app/` and the expected `wc -l`. Restore before any point where
  the work might stop. Survivors are either proven equivalences, recorded with
  the proof, or holes, closed with a test. A compound condition is one arc pair
  to coverage.py, so mutation is the only instrument that sees inside it — last
  round found two real holes exactly there.
- **Coverage** measured with the dotted form `--cov=app.shared.tasks.maintenance`
  and `--cov-branch`, across all four maintenance test files. A path form
  collects nothing, writes no JSON and still exits 0. Read
  `summary.percent_covered`, the combined statement-and-branch figure —
  `percent_statements_covered` read four points higher last round and would set
  an unholdable floor.
- **Floor** raised from 66 to the measured integer, rounded down. Floors only
  rise. Write the JSON outside the repository: `/app` is bind-mounted, so a
  coverage run otherwise leaves an artifact in the working tree.
- **Full suite** run by the controller alone, one pytest session at a time, in
  the foreground and unpiped — a pipeline eats pytest's exit status, and pytest
  exits 1 on a session timeout. Floor enforcement is the separate chained step
  at `tests/README.md:403-405`; confirm the report's mtime falls at the end of
  the run that produced it.

## Register

Findings from D374, facts from 187. Beyond the four production changes and
whatever the mutation pass turns up:

- **`sync_defederation_subscriptions:413-414`** deletes every subscription-sourced
  ban and commits before re-downloading at `:416-417`. Between those points the
  instances are unbanned, and a failure mid-run leaves the table empty. Registered,
  not fixed — the repair is a build-then-swap restructure, outside this round's
  approved scope.
- **`:658` and `:700`** are the reachable instances of DC2's shape, since
  `get_request` raises where `get_request_instance` does not. Sub-project 33's.
- **`:653`** invalidates the emoji cache even when the response was not 200.
  Sub-project 33's.
- **`get_request_instance` swallows everything**, including `KeyboardInterrupt`
  and `SystemExit`, via a bare `except:` at `app/utils.py:192`, and returns a
  synthetic 500 that callers cannot distinguish from a real one.
- **Group C's remaining half** — statements, branch points and the four-phase
  structure — so sub-project 33 does not re-derive them.

## Risks carried deliberately

- **Four production changes** is this campaign's widest scope. Separate commits
  and separate failing observations are the mitigation.
- **DC2's justification is robustness, not a live bug**, and is recorded as such.
- **DC1 may not reproduce.** That is an anticipated outcome, not a failure; the
  round then lands three changes and registers the fourth.
- **DC4 introduces a version comparison** where the module had none. Keeping it
  to this one call site is deliberate; a general helper is a larger change than
  this round approved.

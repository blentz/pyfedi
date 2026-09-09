# Sub-project 29: `maintenance.py` Group A — the retention and counter tasks

**Date:** 2026-09-08
**Branch:** `blentz`
**Predecessor:** sub-project 28, commits `7a4e9ac9..af3c4522`, kept as-is

## Goal

Take the ten pure-database tasks in `app/shared/tasks/maintenance.py` to 100%
statement and branch coverage, give the module its first `coverage_floors.ini`
entry, and land three production changes.

`maintenance.py` is 1181 lines, 636 statements, 202 branches, and today sits at
7.52% — 63 statements executed, 573 missed. It is the last module in
`app/shared/tasks/` below its floor, and at three times the size of the largest
module this campaign has closed in one round (`deletes.py`, 201 statements) it
cannot be one sub-project. This spec takes the first of four groups.

## Why this module is decomposed, and along which seam

The twenty-four top-level functions divide by **testing surface**, not by
subject matter. What a test of one of these tasks has to fake is what
determines how much work it is:

| Group | Functions | Stmts | What a test must fake |
|-------|-----------|-------|-----------------------|
| **A** (this round) | `cleanup_old_notifications`, `cleanup_old_read_posts`, `cleanup_send_queue`, `cleanup_old_activitypub_logs`, `cleanup_old_voting_data`, `update_hashtag_counts`, `update_community_stats`, `unban_expired_users`, `recalculate_user_attitudes`, `calculate_community_activity_stats` | 151 | nothing — rows and clock only |
| **B** | `process_expired_bans`, `remove_old_community_content`, `remove_old_bot_content`, `delete_old_soft_deleted_content`, `archive_old_posts`, `archive_old_users`, `archive_user`, `pwn_bots` | 154 | federation sends, `delete_post` |
| **C** | `check_instance_health`, `monitor_healthy_instances`, `sync_defederation_subscriptions` | 196 | nodeinfo negotiation over `httpx` |
| **D** | `refresh_instance_chooser`, `add_remote_communities`, `add_remote_community_from_post`, `delete_from_s3`, `clean_up_tmp` | 119 | `httpx`, `boto3`, the filesystem |

Sixteen further statements are module-level imports.

Group A goes first because it needs no transport at all. Establishing the
cron-task harness against real Postgres, with no network to mock, is the
cheapest place to learn this module's shape before Groups C and D bring
`httpx` and `boto3` into a package that has never needed them.

## What makes this group unlike sub-projects 24 through 28

**There is no `task_selector` and no federation send.** Every task in Group A
is invoked from a cron entry point in `app/cli.py` (`:813-925`), never from a
route and never through `app/shared/tasks/__init__.py`. The three oracles the
last five rounds relied on — a `flash()`, a return value forked on `SRC_`, and a
delivered-inbox set — all drop out. **The oracle here is a row count or a column
value, sampled before and after.**

**The code is branch-free.** Seven branch points across 151 statements, against
`follows.py`'s forty-four across 155. Ten of these functions share one skeleton:

```python
session = get_task_session()
try:
    ...
    session.commit()
except Exception:
    session.rollback()
    raise
finally:
    session.close()
```

About thirty of the 151 statements are those ten error paths. Covering them
means ten tests that force an exception inside the `try` and assert it
propagates — the same test written ten times against ten different tasks.

**Raw SQL is the majority.** Twenty-seven `text()` calls in the module,
concentrated here: `cleanup_old_voting_data` is four `text()` DELETEs,
`calculate_community_activity_stats` is seven `text()` statements including a
`CREATE TEMPORARY TABLE ... ON COMMIT DROP` and a `CREATE INDEX`. There is no
ORM layer to intercept, so a test cannot patch its way to an assertion; it must
put real rows in the container's Postgres and count them afterwards.

**`patch_db_session` is live and load-bearing.** `cleanup_old_read_posts:50` and
`recalculate_user_attitudes:719` wrap their bodies in it, because `get_setting`
(`app/utils.py:203-211`) and `User.recalculate_attitude()` read `db.session`
rather than the task's session. Facts 156 and 157 bind directly: the harness
pushes only an app context (`tests/conftest.py:112`), and pushing a request
context would disable `patch_db_session` at `app/utils.py:3684`.

**Fact 153 binds throughout.** Each task writes through its own `Session` from
`get_task_session()`. Any oracle that reads an ORM attribute off an object the
test built earlier reads stale state unless the test calls
`db.session.expire_all()` first. A count through a fresh query is immune; an
attribute read is not. These tasks are *defined* by the columns they rewrite, so
this round will do more attribute-reading than any round since 27.

## Production changes

Three, matching sub-projects 24 through 28. **All three are predictions from
reading. None has been observed.** Sub-project 28 was built around a predicted
`ObjectDeletedError` that a container probe disproved, and two of its three
production changes turned out to be hardening rather than repairs. Each change
below therefore carries the same requirement: **the round observes the defect
before it changes anything, and pastes what actually happened.** A prediction
that does not reproduce is registered as a finding and the change is dropped.

### PC1 — `calculate_community_activity_stats` leaves stale activity stats in place

The UPDATE at `:845-858`, driven by the loop at `:844`, runs once per row
returned by the SELECT at `:827-839`, and
that SELECT reads `FROM temp_community_activity tca INNER JOIN "community" c`.
A community with no post, reply, or vote in the last six months contributes no
row to the temp table, so the INNER JOIN drops it and its `active_daily`,
`active_weekly`, `active_monthly` and `active_6monthly` keep whatever they last
held. Nothing else writes those columns: `app/cli.py:788` is the only other
site, and it is a separate command.

The fix is to make absence produce a zero instead of a gap — drive the SELECT
from `community` and outer-join the temp table, so every community that passes
the eligibility filter yields a row whose counts are 0 when it has no activity.

**Scope note, deliberately narrow.** `:836-837`'s `c.banned = FALSE AND
c.last_active > :half_year` stays exactly as it is. A banned community, or one
whose `last_active` has fallen outside six months, still keeps its stale
numbers. Widening that filter is a question about what these columns are
*for* — whether a dormant community should read zero or read its last known
value — and this round can register that better than it can answer it. PC1
makes exactly one claim: a community the filter admits gets a number computed
this run, not a number left over from a previous one.

### PC2 — `update_community_stats` commits inside its loop

`:317`'s `session.commit()` sits inside the `for community in communities:` loop
opened at `:292`. Two consequences follow, and the round must observe both
before touching it:

1. **The task is not atomic despite having a rollback.** A failure at community
   N leaves communities 1..N-1 committed; `:319-321`'s handler rolls back only
   the current unit of work. The rollback reads as if it protects the task's
   whole effect, and it does not.
2. **It forces an N+1.** `expire_on_commit` defaults to `True` (fact 58), so
   every commit expires all loaded `Community` instances. Iteration N+1's first
   attribute read re-SELECTs its row. This is measurable: count queries across a
   run with several eligible communities.

The fix moves the commit out of the loop, to one commit after it.

**This changes transaction size**, which is the reason it is a judgment call
rather than an obvious repair. One transaction over every community active in
the last three days holds its row locks for the whole run. The register entry
must say so, and the test that pins the fix must assert the atomicity property
directly — a failure partway through leaves no partial writes — rather than
merely asserting that the counts come out right.

### PC3 — `recalculate_user_attitudes` carries a dead counter

`:716` assigns `processed = 0` and `:737` increments it. Nothing reads it. Two
statements whose only effect is to be counted as covered. Delete both.

This is the cheapest change in the round and the only one whose prediction
cannot fail: deadness is decidable by reading, and the round confirms it with a
grep over the function's whole range rather than by observation.

## Findings to register, not fix

- **`calculate_community_activity_stats:819-821` indexes a temp table** that
  exactly one aggregate scan reads. Recorded as an observation. This campaign
  does not tune performance, and an index whose build cost exceeds its benefit
  is a performance question, not a correctness one.
- **Two docstrings contradict their code.** `:26` says "Remove notifications
  older than 90 days"; `:32-34` also deletes `RevokedToken` rows older than 365
  days. `:46` says "older than 180 days"; `:51` reads
  `get_setting('read_posts_cutoff', 180)`, so 180 is a default and the real
  cutoff is whatever the setting holds. Registered rather than fixed — the
  approved production scope is three changes, and a docstring correction that
  rides along unapproved is the shape of sub-project 28's fourth edit.
- **Raw `text()` DELETEs bypass the identity map.** `cleanup_old_read_posts:52`
  and `cleanup_old_voting_data`'s four DELETEs remove rows the session may hold
  loaded instances for. Harmless as written, because `get_task_session()`
  sessions are short-lived and these tasks load nothing first. Recorded so the
  next round that adds a query above one of them knows the hazard exists.
- **Facts 156 and 157 carry an off-by-one citation.** `tests/README.md:5181-5182`
  says `app/utils.py:3685` is `if has_request_context():` and `:3688` is the
  `return` inside it. At this tree `:3684` is the `if` and `:3687` is the
  `return`. The tree has not moved since those facts were written, so the
  citation was wrong when recorded. This round corrects both numbers in place —
  a fact whose line number is wrong is worse than no fact, because a reader who
  opens it finds a comment and concludes the fact is stale.

- **`cleanup_old_voting_data` hardcodes `'instance_id': 1`** in all four
  DELETEs. Consistent with fact 141's `Instance.id != 1` idiom rather than a
  defect; recorded so it is not re-litigated.

## Test architecture

One new file: `tests/test_shared_tasks_maintenance_cleanup.py`. The name scopes
it to Group A so Groups B, C and D can take their own files without a rename.
Fact 158 applies to measuring it — grep by import, not by function name.

**Per task, three tests.** One that proves the rows the task targets are gone or
rewritten. One that proves rows on the *other* side of the cutoff survive
untouched — the boundary is where an off-by-one hides, and a test that only
checks the deletion passes just as well against `<=` as against `<`. One that
forces the `except` arm and asserts the exception propagates rather than being
swallowed.

**The clock is the fixture.** Every cutoff in this group is `utcnow() -
timedelta(...)`. Tests set row timestamps relative to `utcnow()` rather than
patching the clock, so a row "91 days old" is written as `utcnow() -
timedelta(days=91)`. This keeps the test readable and avoids a frozen-clock
dependency the harness does not currently carry.

**Config-driven arms need config, not mocks.** `cleanup_old_voting_data`'s two
branch points read `current_app.config['KEEP_LOCAL_VOTE_DATA_TIME']` and
`KEEP_REMOTE_VOTE_DATA_TIME` (`config.py:177-178`, both defaulting to 6). The
`-1` arms are taken by setting the config values, not by patching.

**`calculate_community_activity_stats` needs four kinds of activity row.** Its
temp table is filled from posts, post replies, post votes and post reply votes
(`:774-816`), each with its own `from_bot`/`bot` exclusion. A test that seeds
only posts covers the statements but proves nothing about the other three
INSERTs. Seed all four.

**Error-path tests need a failure the task cannot catch early.** The ten
`except Exception:` arms are reached by making the task's own work raise. The
mechanism differs per task and the plan names one per task rather than leaving
it to the implementer; a monkeypatched `session.execute` that raises is the
default shape.

## Oracles

Row counts and column values through a **fresh query**, never an attribute read
on an object the test built before the task ran (fact 153). Where an attribute
read is genuinely the clearest assertion — `update_community_stats` rewriting
`community.post_count`, for instance — the test calls `db.session.expire_all()`
first and the plan says so at that step.

The federation oracles from rounds 24 through 28 do not apply and must not be
copied in. There is no `post_request`, no `ActivityPubLog` row written by these
tasks, and no respx route to count. Fact 148's warning about swallowed
unmatched requests is irrelevant here, and a test that reaches for it has
misread the module.

## Deliverables

- `tests/test_shared_tasks_maintenance_cleanup.py`, new.
- Group A's ten functions at 100% statement and branch coverage.
- `coverage_floors.ini` gains `app/shared/tasks/maintenance.py` — 20 entries.
  The number is whatever the run measures; from 63 of 636 statements today,
  covering Group A's 151 should land near 30.
- Three production changes: PC1 `calculate_community_activity_stats`,
  PC2 `update_community_stats`, PC3 `recalculate_user_attitudes` — each
  observed before it is made.
- Mutations covering PC1's outer join, PC2's commit placement, both of
  `cleanup_old_voting_data`'s config guards, `update_community_stats`'s
  `is_local()` compound at `:305-306`, and each cutoff boundary.
- Findings from D342; facts from 161.
- Full suite green, all 20 floors met against a report whose mtime postdates the
  run.

## Risks

**PC1's fix may be larger than an outer join.** Driving the SELECT from
`community` changes which rows the aggregate produces and therefore what the
`GROUP BY` at `:838` groups. If the rewritten query proves not to be a
one-clause change, PC1 becomes a register-only finding and the round lands two
production changes rather than three. That is the correct outcome, not a
failure — sub-project 28's lesson is that a change made to hit a count is worse
than a count that comes up short.

**PC2 may not reproduce.** The N+1 claim depends on `expire_on_commit` actually
expiring these instances. Fact 155 is the cautionary case: a deleted-then-
committed instance turns out to be expunged rather than expired, and the
prediction built on the general rule was wrong. These instances are neither
deleted nor detached, so the general rule should hold — but "should hold" is
what sub-project 28 said too. Observe it with a query count before changing
anything.

**`CREATE TEMPORARY TABLE ... ON COMMIT DROP` inside the test transaction.**
`tests/conftest.py`'s session fixture runs each test in a transaction it rolls
back. A temp table declared `ON COMMIT DROP` interacts with that, and
`calculate_community_activity_stats` commits at `:860` — inside a test, that
commit lands against the fixture's transaction. Whether the task can run at all
under the harness is the first thing the plan's first task must establish, before
any test of its behaviour is written. If it cannot, the plan needs a different
fixture for that one function and must say which.

**The suite's session timeout.** `pytest.ini:28` caps a session at 600s and the
suite has hit it when the container stack accumulates state across consecutive
runs. `./run_tests.sh --down` before each measured run, and scoped coverage
targets in the dotted module form, remain mandatory.

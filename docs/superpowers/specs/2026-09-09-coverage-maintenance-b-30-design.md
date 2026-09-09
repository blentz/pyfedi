# Sub-project 30: `maintenance.py` Group B — the content-lifecycle tasks

**Date:** 2026-09-09
**Branch:** `blentz`
**Predecessor:** sub-project 29, commits `af3c4522..908211c8`, kept as-is

## Goal

Take the eight content-lifecycle tasks in `app/shared/tasks/maintenance.py` to
100% statement and branch coverage, raise the module's floor from 24, and land
three production changes.

Sub-project 29 closed Group A — the ten tasks that need no transport — and gave
the module its first floor. Group B is the second of four groups. After this
round the module's two genuinely network-heavy groups, C and D, remain.

Measured at `908211c8`: `maintenance.py` is 634 statements at 24.64%. Group B
is **154 statements, 141 missing, 23 branch points**.

## Group B, measured

| Function | Lines | Stmts | Branch pts | What a test must arrange |
|----------|-------|-------|-----------|--------------------------|
| `process_expired_bans` | `:76-133` | 33 | 4 | a `Notification`, four `cache.delete_memoized` calls |
| `remove_old_community_content` | `:134-159` | 15 | 2 | `delete_post` |
| `remove_old_bot_content` | `:160-193` | 19 | 3 | `delete_post`, a batching loop |
| `delete_old_soft_deleted_content` | `:215-282` | 28 | 5 | raw SQL, `delete_dependencies` |
| `archive_old_posts` | `:885-932` | 20 | 4 | **boto3/S3**, `archive_post` |
| `archive_old_users` | `:933-956` | 13 | 2 | file deletion from disk |
| `archive_user` | `:957-973` | 14 | 2 | `delete_from_disk` |
| `pwn_bots` | `:1161-1180` | 12 | 1 | nothing |

**Twenty-three branch points against Group A's seven.** Group A was
straight-line SQL wrapped in a repeated try/except skeleton; Group B branches on
config values, on whether a row exists, on whether an image is shared between
posts, and on whether S3 is configured. The error-path skeleton is still there
and still needs its eight tests, but it is a smaller share of the work than it
was last round.

## What makes this group unlike Group A

**Two tasks delete content through `app/shared/post.py`.** `delete_post`
(`app/shared/post.py:755`) is 40%-covered production code with its own
federation behaviour, and `remove_old_community_content:150` and
`remove_old_bot_content:184` both call it. This round does **not** test
`delete_post`; it tests that these two tasks select the right posts and hand
them over correctly. Where `delete_post`'s own behaviour would otherwise be
under test, the tests assert on the call rather than on its federation
side effects.

**One task reaches S3.** `archive_old_posts:911-918` builds a `boto3` client,
guarded by `:910`'s `if store_files_in_s3():`, passes it to `archive_post`, and closes it
at `:923`. This is the first S3 dependency the campaign has had to arrange in
the tasks package. Both arms need covering: `s3 = None` when object storage is
off, and a real client object when it is on.

**Two tasks are gated entirely by config.** `archive_old_posts:887` and
`archive_old_users:935` both open with `if current_app.config['ARCHIVE_POSTS'] >
0:`, and `config.py:184` defaults that to **0** — so both tasks are no-ops out
of the box and every test of their bodies must set it. `remove_old_bot_content`
is gated the same way by `BOT_CONTENT_RETENTION` (`config.py:190`, default 6,
with `-1` documented as "forever, no deletion"). Sub-project 29 established the
pattern for this: capture the original value, set it, restore the captured
value in a `finally` — never restore a hardcoded default.

**`archive_post` opens its own session.** `app/utils.py:4945-4948` calls
`get_task_session()` and wraps its body in `patch_db_session`, so
`archive_old_posts`' own session is used only for the SELECT at `:908`. A test
asserting on rows after `archive_old_posts` runs is reading across two task
sessions, not one.

## Production changes

Three, matching sub-projects 24 through 29. **All three are predictions from
reading. None has been observed.** Sub-project 28 was built around a predicted
`ObjectDeletedError` that a container probe disproved. Sub-project 29 predicted
two defects and observed both — which is what made its changes defensible. Each
change below carries the same requirement: **observe the defect before changing
anything, and paste what actually happened.** A prediction that does not
reproduce is registered as a finding and the change is dropped.

### PC1 — `archive_old_users` can never archive a user with only one image

`:942` filters:

```sql
WHERE u.avatar_id IS NOT NULL AND u.cover_id IS NOT NULL AND u.ap_id IS NOT NULL
```

but `archive_user` guards the two images independently — `:959`'s `if
user.avatar_id:` and `:964`'s `if user.cover_id:`. The function that processes a
user handles one-image users correctly; the query that selects them excludes
those users entirely. A remote user with an avatar and no cover is never
archived, however long they have been idle, and the task's stated purpose is
"Remove images from old remote users to reduce image storage".

The fix changes `AND` to `OR` between the two image conditions, leaving
`u.ap_id IS NOT NULL` and `u.last_seen < :cutoff` joined by `AND` as they are.

**Observation shape:** seed a remote user with an avatar and no cover, idle past
the cutoff, run the task, and assert the avatar is gone. Expect it to survive
against unmodified code.

**Scope note.** `:959` and `:964` are unchanged. The fix widens which users are
selected, not what is done to them.

### PC2 — `pwn_bots` reads through `db.session` and writes through its own

`:1166` iterates `BotChallenge.query.filter(...)`. `Model.query` is
Flask-SQLAlchemy's, bound to `db.session` — not to the `session` that
`:1163`'s `get_task_session()` returned, and not wrapped in `patch_db_session`.
The two `session.execute(...)` calls at `:1167` and `:1170` write through the
task's session. So the task reads through one session and writes through
another, in the one function in this group that does not wrap anything.

Every sibling that mixes the two wraps the body: `remove_old_community_content`
at `:140`, `remove_old_bot_content` at `:165`, `delete_old_soft_deleted_content`
at `:220`. This is D314's shape — a `patch_db_session` omission — and D314 is
already a tracked finding.

The fix wraps the body in `with patch_db_session(session):`, matching its
siblings, rather than rewriting the query.

**Observation shape:** this one may not reproduce under the harness, and the
round must say so if it does not. `tests/conftest.py:112` pushes an app context,
so `BotChallenge.query` resolves and reads the same database the task writes to
— the split is invisible in-process. What the split threatens is a Celery worker
with no app context. **If no test can distinguish patched from unpatched, PC2
becomes a register-only finding and the round lands two production changes.**
Sub-project 29 hit exactly this with `patch_db_session` and recorded it as
fact 163's neighbour rather than pretending a test proved it.

### PC3 — two tasks carry no `@celery.task` decorator

`remove_old_bot_content:160` and `pwn_bots:1161` are undecorated. Every other
task in this module has the decorator. `archive_user` and
`add_remote_community_from_post` are also undecorated and correctly so — they
take arguments and are helpers, not tasks.

The fix adds `@celery.task` to both.

**Scope note, and a correction to how this was first put.** Adding the decorator
makes each function **dispatchable** with `.delay()`. It does **not** schedule
them. `remove_old_bot_content` is called at `app/cli.py:911` and nowhere else,
inside the synchronous `daily_maintenance` (`app/cli.py:884-885`); it is absent
from `daily_maintenance_celery`'s import list at `app/cli.py:812-820` and from its
call sequence. `pwn_bots` is imported at `app/cli.py:40` and likewise not part
of the Celery cron path. **This round does not edit `app/cli.py`**, so neither
function starts running anywhere it does not already run. Scheduling them is a
separate decision, and the register entry must say that the decorator alone does
not make it.

## Findings to register, not fix

- **Three more in-loop commits**, the shape sub-project 29 fixed as D342:
  `process_expired_bans:117`, `delete_old_soft_deleted_content:257` and `:273`,
  and `archive_user:970` driven by `archive_old_users:947`'s loop. Registered
  rather than fixed, deliberately: D342's own entry discloses that moving such a
  commit out of its loop cost an all-or-nothing starvation regression, and these
  four sites delete content rather than recompute counters, so the failure mode
  of a partial run is different and worse to get wrong. Recorded with that
  reasoning so a future round has the argument rather than the conclusion.
- **`remove_old_bot_content` never commits.** Its body has no `session.commit()`
  at all; it relies entirely on `delete_post`'s internal commits. Correct as
  written, and worth recording so a future edit that adds a direct write to this
  function knows the write would be lost.
- **The two `delete_post` call sites pass different federation policies, and
  both are defensible.** `delete_post`'s second parameter is `federate_deletion`
  (`app/shared/post.py:755`), not a locality flag.
  `remove_old_community_content:150` passes `False`, so a retention-policy
  deletion is never federated and remote instances keep the post.
  `remove_old_bot_content:184` passes `post.author.is_local()`, so a local
  author's bot post is federated. Two policies, not an asymmetry. Recorded
  because the call sites look inconsistent to a reader who has not opened the
  signature — which is how this round first read them.
- **`archive_old_posts` and `archive_old_users` share one config gate.**
  `ARCHIVE_POSTS` (`config.py:184`, default 0) turns both off together, and
  `archive_old_users` uses it to mean "archive users idle for
  `ARCHIVE_POSTS * 28` days". One knob, two units. Registered as an observation.
- **`delete_old_soft_deleted_content:242-250` computes a shared-image set to
  avoid a cascade failure**, with a source comment saying so and deferring the
  real fix. Registered so the deferral is visible outside the comment.

## Test architecture

One new file: `tests/test_shared_tasks_maintenance_lifecycle.py`. Group A's file
is `tests/test_shared_tasks_maintenance_cleanup.py` and stays as it is; the
names split the module by group so C and D can follow.

**Per task, the same three-test spine as Group A** — the rows the task acts on,
the rows on the other side of its cutoff or filter, and the `except` arm — plus
one test per branch point that the spine does not already take.

**`delete_post` is arranged, not exercised.** For the two tasks that call it,
the oracle is which post ids reach it. Assert on the calls, not on federation.
This keeps the round inside its own module and out of `app/shared/post.py`,
which is a floored module at 40 with its own future sub-project.

**S3 is arranged the same way.** `archive_old_posts`' two arms are distinguished
by whether `store_files_in_s3()` returns true, and the oracle is whether
`archive_post` receives a client or `None`. No S3 endpoint is contacted.

**Config gates need config, not mocks.** `ARCHIVE_POSTS` and
`BOT_CONTENT_RETENTION` are read from `current_app.config`. Capture the original
value, set it, restore the captured value in a `finally` — never a hardcoded
default. Sub-project 29 shipped that bug and had to fix it in a review round.

**`cache.delete_memoized` is called four times in `process_expired_bans`**
(`:111-114`) and twice in its instance-ban loop (`:121-122`). These are
side-effect calls on a real cache. The tests assert the notification and the
membership change; whether they also assert the cache calls is a plan-level
decision, and the plan must make it rather than leave it to an implementer.

**Harness facts 153, 156, 157, 161-171 all bind here**, unchanged from Group A:
the fixture does not roll back, the task runs on its own connection, seeded rows
must be committed before the task runs, and an ORM attribute read afterwards is
stale unless `db.session.expire_all()` is called first.

## Deliverables

- `tests/test_shared_tasks_maintenance_lifecycle.py`, new.
- Group B's eight functions at 100% statement and branch coverage.
- `coverage_floors.ini`'s `app/shared/tasks/maintenance.py` raised from 24 to
  whatever the run measures — Group A plus Group B is 303 of 634 statements, so
  roughly 48, measured rather than assumed.
- Three production changes: PC1 `archive_old_users`, PC2 `pwn_bots`,
  PC3 the two decorators — each observed before it is made, and PC2 dropped to a
  register-only finding if no test can distinguish it.
- Mutations covering PC1's `OR`, both `archive_old_posts` arms,
  `remove_old_bot_content`'s config gate and its batching boundary,
  `delete_old_soft_deleted_content`'s shared-image guard and its
  `has_replies` guard, and `process_expired_bans`' `is_local()` arm.
- Findings from D351; facts from 172.
- Full suite green, all 20 floors met against a report whose mtime postdates the
  run.

## Risks

**PC2 may be untestable in-process, and that is a real outcome.** The spec says
so above rather than discovering it mid-round. The round lands two production
changes if so.

**`delete_post` is 40%-covered code this round calls but does not own.** Two
tasks route through it, and a test that accidentally depends on its uncovered
paths will be fragile for reasons outside this round's control. The plan must
say, per test, what is arranged and what is asserted.

**`archive_post` opens a second task session.** Assertions after
`archive_old_posts` run across two sessions plus the test's own. Fact 153's
`expire_all` discipline is necessary but may not be sufficient; the plan's first
task should establish what is actually visible before later tasks assert on it.

**The suite's session timeout.** `pytest.ini:28` caps a session at 600s and the
suite ran 346s at the end of sub-project 29 — the closest it has been to the cap
in several rounds. `./run_tests.sh --down` before each measured run remains
mandatory, and this round adds tests to a suite that is already slower than when
that rule was written.

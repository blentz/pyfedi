# Sub-project 31: `maintenance.py` Group D — the external-service tasks

**Date:** 2026-09-09
**Branch:** `blentz`
**Predecessor:** sub-project 30, commits `908211c8..9850ef52`, kept as-is

## Goal

Take the five external-service tasks in `app/shared/tasks/maintenance.py` to
100% statement and branch coverage, raise the module's floor from 47, and land
three production changes.

Sub-project 29 closed Group A, the ten tasks needing no transport.
Sub-project 30 closed Group B, the eight content-lifecycle tasks. Group D is
the third of four. After this round only Group C remains — 196 statements and
47 branch points, two-thirds of it in one function.

Measured at `9850ef52`: `maintenance.py` is 637 statements at 47.08%. Group D
is **120 statements, 111 missing, 24 branch points**.

## Group D, measured

| Function | Lines | Stmts | Branch pts | What a test must arrange |
|----------|-------|-------|-----------|--------------------------|
| `refresh_instance_chooser` | `:975-1066` | 60 | 11 | `httpx_client.post`, `get_request`, `InstanceChooser` rows |
| `add_remote_communities` | `:1070-1098` | 18 | 4 | `get_request`, `get_setting`/`set_setting` |
| `add_remote_community_from_post` | `:1101-1117` | 16 | 4 | `search_for_community` |
| `delete_from_s3` | `:1121-1135` | 7 | **0** | a `boto3` client |
| `clean_up_tmp` | `:1139-1159` | 19 | 5 | real files on a real filesystem |

## What makes this group unlike A and B

**It is the first group that leaves the database.** Groups A and B were SQL and
ORM work; the only non-database dependency either touched was `archive_old_posts`'
`boto3` client, which was arranged as a recorder and never used. Group D makes
three outbound HTTP calls, one S3 delete, and reads a directory off disk.

**`delete_from_s3` is the only task in this module with no error handling at
all.** No `try`, no `except`, no `finally`, no session. Every one of the other
twenty-two wraps its body. It is also the only one taking an argument that is
data rather than an id.

**`add_remote_communities` is the only task in this module with no session.**
It calls `get_setting` and `set_setting` (`app/utils.py:203-222`), both of which
go through `db.session`, without `get_task_session()` and without
`patch_db_session`.

**`clean_up_tmp` reads a hardcoded relative path.** `:1144` is
`directory = 'app/static/tmp'`, resolved against the process working directory.
This is the first task in the module whose behaviour depends on where it was
started from.

## Production changes

Three, matching sub-projects 24 through 30. **All three are predictions from
reading. None has been observed.** Sub-project 30's spec predicted that one of
its changes would be untestable in-process and was wrong — a discriminator was
found and the round landed three changes rather than two. Predictions in this
spec are worth exactly as much as the observations that follow them, in either
direction. **Each change is observed before it is made, and dropped if it does
not reproduce.**

### PC1 — `clean_up_tmp` cannot be tested without writing into the repository

`:1144` hardcodes `'app/static/tmp'` as a **relative** path. Under the test
container the working directory is `/app` and the repository is bind-mounted
there (`compose.test.yaml`'s `./:/app:z`), so the path resolves to
`/app/app/static/tmp` — inside the working tree. **That directory does not
exist and is not gitignored.** Covering this function today means creating real
files inside the repository and deleting them afterwards, and a run that dies
between the two leaves them in the tree.

Fact 154's remedy does not apply: it says a test needing a real file should
create it with `tempfile`, which lands in the container's `/tmp`. That works
only when the code under test is *told* where to look. This code is not.

**The fix gives the function an optional parameter and an absolute default:**

```python
@celery.task
def clean_up_tmp(directory=None):
    ...
    if directory is None:
        directory = os.path.join(current_app.root_path, 'static', 'tmp')
```

**This is behaviour-preserving in production, and the round must verify that
rather than assume it.** `current_app.root_path` is `/app/app` in the container,
so the default resolves to `/app/app/static/tmp` — byte-identical to what the
relative path resolves to from `/app` today. All four call sites in `app/cli.py`
(`:851`, `:876` via `.delay()`, `:936`, and the import at `:819`/`:893`) pass no
arguments and are unaffected.

It also removes a latent dependency: today the task deletes nothing if started
from any directory other than the repository root, silently.

**Observation shape:** the test passes a `tempfile.mkdtemp()` directory
containing a stale `.jpg` and asserts it is removed. Against unmodified code the
parameter does not exist, so the test fails with `TypeError`. That is a weaker
observation than the round's usual failing-assertion, and the spec says so
plainly: this is a testability fix, and its justification is that the function
is otherwise untestable without touching the working tree, not that it is
broken for users today.

### PC2 — `add_remote_communities` writes through `db.session` with no session of its own

`:1085` calls `get_setting` and `:1098` calls `set_setting`
(`app/utils.py:203-211` and `:215-222`). Both use `db.session`; `set_setting`
also commits and invalidates a memoized cache. The function opens no
`get_task_session()` and wraps nothing in `patch_db_session`.

Every other task in this module that reaches a `db.session`-using helper wraps
it — seven sites, at `:49`, `:140`, `:166`, `:221`, `:431`, `:719` and `:1168`.
The last of those is sub-project 30's own PC2, added after a connection-checkout
listener showed the split was real.

The fix opens a task session and wraps the body, matching its siblings.

**Observation shape:** the same connection-checkout discriminator sub-project 30
used — a `before_cursor_execute` listener on `db.engine` recording
`id(conn.connection)` per statement. If the reads and writes land on different
checkouts unpatched and one checkout wrapped, the defect is real.

**This one may not reproduce, and that is an acceptable outcome.** Unlike
`pwn_bots`, this function has no task session to be split *from* — everything
goes through `db.session` today, consistently. The listener may therefore show
one checkout either way, in which case the change is conformity rather than
repair and **PC2 becomes a register-only finding**, landing two production
changes. Sub-project 30 recorded exactly this kind of outcome honestly rather
than pretending a test proved something.

### PC3 — `delete_from_s3` has no error handling

`:1121-1135` has no `try`, no `except`, no `finally`. `:1135`'s `s3.close()` is
unreachable if `:1134`'s `delete_objects` raises, leaking the client's
connection pool. Every other task in this module wraps its body.

**There is no good model to copy inside this module, and an earlier draft of
this spec wrongly said there was.** It cited `archive_old_posts:923-924` as
closing its client correctly. It does not: `:923`'s `if s3:` and `:924`'s
`s3.close()` sit at the end of the `try`, not in a `finally`, so a raise from
`:921`'s `archive_post` skips the close there too. That is the same leak in a
second function, and it is registered below rather than fixed — it is outside
this round's approved scope.

The fix wraps `delete_from_s3`'s body in `try` with `finally: s3.close()`. There
is no session, so there is no rollback and no `except` clause is needed: an
unhandled exception propagates to Celery as it does today, and the only change
is that the client closes on the way out.

**Scope note, deliberately narrow.** The fix does **not** add logging, does not
swallow the exception, and does not retry. Celery already records a failing
task. The change makes `s3.close()` run on the failure path and nothing else.

**Observation shape:** a test making `delete_objects` raise, asserting the
exception propagates and that `close()` was still called. Against unmodified
code the close assertion fails.

## Findings to register, not fix

- **A fifth in-loop commit**, at `refresh_instance_chooser:1052`, inside the
  `for node` loop. Same shape as D342 and the four registered as D354.
  Registered rather than fixed on the same reasoning: D342's entry discloses
  that fixing that pattern cost an all-or-nothing starvation regression, and
  this loop *deletes* `InstanceChooser` rows on failure, so a partial run leaves
  a partly-pruned table — a different and worse failure than a stale counter.
- **`add_remote_communities:1098` calls `set_setting` once per post**, and
  `set_setting` commits and invalidates a memoized cache each time. Up to 50
  posts per run, so up to 50 commits and 50 cache invalidations where one would
  do. Registered as an observation; moving it outside the loop would change what
  survives a mid-run failure, which is a behaviour question this round can
  register better than it can answer.
- **`:1116`'s bare `except Exception: pass`** around `search_for_community` is
  fact 111's shape, which this campaign has reasoned about twice already in
  `notes.py` and `pages.py`. Recorded as a third instance rather than
  re-litigated.
- **`add_remote_communities:1077` catches only `httpx.HTTPError`**, not
  `Exception`. Narrower than every sibling. Correct as written if `get_request`
  raises nothing else; recorded so a future round that widens `get_request`'s
  failure modes knows this site assumes otherwise.
- **`archive_old_posts:923-924` leaks its S3 client on the failure path**, the
  same defect PC3 fixes in `delete_from_s3`. `:923`'s `if s3:` guard and
  `:924`'s `close()` are the last statements of the `try` rather than a
  `finally`, so a raise from `:921`'s `archive_post` skips them. Registered, not
  fixed: `archive_old_posts` belongs to Group B, which sub-project 30 closed,
  and widening this round's scope to a second function is the kind of drift the
  approved three-change shape exists to prevent. Recorded so the next round
  touching Group B has it.
- **`refresh_instance_chooser` has nested exception handling** — `:1010`'s inner
  `except` handles a connection failure per domain, and `:1046`'s outer one
  handles anything else in the same iteration. Both delete the existing
  `InstanceChooser` row. Recorded because the duplication reads as redundancy
  and is not: the inner one `continue`s, the outer one falls through to
  `:1052`'s commit.

## Test architecture

One new file: `tests/test_shared_tasks_maintenance_external.py`. Group A's file
is `..._cleanup.py` and Group B's is `..._lifecycle.py`; the names split the
module by group so Group C can follow.

**HTTP is arranged with respx**, which the campaign already uses throughout
`app/activitypub/`. **Fact 148 binds and is the trap here**: a respx
unmatched-request assertion is swallowed by `signature.py:143`'s
`except Exception as e:` into an `ActivityPubLog` failure row — but that applies
to `post_request`, which this group does not use. `refresh_instance_chooser`
calls `httpx_client.post` directly at `:984` and `get_request` at `:1009`;
`add_remote_communities` calls `get_request` at `:1072`. The plan must establish,
in its first task, whether an unmatched request through these paths fails a test
or is swallowed — the answer differs from the `post_request` case and the round
must not assume either way.

**`random.shuffle` at `:999` makes node order nondeterministic.** No test may
assert on the order in which domains are processed. Assert on the resulting set
of `InstanceChooser` rows.

**`search_for_community` and `find_language_or_create` are arranged, not
exercised.** Both belong to other modules — `app/community/util.py` and
`app/activitypub/util.py` — and this round tests which arguments reach them, by
replacing them in this module's namespace with the `_Recorder` idiom fact 179
records.

**S3 is mocked at the client.** `boto3` client construction makes no network
call (fact 181), but `delete_objects` does. `delete_from_s3` must receive a
stubbed client, which means either monkeypatching `boto3.session.Session` or
accepting a real call — the plan chooses the former and says so.

**`clean_up_tmp` uses a real temporary directory**, once PC1 makes that
possible. Before PC1 it cannot be tested without writing into the working tree,
which is why PC1 comes before its coverage in the plan's ordering.

**Harness facts 153, 162, 163, 175, 179, 181 all bind**, unchanged: the fixture
does not roll back, the task runs on its own connection where it has one, seeded
rows must be committed first, and `ObjectDeletedError` depends on which session
deleted the row.

## Deliverables

- `tests/test_shared_tasks_maintenance_external.py`, new.
- Group D's five functions at 100% statement and branch coverage.
- `coverage_floors.ini`'s `app/shared/tasks/maintenance.py` raised from 47 to
  whatever the run measures — Groups A, B and D together are 423 of 637
  statements, so expect roughly 66, measured rather than assumed.
- Three production changes: PC1 `clean_up_tmp`, PC2 `add_remote_communities`,
  PC3 `delete_from_s3` — each observed before it is made, and PC2 dropped to a
  register-only finding if the discriminator shows nothing.
- Mutations covering PC1's default path, PC3's `finally`, both early returns in
  `refresh_instance_chooser`, its 200-versus-other arm, `add_remote_communities`'
  two `continue` guards, `add_remote_community_from_post`'s url-versus-body fork,
  and `clean_up_tmp`'s extension and age filters.
- Findings from D360; facts from 183.
- Full suite green, all 20 floors met against a report whose mtime postdates the
  run.

## Risks

**PC1's justification is weaker than this round's usual bar, and the spec says
so rather than dressing it up.** It is a testability fix. The failing
observation is a `TypeError` from a parameter that does not exist yet, not a
wrong result users see. The argument for making it anyway is that the
alternative is a test suite that writes into the working tree on every run, and
that the change is verifiably behaviour-preserving in production. If the round
finds the resolved paths differ, PC1 changes shape and the register says what
was found.

**PC2 may not reproduce**, for the reason given above. Two production changes is
the correct outcome if so.

**respx's failure mode through these call paths is unestablished.** The plan's
first task must determine whether an unmatched request fails a test or is
swallowed, before any test relies on either. Getting this wrong in the
swallowing direction produces tests that pass while asserting nothing — the
defect this campaign registers most often.

**The suite is at 371s against `pytest.ini:28`'s 600s cap** — the closest it has
been. This round adds tests that make real HTTP calls through respx, which is
fast, and one that touches a temporary directory. `./run_tests.sh --down` before
each measured run remains mandatory, and the plan should watch the total rather
than assume headroom.

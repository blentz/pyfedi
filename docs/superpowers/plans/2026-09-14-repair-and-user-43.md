# Sub-project 43: the `reply_count_cross_posted` backfill, and `app/shared/user.py`

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Backfill the `post.reply_count_cross_posted` column whose `NULL` values crash five code paths, then take `app/shared/user.py` from 35.831% to 100% — fixing, along the way, four lines in `ban_user` that test a constant instead of the caller's source.

**Architecture:** Part 1 is one Alembic revision on head `7b8bf43fa079`, with a test that drives the real defect through `mod_remove_reply` before and after applying the revision's own SQL. Part 2 is four new test files, one per functional group, written against today's behaviour first; the `if SRC_WEB:` fix lands in its own task, after the tests that pin the bug are green, and inverts them in the same commit.

**Tech Stack:** Python 3.13, Flask, SQLAlchemy 2.0.52, Alembic, pytest, podman-compose. No host Python has flask or pytest — everything runs in the `test-runner` container.

**Spec:** `docs/superpowers/specs/2026-09-14-repair-and-user-43-design.md` (amended at `e0384ece`; read the `CORRECTION` subsection before Task 6 and the retraction in `## Verification` before Task 9).

---

## Global Constraints

Every task's requirements implicitly include this section. These are not style preferences. Each one is here because it cost this campaign a defect, a retraction, or a day.

### Running anything

- **There is no host Python with flask or pytest.** Every pytest invocation goes through `./run_tests.sh`, which is `podman compose exec -T test-runner pytest "$@"` (`run_tests.sh:85`). There is no `--exec` flag; passing one is rejected with pytest exit 4.
- `run_tests.sh:83` runs `flask db upgrade` before every pytest invocation. A new migration is therefore applied automatically by the harness — you never apply it by hand.
- **Container Python must be inlined**, never staged through a host file: `podman-compose -f compose.test.yaml exec -T test-runner python -c "..."`. The host and container do not share a filesystem.
- **Only the controller runs the full suite**, one pytest session at a time, in the foreground. **Never kill a running pytest** — teardown will not run and the test database is left corrupt, which then presents as ~87 failures with `psycopg2.errors.UniqueViolation ... "site_pkey"` that look like a code failure. Recover with `./run_tests.sh --down`.
- Before believing any failure, run `./run_tests.sh --down` and retry once.
- The suite currently needs `-o session_timeout=1800` on a loaded machine, passed as a command-line ini override. **`pytest.ini` must NOT be edited.**
- **`pytest` exits 1 on session timeout and `run_tests.sh` propagates it.** A shell pipeline eats the status — read `${PIPESTATUS[0]}`, or do not pipe.

### Measuring

- **Coverage takes the DOTTED module form**: `--cov=app.shared.user`. A path form (`--cov=app/shared/user.py`) collects nothing, writes no JSON, and exits 0 — a silent green failure.
- **Write the coverage JSON outside the repository** (`/tmp/...`), and remember it lands in the **container's** `/tmp`, not the host's. Read it back with an inlined container Python.
- `tests/check_coverage_floors.py` takes **TWO** arguments — the coverage JSON *and* the floors file — and fails closed (exit 2) if given one.
- **Test counts come from pytest's own collection output**, never from a number written in this plan.
- **A count is a claim — re-derive it like a line number.** Publish the derivation command and its raw output beside any count you report.
- **A production change mid-round REOPENS COVERAGE.** Sub-project 41 added four lines in its Task 7 and created two new uncovered arcs that went unnoticed for two tasks. Task 6 changes production code; Task 8 re-measures everything after it.

### Editing

- **Delete nothing the task did not create.**
- **`git checkout -- app/` is BANNED** while a round holds uncommitted production changes — HEAD is not the baseline then. It silently reverted a real fix in sub-project 40. **Reverse edits only**, and re-read the restored line to confirm.
- **`git diff --quiet -- app/` is THE tree check**, not `wc -l`. A same-line-count replacement is invisible to a line count.
- **Re-derive every line number** with `awk 'NR>=X && NR<=Y {printf "%d\t%s\n",NR,$0}' FILE`. **Never an `awk` that assigns to a field** — it rebuilds `$0` and destroys leading whitespace.
- **Use `/usr/bin/grep`, not the interactive `grep`** (a `ugrep` wrapper with `--ignore-files` that silently skips gitignored paths).
- **No duplicate test names.** Check every file you touch with:
  `/usr/bin/grep -oE "^ *def (test_[a-z0-9_]+)" FILE | sed 's/^ *//' | sort | uniq -d` — the `0-9` matters.
- **No ordered assertions over rows a query planner returned.** Compare sets.

### Committing

- Commit with `git commit -F <file>`, never `-m`. Lowercase `type:` subject prefix, normal English prose in the body — not the terse register the controller answers in.
- Trailers, the last two lines, in this order:
  ```
  Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01BF7kVN2spWD86NYKWyGrVn
  ```

### Evidence

- **An equivalence claim needs a proof of unkillability, never a failure to kill.** A surviving mutant means either "no test *can* kill this" or "no test *does*"; resolving that by assumption is how a dead line stayed uncovered for a whole round.
- **A crash kill is not a kill** unless a viable non-crashing variant of the same fault also dies.
- **Fix-catching is not a unique kill.**
- **The five false-witness mechanisms.** Before asserting anything, ask which of these you are standing on: (a) asserting on state something else sets unconditionally; (b) a fixture coincidence; (c) emptiness with no same-mechanism positive control; (d) an input taking the same path under both arms; (e) two conditions exercised only in lockstep.

### Module-specific traps

These are specific to `app/shared/user.py` and its fixtures. Every one was verified by reading, this session.

- **The id-1 admin trap.** `app/models.py:1259-1261` is `def is_admin(self): if self.id == 1: return True`. `tests/conftest.py:131-132` runs `SELECT setval(c.oid, 1, false)` over every sequence after every test, so **the first-minted user is id 1 deterministically, in every test**, and is silently an admin. `ban_user` calls `add_to_modlog`, which branches on `actor.is_admin()` at `app/utils.py:3570`. The established remedy is a seed-burning helper — see `_burn_a_seed()` at `tests/test_shared_reply_make.py:247`.
- **`ROLE_STAFF = 3` and `ROLE_ADMIN = 4`** (`app/constants.py:80-81`), and `block_another_user:33-35` compares a raw `SELECT role_id FROM "user_role"` result against those two **integers**. `tests/factories.py:365`'s `grant_permission` mints a fresh `Role` per call with a **sequential** id after the sequence reset — so the third or fourth `grant_permission` call in a test silently makes its subject staff or admin **by id**, and a block is refused for a reason the test did not intend. **Create roles for these tests with an explicit id** (`Role(id=ROLE_ADMIN, name='role-with-id-4', weight=0)`), never by counting `grant_permission` calls. Give it a name that is NOT `'Admin'` or `'Staff'`, so the test proves the id comparison rather than accidentally satisfying `User.is_admin()`'s name path at `app/models.py:1263`.
- **`tests/test_ap_moderation.py` exercises a DIFFERENT `ban_user`** — `app/activitypub/util.ban_user`, four positional arguments. It is not precedent for this module. Do not cite it.
- **fakeredis cannot serve a redis-py lock in this environment.** `Lock.acquire()` succeeds, `Lock.release()` calls EVALSHA and raises `redis.exceptions.ResponseError: unknown command 'evalsha'` on `__exit__`, every time. `ban_user:178` is a `with redis_client.lock(...)` block, so the `redis_double` fixture cannot be used for it. The remedy is a local double whose `.lock()` returns `contextlib.nullcontext()`; `_RedisLockOnlyDouble` / `redis_lock_only_double` at `tests/test_inbox_dispatch_votes.py:145-159` is the precedent, and **it is a precedent to copy, not a fixture to import** — a fixture defined in one test module is not visible in another unless it lives in `conftest.py`. `app/shared/user.py:177` does `from app import redis_client` inside the function body, so monkeypatching the single attribute `app.redis_client` reaches it.
- **Patching an unqualified call means rebinding the name ON THE MODULE.** `app/shared/user.py:10-12` does `from app.shared.tasks import task_selector`, `from app.user.utils import purge_user_then_delete`, `from app.utils import ... add_to_modlog ...` — patching `app.shared.tasks.task_selector` would NOT intercept, because the `from ... import` already bound the original into this module's globals. Patch `app.shared.user.<name>`. The precedent is `recording_task_selector` at `tests/test_shared_post_moderation.py:142`.
- **`make_user(instance, name, local=False, with_keys=False)`** (`tests/factories.py:41`). `local=True` leaves `ap_id` None; `User.is_local()` at `app/models.py:1252` is `self.ap_id is None or self.ap_profile_id.startswith(...)`, so a local user short-circuits on the first operand and never touches `ap_profile_id`.
- **`web_ctx(app, user, query_string='')`** at `tests/factories.py:1217` — three arguments, `app` first. Sub-project 41's plan got this signature wrong five times. **`bearer(user)`** at `tests/factories.py:1228`.
- `is_admin` and `is_staff` carry `@cache.memoize(timeout=30)`, but `TestConfig.CACHE_TYPE` is `'NullCache'`, so memoization is inert in tests.

### Scope

- **Production changes this round: exactly two.** Task 1's migration, and Task 6's four `if SRC_WEB:` lines. Everything else that surfaces is **registered, not fixed** — see Task 10.
- Register new findings from **D554**. `tests/README.md` facts from **242**.

---

## File Structure

**Created:**

| File | Responsibility |
|---|---|
| `migrations/versions/<rev>_backfill_reply_count_cross_posted.py` | One Alembic revision on head `7b8bf43fa079`: backfill the NULLs, set a server default. |
| `tests/test_migration_cross_posted_backfill.py` | The before/after evidence pair for that revision, plus a server-default test. |
| `tests/test_shared_user_blocks.py` | `block_another_user`, `unblock_another_user`, `bot_challenge_user` (Group A). |
| `tests/test_shared_user_bans.py` | `ban_user`, `unban_user` (Group B), including the `if SRC_WEB:` pins and their inversion. |
| `tests/test_shared_user_follows.py` | The residual arcs in `follow_user`, `subscribe_user`, `unfollow_user` (Group C). |

**Modified:**

| File | Change |
|---|---|
| `app/shared/user.py:161,171,183,192` | `if SRC_WEB:` becomes `if src == SRC_WEB:`. Task 6 only. |
| `coverage_floors.ini` | A **new** entry for `app/shared/user.py`. Task 8 only. |
| `docs/superpowers/findings-register.md` | Entries from D554. Task 10 only. |
| `tests/README.md` | Facts from 242. Task 10 only. |

Four test files rather than one: `bans` is where the production change lands and needs its own review surface; `blocks` and `follows` are independent of it and of each other. Splitting by function group keeps each file small enough to hold in context whole, which is the same reason `app/shared/reply.py`'s tests live across four files.

---

### Task 1: The `reply_count_cross_posted` backfill migration

**Files:**
- Create: `migrations/versions/<rev>_backfill_reply_count_cross_posted.py`
- Create: `tests/test_migration_cross_posted_backfill.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: nothing later tasks rely on. Part 1 is self-contained and goes first because it is a live production defect and Part 2 is not.

**Background the implementer needs.**

`migrations/versions/6814385881d3_cross_posted_reply_count.py` added the column:

```python
batch_op.add_column(sa.Column('reply_count_cross_posted', sa.Integer(), nullable=True))
```

No `server_default`, no backfill. Every `post` row predating that migration holds `NULL`. `app/models.py:1723`'s `default=0` is a *Python-side* default: it applies to rows the ORM inserts and never to rows already on disk.

Six sites do arithmetic on the column and only one guards it:

```
app/activitypub/util.py:2272        if to_delete.post.reply_count_cross_posted:   <- the guard
app/activitypub/util.py:2273            to_delete.post.reply_count_cross_posted -= 1
app/activitypub/util.py:2315        to_restore.post.reply_count_cross_posted += 1
app/shared/reply.py:267             reply.post.reply_count_cross_posted -= 1
app/shared/reply.py:295             reply.post.reply_count_cross_posted += 1
app/shared/reply.py:431             reply.post.reply_count_cross_posted -= 1
app/shared/reply.py:468             reply.post.reply_count_cross_posted += 1
```

`None - 1` raises `TypeError`, which Flask renders as a 500.

**Why `reply_count` and not zero.** `app/models.py:3108` sets `post.reply_count_cross_posted = post.reply_count` for a post with no cross-posts, and `:3100-3104` sums `reply_count` across the cross-post set when there are. A backfill to 0 would satisfy the type and violate the invariant.

**The precedent for the whole file shape** is the current head itself, `migrations/versions/7b8bf43fa079_fix_null_values_in_total_subscriptions_.py` — a NULL backfill with a deliberately empty `downgrade()`. Follow it. `downgrade()` must never re-`NULL` the data: a downgrade that destroys rows is worse than the defect it reverses.

**Read this before writing the test.** The migration repairs *existing data*. It does not make the six arithmetic sites `None`-safe. A row explicitly set to `NULL` after the migration still raises `TypeError`. So the test is not "the crash goes away" — it is (a) the crash is real, (b) the revision's SQL repairs a NULL row to the right value, (c) the server default stops a non-ORM insert from producing a new NULL. State that distinction in the test file's module docstring; Task 10 registers the residual as a finding.

- [ ] **Step 1: Confirm the head, from the tool rather than from the files**

```bash
cd /home/blentz/git/pyfedi
podman-compose -f compose.test.yaml exec -T test-runner flask db heads
```

Expected: exactly one line, `7b8bf43fa079 (head)`. A `/usr/bin/grep` sweep for revisions nobody names as a `down_revision` reports three candidates; two are artifacts (one revision id is quoted differently in its file, one file alembic does not treat as a head). **Ask the tool, not the inputs.** If this prints anything other than one head, stop and report it.

- [ ] **Step 2: Write the failing test**

Create `tests/test_migration_cross_posted_backfill.py`:

```python
"""The backfill for `post.reply_count_cross_posted` (D543).

`migrations/versions/6814385881d3_cross_posted_reply_count.py` added the
column as `nullable=True` with no server default and no backfill, so every
`post` row predating it holds NULL. `app/models.py:1723`'s `default=0` is a
Python-side default: it applies to rows the ORM inserts and never to rows
already on disk. Five of the six sites that do arithmetic on the column are
unguarded, and `None - 1` raises TypeError, which Flask renders as a 500.

WHAT THIS MIGRATION DOES NOT DO. It repairs existing data. It does not make
those five sites None-safe, and a row explicitly set to NULL after the
migration still raises. So the pair below is not "the crash goes away" -- it
is "the crash is real, and the revision's own SQL repairs the row that causes
it". The residual is registered rather than fixed; the production budget for
this round is the migration and `ban_user`'s source test, nothing else.
"""
import pytest
from sqlalchemy import text

from app import db
from app.constants import SRC_API
from app.shared.reply import mod_remove_reply
from tests.factories import (bearer, make_community, make_community_member,
                             make_instance, make_post, make_post_reply,
                             make_user)


BACKFILL_SQL = ('UPDATE post SET reply_count_cross_posted = reply_count '
                'WHERE reply_count_cross_posted IS NULL')


def _seed_removable_reply():
    """A moderator, an author, a local community, a post and a reply on it.

    `mod_remove_reply` (app/shared/reply.py:416) needs the actor to pass
    `reply.community.is_moderator(user)`, and reaches
    `reply.post.reply_count_cross_posted -= 1` at :431 only when the reply's
    author is not a bot -- `make_user` leaves `bot` at its default, which is
    false, so the guard at :429 is open.

    ORDER MATTERS HERE. `make_community` (tests/factories.py:141-142)
    hardcodes `instance_id=1, user_id=1`, so an Instance and a User must
    already occupy id 1 when it is called. The instance and the id-1 burn
    below fill both seats, which is why the burn is load-bearing twice over:
    it keeps `moderator` off the id-1 admin shortcut AND gives the community
    an owner that exists.
    """
    instance = make_instance('remote.example')
    burn = make_user(instance, 'burn-the-id-1-seat', local=True)
    assert burn.id == 1, 'the id-1 admin trap moved; re-derive before trusting this'

    moderator = make_user(instance, 'mod', local=True)
    author = make_user(instance, 'author', local=True)
    community = make_community('microblogs')
    make_community_member(moderator, community, is_moderator=True)
    post = make_post(community, author, 'https://test.piefed.local/post/1')
    reply = make_post_reply(post, author, 'a reply')
    db.session.commit()
    return moderator, post, reply


def test_a_null_cross_posted_count_makes_mod_remove_reply_raise(app, db_session):
    """THE DEFECT, demonstrated rather than described.

    This is the observation the migration exists for. It stays true after the
    migration -- the column is still nullable and the arithmetic is still
    unguarded -- which is why the assertion below is not inverted by Task 1.
    """
    moderator, post, reply = _seed_removable_reply()
    db.session.execute(text('UPDATE post SET reply_count_cross_posted = NULL '
                            'WHERE id = :id'), {'id': post.id})
    db.session.commit()
    db.session.expire_all()

    with pytest.raises(TypeError):
        mod_remove_reply(reply.id, 'spam', SRC_API, bearer(moderator))


def test_the_backfill_sql_repairs_a_null_row_to_reply_count(app, db_session):
    """The revision's own UPDATE, applied to the row that crashes.

    reply_count, not zero: app/models.py:3108 defines
    `post.reply_count_cross_posted = post.reply_count` for a post with no
    cross-posts, and :3100-3104 sums reply_count across the cross-post set
    when there are. A backfill to 0 satisfies the type and violates that.

    The reply_count is set to 7 rather than left at whatever make_reply
    produces, so a backfill to 0 AND a backfill that merely copies a
    coincidentally-equal value both fail this assertion.
    """
    moderator, post, reply = _seed_removable_reply()
    db.session.execute(text('UPDATE post SET reply_count = 7, '
                            'reply_count_cross_posted = NULL WHERE id = :id'),
                       {'id': post.id})
    db.session.commit()

    db.session.execute(text(BACKFILL_SQL))
    db.session.commit()
    db.session.expire_all()

    repaired = db.session.execute(
        text('SELECT reply_count_cross_posted FROM post WHERE id = :id'),
        {'id': post.id}).scalar()
    assert repaired == 7

    mod_remove_reply(reply.id, 'spam', SRC_API, bearer(moderator))
    db.session.expire_all()
    after = db.session.execute(
        text('SELECT reply_count_cross_posted FROM post WHERE id = :id'),
        {'id': post.id}).scalar()
    assert after == 6


def test_the_backfill_leaves_a_non_null_row_alone(app, db_session):
    """The WHERE clause is load-bearing.

    Without `WHERE reply_count_cross_posted IS NULL` the migration would
    overwrite every correctly-maintained cross-post count with the post's own
    reply_count, silently destroying the sum at app/models.py:3100-3104. This
    is the positive control for that clause: a row whose two counts DISAGREE,
    which a clause-less UPDATE would flatten.
    """
    moderator, post, reply = _seed_removable_reply()
    db.session.execute(text('UPDATE post SET reply_count = 7, '
                            'reply_count_cross_posted = 99 WHERE id = :id'),
                       {'id': post.id})
    db.session.commit()

    db.session.execute(text(BACKFILL_SQL))
    db.session.commit()

    untouched = db.session.execute(
        text('SELECT reply_count_cross_posted FROM post WHERE id = :id'),
        {'id': post.id}).scalar()
    assert untouched == 99


def test_the_column_has_a_server_default_of_zero(app, db_session):
    """An INSERT that omits the column must not produce a new NULL.

    Raw SQL rather than the ORM on purpose: the ORM already supplies
    `default=0` (app/models.py:1723), so an ORM insert would pass this test
    with or without the migration -- mechanism (a) of the five false
    witnesses, asserting on state something else sets unconditionally. This
    INSERT names no counter column at all, so only a database-level default
    can satisfy it.
    """
    instance = make_instance('remote.example')
    author = make_user(instance, 'author', local=True)
    community = make_community('microblogs')
    db.session.commit()

    db.session.execute(text(
        'INSERT INTO post (user_id, community_id, title, type, posted_at, '
        'last_active, ap_id) VALUES (:u, :c, :t, 0, now(), now(), :ap)'),
        {'u': author.id, 'c': community.id, 't': 'a post',
         'ap': 'https://test.piefed.local/post/default-probe'})
    db.session.commit()

    value = db.session.execute(text(
        "SELECT reply_count_cross_posted FROM post WHERE ap_id = :ap"),
        {'ap': 'https://test.piefed.local/post/default-probe'}).scalar()
    assert value == 0
```

- [ ] **Step 3: Run the tests and confirm which ones fail, and why**

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh tests/test_migration_cross_posted_backfill.py -v
echo "PYTEST_EXIT=$?"
```

Expected before the migration exists: the first three pass (they apply the SQL themselves) and `test_the_column_has_a_server_default_of_zero` **FAILS** with `assert None == 0`. That single failure is the migration's reason to exist, stated as a test.

If the raw INSERT fails on a NOT NULL column this plan did not anticipate, add the missing column to the INSERT with a minimal value and say so in the report — do not weaken the assertion, and do not switch to an ORM insert, which would defeat the test's whole purpose.

If `assert burn.id == 1` fails in `_seed_removable_reply`, the id-1 trap has moved: stop and report, because every later task's seeding depends on it.

- [ ] **Step 4: Generate the revision**

```bash
cd /home/blentz/git/pyfedi
podman-compose -f compose.test.yaml exec -T test-runner \
    flask db revision -m "backfill reply_count_cross_posted"
```

This writes a new file under `migrations/versions/` and prints its path. Alembic picks the revision id; **do not invent one**. Confirm the generated file's `down_revision` is `'7b8bf43fa079'` — if alembic chose something else, the head moved between Step 1 and now, and you must stop and report rather than edit the linkage by hand.

- [ ] **Step 5: Write the revision body**

Replace the generated `upgrade()` and `downgrade()` with exactly this, keeping the generated header block (`revision`, `down_revision`, `branch_labels`, `depends_on`) untouched:

```python
def upgrade():
    # post.reply_count_cross_posted was added nullable with no server default
    # and no backfill (6814385881d3), so every row predating that revision
    # holds NULL. app/models.py:1723's default=0 is Python-side and applies
    # only to rows the ORM inserts. Five of the six sites that decrement or
    # increment this column are unguarded, and None - 1 raises TypeError.
    #
    # reply_count is the right value rather than 0: app/models.py:3108 defines
    # the column as the post's own reply_count when it has no cross-posts, and
    # :3100-3104 as the sum across the cross-post set when it does. The WHERE
    # clause keeps correctly-maintained counts from being flattened.
    op.execute("UPDATE post SET reply_count_cross_posted = reply_count "
               "WHERE reply_count_cross_posted IS NULL")
    op.execute("ALTER TABLE post ALTER COLUMN reply_count_cross_posted "
               "SET DEFAULT 0")


def downgrade():
    # Drop only the default. The backfilled values are not restored to NULL:
    # a downgrade that destroys rows is worse than the defect it reverses.
    op.execute("ALTER TABLE post ALTER COLUMN reply_count_cross_posted "
               "DROP DEFAULT")
```

Delete any `# ### commands auto generated by Alembic ###` scaffolding the generator left behind — that scaffolding is part of the file this task created, so removing it is in scope.

- [ ] **Step 6: Run the tests again**

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh tests/test_migration_cross_posted_backfill.py -v
echo "PYTEST_EXIT=$?"
```

`run_tests.sh:83` applies the migration before pytest runs, so no manual `flask db upgrade` is needed. Expected: 4 passed, `PYTEST_EXIT=0`.

- [ ] **Step 7: Verify the downgrade actually runs**

A migration whose downgrade has never been executed is a migration with an untested half.

```bash
cd /home/blentz/git/pyfedi
podman-compose -f compose.test.yaml exec -T test-runner flask db downgrade
podman-compose -f compose.test.yaml exec -T test-runner flask db upgrade
podman-compose -f compose.test.yaml exec -T test-runner flask db current
```

Expected: both commands succeed, and `flask db current` ends on the new revision. Paste all three outputs into the report.

- [ ] **Step 8: Commit**

```bash
cd /home/blentz/git/pyfedi
git status --porcelain
git add migrations/versions/ tests/test_migration_cross_posted_backfill.py
git commit -F <message-file>
```

Subject: `fix: backfill post.reply_count_cross_posted and default it to zero`.
Body, in prose: what was NULL and why, that `default=0` is Python-side only, why the backfill copies `reply_count` rather than writing 0, that `downgrade()` drops only the default, and — explicitly — that the five unguarded arithmetic sites remain unguarded and a row set to NULL after this migration still raises.

---

### Task 2: `block_another_user` and `unblock_another_user`

**Files:**
- Create: `tests/test_shared_user_blocks.py`
- Read: `app/shared/user.py:20-87`

**Interfaces:**
- Consumes: nothing.
- Produces: `_seed_blockers()`, used again by Task 3 in the same file. Returns a `SimpleNamespace` with `.blocker` and `.target`, both local `User` rows, neither of them id 1.

**Target:** `block_another_user` — 24 statements, 14 arcs, **all missing**. `unblock_another_user` — 16 statements, 10 arcs, **all missing**.

The two functions are near-twins. `block_another_user` has one branch `unblock_another_user` lacks — the role check at `:33-40` — and inserts where the other deletes. Write them in one file so the shared seeding is written once.

**The shape you are covering** (`app/shared/user.py:20-58`):

```
:21   if src == SRC_API:  -> authorise_api_user(auth)   else current_user.id
:26   if user_id == person_id:
:27       if src == SRC_API: raise Exception('cannot_block_self')
:29       else: flash(...); return
:33   role = SELECT role_id FROM "user_role" WHERE user_id = :person_id  (.scalar())
:35   if role == ROLE_ADMIN or role == ROLE_STAFF:
:36       if src == SRC_API: raise Exception('cannot_block_admin_or_staff')
:38       else: flash(...); return
:43   if not existing_block:   -> insert UserBlock, DELETE notification_subscription, commit, cache.delete_memoized
:55   if src == SRC_API: return user_id   else: return None
```

- [ ] **Step 1: Write the failing tests**

Create `tests/test_shared_user_blocks.py`:

```python
"""`block_another_user` and `unblock_another_user` (app/shared/user.py:20-87).

Both functions were at literally zero coverage before this file: 24
statements / 14 arcs and 16 / 10 respectively, every one missing.

THE ROLE TRAP. `block_another_user:33-35` reads
`SELECT role_id FROM "user_role" WHERE user_id = :person_id` with `.scalar()`
and compares the result against the INTEGERS ROLE_ADMIN (4) and ROLE_STAFF
(3) from app/constants.py:80-81. tests/factories.py:365's `grant_permission`
mints a fresh Role per call with a sequential id, and tests/conftest.py:131
resets every sequence between tests -- so the third or fourth
`grant_permission` call in a test would make its subject staff or admin BY
ID, refusing a block for a reason the test never intended. Every role in this
file is therefore created with an EXPLICIT id, and named something that is
neither 'Admin' nor 'Staff' so that `User.is_admin()`'s name path
(app/models.py:1263) cannot satisfy an assertion the id comparison was
supposed to carry.
"""
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app import db
from app.constants import NOTIF_USER, ROLE_ADMIN, ROLE_STAFF, SRC_API, SRC_WEB
from app.models import NotificationSubscription, Role, UserBlock, user_role
from app.shared.user import block_another_user, unblock_another_user
from tests.factories import bearer, make_instance, make_user, web_ctx


def _seed_blockers():
    """Two local users, neither of them id 1.

    The first user minted in any test is id 1, deterministically:
    tests/conftest.py:131-132 runs `SELECT setval(c.oid, 1, false)` over every
    sequence after every test. `User.is_admin` (app/models.py:1259-1261)
    returns True for id 1 regardless of roles. Nothing in THIS file reads
    is_admin -- block_another_user compares role ids, not names -- but the
    burn is kept so that a later test added to this file cannot inherit the
    trap silently. tests/test_shared_reply_make.py:247 is the precedent.
    """
    instance = make_instance('remote.example')
    burn = make_user(instance, 'burn-the-id-1-seat', local=True)
    assert burn.id == 1, 'the id-1 admin trap moved; re-derive before trusting this'

    blocker = make_user(instance, 'blocker', local=True)
    target = make_user(instance, 'target', local=True)
    db.session.commit()
    return SimpleNamespace(blocker=blocker, target=target)


def _give_role_with_id(user, role_id):
    """Put `user` in `user_role` against a role whose id is exactly role_id.

    `user_role.role_id` is a foreign key to `role.id` (app/models.py:937), so
    the Role row has to exist. The name deliberately avoids 'Admin' and
    'Staff' -- block_another_user reads the ID, and a name that also satisfies
    User.is_admin() would let a future assertion pass for the wrong reason.
    """
    db.session.add(Role(id=role_id, name=f'role-with-id-{role_id}', weight=0))
    db.session.commit()
    db.session.execute(user_role.insert().values(user_id=user.id, role_id=role_id))
    db.session.commit()


def test_block_another_user_api_creates_the_block_and_returns_the_blocker(app, db_session):
    s = _seed_blockers()

    returned = block_another_user(s.target.id, SRC_API, bearer(s.blocker))

    assert returned == s.blocker.id
    assert db.session.query(UserBlock).filter_by(
        blocker_id=s.blocker.id, blocked_id=s.target.id).count() == 1


def test_block_another_user_web_creates_the_block_and_returns_none(app, db_session):
    """The web arm returns None and leaves the flash to its caller (:58).

    Both the return value and the row are asserted: a function that created
    nothing would also return None.
    """
    s = _seed_blockers()

    with web_ctx(app, s.blocker):
        returned = block_another_user(s.target.id, SRC_WEB)

    assert returned is None
    assert db.session.query(UserBlock).filter_by(
        blocker_id=s.blocker.id, blocked_id=s.target.id).count() == 1


def test_block_another_user_deletes_the_targets_subscription_to_the_blocker(app, db_session):
    """:46-48's raw DELETE, which is easy to mistake for symmetry.

    Read the parameters: `entity_id = :current_user AND user_id = :user_id`
    with current_user bound to the BLOCKER's id and user_id to the TARGET's.
    So the row removed is the one where the TARGET subscribes to the BLOCKER
    -- not the blocker's own subscription to the target. Two subscriptions are
    seeded, in both directions, and only one may survive. With a single
    subscription this test would pass under a swapped binding.
    """
    s = _seed_blockers()
    db.session.add(NotificationSubscription(
        name='target follows blocker', user_id=s.target.id,
        entity_id=s.blocker.id, type=NOTIF_USER))
    db.session.add(NotificationSubscription(
        name='blocker follows target', user_id=s.blocker.id,
        entity_id=s.target.id, type=NOTIF_USER))
    db.session.commit()

    block_another_user(s.target.id, SRC_API, bearer(s.blocker))
    db.session.expire_all()

    survivors = {(n.user_id, n.entity_id)
                 for n in db.session.query(NotificationSubscription).all()}
    assert survivors == {(s.blocker.id, s.target.id)}


def test_block_another_user_is_idempotent(app, db_session):
    """:43's `if not existing_block` -- the false arc.

    The second call must not add a second row, and must still return the
    blocker's id from :56 rather than falling out of the function early.
    """
    s = _seed_blockers()
    block_another_user(s.target.id, SRC_API, bearer(s.blocker))

    returned = block_another_user(s.target.id, SRC_API, bearer(s.blocker))

    assert returned == s.blocker.id
    assert db.session.query(UserBlock).filter_by(
        blocker_id=s.blocker.id, blocked_id=s.target.id).count() == 1


def test_block_another_user_api_refuses_self(app, db_session):
    s = _seed_blockers()

    with pytest.raises(Exception, match='cannot_block_self'):
        block_another_user(s.blocker.id, SRC_API, bearer(s.blocker))

    assert db.session.query(UserBlock).count() == 0


def test_block_another_user_web_refuses_self_without_raising(app, db_session):
    """The web arm flashes and returns (:30-31) where the API arm raises.

    The row count is the assertion that matters: a `return` that skipped the
    flash would also produce no rows, so the flash is checked too, through the
    session's `_flashes`.
    """
    s = _seed_blockers()

    with web_ctx(app, s.blocker):
        from flask import session
        returned = block_another_user(s.blocker.id, SRC_WEB)
        flashed = [message for _category, message in session.get('_flashes', [])]

    assert returned is None
    assert flashed == ['You cannot block yourself.']
    assert db.session.query(UserBlock).count() == 0


def test_block_another_user_api_refuses_an_admin_by_role_id(app, db_session):
    """ROLE_ADMIN is 4 and the comparison at :35 is against that INTEGER.

    The role is named 'role-with-id-4', not 'Admin', so nothing here can be
    satisfied by User.is_admin()'s name path.
    """
    s = _seed_blockers()
    _give_role_with_id(s.target, ROLE_ADMIN)

    with pytest.raises(Exception, match='cannot_block_admin_or_staff'):
        block_another_user(s.target.id, SRC_API, bearer(s.blocker))

    assert db.session.query(UserBlock).count() == 0


def test_block_another_user_api_refuses_staff_by_role_id(app, db_session):
    """The second operand of :35's `or`. Without this test that operand could
    be deleted outright and every other test in this file would stay green."""
    s = _seed_blockers()
    _give_role_with_id(s.target, ROLE_STAFF)

    with pytest.raises(Exception, match='cannot_block_admin_or_staff'):
        block_another_user(s.target.id, SRC_API, bearer(s.blocker))

    assert db.session.query(UserBlock).count() == 0


def test_block_another_user_allows_a_target_holding_an_unprivileged_role(app, db_session):
    """The positive control for the role check.

    Without this, every role-check test above would also pass against a
    version of :35 that refused EVERY user holding any role at all.
    """
    s = _seed_blockers()
    _give_role_with_id(s.target, 2)

    returned = block_another_user(s.target.id, SRC_API, bearer(s.blocker))

    assert returned == s.blocker.id
    assert db.session.query(UserBlock).count() == 1


def test_block_another_user_web_refuses_an_admin_without_raising(app, db_session):
    s = _seed_blockers()
    _give_role_with_id(s.target, ROLE_ADMIN)

    with web_ctx(app, s.blocker):
        from flask import session
        returned = block_another_user(s.target.id, SRC_WEB)
        flashed = [message for _category, message in session.get('_flashes', [])]

    assert returned is None
    assert flashed == ['You cannot block admin or staff.']
    assert db.session.query(UserBlock).count() == 0


def test_unblock_another_user_api_removes_the_block(app, db_session):
    s = _seed_blockers()
    db.session.add(UserBlock(blocker_id=s.blocker.id, blocked_id=s.target.id))
    db.session.commit()

    returned = unblock_another_user(s.target.id, SRC_API, bearer(s.blocker))

    assert returned == s.blocker.id
    assert db.session.query(UserBlock).count() == 0


def test_unblock_another_user_web_removes_the_block_and_returns_none(app, db_session):
    s = _seed_blockers()
    db.session.add(UserBlock(blocker_id=s.blocker.id, blocked_id=s.target.id))
    db.session.commit()

    with web_ctx(app, s.blocker):
        returned = unblock_another_user(s.target.id, SRC_WEB)

    assert returned is None
    assert db.session.query(UserBlock).count() == 0


def test_unblock_another_user_leaves_someone_elses_block_alone(app, db_session):
    """:74's filter is on BOTH blocker_id and blocked_id.

    A third user's block of the same target is seeded so that a filter
    narrowed to blocked_id alone would delete a row it must not touch. With
    only one block row present, that fault is invisible.
    """
    s = _seed_blockers()
    bystander = make_user(make_instance('other.example'), 'bystander', local=True)
    db.session.add(UserBlock(blocker_id=s.blocker.id, blocked_id=s.target.id))
    db.session.add(UserBlock(blocker_id=bystander.id, blocked_id=s.target.id))
    db.session.commit()

    unblock_another_user(s.target.id, SRC_API, bearer(s.blocker))
    db.session.expire_all()

    remaining = {(b.blocker_id, b.blocked_id)
                 for b in db.session.query(UserBlock).all()}
    assert remaining == {(bystander.id, s.target.id)}


def test_unblock_another_user_is_idempotent(app, db_session):
    """:75's `if existing_block` -- the false arc. No block exists, so the
    body is skipped and :84 still returns the caller's id."""
    s = _seed_blockers()

    returned = unblock_another_user(s.target.id, SRC_API, bearer(s.blocker))

    assert returned == s.blocker.id
    assert db.session.query(UserBlock).count() == 0


def test_unblock_another_user_api_refuses_self(app, db_session):
    s = _seed_blockers()

    with pytest.raises(Exception, match='cannot_unblock_self'):
        unblock_another_user(s.blocker.id, SRC_API, bearer(s.blocker))


def test_unblock_another_user_web_refuses_self_without_raising(app, db_session):
    s = _seed_blockers()

    with web_ctx(app, s.blocker):
        from flask import session
        returned = unblock_another_user(s.blocker.id, SRC_WEB)
        flashed = [message for _category, message in session.get('_flashes', [])]

    assert returned is None
    assert flashed == ['You cannot unblock yourself.']
```

- [ ] **Step 2: Run them and watch them fail for the right reason**

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh tests/test_shared_user_blocks.py -v
echo "PYTEST_EXIT=$?"
```

These tests target code that already exists, so most should pass on the first run. **Any failure here is a finding about the production code or about this plan's reading of it, not something to code around.** Investigate each one at source before changing a test. In particular:

- If `unblock_another_user` has no `'cannot_unblock_self'` string, re-read `:69` and use what is there.
- `make_community` is not called in this file, so nothing here needs an Instance or User at id 1 for its sake — but the burn in `_seed_blockers` still runs, deliberately.

- [ ] **Step 3: Measure what these two functions now reach**

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh tests/test_shared_user_blocks.py \
    --cov=app.shared.user --cov-report=json:/tmp/user-blocks.json -q
echo "PYTEST_EXIT=$?"
podman-compose -f compose.test.yaml exec -T test-runner python -c "
import json
d = json.load(open('/tmp/user-blocks.json'))['files']['app/shared/user.py']
fns = d['functions']
for name in ('block_another_user', 'unblock_another_user'):
    key = [k for k in fns if k.endswith(name)][0]
    s = fns[key]['summary']
    print(name, 'missing stmts', s['missing_lines'], 'missing arcs', s['missing_branches'])
    print('   lines:', fns[key]['missing_lines'])
    print('   arcs :', fns[key]['missing_branches'])
"
```

Note the **dotted** `--cov=app.shared.user`. A path form collects nothing, writes no JSON and exits 0.

Both functions must report `missing_lines: []` and `missing_branches: []`. If anything remains, add the test that reaches it before committing — do not defer it to Task 8.

- [ ] **Step 4: Check for duplicate test names**

```bash
cd /home/blentz/git/pyfedi
/usr/bin/grep -oE "^ *def (test_[a-z0-9_]+)" tests/test_shared_user_blocks.py \
  | sed 's/^ *//' | sort | uniq -d
```

Expected: no output.

- [ ] **Step 5: Confirm no production file was touched**

```bash
cd /home/blentz/git/pyfedi
git diff --quiet -- app/ && echo "TREE CLEAN under app/" || echo "TREE DIRTY under app/"
```

Expected: `TREE CLEAN under app/`. This task is tests only.

- [ ] **Step 6: Commit**

```bash
cd /home/blentz/git/pyfedi
git add tests/test_shared_user_blocks.py
git commit -F <message-file>
```

Subject: `test: cover block_another_user and unblock_another_user`.
Body: both functions were at zero; what the role trap is and why every role here carries an explicit id and a non-privileged name; why the subscription-deletion test seeds two subscriptions rather than one.

---

### Task 3: `bot_challenge_user`

**Files:**
- Modify: `tests/test_shared_user_blocks.py` (append)
- Read: `app/shared/user.py:303-339`

**Interfaces:**
- Consumes: `_seed_blockers()` from Task 2 — a `SimpleNamespace` with `.blocker` and `.target`, both local `User` rows, neither id 1.
- Produces: nothing.

**Target:** 23 statements, 8 arcs, **all missing**.

**The shape** (`app/shared/user.py:303-339`):

```
:306  if src == SRC_API: user = authorise_api_user(auth, return_type='model')  else current_user
:311  recipient = User.get(user_id)
:312  existing_challenge = BotChallenge.query.filter(...).first()
:313  if existing_challenge:
:314      if existing_challenge.is_a_bot is False: raise Exception('This person has already responded to the challenge')
:316      uuid = existing_challenge.uuid
:317  else: uuid = gibberish(49)
:320-324  Conversation(user_id=user.id); members.append(recipient); members.append(current_user); commit
:326-334  build challenge_text
:335  send_message(challenge_text, conversation.id)
:337  if existing_challenge is None: add BotChallenge(...); commit
```

**Read `:320-322` carefully before writing.** `:320` uses `user`; `:322` appends `current_user`. On an API call those are different objects and `current_user` is anonymous. `send_message` at `app/chat/util.py:12` has `user: User = current_user` as a default argument evaluated at definition time, and `:335` calls it without a `user`, so the API arm reaches `user.id` on an anonymous proxy. **This is a defect to register, not to fix** — this round's production budget is Task 1's migration and Task 6's four lines.

That has a direct consequence for the tests: **the SRC_API arm of `bot_challenge_user` may not be drivable without a request context at all.** Find out by running it, and let the result decide:

- If it raises, write the test to pin the raise with the exact exception text observed, and say in the docstring that this pins a defect rather than intended behaviour.
- If it succeeds because a `web_ctx` is active and `current_user` is therefore a real user, write the test that way and note in the docstring that the API arm is only exercisable with a logged-in web session present — which is itself the finding.

Do not decide this from the plan. Run it, paste what happened, and write the test against the observed behaviour.

- [ ] **Step 1: Find out what the API arm actually does**

```bash
cd /home/blentz/git/pyfedi
cat > /tmp/probe_bot_challenge.py <<'PROBE'
def test_probe_api_arm(app, db_session):
    from tests.test_shared_user_blocks import _seed_blockers
    from tests.factories import bearer
    from app.constants import SRC_API
    from app.shared.user import bot_challenge_user
    s = _seed_blockers()
    try:
        bot_challenge_user(s.target.id, SRC_API, bearer(s.blocker))
        print('PROBE: API arm SUCCEEDED with no request context')
    except Exception as exc:
        print(f'PROBE: API arm raised {type(exc).__name__}: {exc}')
    assert True
PROBE
podman-compose -f compose.test.yaml exec -T test-runner python -c "
import pathlib, sys
src = sys.stdin.read()
pathlib.Path('/app/tests/test_probe_bot_challenge.py').write_text(src)
print('written')
" < /tmp/probe_bot_challenge.py
./run_tests.sh tests/test_probe_bot_challenge.py -v -s
echo "PYTEST_EXIT=$?"
```

Paste the `PROBE:` line into your report verbatim. Then remove the probe file — it is a file this task created, so deleting it is in scope:

```bash
podman-compose -f compose.test.yaml exec -T test-runner rm /app/tests/test_probe_bot_challenge.py
```

- [ ] **Step 2: Append the tests**

Append to `tests/test_shared_user_blocks.py`. Add `BotChallenge`, `ChatMessage` and `Conversation` to the `app.models` import at the top of the file, and `bot_challenge_user` to the `app.shared.user` import.

```python
def test_bot_challenge_user_web_sends_a_message_and_records_the_challenge(app, db_session):
    """The first-challenge path: no existing BotChallenge, so :318 mints a
    fresh uuid and :337-339 records it.

    The uuid is asserted to appear IN the message body rather than merely to
    exist, because :333 interpolates it into the link the recipient has to
    visit -- a challenge row whose uuid never reached the message is useless.
    """
    s = _seed_blockers()

    with web_ctx(app, s.blocker):
        bot_challenge_user(s.target.id, SRC_WEB)

    challenge = db.session.query(BotChallenge).filter_by(user_id=s.target.id).one()
    assert challenge.sent_by == s.blocker.id
    assert len(challenge.uuid) == 49

    message = db.session.query(ChatMessage).one()
    assert f'/bot_challenge/{challenge.uuid}' in message.body


def test_bot_challenge_user_puts_the_recipient_in_the_conversation(app, db_session):
    """:321's `members.append(recipient)`.

    The membership is compared as a SET: `conversation.members` comes back
    through a relationship whose row order no test may depend on.
    """
    s = _seed_blockers()

    with web_ctx(app, s.blocker):
        bot_challenge_user(s.target.id, SRC_WEB)

    conversation = db.session.query(Conversation).one()
    assert {m.id for m in conversation.members} == {s.blocker.id, s.target.id}
    assert conversation.user_id == s.blocker.id


def test_bot_challenge_user_reuses_the_uuid_of_an_unanswered_challenge(app, db_session):
    """:313-316 -- an existing challenge with is_a_bot left NULL.

    The second call must reuse the stored uuid and must NOT insert a second
    BotChallenge row (:337's `is None` is false). The uuid is set to a
    recognisable constant so that "reused" is distinguishable from "minted a
    new one that happens to be 49 characters".
    """
    s = _seed_blockers()
    db.session.add(BotChallenge(user_id=s.target.id, sent_by=s.blocker.id,
                                uuid='reused-uuid-from-the-first-challenge-0000000000000'[:49]))
    db.session.commit()

    with web_ctx(app, s.blocker):
        bot_challenge_user(s.target.id, SRC_WEB)

    assert db.session.query(BotChallenge).filter_by(user_id=s.target.id).count() == 1
    message = db.session.query(ChatMessage).one()
    stored = db.session.query(BotChallenge).filter_by(user_id=s.target.id).one().uuid
    assert f'/bot_challenge/{stored}' in message.body


def test_bot_challenge_user_refuses_someone_who_already_answered(app, db_session):
    """:314's `is_a_bot is False` -- identity, not truthiness.

    is_a_bot is set to False explicitly. The previous test leaves it NULL and
    proceeds, which is the positive control that distinguishes `is False`
    from a plain falsiness check: NULL is falsy too, and a `not
    existing_challenge.is_a_bot` would refuse both.
    """
    s = _seed_blockers()
    db.session.add(BotChallenge(user_id=s.target.id, sent_by=s.blocker.id,
                                uuid='x' * 49, is_a_bot=False))
    db.session.commit()

    with web_ctx(app, s.blocker):
        with pytest.raises(Exception, match='already responded to the challenge'):
            bot_challenge_user(s.target.id, SRC_WEB)

    assert db.session.query(ChatMessage).count() == 0
    assert db.session.query(Conversation).count() == 0


def test_bot_challenge_user_continues_for_a_confirmed_bot(app, db_session):
    """is_a_bot True reaches :316, not :315.

    Without this arm, `is False` at :314 could be replaced by `is not None`
    and every other test here would stay green.
    """
    s = _seed_blockers()
    db.session.add(BotChallenge(user_id=s.target.id, sent_by=s.blocker.id,
                                uuid='y' * 49, is_a_bot=True))
    db.session.commit()

    with web_ctx(app, s.blocker):
        bot_challenge_user(s.target.id, SRC_WEB)

    assert db.session.query(ChatMessage).count() == 1
```

Then add **one** test for the SRC_API arm, written against what Step 1 observed. If the probe raised, use this shape, substituting the exception type and message text you actually saw:

```python
def test_bot_challenge_user_api_arm_reaches_current_user(app, db_session):
    """PINS A DEFECT. :320 builds the Conversation from `user` -- the
    API-authorised caller -- but :322 appends `current_user`, and
    `send_message` (app/chat/util.py:12) takes `user: User = current_user` as
    a default evaluated at definition time, which :335 does not override. On
    an API call with no logged-in session, `current_user` is anonymous.

    Registered rather than fixed: this round's production budget is the
    backfill migration and ban_user's source test. See the register entry.

    The exception below was OBSERVED, not predicted -- see this task's report.
    """
    s = _seed_blockers()

    with pytest.raises(<observed type>, match='<observed text>'):
        bot_challenge_user(s.target.id, SRC_API, bearer(s.blocker))
```

If the probe instead succeeded, drop the `pytest.raises` and assert the rows it produced, keeping the docstring's explanation of why `:322` is still wrong.

- [ ] **Step 3: Run, and measure the function to zero**

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh tests/test_shared_user_blocks.py -v
echo "PYTEST_EXIT=$?"
./run_tests.sh tests/test_shared_user_blocks.py \
    --cov=app.shared.user --cov-report=json:/tmp/user-groupa.json -q
echo "PYTEST_EXIT=$?"
podman-compose -f compose.test.yaml exec -T test-runner python -c "
import json
fns = json.load(open('/tmp/user-groupa.json'))['files']['app/shared/user.py']['functions']
for name in ('block_another_user', 'unblock_another_user', 'bot_challenge_user'):
    key = [k for k in fns if k.endswith(name)][0]
    print(name, fns[key]['missing_lines'], fns[key]['missing_branches'])
"
```

All three functions must print `[] []`.

- [ ] **Step 4: Duplicate-name check and tree check**

```bash
cd /home/blentz/git/pyfedi
/usr/bin/grep -oE "^ *def (test_[a-z0-9_]+)" tests/test_shared_user_blocks.py \
  | sed 's/^ *//' | sort | uniq -d
git diff --quiet -- app/ && echo "TREE CLEAN under app/" || echo "TREE DIRTY under app/"
```

Expected: no duplicates, `TREE CLEAN under app/`.

- [ ] **Step 5: Commit**

```bash
cd /home/blentz/git/pyfedi
git add tests/test_shared_user_blocks.py
git commit -F <message-file>
```

Subject: `test: cover bot_challenge_user, pinning its anonymous-caller defect`.
Body: what the function does, what the probe in Step 1 showed about the API arm, and that `:322`'s `current_user` is registered rather than fixed.

---

### Task 4: `ban_user` without a purge, and `unban_user`

**Files:**
- Create: `tests/test_shared_user_bans.py`
- Read: `app/shared/user.py:141-229`

**Interfaces:**
- Consumes: nothing.
- Produces: `_seed_ban_scenario(target_local=True)`, used by Tasks 5 and 6 in the same file. Returns a `SimpleNamespace` with `.admin` (the banning actor, holding the `'ban users'` permission), `.target` (the user to ban) and `.instance`.

**Target this task:** `ban_user`'s `purge_content` false path (`:188-198`), the IP-ban block (`:201-205`), the tail (`:207-210`), and all of `unban_user` (`:213-229`). `ban_user`'s purge paths are Task 5.

**Read this before you write a line of it.**

`ban_user` has **no permission guard.** `:141-210` contains no authorization check of any kind. `authorise_api_user` at `:143` authenticates and does not authorize. Both callers gate before calling — `app/api/alpha/utils/user.py:971` and `app/user/routes.py:767` each test `user_access('ban users', ...) or user_access('manage users', ...)`. So do not write a test asserting that `ban_user` refuses an unprivileged caller; it does not, and such a test would fail. The spec's earlier claim to the contrary is retracted in its `## Verification` section.

`add_to_modlog` (`app/utils.py:3564`) **does** read the actor: `:3570` branches on `actor.is_instance_admin() or actor.is_admin() or actor.is_staff()` to choose `'admin'` or `'mod'` as the action type. That is why `_seed_ban_scenario` must burn the id-1 seat — otherwise the actor is id 1, `is_admin()` returns True from the shortcut at `app/models.py:1260`, and the modlog type is `'admin'` for a reason no test intended.

**The four `if SRC_WEB:` lines at `:161`, `:171`, `:183` and `:192` are a defect this task does not fix.** `SRC_WEB` is 1, so all four are unconditionally true and the flashes fire on an API ban as well as a web one. **Task 4 and Task 5 pin that behaviour; Task 6 fixes it and inverts the pins.** Write the pinning assertions so they are unmistakable — a docstring saying PINS A DEFECT, and an assertion that the flash *is* present on an API call — so that Task 6's inversion is a visible edit rather than a silent one.

- [ ] **Step 1: Write the file**

Create `tests/test_shared_user_bans.py`:

```python
"""`ban_user` and `unban_user` (app/shared/user.py:141-229).

Both were at literally zero coverage before this file: 51 statements / 26
arcs and 10 / 2, every one missing.

NO PERMISSION GUARD. ban_user contains no authorization check. :143's
`authorise_api_user` authenticates and does not authorize, and both callers
gate before calling -- app/api/alpha/utils/user.py:971 and
app/user/routes.py:767 each test
`user_access('ban users', ...) or user_access('manage users', ...)`. There is
therefore no "unprivileged caller is refused" test in this file, because
there is nothing to refuse it.

THE id-1 ADMIN TRAP IS LIVE HERE. `add_to_modlog` (app/utils.py:3570)
branches on `actor.is_instance_admin() or actor.is_admin() or
actor.is_staff()` to pick 'admin' or 'mod' as the action type, and
`User.is_admin` (app/models.py:1259-1261) returns True for id 1 outright.
tests/conftest.py:131-132 resets every sequence between tests, so the first
user minted is id 1 deterministically. _seed_ban_scenario burns that seat.

FOUR TESTS IN THIS FILE PIN A DEFECT. app/shared/user.py:161, :171, :183 and
:192 read `if SRC_WEB:` -- a bare imported name whose value is 1
(app/constants.py:91) -- where :93 in the same module reads
`if src == SRC_WEB:`. All four are unconditionally true, so ban_user's
web-only flash messages fire on API calls too. Those tests assert today's
behaviour and are INVERTED by the task that fixes the four lines. Each one
says PINS A DEFECT in its docstring.
"""
import contextlib
from types import SimpleNamespace

import pytest
from flask import session as flask_session

from app import db
from app.constants import SRC_API, SRC_WEB
from app.models import IpBan, ModLog, User
from app.shared.user import ban_user, unban_user
from tests.factories import (bearer, grant_permission, make_instance,
                             make_user, web_ctx)


class _BanForm:
    """The web arm's `input`, which is a WTForms object rather than a dict.

    :151-155 read `input.person_id`, `input.purge.data`, `input.ip_address.data`,
    `input.reason.data` and `input.flush.data`. app/user/routes.py:782 sets
    `form.person_id = user.id` as a plain attribute on the form immediately
    before calling, so person_id is an int here and the other four are
    field-like objects with a `.data`.
    """

    class _Field:
        def __init__(self, data):
            self.data = data

    def __init__(self, person_id, purge=False, ip_address=False, reason='',
                 flush=False):
        self.person_id = person_id
        self.purge = self._Field(purge)
        self.ip_address = self._Field(ip_address)
        self.reason = self._Field(reason)
        self.flush = self._Field(flush)


def _seed_ban_scenario(target_local=True):
    """An admin who may ban, and a target to ban.

    The first user minted is id 1 and `User.is_admin` (app/models.py:1259-1261)
    calls id 1 an admin regardless of roles. `add_to_modlog` (app/utils.py:3570)
    reads exactly that to choose between the 'admin' and 'mod' action types,
    so without this burn every modlog assertion in this file would be
    satisfied by the id-1 shortcut rather than by the role the test granted.
    tests/test_shared_reply_make.py:247 is the established precedent.

    `grant_permission` is called ONCE here. It mints a Role with a sequential
    id, and app/shared/user.py does not compare role ids -- but
    block_another_user in the sibling test file does, against ROLE_STAFF (3)
    and ROLE_ADMIN (4), so the call count is kept deliberate and low.
    """
    instance = make_instance('remote.example')
    burn = make_user(instance, 'burn-the-id-1-seat', local=True)
    assert burn.id == 1, 'the id-1 admin trap moved; re-derive before trusting this'

    admin = make_user(instance, 'banner', local=True)
    target = make_user(instance, 'target', local=target_local)
    grant_permission(admin, 'ban users')
    db.session.commit()
    return SimpleNamespace(admin=admin, target=target, instance=instance)


@contextlib.contextmanager
def _recording_task_selector():
    """Collect every task key ban_user and unban_user federate.

    Both call `task_selector(...)` unqualified, and app/shared/user.py:10's
    `from app.shared.tasks import task_selector` already bound the original
    into this module's globals -- so patching app.shared.tasks.task_selector
    would NOT intercept. Rebinding the name ON app.shared.user does.
    tests/test_shared_post_moderation.py:142 is the precedent.

    Restores in a finally: app.shared.user is imported once per session, so a
    leaked patch would corrupt every test that ran after this one.
    """
    import app.shared.user as user_module
    calls = []
    original = user_module.task_selector

    def recorder(task_key, **kwargs):
        calls.append((task_key, kwargs))
        return None

    user_module.task_selector = recorder
    try:
        yield calls
    finally:
        user_module.task_selector = original


def test_ban_user_api_without_purge_bans_and_logs(app, db_session):
    s = _seed_ban_scenario()

    with _recording_task_selector() as calls:
        ban_user({'person_id': s.target.id, 'purge_content': False,
                  'ban_ip_address': False, 'reason': 'spam'},
                 SRC_API, bearer(s.admin))

    db.session.expire_all()
    assert db.session.query(User).get(s.target.id).banned is True
    entry = db.session.query(ModLog).one()
    assert entry.action == 'ban_user'
    assert entry.reason == 'spam'
    assert [key for key, _kwargs in calls] == ['ban_from_site']


def test_ban_user_without_purge_does_not_delete_the_target(app, db_session):
    """The `else` at :188 is the no-purge branch: it logs 'ban_user', not
    'delete_user', and reaches no deletion path at all.

    Asserting the modlog action alone would not separate the branches -- both
    write a ModLog row -- so `deleted` is asserted too.
    """
    s = _seed_ban_scenario()

    with _recording_task_selector():
        ban_user({'person_id': s.target.id, 'purge_content': False,
                  'ban_ip_address': False, 'reason': 'spam'},
                 SRC_API, bearer(s.admin))

    db.session.expire_all()
    target = db.session.query(User).get(s.target.id)
    assert target.deleted is False
    assert db.session.query(ModLog).one().action == 'ban_user'


def test_ban_user_passes_remove_data_false_when_not_purging(app, db_session):
    """:207's `remove_data=purge_content and to_ban.is_local()`.

    The first operand. Task 5 covers the second.
    """
    s = _seed_ban_scenario()

    with _recording_task_selector() as calls:
        ban_user({'person_id': s.target.id, 'purge_content': False,
                  'ban_ip_address': False, 'reason': 'spam'},
                 SRC_API, bearer(s.admin))

    key, kwargs = calls[0]
    assert key == 'ban_from_site'
    assert kwargs['remove_data'] is False
    assert kwargs['user_id'] == s.target.id
    assert kwargs['mod_id'] == s.admin.id


def test_ban_user_api_flashes_anyway(app, db_session):
    """PINS A DEFECT. :192 reads `if SRC_WEB:` -- the bare constant, value 1
    (app/constants.py:91) -- where :93 in the same module reads
    `if src == SRC_WEB:`. So this web-only block runs on an API ban as well,
    and :198 writes an interface message into the API caller's session.

    A request context is pushed here ONLY so that flash() has somewhere to
    write; the call itself is SRC_API and carries a bearer token. That is the
    point: app/api/alpha/utils/user.py:975 calls ban_user with SRC_API from a
    real API request handler, so a request context is exactly what production
    has.

    THIS ASSERTION IS INVERTED by the task that fixes the four lines.
    """
    s = _seed_ban_scenario()

    with app.test_request_context('/'):
        with _recording_task_selector():
            ban_user({'person_id': s.target.id, 'purge_content': False,
                      'ban_ip_address': False, 'reason': 'spam'},
                     SRC_API, bearer(s.admin))
        flashed = [message for _category, message in
                   flask_session.get('_flashes', [])]

    assert flashed == [f'{s.target.display_name()} has been banned.']


def test_ban_user_web_flashes_the_plain_message(app, db_session):
    """:198's else -- a target holding no role, so :195 is false.

    Note which `else` that is: it hangs off the role check at :195, not off
    the source test at :192. A banned user WITH a role gets only the warning
    at :196 and never this message.
    """
    s = _seed_ban_scenario()

    with web_ctx(app, s.admin):
        with _recording_task_selector():
            ban_user(_BanForm(s.target.id, reason='spam'), SRC_WEB, None)
        flashed = [message for _category, message in
                   flask_session.get('_flashes', [])]

    assert flashed == [f'{s.target.display_name()} has been banned.']


def test_ban_user_warns_instead_when_the_target_holds_a_role(app, db_session):
    """:195-196 -- and the suppression at :197-198 that follows from it.

    `is_admin()` matches on the role NAME (app/models.py:1263), so the role
    granted here is named 'Admin' deliberately. The assertion is that the
    plain "has been banned" message is ABSENT: that absence is the whole
    behaviour of the else at :197.
    """
    s = _seed_ban_scenario()
    from app.models import Role, user_role
    role = Role(name='Admin', weight=0)
    db.session.add(role)
    db.session.commit()
    db.session.execute(user_role.insert().values(user_id=s.target.id,
                                                 role_id=role.id))
    db.session.commit()

    with web_ctx(app, s.admin):
        with _recording_task_selector():
            ban_user(_BanForm(s.target.id, reason='spam'), SRC_WEB, None)
        flashed = [message for _category, message in
                   flask_session.get('_flashes', [])]

    assert flashed == ['Banned user with role permissions.']


def test_ban_user_creates_an_ip_ban(app, db_session):
    """:201-205. Both operands of :201's `and` need to be true, so the target
    is given an ip_address as well as the flag."""
    s = _seed_ban_scenario()
    s.target.ip_address = '203.0.113.7'
    db.session.commit()

    with _recording_task_selector():
        ban_user({'person_id': s.target.id, 'purge_content': False,
                  'ban_ip_address': True, 'reason': 'spam'},
                 SRC_API, bearer(s.admin))

    ban = db.session.query(IpBan).one()
    assert ban.ip_address == '203.0.113.7'
    assert ban.notes == 'spam'


def test_ban_user_skips_the_ip_ban_when_the_target_has_no_address(app, db_session):
    """The second operand of :201. The flag is TRUE here and no IpBan is
    created, which is what separates the two operands: a version testing only
    `ban_ip_address` would insert a row with a NULL address."""
    s = _seed_ban_scenario()
    assert s.target.ip_address is None

    with _recording_task_selector():
        ban_user({'person_id': s.target.id, 'purge_content': False,
                  'ban_ip_address': True, 'reason': 'spam'},
                 SRC_API, bearer(s.admin))

    assert db.session.query(IpBan).count() == 0


def test_ban_user_does_not_duplicate_an_existing_ip_ban(app, db_session):
    """:203's `if not existing_ip_ban` -- the false arc."""
    s = _seed_ban_scenario()
    s.target.ip_address = '203.0.113.7'
    db.session.add(IpBan(ip_address='203.0.113.7', notes='an earlier ban'))
    db.session.commit()

    with _recording_task_selector():
        ban_user({'person_id': s.target.id, 'purge_content': False,
                  'ban_ip_address': True, 'reason': 'spam'},
                 SRC_API, bearer(s.admin))

    ban = db.session.query(IpBan).one()
    assert ban.notes == 'an earlier ban'


def test_unban_user_api_clears_banned_and_deleted(app, db_session):
    """:221-222 clear BOTH flags. The target is seeded with both set, so a
    version clearing only `banned` fails here."""
    s = _seed_ban_scenario()
    s.target.banned = True
    s.target.deleted = True
    db.session.commit()

    with _recording_task_selector() as calls:
        returned = unban_user({'person_id': s.target.id}, SRC_API,
                              bearer(s.admin))

    db.session.expire_all()
    target = db.session.query(User).get(s.target.id)
    assert target.banned is False
    assert target.deleted is False
    assert [key for key, _kwargs in calls] == ['unban_from_site']
    assert returned is None
    assert db.session.query(ModLog).one().action == 'unban_user'


def test_unban_user_web_takes_the_same_dict_shaped_input(app, db_session):
    """:214-219's fork is real but its two arms are near-identical: both read
    `input['person_id']`, so unban_user takes a DICT even on the web path --
    unlike ban_user, whose web arm reads form attributes. app/user/routes.py:817
    passes `{'person_id': user.id}` accordingly. The arms differ only in where
    the actor comes from, which is why `mod_id` is asserted here.
    """
    s = _seed_ban_scenario()
    s.target.banned = True
    db.session.commit()

    with web_ctx(app, s.admin):
        with _recording_task_selector() as calls:
            unban_user({'person_id': s.target.id}, SRC_WEB, None)

    db.session.expire_all()
    assert db.session.query(User).get(s.target.id).banned is False
    _key, kwargs = calls[0]
    assert kwargs['mod_id'] == s.admin.id
```

- [ ] **Step 2: Run them**

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh tests/test_shared_user_bans.py -v
echo "PYTEST_EXIT=$?"
```

Every failure is a finding about the code or about this plan's reading of it. Two are worth anticipating:

- `plugins.fire_hook("ban_user", to_ban)` at `:210` may raise if no plugin system is initialised in tests. If it does, patch `app.shared.user.plugins` with a double whose `fire_hook` records and returns None, using the same module-rebinding technique as `_recording_task_selector`, and say so in the report.
- If `ModLog` rejects the `'ban_user'` or `'unban_user'` action key, read `ModLog.action_map` (`app/utils.py:3568` checks against it) and report what you found.

- [ ] **Step 3: Commit**

```bash
cd /home/blentz/git/pyfedi
/usr/bin/grep -oE "^ *def (test_[a-z0-9_]+)" tests/test_shared_user_bans.py \
  | sed 's/^ *//' | sort | uniq -d
git diff --quiet -- app/ && echo "TREE CLEAN under app/" || echo "TREE DIRTY under app/"
git add tests/test_shared_user_bans.py
git commit -F <message-file>
```

Subject: `test: cover ban_user's no-purge path and unban_user`.
Body: that `ban_user` has no permission guard and why there is therefore no refusal test; the id-1 trap and the burn; and — named explicitly — that one test in this file pins the `if SRC_WEB:` defect and will be inverted.

---

### Task 5: `ban_user`'s purge paths

**Files:**
- Modify: `tests/test_shared_user_bans.py` (append)
- Read: `app/shared/user.py:160-187`

**Interfaces:**
- Consumes: `_seed_ban_scenario(target_local=True)`, `_BanForm`, `_recording_task_selector` from Task 4.
- Produces: nothing.

**Target:** `:160-187` — the `purge_content` true branch, both arms of `to_ban.is_local()` at `:168`, and the two warning flashes at `:161-165`.

**The two hazards in this branch.**

1. **`purge_user_then_delete` at `:170` really runs.** `app/user/utils.py:21` dispatches to a Celery task; `tests/conftest.py:105-110` sets `task_always_eager=True` with `eager_propagates`, so the whole purge executes inline against a separate task session. Patch it: `app/shared/user.py:11` does `from app.user.utils import purge_user_then_delete`, so rebind **`app.shared.user.purge_user_then_delete`** — patching `app.user.utils.purge_user_then_delete` would not intercept.

2. **`:178` is `with redis_client.lock(...)`, and fakeredis cannot serve one.** `Lock.acquire()` succeeds; `Lock.release()` calls EVALSHA and raises `redis.exceptions.ResponseError: unknown command 'evalsha'` on `__exit__`, every time. Use the `redis_lock_only_double` fixture (`tests/test_inbox_dispatch_votes.py:157-159`), whose `.lock()` returns `contextlib.nullcontext()`. **Do not use `redis_double`**, and do not try to fix fakeredis. `:177` does `from app import redis_client` inside the function body, so monkeypatching the attribute `app.redis_client` reaches it.

- [ ] **Step 1: Append the tests**

Import `InstanceRole` from `app.models` and add the fixture import. Append:

```python
@pytest.fixture
def no_real_purge(monkeypatch):
    """Record purge_user_then_delete instead of running it.

    app/shared/user.py:11 does `from app.user.utils import
    purge_user_then_delete`, which bound the original into this module's
    globals at import time -- so patching app.user.utils would not intercept
    :170's unqualified call. The real function dispatches a Celery task, and
    tests/conftest.py:105-110 sets task_always_eager with eager_propagates,
    so leaving it unpatched runs the entire purge inline against a separate
    task session.
    """
    calls = []
    monkeypatch.setattr('app.shared.user.purge_user_then_delete',
                        lambda user_id, flush=True: calls.append((user_id, flush)))
    return calls


@pytest.fixture
def redis_lock_only_double(monkeypatch):
    """A `app.redis_client` double covering only `.lock(...)`.

    This environment's fakeredis has no Lua scripting, and redis-py's
    Lock.release() needs EVALSHA -- so the redis_double fixture raises
    `redis.exceptions.ResponseError: unknown command 'evalsha'` on __exit__
    of any `with redis_client.lock(...)` block, which app/shared/user.py:178
    is. See tests/test_inbox_dispatch_votes.py:145-159 and "The fakeredis lock
    limitation" in tests/README.md.

    app/shared/user.py:177 imports redis_client INSIDE the function body, so
    patching the single module attribute reaches it.
    """
    class _LockOnly:
        def lock(self, *args, **kwargs):
            return contextlib.nullcontext()

    monkeypatch.setattr('app.redis_client', _LockOnly())


def test_ban_user_purging_a_local_target_deletes_it_through_the_purge_task(
        app, db_session, no_real_purge):
    """:168's true arm. A local target reaches :169-170: deleted_by is
    stamped and purge_user_then_delete is dispatched with the flush flag the
    caller supplied.

    flush_cdn is False on the API path (:148 sets it unconditionally), which
    is what the second element of the recorded call asserts.
    """
    s = _seed_ban_scenario(target_local=True)

    with _recording_task_selector():
        ban_user({'person_id': s.target.id, 'purge_content': True,
                  'ban_ip_address': False, 'reason': 'spam'},
                 SRC_API, bearer(s.admin))

    db.session.expire_all()
    assert no_real_purge == [(s.target.id, False)]
    assert db.session.query(User).get(s.target.id).deleted_by == s.admin.id
    assert db.session.query(ModLog).one().action == 'delete_user'


def test_ban_user_purging_passes_remove_data_true_for_a_local_target(
        app, db_session, no_real_purge):
    """:207's `remove_data=purge_content and to_ban.is_local()` -- the
    second operand, whose first operand Task 4 covered."""
    s = _seed_ban_scenario(target_local=True)

    with _recording_task_selector() as calls:
        ban_user({'person_id': s.target.id, 'purge_content': True,
                  'ban_ip_address': False, 'reason': 'spam'},
                 SRC_API, bearer(s.admin))

    _key, kwargs = calls[0]
    assert kwargs['remove_data'] is True


def test_ban_user_purging_a_remote_target_takes_the_local_deletion_path(
        app, db_session, no_real_purge, redis_lock_only_double):
    """:174-184's else arm. A remote target is NOT dispatched to the purge
    task: its content is removed inline and the row is marked deleted under a
    redis lock.

    `no_real_purge` is requested even though nothing should reach it -- an
    empty recorder is the assertion that :170 was not taken, and without the
    patch a wrong branch would run the real Celery purge instead of failing
    visibly.

    make_user leaves a remote user's ap_id set (tests/factories.py:60), and
    User.is_local() (app/models.py:1252) is
    `self.ap_id is None or self.ap_profile_id.startswith(SERVER_URL)` -- so a
    user built against remote.example is not local on either operand.
    """
    s = _seed_ban_scenario(target_local=False)

    with _recording_task_selector():
        ban_user({'person_id': s.target.id, 'purge_content': True,
                  'ban_ip_address': False, 'reason': 'spam'},
                 SRC_API, bearer(s.admin))

    db.session.expire_all()
    assert no_real_purge == []
    target = db.session.query(User).get(s.target.id)
    assert target.deleted is True
    assert target.deleted_by == s.admin.id
    assert db.session.query(ModLog).one().action == 'delete_user'


def test_ban_user_purging_passes_remove_data_false_for_a_remote_target(
        app, db_session, no_real_purge, redis_lock_only_double):
    """The other half of :207's `and`: purge_content is True but the target
    is remote, so remove_data is False. Paired with the local test above,
    this is what makes the `and` non-void."""
    s = _seed_ban_scenario(target_local=False)

    with _recording_task_selector() as calls:
        ban_user({'person_id': s.target.id, 'purge_content': True,
                  'ban_ip_address': False, 'reason': 'spam'},
                 SRC_API, bearer(s.admin))

    _key, kwargs = calls[0]
    assert kwargs['remove_data'] is False


def test_ban_user_purging_a_local_target_flashes_on_the_web(
        app, db_session, no_real_purge):
    """:171-173. The web form path also carries flush_cdn from
    `input.flush.data` (:155), which the API path hardcodes False -- so
    _BanForm sets flush=True and the recorded call asserts it."""
    s = _seed_ban_scenario(target_local=True)

    with web_ctx(app, s.admin):
        with _recording_task_selector():
            ban_user(_BanForm(s.target.id, purge=True, reason='spam',
                              flush=True), SRC_WEB, None)
        flashed = [message for _category, message in
                   flask_session.get('_flashes', [])]

    assert no_real_purge == [(s.target.id, True)]
    assert flashed == [
        f'{s.target.display_name()} has been banned, deleted and all their '
        f'content deleted. This might take a few minutes.']


def test_ban_user_purging_a_remote_target_flashes_the_shorter_message(
        app, db_session, no_real_purge, redis_lock_only_double):
    """:183-184. The remote message omits "This might take a few minutes."
    because nothing was queued -- the deletion already happened inline. The
    two messages are compared in full so that a swapped pair fails."""
    s = _seed_ban_scenario(target_local=False)

    with web_ctx(app, s.admin):
        with _recording_task_selector():
            ban_user(_BanForm(s.target.id, purge=True, reason='spam'),
                     SRC_WEB, None)
        flashed = [message for _category, message in
                   flask_session.get('_flashes', [])]

    assert flashed == [
        f'{s.target.display_name()} has been banned, deleted and all their '
        f'content deleted.']


def test_ban_user_purging_an_instance_admin_warns_first(
        app, db_session, no_real_purge, redis_lock_only_double):
    """:162-163. `is_instance_admin` (app/models.py:1277-1284) needs an
    InstanceRole row with role 'admin' on the user's own instance -- it is
    NOT the same thing as the 'Admin' Role that is_admin() matches by name,
    and the two warnings at :163 and :165 are independent `if`s rather than a
    chain.

    The target is remote so that :174's arm is the one taken, keeping this
    test about the warning rather than about the purge task.
    """
    s = _seed_ban_scenario(target_local=False)
    db.session.add(InstanceRole(instance_id=s.target.instance_id,
                                user_id=s.target.id, role='admin'))
    db.session.commit()

    with web_ctx(app, s.admin):
        with _recording_task_selector():
            ban_user(_BanForm(s.target.id, purge=True, reason='spam'),
                     SRC_WEB, None)
        flashed = [message for _category, message in
                   flask_session.get('_flashes', [])]

    assert 'Purged user was a remote instance admin.' in flashed


def test_ban_user_purging_a_role_holder_warns_about_permissions(
        app, db_session, no_real_purge, redis_lock_only_double):
    """:164-165. Named 'Admin' because is_admin() matches the role NAME
    (app/models.py:1263). Independent of the instance-admin warning above:
    this target has no InstanceRole, so only one of the two warnings fires,
    which is what separates :162 from :164."""
    s = _seed_ban_scenario(target_local=False)
    from app.models import Role, user_role
    role = Role(name='Admin', weight=0)
    db.session.add(role)
    db.session.commit()
    db.session.execute(user_role.insert().values(user_id=s.target.id,
                                                 role_id=role.id))
    db.session.commit()

    with web_ctx(app, s.admin):
        with _recording_task_selector():
            ban_user(_BanForm(s.target.id, purge=True, reason='spam'),
                     SRC_WEB, None)
        flashed = [message for _category, message in
                   flask_session.get('_flashes', [])]

    assert 'Purged user with role permissions.' in flashed
    assert 'Purged user was a remote instance admin.' not in flashed


def test_ban_user_purging_via_the_api_warns_anyway(
        app, db_session, no_real_purge, redis_lock_only_double):
    """PINS A DEFECT. :161 reads `if SRC_WEB:` -- the bare constant, value 1
    -- so the warning block runs on an API ban too.

    THIS ASSERTION IS INVERTED by the task that fixes the four lines.
    """
    s = _seed_ban_scenario(target_local=False)
    db.session.add(InstanceRole(instance_id=s.target.instance_id,
                                user_id=s.target.id, role='admin'))
    db.session.commit()

    with app.test_request_context('/'):
        with _recording_task_selector():
            ban_user({'person_id': s.target.id, 'purge_content': True,
                      'ban_ip_address': False, 'reason': 'spam'},
                     SRC_API, bearer(s.admin))
        flashed = [message for _category, message in
                   flask_session.get('_flashes', [])]

    assert 'Purged user was a remote instance admin.' in flashed


def test_ban_user_purging_a_local_target_via_the_api_flashes_anyway(
        app, db_session, no_real_purge):
    """PINS A DEFECT. :171, the same bare-constant fault as :161.

    THIS ASSERTION IS INVERTED by the task that fixes the four lines.
    """
    s = _seed_ban_scenario(target_local=True)

    with app.test_request_context('/'):
        with _recording_task_selector():
            ban_user({'person_id': s.target.id, 'purge_content': True,
                      'ban_ip_address': False, 'reason': 'spam'},
                     SRC_API, bearer(s.admin))
        flashed = [message for _category, message in
                   flask_session.get('_flashes', [])]

    assert flashed == [
        f'{s.target.display_name()} has been banned, deleted and all their '
        f'content deleted. This might take a few minutes.']


def test_ban_user_purging_a_remote_target_via_the_api_flashes_anyway(
        app, db_session, no_real_purge, redis_lock_only_double):
    """PINS A DEFECT. :183, the same bare-constant fault.

    THIS ASSERTION IS INVERTED by the task that fixes the four lines.
    """
    s = _seed_ban_scenario(target_local=False)

    with app.test_request_context('/'):
        with _recording_task_selector():
            ban_user({'person_id': s.target.id, 'purge_content': True,
                      'ban_ip_address': False, 'reason': 'spam'},
                     SRC_API, bearer(s.admin))
        flashed = [message for _category, message in
                   flask_session.get('_flashes', [])]

    assert flashed == [
        f'{s.target.display_name()} has been banned, deleted and all their '
        f'content deleted.']
```

- [ ] **Step 2: Run, and drive both ban functions to zero missing**

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh tests/test_shared_user_bans.py -v
echo "PYTEST_EXIT=$?"
./run_tests.sh tests/test_shared_user_bans.py \
    --cov=app.shared.user --cov-report=json:/tmp/user-bans.json -q
echo "PYTEST_EXIT=$?"
podman-compose -f compose.test.yaml exec -T test-runner python -c "
import json
fns = json.load(open('/tmp/user-bans.json'))['files']['app/shared/user.py']['functions']
for name in ('ban_user', 'unban_user'):
    key = [k for k in fns if k.endswith(name)][0]
    print(name, fns[key]['missing_lines'], fns[key]['missing_branches'])
"
```

**Expected, and this is the point of the task:** `unban_user` prints `[] []`. **`ban_user` does NOT.** Four branches — at `:161`, `:171`, `:183` and `:192` — will report as partial, because `SRC_WEB` is an imported name that CPython cannot constant-fold, so the branch survives into bytecode with an unreachable false arc. Paste the exact `missing_branches` list into your report. Those four are Task 6's deliverable; do not chase them here, and do not add a `# pragma` (`.coveragerc:7` excludes only `pragma: no cover`, and excluding a live defect is not a fix).

- [ ] **Step 3: Commit**

```bash
cd /home/blentz/git/pyfedi
/usr/bin/grep -oE "^ *def (test_[a-z0-9_]+)" tests/test_shared_user_bans.py \
  | sed 's/^ *//' | sort | uniq -d
git diff --quiet -- app/ && echo "TREE CLEAN under app/" || echo "TREE DIRTY under app/"
git add tests/test_shared_user_bans.py
git commit -F <message-file>
```

Subject: `test: cover ban_user's purge paths, local and remote`.
Body: the two hazards (the eager Celery purge and the fakeredis lock) and how each was handled; the four partial branches that remain and why no test can reach them.

---

### Task 6: Fix `ban_user`'s source test and invert the pins

**Files:**
- Modify: `app/shared/user.py:161,171,183,192`
- Modify: `tests/test_shared_user_bans.py` (the four tests whose docstrings say PINS A DEFECT)

**Interfaces:**
- Consumes: the four pinning tests from Tasks 4 and 5.
- Produces: `app/shared/user.py` with no bare-constant source tests. Task 8 re-measures the whole module against this.

**This is one of the two production changes this round is allowed.** Read the spec's `CORRECTION` subsection before starting.

**The defect.** `app/constants.py:91` is `SRC_WEB = 1`. Four lines test that name directly instead of comparing it to the caller's `src`:

```
app/shared/user.py:161        if SRC_WEB:
app/shared/user.py:171            if SRC_WEB:
app/shared/user.py:183            if SRC_WEB:
app/shared/user.py:192        if SRC_WEB:
```

The correct form is in the same module at `:93`: `if src == SRC_WEB:`. A tree-wide sweep finds no others. **Four, not five** — the `else` at `:197` hangs off the role check at `:195`, not off the source test.

- [ ] **Step 1: Re-derive the four lines before touching them**

```bash
cd /home/blentz/git/pyfedi
/usr/bin/grep -rn "if SRC_WEB\|if SRC_API\|if SRC_PLD" app/
awk 'NR==93 || NR==161 || NR==171 || NR==183 || NR==192 {printf "%d\t%s\n",NR,$0}' app/shared/user.py
```

Expected: the sweep returns exactly the four `app/shared/user.py` lines above and nothing else; the `awk` shows `:93` in the correct form and the other four in the bare form. If the line numbers have moved, use what the `awk` prints — never a number from this plan.

- [ ] **Step 2: Make the four edits**

Each is the same change, preserving the existing indentation exactly:

| Line | From | To |
|---|---|---|
| 161 | `        if SRC_WEB:` | `        if src == SRC_WEB:` |
| 171 | `            if SRC_WEB:` | `            if src == SRC_WEB:` |
| 183 | `            if SRC_WEB:` | `            if src == SRC_WEB:` |
| 192 | `        if SRC_WEB:` | `        if src == SRC_WEB:` |

- [ ] **Step 3: Confirm the edit landed and nothing else did**

```bash
cd /home/blentz/git/pyfedi
/usr/bin/grep -rn "if SRC_WEB\|if SRC_API\|if SRC_PLD" app/
git diff -- app/shared/user.py
```

Expected: the sweep now returns **nothing**, and the diff is exactly four lines changed, four added, four removed, with no whitespace-only changes anywhere else in the file.

From this point until Task 6 is committed, the tree holds an uncommitted production change. **`git checkout -- app/` is banned** for the rest of this task — it would revert this fix silently, which is exactly what happened in sub-project 40. If you need to undo an edit, reverse it by hand and re-read the restored line.

- [ ] **Step 4: Watch the four pinning tests fail**

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh tests/test_shared_user_bans.py -v
echo "PYTEST_EXIT=$?"
```

Expected: exactly **four** failures, and they must be these four:

- `test_ban_user_api_flashes_anyway`
- `test_ban_user_purging_via_the_api_warns_anyway`
- `test_ban_user_purging_a_local_target_via_the_api_flashes_anyway`
- `test_ban_user_purging_a_remote_target_via_the_api_flashes_anyway`

**If a different count or a different set fails, stop and report it.** A fifth failure means the fix changed behaviour this plan did not predict; three means one of the pins was not actually pinning. Either way the discrepancy is the finding, not an obstacle.

- [ ] **Step 5: Invert the four tests**

Rename each of the four, replace its docstring, and invert its assertion. `test_ban_user_api_flashes_anyway` becomes:

```python
def test_ban_user_api_does_not_flash(app, db_session):
    """The inversion of a pin. Before the fix, :192 read `if SRC_WEB:` -- the
    bare constant, value 1 -- so this web-only block ran on API bans and wrote
    an interface message into the API caller's session. It now reads
    `if src == SRC_WEB:`, matching :93.

    The request context is still pushed, and flash() would still succeed if
    the block ran: the empty list below is therefore evidence that the block
    was SKIPPED, not that flashing was impossible. That distinction is what
    makes this an assertion rather than an artefact of the harness -- see the
    web test above, which flashes under the same conditions.
    """
    s = _seed_ban_scenario()

    with app.test_request_context('/'):
        with _recording_task_selector():
            ban_user({'person_id': s.target.id, 'purge_content': False,
                      'ban_ip_address': False, 'reason': 'spam'},
                     SRC_API, bearer(s.admin))
        flashed = [message for _category, message in
                   flask_session.get('_flashes', [])]

    assert flashed == []
    db.session.expire_all()
    assert db.session.query(User).get(s.target.id).banned is True
```

Note the second assertion. An empty flash list is mechanism (c) of the five false witnesses — emptiness with no same-mechanism positive control. `banned is True` proves the function ran at all, and the surviving web-arm tests are the positive control proving flashes are reachable in this harness.

Invert the other three the same way: rename `..._warns_anyway` to `..._does_not_warn`, `..._flashes_anyway` to `..._does_not_flash`, assert `flashed == []`, and in each case add an assertion that the ban actually happened — `no_real_purge == [(s.target.id, False)]` for the local-purge test, `db.session.query(ModLog).one().action == 'delete_user'` for the remote ones.

Also update the module docstring: the paragraph beginning "FOUR TESTS IN THIS FILE PIN A DEFECT" now describes history. Replace it with a paragraph saying the four lines were fixed in this round, what they read before, and that the four tests below are the inverted pins.

- [ ] **Step 6: Run the file green, then re-measure `ban_user`**

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh tests/test_shared_user_bans.py -v
echo "PYTEST_EXIT=$?"
./run_tests.sh tests/test_shared_user_bans.py \
    --cov=app.shared.user --cov-report=json:/tmp/user-bans-fixed.json -q
echo "PYTEST_EXIT=$?"
podman-compose -f compose.test.yaml exec -T test-runner python -c "
import json
fns = json.load(open('/tmp/user-bans-fixed.json'))['files']['app/shared/user.py']['functions']
for name in ('ban_user', 'unban_user'):
    key = [k for k in fns if k.endswith(name)][0]
    print(name, fns[key]['missing_lines'], fns[key]['missing_branches'])
"
```

Expected: the file is green, and **both functions now print `[] []`**. The four arcs that were structurally unreachable in Task 5 are reachable now, and the four inverted tests reach them. If any remain, the fix did not do what this task claims and you must report that rather than paper over it.

- [ ] **Step 7: Check the whole suite for collateral damage**

The four lines changed behaviour for every API caller of `ban_user`. Something elsewhere may have depended on the old behaviour.

```bash
cd /home/blentz/git/pyfedi
/usr/bin/grep -rn "ban_user" tests/ --include=*.py | /usr/bin/grep -v "test_shared_user_bans\|ban_user_from_community\|ap_util\|test_ap_moderation"
```

Run any file this turns up. Report what you found, including "nothing".

- [ ] **Step 8: Commit**

```bash
cd /home/blentz/git/pyfedi
git diff --stat -- app/
git add app/shared/user.py tests/test_shared_user_bans.py
git commit -F <message-file>
```

Subject: `fix: test the caller's source in ban_user rather than the constant`.
Body, in prose: what the four lines read, that `SRC_WEB` is 1 so all four were unconditionally true, that `:93` in the same module already had the correct form, that the seven flash calls therefore fired on API bans, and that the four tests inverted in this commit were written first against the old behaviour.

---

### Task 7: The residual arcs in `follow_user`, `subscribe_user` and `unfollow_user`

**Files:**
- Create: `tests/test_shared_user_follows.py`
- Read: `app/shared/user.py:89-138`, `:231-300`

**Interfaces:**
- Consumes: nothing.
- Produces: nothing.

**Target:** 7 statements and 6 arcs across three functions that are otherwise nearly closed — reached incidentally by API tests elsewhere (`tests/test_api_user_subscriptions.py`, `tests/test_ap_notify_post.py`).

**This task does not know which lines those are, and neither does this plan.** The counts come from the full-suite JSON at `01845836`; the identities do not. **Step 1 derives them.** Do not write a test before you have the list.

- [ ] **Step 1: Derive the missing lines from a full-suite measurement**

This must be a full-suite run, not a targeted one: these three functions are nearly closed *because of other test files*, and a targeted run would report them as almost entirely missing.

Ask the controller to run it — **only the controller runs the full suite**, one session at a time, in the foreground:

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh -o session_timeout=1800 \
    --cov=app.shared.user --cov-report=json:/tmp/user-full.json -q
echo "PYTEST_EXIT=$?"
podman-compose -f compose.test.yaml exec -T test-runner python -c "
import json
d = json.load(open('/tmp/user-full.json'))['files']['app/shared/user.py']
fns = d['functions']
for name in ('follow_user', 'subscribe_user', 'unfollow_user'):
    key = [k for k in fns if k.endswith(name)][0]
    print(name, 'lines', fns[key]['missing_lines'], 'arcs', fns[key]['missing_branches'])
print('module total', d['summary']['missing_lines'], d['summary']['missing_branches'])
"
```

Paste the output verbatim into your report. **The lines it prints are this task's specification.** If the totals across the three functions do not come to 7 statements and 6 arcs, say so — the plan's numbers came from an older tree and yours are the current ones.

- [ ] **Step 2: Read each missing line at source**

For every line the previous step printed:

```bash
cd /home/blentz/git/pyfedi
awk 'NR>=89 && NR<=138 {printf "%d\t%s\n",NR,$0}' app/shared/user.py
awk 'NR>=231 && NR<=300 {printf "%d\t%s\n",NR,$0}' app/shared/user.py
```

For each one, write down in your report: what the line does, which input reaches it, and which input does not. That list is what you turn into tests. The three functions' shapes:

- `subscribe_user:89-138` — `src` fork at `:90`; `:91` loads the person with `.one()`; `:93`'s web override recomputes `subscribe` from `notify_new_posts`; `:98`'s unsubscribe branch with its already-absent case at `:102-107`; the subscribe branch's already-present case at `:110-115`, self-target at `:117-122`, blocked-by-target at `:123-128`, and the success at `:129-133`; the return fork at `:135-138`, whose web arm renders `user/_notification_toggle.html`.
- `follow_user:231-276` — `:240`'s `is_local()`; `:241`'s `ap_manually_approves_followers is True`, which sets `is_accepted = None` and **skips both counter increments** at `:245-246`; `:251`'s second `is_local()`; `:252`'s `is_accepted is None` picking between two `Notification` titles; `:274-275`'s remote arm dispatching `follow_user` as a task.
- `unfollow_user:278-300` — `:286-287`'s unconditional decrements; `:294`'s `is_local()` fork, dispatching a task for a remote target and deleting the follow request for a local one.

- [ ] **Step 3: Write one test per missing line, in a new file**

Create `tests/test_shared_user_follows.py` with a module docstring that names the three functions, states that they were nearly closed before this file, and lists — with line numbers — exactly which arcs this file adds and which test covers each. Follow the seeding conventions established in Tasks 2 and 4: burn the id-1 seat with an explicit `assert burn.id == 1`, use `web_ctx(app, user)` for web arms and `bearer(user)` for API arms, and patch `app.shared.user.task_selector` by module rebinding rather than patching `app.shared.tasks`.

Two traps specific to these three functions, both of which must appear in the relevant test's docstring:

- **`subscribe_user:93` is the correct form** — `if src == SRC_WEB:` — and is the model the four fixed lines in `ban_user` were matched against. A test reaching `:94` proves it discriminates. Reaching it requires an existing subscription for the "unsubscribe" direction and none for "subscribe", because `:94` recomputes the caller's `subscribe` argument from `notify_new_posts` and **ignores what the caller passed**. Pass `subscribe=False` into a web call with no existing subscription and assert a subscription was created: that is the assertion a `src`-less version of `:93` would fail.
- **`follow_user`'s manually-approving arm at `:241-242` sets `is_accepted = None` and skips `:245-246` entirely**, so neither `num_following` nor `num_followers` moves — while `unfollow_user:286-287` decrements both unconditionally. Assert the counters explicitly in both tests. The asymmetry is a defect to register in Task 10, and pinning it here is what makes the register entry checkable.

**Two worked examples, to fix the file's conventions.** These two are written out in full because they are the two the traps above bear on; the rest of the file follows their shape against the list Step 1 derived.

```python
def test_subscribe_user_web_ignores_the_callers_subscribe_argument(app, db_session):
    """`:93-94` -- and the correct form of the source test, for contrast.

    `:93` reads `if src == SRC_WEB:`, which is what `ban_user`'s four bare
    `if SRC_WEB:` lines were matched against and fixed to. `:94` then
    RECOMPUTES `subscribe` from `person.notify_new_posts(user_id)` and
    discards whatever the caller passed.

    So `subscribe=False` is passed here with NO existing subscription:
    notify_new_posts is false, `:94` flips subscribe to True, and a
    subscription is created. A version of `:93` that did not discriminate on
    src would leave subscribe False and take the unsubscribe branch at `:98`,
    flashing 'A subscription for this user did not exist.' and creating
    nothing -- which is exactly what the assertion below rules out.
    """
    s = _seed_followers()

    with web_ctx(app, s.follower):
        subscribe_user(s.target.id, False, SRC_WEB)

    subscriptions = {(n.user_id, n.entity_id) for n in
                     db.session.query(NotificationSubscription).all()}
    assert subscriptions == {(s.follower.id, s.target.id)}


def test_follow_user_does_not_move_the_counters_when_approval_is_manual(app, db_session):
    """PINS AN ASYMMETRY. `:241-242` sets `is_accepted = None` and skips
    `:245-246`, so neither counter moves -- but `unfollow_user:286-287`
    decrements both unconditionally, with no matching guard. Following and
    then unfollowing a manually-approving user therefore drives both counters
    NEGATIVE.

    Both halves are asserted here rather than only the follow, because the
    follow alone is consistent with the counters simply not being implemented.
    The drift is the finding; it is registered in Task 10, not fixed.
    """
    s = _seed_followers()
    s.target.ap_manually_approves_followers = True
    db.session.commit()

    follow_user(s.target.id, SRC_API, bearer(s.follower))
    db.session.expire_all()
    assert db.session.query(User).get(s.follower.id).num_following == 0
    assert db.session.query(User).get(s.target.id).num_followers == 0

    unfollow_user(s.target.id, SRC_API, bearer(s.follower))
    db.session.expire_all()
    assert db.session.query(User).get(s.follower.id).num_following == -1
    assert db.session.query(User).get(s.target.id).num_followers == -1
```

`_seed_followers()` follows the conventions of `_seed_blockers` in Task 2: an instance, an id-1 burn with its explicit assertion, then `follower` and `target` as local users. `follow_user`'s local arm writes a `Notification` and touches `to_follow.unread_notifications`, so no task selector is reached on that path; the remote arm at `:274-275` does dispatch one, and the test for it needs the module-rebinding patch.

If either worked example fails, the behaviour differs from this plan's reading of the source. Report what happened and write the test against what you observed — do not adjust the assertion to match the plan.

- [ ] **Step 4: Confirm the three functions close**

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh tests/test_shared_user_follows.py -v
echo "PYTEST_EXIT=$?"
/usr/bin/grep -oE "^ *def (test_[a-z0-9_]+)" tests/test_shared_user_follows.py \
  | sed 's/^ *//' | sort | uniq -d
git diff --quiet -- app/ && echo "TREE CLEAN under app/" || echo "TREE DIRTY under app/"
```

The module-level confirmation happens in Task 8, against the full suite — a targeted run cannot show these three closed, because part of their coverage comes from other files.

- [ ] **Step 5: Commit**

```bash
cd /home/blentz/git/pyfedi
git add tests/test_shared_user_follows.py
git commit -F <message-file>
```

Subject: `test: close the residual arcs in follow_user, subscribe_user and unfollow_user`.
Body: the derived list of missing lines from Step 1, and the counter asymmetry between `follow_user`'s manually-approving arm and `unfollow_user`'s unconditional decrements.

---

### Task 8: Close the module, add its floor, and run the full suite

**Files:**
- Modify: `coverage_floors.ini`

**Interfaces:**
- Consumes: every test file from Tasks 1-7 and the production change from Task 6.
- Produces: the measured module percentage that Task 9's mutation pass is scoped against.

**Only the controller runs this task.** One pytest session at a time, in the foreground. Never kill it.

- [ ] **Step 1: Full suite with coverage**

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh -o session_timeout=1800 \
    --cov=app.shared.user --cov-report=json:/tmp/user-final.json -q
echo "PYTEST_EXIT=$?"
```

Note the dotted `--cov=app.shared.user`. `PYTEST_EXIT` must be 0; **1 means the session timed out**, not that a test failed. Do not pipe this command — a pipeline eats the status.

- [ ] **Step 2: Check the module as lists, not as a percentage**

```bash
cd /home/blentz/git/pyfedi
podman-compose -f compose.test.yaml exec -T test-runner python -c "
import json
d = json.load(open('/tmp/user-final.json'))['files']['app/shared/user.py']
s = d['summary']
print('percent_covered   ', s['percent_covered'])
print('missing_lines     ', d['missing_lines'])
print('missing_branches  ', d['missing_branches'])
print('num_statements    ', s['num_statements'])
print('num_branches      ', s['num_branches'])
print('num_partial_branches', s['num_partial_branches'])
"
```

The success criterion is `missing_lines == []`, `missing_branches == []` and `num_partial_branches == 0`. A percentage that rounds to 100 is not the criterion — sub-project 42 raised a floor to 99 against a module one test short, correctly measured.

If anything remains, the round is not finished: add the test, and re-run from Step 1.

- [ ] **Step 3: Add the new floor entry**

`app/shared/user.py` has no entry in `coverage_floors.ini` — this round adds one. Insert it in the existing `[floors]` block next to its siblings:

```ini
app/shared/post.py = 100
app/shared/reply.py = 100
app/shared/user.py = 100
```

Use `floor(percent_covered)` from Step 2. Floors only ever rise; never lower one to make a run pass.

- [ ] **Step 4: Full suite again, with the floors check chained**

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh -o session_timeout=1800 \
    --cov=app.shared.user --cov-report=json:/tmp/user-final.json -q \
  && podman-compose -f compose.test.yaml exec -T test-runner \
       python tests/check_coverage_floors.py /tmp/user-final.json coverage_floors.ini
echo "CHAIN_EXIT=$?"
```

**Both arguments.** `tests/check_coverage_floors.py` fails closed with exit 2 if given one, and a fail-closed exit looks like a floor violation.

Note that this JSON carries coverage only for `app.shared.user`, so the floors check can only confirm that module. Confirming all 22 floors needs a run with the full `--cov` set the campaign normally uses; if the floors file names modules the JSON does not contain, report what the checker said rather than assuming it passed.

Record from pytest's own output: the passed count, the skipped count, the duration, and `PYTEST_EXIT`. **Take those numbers from pytest, never from this plan.**

- [ ] **Step 5: Commit**

```bash
cd /home/blentz/git/pyfedi
git add coverage_floors.ini
git commit -F <message-file>
```

Subject: `test: add a coverage floor for app/shared/user.py at 100`.
Body: the measured percentage, the empty missing-lines and missing-branches lists, the partial-branch count, and the suite's own passed/skipped/duration figures.

---

### Task 9: Mutation pass

**Files:** none modified permanently. Every mutation is reverted before the next.

**Interfaces:**
- Consumes: the closed module from Task 8.
- Produces: the survivor list Task 10 registers.

**Scope by the STATEMENT list, not the arc table.** Derive both the statement list and the compound-condition list mechanically with `ast.walk`, and **publish the derivation command and its raw output beside every count**. Sub-project 41 silently excluded eight lines and reported 12 where 20 existed; sub-project 42's pass reported three different numbers for one quantity and reconciled them only under review.

- [ ] **Step 1: Derive the mutation scope mechanically**

```bash
cd /home/blentz/git/pyfedi
podman-compose -f compose.test.yaml exec -T test-runner python -c "
import ast
src = open('/app/app/shared/user.py').read()
tree = ast.parse(src)
stmts = sorted({n.lineno for n in ast.walk(tree) if isinstance(n, ast.stmt)})
compounds = sorted({n.lineno for n in ast.walk(tree) if isinstance(n, ast.BoolOp)})
print('STATEMENT LINES', len(stmts))
print(stmts)
print('COMPOUND LINES', len(compounds))
print(compounds)
"
```

Paste the command and its complete output into your report. These two lists are the scope. A line not in them is not mutated; a line in them that you skip must be named and justified.

- [ ] **Step 2: Run the pass**

For each mutation: apply one edit, run `./run_tests.sh tests/test_shared_user_blocks.py tests/test_shared_user_bans.py tests/test_shared_user_follows.py -q`, record killed or survived, then **reverse the edit by hand** and re-read the restored line with `awk` to confirm it came back byte-identical. **`git checkout -- app/` is banned** — reverse edits only.

Rules that decide what counts:

- **A crash kill is not a kill** unless a viable non-crashing variant of the same fault also dies. A mutation that makes the module fail to import kills everything and proves nothing.
- **Fix-catching is not a unique kill.** If a mutant dies only in the test written to catch a different mutant, say so.
- **An operator can be structurally void.** If both operands of an `and` are always true together in every reachable state, note it as void rather than as a survivor.
- **Non-failures are evidence.** Record every survivor with its line, its mutation and why nothing killed it.
- **An equivalence claim needs a proof of unkillability.** If you want to call a survivor equivalent, show why no test *can* kill it. A test that merely does not is not that proof.

- [ ] **Step 3: The two mandatory mutations beyond the list**

These are not in the statement list and must be run anyway.

1. **Revert one fixed line to the bare form.** Change `:192` back to `if SRC_WEB:`. `test_ban_user_api_does_not_flash` must fail. This is Task 6's fix, restated as a mutant: if nothing dies, the inverted pin is decorative.

2. **Invert `to_ban.is_local()` at `:168`** to `not to_ban.is_local()`. It selects between `purge_user_then_delete` (`:170`) and the inline `delete_dependencies` / `purge_content` / redis-lock path (`:175-182`) — two materially different deletions. Both of Task 5's local and remote purge tests must fail. If only one does, the suite cannot tell the two deletions apart and a test is missing.

The spec originally called for a third mandatory mutation — neutralising `ban_user`'s permission guard. **That was retracted: `ban_user` has no permission guard.** See the spec's `## Verification` section.

- [ ] **Step 4: Confirm the tree is clean**

```bash
cd /home/blentz/git/pyfedi
git diff --quiet -- app/ && echo "TREE CLEAN under app/" || echo "TREE DIRTY under app/"
git status --porcelain
```

Expected: `TREE CLEAN under app/` and no modified files. **`git diff --quiet` is the check, not `wc -l`** — a same-line-count replacement is invisible to a line count. If the tree is dirty, a reversal was missed: find it and reverse it by hand.

- [ ] **Step 5: Report**

Write the pass into the task report as a table — line, mutation, killed or survived, which test killed it. Report the killed and survived counts **with the derivation beside them**. A count is a claim.

There is no commit for this task; nothing persistent changed.

---

### Task 10: Register the findings and update `tests/README.md`

**Files:**
- Modify: `docs/superpowers/findings-register.md`
- Modify: `tests/README.md`

**Interfaces:**
- Consumes: every earlier task's report.
- Produces: the round's written record.

Register from **D554**, and update the register's marker. `tests/README.md` facts from **242**.

- [ ] **Step 1: Register the findings**

Each entry names the file and line, quotes the code, says what is wrong, and says whether it was fixed or pinned. Verified by reading this session:

1. **`bot_challenge_user:322` appends `current_user` while `:320` uses `user`.** On an API call those differ and `current_user` is anonymous. `send_message` (`app/chat/util.py:12`) takes `user: User = current_user` as a default evaluated at definition time, and `:335` does not override it, so the API arm reaches `user.id` on an anonymous proxy. Pinned by Task 3, not fixed.
2. **`unfollow_user:286-287` decrements `num_following` and `num_followers` unconditionally** — including when no `UserFollower` row exists, and when `follow_user` never incremented them, because `:241-242`'s manually-approves-followers arm sets `is_accepted = None` and skips `:245-246`. Same counter-drift family as D523 and D522. Pinned by Task 7, not fixed.
3. **`block_another_user:33-34` uses `.scalar()`** on `SELECT role_id FROM "user_role" WHERE user_id = :person_id`, reading one role_id out of possibly many. A user holding an admin role plus another may slip the check depending on row order. Registered, not fixed.
4. **`subscribe_user:91` uses `.one()`**, which raises `NoResultFound` — a 500 — for a missing or banned person, where the siblings use `.get()`. Registered, not fixed.
5. **`ban_user` performs no authorization**, where `app/shared/post.py` and `app/shared/reply.py` check in the shared layer. Both current callers gate it (`app/api/alpha/utils/user.py:971`, `app/user/routes.py:767`), so nothing is reachable today; the divergence is what a third caller would walk into. **This entry also retracts the spec's pre-correction claim that a guard exists.**
6. **`follow_user:252-269`'s two branches build byte-identical `targets_data`** and differ only in the notification title. Registered, not fixed.
7. **`ban_user:161,171,183,192` tested the constant instead of `src`** — value 1, so all four were unconditionally true and seven `flash()` calls fired on API bans. **FIXED in Task 6**, pinned first in Tasks 4 and 5. Record that the count was first stated as five and corrected to four: the `else` at `:197` hangs off the role check at `:195`.
8. **The `reply_count_cross_posted` backfill repairs data, not code.** After Task 1's migration the five unguarded arithmetic sites remain unguarded, and a row explicitly set to NULL still raises `TypeError`. **This closes D543's data half and leaves its code half open.**
9. Every survivor from Task 9, each with its line, its mutation, and why nothing killed it.
10. Anything Task 3's probe or any task's unexpected test failure turned up.

- [ ] **Step 2: Add the facts to `tests/README.md`**

From fact **242**. Candidates, all verified this session:

- `ROLE_STAFF` is 3 and `ROLE_ADMIN` is 4, `block_another_user` compares raw `role_id` values against those integers, and `grant_permission` mints sequential role ids after the sequence reset — so counting `grant_permission` calls is load-bearing unless roles are created with explicit ids.
- `run_tests.sh:83` runs `flask db upgrade` before every pytest invocation, so a new migration needs no manual application.
- `app/shared/user.py:177` imports `redis_client` inside the function body, so monkeypatching the single attribute `app.redis_client` reaches it — unlike the four import-time bindings of `get_redis_connection` that `redis_double` has to patch separately.
- `ban_user`'s web arm takes a WTForms object and reads `input.person_id` as a plain attribute (set by `app/user/routes.py:782`), while `unban_user`'s web arm takes a dict and reads `input['person_id']`. The two functions' web inputs are different shapes.
- `send_message` (`app/chat/util.py:12`) has `user: User = current_user` as a default argument, evaluated at definition time — a LocalProxy, not a user.

Do not restate a fact the file already has. **Re-derive any count you write down**: fact 241's predecessor stated a count inherited verbatim from a ruling and never re-checked, and the sentence naming that defect class was itself an instance of it.

- [ ] **Step 3: Commit**

```bash
cd /home/blentz/git/pyfedi
git add docs/superpowers/findings-register.md tests/README.md
git commit -F <message-file>
```

Subject: `docs: register sub-project 43's findings and the user.py test facts`.
Body: how many entries were added, which were fixed versus pinned, and the corrected `if SRC_WEB:` site count with the reason the first count was wrong.

---

## Success criteria

- The backfill migration applied, with the `TypeError` demonstrated and the revision's own SQL shown to repair the row.
- `app/shared/user.py` at **`missing_lines == []`, `missing_branches == []`, `num_partial_branches == 0`** — checked as lists, not as a percentage.
- A **new** `coverage_floors.ini` entry for `app/shared/user.py` at `floor(percent_covered)`.
- `ban_user`'s four bare-constant source tests fixed, with four inverted pins proving the change.
- Full suite green, run by the controller, floors check chained with `&&` and **both** arguments.
- Findings registered from **D554**, marker updated. `tests/README.md` facts from **242**.
- Exactly two production changes: the migration and the four lines. Everything else registered.

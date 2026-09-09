# Sub-project 29: `maintenance.py` Group A Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Take the ten pure-database tasks in `app/shared/tasks/maintenance.py` to 100% statement and branch coverage, give the module its first `coverage_floors.ini` entry, and land three production changes — each observed before it is made.

**Architecture:** One new test file, `tests/test_shared_tasks_maintenance_cleanup.py`, covering Group A only. These tasks send nothing and return nothing; the oracle throughout is a row count or a column value read through a fresh query after the task's own `Session` has committed. Two of the three production changes are predictions that the round must reproduce first and drop if it cannot.

**Tech Stack:** Flask, SQLAlchemy 2.0.52, Celery (eager in tests), pytest, PostgreSQL in a podman container.

**Spec:** `docs/superpowers/specs/2026-09-08-coverage-maintenance-a-29-design.md`

## Global Constraints

- **Delete nothing the task did not create.** `git checkout -- app/` is permitted only as a mutation-restore step.
- **There is no host Python with flask or pytest.** Everything runs through `./run_tests.sh <pytest args>`, which is `podman compose exec -T test-runner pytest "$@"` (`run_tests.sh:85`).
- **Only the controller runs the full suite**, one pytest session at a time, in the foreground.
- **`pytest` exits 1 on a session timeout and `run_tests.sh` propagates it.** A shell pipeline eats the status — read `${PIPESTATUS[0]}`, or do not pipe.
- **Coverage takes the dotted module form**: `--cov=app.shared.tasks.maintenance`. A path form collects nothing, writes no JSON, and exits 0 — a silent green failure.
- **The coverage JSON is written inside the `pyfedi_test-runner` container.** Retrieve it with `podman cp` and check its mtime postdates the run before believing any number in it.
- **Run `./run_tests.sh --down` before any measured run**, and before believing any failure. `pytest.ini:28` caps a session at 600s and the suite hits it when the stack accumulates state.
- **Test counts come from pytest's own collection output**, never `grep -c '^def test_'`.
- **No ordered assertions over rows a query planner returned.** Compare sets, or sort explicitly in Python.
- **Every line number is re-derived against the current tree.** Citations into a file your own diff touches are re-derived AFTER the diff is final.
- **Where prose describes a statement, cite that statement's line, not the `if` guarding it.**
- **Commit with `git commit -F <file>`, never `-m`.** Lowercase `type:` subject prefix, normal English prose in the body.
- **Commit trailers, in this order:**
  ```
  Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01BF7kVN2spWD86NYKWyGrVn
  ```
- **Mutations run one at a time:** dry-run without `-i` and read the produced line first, apply, run, restore, then assert an empty `git diff -- app/` and the expected `wc -l`. Restore before any point where you might stop and report. Paste every dry-run line and every failure.
- **PC1 and PC2 are predictions, not observations.** Observe the defect and paste what happened before changing anything. A prediction that does not reproduce becomes a registered finding and the change is dropped — landing two production changes instead of three is the correct outcome, not a failure.

---

## File Structure

| File | Responsibility |
|------|----------------|
| `tests/test_shared_tasks_maintenance_cleanup.py` | **Create.** Every test in this round. Named for Group A so Groups B, C and D can take their own files without a rename. |
| `app/shared/tasks/maintenance.py` | **Modify.** Three production changes: `calculate_community_activity_stats`'s outer join, `update_community_stats`'s commit placement, `recalculate_user_attitudes`'s dead counter. |
| `coverage_floors.ini` | **Modify.** Gains `app/shared/tasks/maintenance.py` — 20 entries. |
| `tests/README.md` | **Modify.** New facts from 161; corrects facts 156 and 157's off-by-one citation. |
| `docs/superpowers/findings-register.md` | **Modify.** New findings from D342. |

## Harness facts that bind every task

Read these once. They are the difference between a test that measures something and a test that passes for the wrong reason.

**There is no fixture transaction.** `tests/conftest.py:137-202`'s `db_session` yields `db.session`, then DELETEs every row and commits (`:160-202`). A `session.commit()` inside a test is a real commit. Do not write a test that assumes a rollback will clean up.

**The task runs on its own connection.** `get_task_session()` returns `Session(bind=db.engine)` (`app/utils.py:3673-3675`). Two consequences, and both bite:
1. Rows a test seeds must be **committed** before the task runs, or the task's connection cannot see them. Every `tests/factories.py` factory commits, so factory-built rows are fine; a column a test assigns afterwards is not, until the test commits it.
2. After the task runs, an ORM attribute read on an object the test built earlier is **stale**. Read through a fresh query, or call `db.session.expire_all()` first (harness fact 153).

**`patch_db_session` is live under this harness and load-bearing in two tasks.** `cleanup_old_read_posts:49` and `recalculate_user_attitudes:719` wrap their bodies in it, because `get_setting` (`app/utils.py:203-211`) and `User.recalculate_attitude` (`app/models.py:1348`) read `db.session` rather than the task's session. The harness pushes only an app context (`tests/conftest.py:112`); pushing a request context would disable `patch_db_session` at `app/utils.py:3685` and both tasks would then write through the wrong session.

**The uniform error-path idiom.** Nine of the ten tasks call `utcnow()` as their first or near-first statement inside the `try`. `utcnow` is bound into this module's namespace by `app/shared/tasks/maintenance.py:16`, so monkeypatching `app.shared.tasks.maintenance.utcnow` raises inside the `try` without touching the factories, which reach `utcnow` through `app.models`. `update_hashtag_counts` is the exception — it calls no clock — so its error-path test monkeypatches `app.shared.tasks.maintenance.text` instead.

**Do not import the federation oracles.** There is no `post_request`, no `ActivityPubLog` row written by any Group A task, and no respx route to count. Fact 148's warning about swallowed unmatched requests does not apply here. A test in this file that reaches for `_delivered_inboxes` or `len(route.calls)` has misread the module.

---

## Task 1: Probe the harness, then open the test file

**Files:**
- Create: `tests/test_shared_tasks_maintenance_cleanup.py`
- Read: `app/shared/tasks/maintenance.py:61-74`, `:750-868`

**Interfaces:**
- Consumes: nothing.
- Produces: the module docstring, the import block, the `_seed()` helper, and one passing test. Every later task adds to this file and calls `_seed()`.

The spec's open question is whether `calculate_community_activity_stats` can run under this harness at all: it issues `CREATE TEMPORARY TABLE temp_community_activity (...) ON COMMIT DROP` at `app/shared/tasks/maintenance.py:765-771` and commits at `:861`. Answer it before writing any behaviour test, because the answer decides whether Task 8 needs a different fixture.

- [ ] **Step 1: Probe the temp table, and paste what happens**

Write this file to the session scratchpad (NOT the repo — a stray probe file in the repo root is a defect this campaign has already made once):

```python
# probe_temp_table.py
from app import create_app, db
from config import Config


class TestConfig(Config):
    SERVER_NAME = 'test.piefed.local'
    TESTING = True


application = create_app(TestConfig)
with application.app_context():
    from app.shared.tasks.maintenance import calculate_community_activity_stats
    calculate_community_activity_stats()
    print('RAN WITHOUT RAISING')
    print('temp table visible afterwards:',
          db.session.execute(db.text(
              "SELECT to_regclass('temp_community_activity')")).scalar())
```

Run it inside the container against the test database:

```bash
podman cp /tmp/.../probe_temp_table.py pyfedi_test-runner:/tmp/probe_temp_table.py
podman exec -e DATABASE_URL="$TEST_DATABASE_URL" pyfedi_test-runner python /tmp/probe_temp_table.py
```

Paste the complete output into your report, whatever it is. If it raises, paste the traceback. Do not adapt the plan around a failure you did not show.

- [ ] **Step 2: Record the answer in the report**

State one of:
- **It runs.** Task 8 uses the ordinary `db_session` fixture like every other task.
- **It raises `<exact error>`.** Task 8 needs a different arrangement, and your report names what the error was so the controller can rule on it. Stop after this step and report; do not invent a fixture.

- [ ] **Step 3: Write the test file's prelude**

Create `tests/test_shared_tasks_maintenance_cleanup.py`:

```python
"""Group A of `app/shared/tasks/maintenance.py` -- the ten pure-database tasks.

`maintenance.py` is 1181 lines and 636 statements, three times the largest
module this campaign has closed in one round, so it is split by TESTING
SURFACE rather than by subject. Group A is the ten tasks that need no
transport at all:

  `cleanup_old_notifications:25`   `cleanup_old_read_posts:45`
  `cleanup_send_queue:61`          `update_hashtag_counts:194`
  `update_community_stats:283`     `cleanup_old_voting_data:327`
  `unban_expired_users:392`        `recalculate_user_attitudes:712`
  `calculate_community_activity_stats:750`
  `cleanup_old_activitypub_logs:872`

NONE OF THE FEDERATION ORACLES APPLY HERE. These tasks are invoked from cron
entry points in `app/cli.py:808-947`, never from a route and never through
`task_selector`. There is no `flash()`, no `SRC_`-forked return value, no
delivered-inbox set, no `post_request`, and no `ActivityPubLog` row written by
any of them. Fact 148's warning about respx swallowing unmatched requests is
irrelevant in this file. The oracle is a row count or a column value.

THE TASK RUNS ON ITS OWN CONNECTION. `get_task_session()` returns
`Session(bind=db.engine)` (app/utils.py:3673-3675), so rows a test seeds must
be COMMITTED before the task runs or the task cannot see them, and an ORM
attribute read after the task runs is stale unless the test calls
`db.session.expire_all()` first (fact 153). Reading through a fresh query is
immune and is what most tests here do.

THE ERROR PATHS ARE THE SAME TEST TEN TIMES. Every task in this group wraps
its body in `try / except Exception: session.rollback(); raise / finally:
session.close()`, which is about thirty of the group's 151 statements. Nine of
the ten call `utcnow()` inside the `try`, and `utcnow` is bound into this
module's namespace at `app/shared/tasks/maintenance.py:16`, so monkeypatching
`app.shared.tasks.maintenance.utcnow` raises inside the `try` without touching
`tests/factories.py`, which reaches `utcnow` through `app.models`.
`update_hashtag_counts` calls no clock and is the one exception -- its
error-path test patches `app.shared.tasks.maintenance.text`.
"""

from datetime import timedelta

import pytest

from app import db
from app.models import (
    ActivityPubLog, Community, Notification, RevokedToken, SendQueue, Tag,
    User, utcnow,
)
from app.shared.tasks.maintenance import (
    calculate_community_activity_stats, cleanup_old_activitypub_logs,
    cleanup_old_notifications, cleanup_old_read_posts, cleanup_old_voting_data,
    cleanup_send_queue, recalculate_user_attitudes, unban_expired_users,
    update_community_stats, update_hashtag_counts,
)
from tests.factories import (
    make_activitypub_log, make_community, make_community_member, make_instance,
    make_notification, make_post, make_post_reply, make_post_reply_vote,
    make_post_vote, make_user,
)


def _boom(*args, **kwargs):
    """Raise from inside a task's `try`, to reach its `except` arm."""
    raise RuntimeError('the task itself failed')


def _seed():
    """instance, a local user, a community, and one post -- all committed.

    Every factory here commits, so the task's own connection can see these
    rows. `make_community` leaves `ap_id` None, which makes `is_local()` true
    (app/models.py:796) -- a test that needs the false arm sets `ap_id` and
    `ap_profile_id` to a remote host and commits.
    """
    instance = make_instance('peer.example')
    user = make_user(instance, 'reader', local=True)
    community = make_community()
    post = make_post(community, user, 'https://peer.example/p/1')
    return instance, user, community, post
```

- [ ] **Step 4: Write the first two tests**

`cleanup_send_queue:61-74` is the simplest task in the group: one delete by cutoff, one commit, the standard error skeleton. Append:

```python
class TestCleanupSendQueue:
    """`cleanup_send_queue:61` -- SendQueue rows older than seven days."""

    def test_a_send_queue_row_older_than_seven_days_is_removed(self, db_session):
        old = SendQueue(destination_domain='peer.example',
                        created=utcnow() - timedelta(days=8))
        db.session.add(old)
        db.session.commit()

        cleanup_send_queue()

        assert db.session.query(SendQueue).count() == 0

    def test_a_send_queue_row_inside_seven_days_survives(self, db_session):
        """The BOUNDARY, which is the half a deletion test cannot prove.

        A test that only checks the old row is gone passes just as well against
        a task with no cutoff at all. This is the test that fails if the
        comparison at `:65` is widened.
        """
        fresh = SendQueue(destination_domain='peer.example',
                          created=utcnow() - timedelta(days=6))
        db.session.add(fresh)
        db.session.commit()

        cleanup_send_queue()

        assert db.session.query(SendQueue).count() == 1

    def test_a_failure_inside_the_task_propagates(self, db_session, monkeypatch):
        monkeypatch.setattr('app.shared.tasks.maintenance.utcnow', _boom)

        with pytest.raises(RuntimeError, match='the task itself failed'):
            cleanup_send_queue()
```

- [ ] **Step 5: Run the tests**

```bash
./run_tests.sh tests/test_shared_tasks_maintenance_cleanup.py -v
```
Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add tests/test_shared_tasks_maintenance_cleanup.py
git commit -F <message-file>
```
Subject: `test: open the maintenance Group A file with cleanup_send_queue`

---

## Task 2: `cleanup_old_notifications` and `cleanup_old_activitypub_logs`

**Files:**
- Modify: `tests/test_shared_tasks_maintenance_cleanup.py`
- Read: `app/shared/tasks/maintenance.py:25-42`, `:872-885`

**Interfaces:**
- Consumes: `_seed()`, `_boom` from Task 1.
- Produces: nothing later tasks depend on.

`cleanup_old_notifications` deletes **two** tables on two different cutoffs: `Notification` older than 90 days at `:29-30`, and `RevokedToken` older than 365 days at `:33-34`. Its docstring at `:26` mentions only the first. A test that covers only notifications leaves the second delete unpinned.

- [ ] **Step 1: Write the tests**

```python
class TestCleanupOldNotifications:
    """`cleanup_old_notifications:25` -- TWO tables, TWO cutoffs.

    `:29-30` deletes Notification rows older than 90 days; `:33-34` deletes
    RevokedToken rows older than 365 days. The docstring at `:26` names only
    the first, which is registered as a finding rather than fixed.
    """

    def test_a_notification_older_than_ninety_days_is_removed(self, db_session):
        _, user, _, post = _seed()
        old = make_notification(user, post)
        old.created_at = utcnow() - timedelta(days=91)
        db.session.commit()

        cleanup_old_notifications()

        assert db.session.query(Notification).count() == 0

    def test_a_notification_inside_ninety_days_survives(self, db_session):
        _, user, _, post = _seed()
        fresh = make_notification(user, post)
        fresh.created_at = utcnow() - timedelta(days=89)
        db.session.commit()

        cleanup_old_notifications()

        assert db.session.query(Notification).count() == 1

    def test_the_two_cutoffs_are_different(self, db_session):
        """A RevokedToken 100 days old outlives a Notification 100 days old.

        This is the assertion that fails if `:33`'s 365 is changed to 90, or
        if the RevokedToken delete is dropped onto the notification cutoff.
        Neither of the two single-table tests above can see that.
        """
        _, user, _, post = _seed()
        notification = make_notification(user, post)
        notification.created_at = utcnow() - timedelta(days=100)
        token = RevokedToken(jti='a-hundred-days-old',
                             revoked_at=utcnow() - timedelta(days=100))
        db.session.add(token)
        db.session.commit()

        cleanup_old_notifications()

        assert db.session.query(Notification).count() == 0
        assert db.session.query(RevokedToken).count() == 1

    def test_a_revoked_token_older_than_a_year_is_removed(self, db_session):
        token = RevokedToken(jti='old', revoked_at=utcnow() - timedelta(days=366))
        db.session.add(token)
        db.session.commit()

        cleanup_old_notifications()

        assert db.session.query(RevokedToken).count() == 0

    def test_a_failure_inside_the_task_propagates(self, db_session, monkeypatch):
        monkeypatch.setattr('app.shared.tasks.maintenance.utcnow', _boom)

        with pytest.raises(RuntimeError, match='the task itself failed'):
            cleanup_old_notifications()


class TestCleanupOldActivityPubLogs:
    """`cleanup_old_activitypub_logs:872` -- logs older than three days."""

    def test_a_log_older_than_three_days_is_removed(self, db_session):
        log = make_activitypub_log('https://peer.example/activities/create/1')
        log.created_at = utcnow() - timedelta(days=4)
        db.session.commit()

        cleanup_old_activitypub_logs()

        assert db.session.query(ActivityPubLog).count() == 0

    def test_a_log_inside_three_days_survives(self, db_session):
        log = make_activitypub_log('https://peer.example/activities/create/2')
        log.created_at = utcnow() - timedelta(days=2)
        db.session.commit()

        cleanup_old_activitypub_logs()

        assert db.session.query(ActivityPubLog).count() == 1

    def test_a_failure_inside_the_task_propagates(self, db_session, monkeypatch):
        monkeypatch.setattr('app.shared.tasks.maintenance.utcnow', _boom)

        with pytest.raises(RuntimeError, match='the task itself failed'):
            cleanup_old_activitypub_logs()
```

- [ ] **Step 2: Run them**

```bash
./run_tests.sh tests/test_shared_tasks_maintenance_cleanup.py -v
```
Expected: 11 passed.

- [ ] **Step 3: Commit**

Subject: `test: cover the two notification cutoffs and the log cleanup`

---

## Task 3: `cleanup_old_read_posts`, `unban_expired_users`, `update_hashtag_counts`

**Files:**
- Modify: `tests/test_shared_tasks_maintenance_cleanup.py`
- Read: `app/shared/tasks/maintenance.py:45-58`, `:194-211`, `:392-405`

**Interfaces:**
- Consumes: `_seed()`, `_boom`.
- Produces: nothing later tasks depend on.

All three are raw SQL against tables with no ORM model of their own or with an unusual shape. `read_posts` is an association table (`app/models.py:942-948`), `post_tag` likewise (`:340-343`), and `unban_expired_users` writes `"user"` directly.

`cleanup_old_read_posts:50` reads `get_setting('read_posts_cutoff', 180)` through `db.session`, which is why `:49` wraps the body in `patch_db_session`. Its docstring at `:46` says "180 days" as though that were fixed; 180 is the default and the setting overrides it. Registered, not fixed.

- [ ] **Step 1: Write the tests**

```python
class TestCleanupOldReadPosts:
    """`cleanup_old_read_posts:45` -- a raw DELETE against an association table.

    `:50` reads the cutoff from `get_setting('read_posts_cutoff', 180)`, which
    goes through `db.session`; `:49`'s `patch_db_session(session)` is what makes
    that read land on the task's own session. The harness leaves
    `patch_db_session` live because `tests/conftest.py:112` pushes only an app
    context (facts 156 and 157).
    """

    def test_a_read_post_older_than_the_cutoff_is_removed(self, db_session):
        _, user, _, post = _seed()
        db.session.execute(db.text(
            'INSERT INTO read_posts (user_id, read_post_id, interacted_at) '
            'VALUES (:u, :p, :t)'),
            {'u': user.id, 'p': post.id, 't': utcnow() - timedelta(days=181)})
        db.session.commit()

        cleanup_old_read_posts()

        assert db.session.execute(
            db.text('SELECT COUNT(*) FROM read_posts')).scalar() == 0

    def test_a_read_post_inside_the_cutoff_survives(self, db_session):
        _, user, _, post = _seed()
        db.session.execute(db.text(
            'INSERT INTO read_posts (user_id, read_post_id, interacted_at) '
            'VALUES (:u, :p, :t)'),
            {'u': user.id, 'p': post.id, 't': utcnow() - timedelta(days=179)})
        db.session.commit()

        cleanup_old_read_posts()

        assert db.session.execute(
            db.text('SELECT COUNT(*) FROM read_posts')).scalar() == 1

    def test_the_cutoff_comes_from_the_setting_not_the_default(self, db_session):
        """A row 100 days old survives at the default and dies at a 90-day setting.

        This is the test that proves `:50`'s `get_setting` call is load-bearing
        rather than decorative -- and therefore that `:49`'s `patch_db_session`
        is doing something, since `get_setting` reads `db.session`.
        """
        from app.utils import set_setting
        _, user, _, post = _seed()
        db.session.execute(db.text(
            'INSERT INTO read_posts (user_id, read_post_id, interacted_at) '
            'VALUES (:u, :p, :t)'),
            {'u': user.id, 'p': post.id, 't': utcnow() - timedelta(days=100)})
        set_setting('read_posts_cutoff', 90)
        db.session.commit()

        cleanup_old_read_posts()

        assert db.session.execute(
            db.text('SELECT COUNT(*) FROM read_posts')).scalar() == 0

    def test_a_failure_inside_the_task_propagates(self, db_session, monkeypatch):
        monkeypatch.setattr('app.shared.tasks.maintenance.utcnow', _boom)

        with pytest.raises(RuntimeError, match='the task itself failed'):
            cleanup_old_read_posts()


class TestUnbanExpiredUsers:
    """`unban_expired_users:392` -- one UPDATE with three conditions."""

    def test_a_user_whose_ban_has_expired_is_unbanned(self, db_session):
        instance, user, _, _ = _seed()
        user.banned = True
        user.banned_until = utcnow() - timedelta(days=1)
        db.session.commit()

        unban_expired_users()

        db.session.expire_all()
        assert db.session.get(User, user.id).banned is False

    def test_a_user_whose_ban_has_not_expired_stays_banned(self, db_session):
        instance, user, _, _ = _seed()
        user.banned = True
        user.banned_until = utcnow() + timedelta(days=1)
        db.session.commit()

        unban_expired_users()

        db.session.expire_all()
        assert db.session.get(User, user.id).banned is True

    def test_a_permanent_ban_has_no_banned_until_and_survives(self, db_session):
        """`:397`'s third condition, `banned_until is not null`.

        Without it the NULL comparison would already exclude the row, so this
        test cannot fail by deleting that conjunct alone -- it pins the
        BEHAVIOUR (a permanent ban is not lifted) rather than the conjunct, and
        the mutation transcript records that distinction.
        """
        instance, user, _, _ = _seed()
        user.banned = True
        user.banned_until = None
        db.session.commit()

        unban_expired_users()

        db.session.expire_all()
        assert db.session.get(User, user.id).banned is True

    def test_a_failure_inside_the_task_propagates(self, db_session, monkeypatch):
        monkeypatch.setattr('app.shared.tasks.maintenance.utcnow', _boom)

        with pytest.raises(RuntimeError, match='the task itself failed'):
            unban_expired_users()


class TestUpdateHashtagCounts:
    """`update_hashtag_counts:194` -- one correlated UPDATE, no clock at all.

    This is the one task in Group A that never calls `utcnow()`, so its
    error-path test patches `app.shared.tasks.maintenance.text` instead.
    """

    def test_a_tag_count_is_recomputed_from_post_tag(self, db_session):
        _, user, community, post = _seed()
        second = make_post(community, user, 'https://peer.example/p/2')
        tag = Tag(name='solarstorm', display_as='SolarStorm', post_count=0)
        db.session.add(tag)
        db.session.commit()
        db.session.execute(db.text(
            'INSERT INTO post_tag (post_id, tag_id) VALUES (:p, :t)'),
            {'p': post.id, 't': tag.id})
        db.session.execute(db.text(
            'INSERT INTO post_tag (post_id, tag_id) VALUES (:p, :t)'),
            {'p': second.id, 't': tag.id})
        db.session.commit()

        update_hashtag_counts()

        db.session.expire_all()
        assert db.session.get(Tag, tag.id).post_count == 2

    def test_a_tag_with_no_posts_is_set_to_zero(self, db_session):
        """The UPDATE has no WHERE, so a stale count on an unused tag is reset.

        This is the arm that distinguishes "recompute every tag" from
        "recompute the tags that have posts", and nothing else in the file
        covers it.
        """
        tag = Tag(name='stale', display_as='Stale', post_count=7)
        db.session.add(tag)
        db.session.commit()

        update_hashtag_counts()

        db.session.expire_all()
        assert db.session.get(Tag, tag.id).post_count == 0

    def test_a_failure_inside_the_task_propagates(self, db_session, monkeypatch):
        monkeypatch.setattr('app.shared.tasks.maintenance.text', _boom)

        with pytest.raises(RuntimeError, match='the task itself failed'):
            update_hashtag_counts()
```

- [ ] **Step 2: Run them**

```bash
./run_tests.sh tests/test_shared_tasks_maintenance_cleanup.py -v
```
Expected: 22 passed.

If `test_the_cutoff_comes_from_the_setting_not_the_default` fails, do not delete it and do not weaken it. Paste the failure and report — a failure there means `patch_db_session` is not behaving as facts 156 and 157 describe, which is a finding in its own right.

- [ ] **Step 3: Commit**

Subject: `test: cover the read-post, unban and hashtag maintenance tasks`

---

## Task 4: `cleanup_old_voting_data`

**Files:**
- Modify: `tests/test_shared_tasks_maintenance_cleanup.py`
- Read: `app/shared/tasks/maintenance.py:327-389`

**Interfaces:**
- Consumes: `_seed()`, `_boom`.
- Produces: nothing later tasks depend on.

Two branch points, four arms: `:334`'s `if local_months != -1` and `:359`'s `if remote_months != -1`. Both read `current_app.config`, whose defaults are 6 and 6 (`config.py:177-178`). The `-1` arms are taken by setting the config values, not by patching.

The task deletes from four tables. Local and remote are distinguished by `instance_id`: `= 1` for local, `!= 1` for remote. `make_user(instance, name, local=True)` still sets `instance_id` from the instance it is passed, so a genuinely local user must be built against an instance whose id is 1 — the `db_session` fixture resets sequences, so the first `make_instance` in a test gets id 1 and later ones do not. Build the local user against the FIRST instance created in the test.

- [ ] **Step 1: Write the tests**

```python
class TestCleanupOldVotingData:
    """`cleanup_old_voting_data:327` -- four DELETEs behind two config guards.

    `:334`'s `if local_months != -1` and `:359`'s `if remote_months != -1` read
    `KEEP_LOCAL_VOTE_DATA_TIME` and `KEEP_REMOTE_VOTE_DATA_TIME`
    (config.py:177-178, both defaulting to 6). The cutoff is 28 days per month,
    so at the default a vote older than 168 days goes and one younger stays.

    LOCAL AND REMOTE ARE `instance_id = 1` AND `instance_id != 1`. The
    `db_session` fixture resets sequences, so the first `make_instance` in a
    test receives id 1; a test that wants both kinds of voter builds two
    instances and takes the first as local.
    """

    def _two_voters_with_votes(self, age_days):
        """A local voter and a remote voter, each with a post vote and a reply
        vote of the given age. Returns (local_user, remote_user)."""
        local_instance = make_instance('local.example')
        remote_instance = make_instance('remote.example')
        assert local_instance.id == 1
        local_user = make_user(local_instance, 'homebody', local=True)
        remote_user = make_user(remote_instance, 'visitor')
        community = make_community()
        post = make_post(community, local_user, 'https://local.example/p/1')
        reply = make_post_reply(post, local_user)
        stamp = utcnow() - timedelta(days=age_days)
        for voter in (local_user, remote_user):
            post_vote = make_post_vote(voter, post, 1.0)
            reply_vote = make_post_reply_vote(voter, reply, 1.0)
            post_vote.created_at = stamp
            reply_vote.created_at = stamp
        db.session.commit()
        return local_user, remote_user

    def _vote_counts(self):
        return (
            db.session.execute(db.text('SELECT COUNT(*) FROM post_vote')).scalar(),
            db.session.execute(db.text('SELECT COUNT(*) FROM post_reply_vote')).scalar(),
        )

    def test_old_votes_from_both_kinds_of_voter_are_removed(self, db_session):
        self._two_voters_with_votes(age_days=28 * 6 + 1)

        cleanup_old_voting_data()

        assert self._vote_counts() == (0, 0)

    def test_recent_votes_survive(self, db_session):
        self._two_voters_with_votes(age_days=28 * 6 - 1)

        cleanup_old_voting_data()

        assert self._vote_counts() == (2, 2)

    def test_minus_one_for_local_keeps_local_votes_and_drops_remote(self, db_session, app):
        """`:334`'s false arm. -1 means "keep local vote data forever".

        The remote deletes still run, so this test also proves the two guards
        are independent rather than one guard read twice.
        """
        self._two_voters_with_votes(age_days=28 * 6 + 1)
        original = app.config['KEEP_LOCAL_VOTE_DATA_TIME']
        app.config['KEEP_LOCAL_VOTE_DATA_TIME'] = -1

        try:
            cleanup_old_voting_data()
        finally:
            app.config['KEEP_LOCAL_VOTE_DATA_TIME'] = original

        assert self._vote_counts() == (1, 1)

    def test_minus_one_for_remote_keeps_remote_votes_and_drops_local(self, db_session, app):
        """`:359`'s false arm, the mirror of the test above."""
        self._two_voters_with_votes(age_days=28 * 6 + 1)
        original = app.config['KEEP_REMOTE_VOTE_DATA_TIME']
        app.config['KEEP_REMOTE_VOTE_DATA_TIME'] = -1

        try:
            cleanup_old_voting_data()
        finally:
            app.config['KEEP_REMOTE_VOTE_DATA_TIME'] = original

        assert self._vote_counts() == (1, 1)

    def test_minus_one_for_both_deletes_nothing(self, db_session, app):
        """Both false arms at once -- the task becomes a no-op."""
        self._two_voters_with_votes(age_days=28 * 6 + 1)
        original_local = app.config['KEEP_LOCAL_VOTE_DATA_TIME']
        original_remote = app.config['KEEP_REMOTE_VOTE_DATA_TIME']
        app.config['KEEP_LOCAL_VOTE_DATA_TIME'] = -1
        app.config['KEEP_REMOTE_VOTE_DATA_TIME'] = -1

        try:
            cleanup_old_voting_data()
        finally:
            app.config['KEEP_LOCAL_VOTE_DATA_TIME'] = original_local
            app.config['KEEP_REMOTE_VOTE_DATA_TIME'] = original_remote

        assert self._vote_counts() == (2, 2)

    def test_a_failure_inside_the_task_propagates(self, db_session, monkeypatch):
        monkeypatch.setattr('app.shared.tasks.maintenance.utcnow', _boom)

        with pytest.raises(RuntimeError, match='the task itself failed'):
            cleanup_old_voting_data()
```

Note the two config tests assert `(1, 1)` rather than naming which vote survived. Asserting the survivor's `user_id` would be better, and Step 2 upgrades them once you have confirmed the counts behave.

- [ ] **Step 2: Strengthen the two asymmetric tests**

A count of 1 does not say *which* vote survived, so it passes if the task deletes the wrong one. Change both assertions to name the survivor:

```python
        surviving = db.session.execute(db.text(
            'SELECT user_id FROM post_vote')).scalars().all()
        assert set(surviving) == {local_user.id}
```

with `local_user, remote_user = self._two_voters_with_votes(...)` capturing the return, `{local_user.id}` in the KEEP_LOCAL test and `{remote_user.id}` in the KEEP_REMOTE test. Use a set, not a list — the ordering of rows a planner returns is not part of the contract.

- [ ] **Step 3: Run them**

```bash
./run_tests.sh tests/test_shared_tasks_maintenance_cleanup.py -v
```
Expected: 28 passed.

- [ ] **Step 4: Commit**

Subject: `test: cover cleanup_old_voting_data's four arms and both cutoffs`

---

## Task 5: `update_community_stats` coverage

**Files:**
- Modify: `tests/test_shared_tasks_maintenance_cleanup.py`
- Read: `app/shared/tasks/maintenance.py:283-324`

**Interfaces:**
- Consumes: `_seed()`, `_boom`.
- Produces: the tests Task 6 will re-run after changing `:317`.

**Do not change `app/shared/tasks/maintenance.py` in this task.** Task 6 owns the production change; this task establishes the behaviour that must survive it.

Two branch points: `:292`'s `for` loop and the compound at `:305-306`. The compound is `community.is_local() and (community.total_subscriptions_count is None or community.total_subscriptions_count < community.subscriptions_count)` — three conditions coverage.py sees as one arc pair (fact 142). Each needs its own test even though branch coverage will read 100% with two of them untested.

Eligibility comes from `:288-290`: `banned == False` and `last_active > utcnow() - timedelta(days=3)`.

- [ ] **Step 1: Write the tests**

```python
class TestUpdateCommunityStats:
    """`update_community_stats:283` -- recount subscribers, posts and replies.

    `:288-290` selects communities that are not banned and were active in the
    last three days. `:303` writes `subscriptions_count` from a join that
    excludes banned members and bots; `:309` and `:313` write `post_count` and
    `post_reply_count` from raw counts that exclude deleted rows.

    `:305-306` IS THREE CONDITIONS IN ONE ARC PAIR. Branch coverage reads 100%
    with two of them untested (fact 142), so each gets its own test:
    `is_local()`, `total_subscriptions_count is None`, and
    `total_subscriptions_count < subscriptions_count`.
    """

    def test_subscriptions_count_excludes_bots_and_banned_members(self, db_session):
        instance, user, community, _ = _seed()
        bot = make_user(instance, 'botty', local=True)
        bot.bot = True
        banned = make_user(instance, 'outcast', local=True)
        db.session.commit()
        make_community_member(user, community)
        make_community_member(bot, community)
        banned_membership = make_community_member(banned, community)
        banned_membership.is_banned = True
        db.session.commit()

        update_community_stats()

        db.session.expire_all()
        assert db.session.get(Community, community.id).subscriptions_count == 1

    def test_post_and_reply_counts_exclude_deleted_rows(self, db_session):
        instance, user, community, post = _seed()
        deleted_post = make_post(community, user, 'https://peer.example/p/2')
        deleted_post.deleted = True
        reply = make_post_reply(post, user)
        deleted_reply = make_post_reply(post, user)
        deleted_reply.deleted = True
        db.session.commit()

        update_community_stats()

        db.session.expire_all()
        refreshed = db.session.get(Community, community.id)
        assert (refreshed.post_count, refreshed.post_reply_count) == (1, 1)

    def test_a_banned_community_is_skipped(self, db_session):
        _, user, community, _ = _seed()
        community.banned = True
        community.post_count = 99
        db.session.commit()

        update_community_stats()

        db.session.expire_all()
        assert db.session.get(Community, community.id).post_count == 99

    def test_a_community_idle_for_more_than_three_days_is_skipped(self, db_session):
        _, user, community, _ = _seed()
        community.last_active = utcnow() - timedelta(days=4)
        community.post_count = 99
        db.session.commit()

        update_community_stats()

        db.session.expire_all()
        assert db.session.get(Community, community.id).post_count == 99

    def test_a_local_community_with_no_total_gets_one(self, db_session):
        """`:305-306`'s second condition: total_subscriptions_count is None."""
        instance, user, community, _ = _seed()
        make_community_member(user, community)
        community.total_subscriptions_count = None
        db.session.commit()

        update_community_stats()

        db.session.expire_all()
        assert db.session.get(Community, community.id).total_subscriptions_count == 1

    def test_a_local_community_whose_total_lags_is_raised(self, db_session):
        """`:305-306`'s third condition: total < subscriptions."""
        instance, user, community, _ = _seed()
        make_community_member(user, community)
        community.total_subscriptions_count = 0
        db.session.commit()

        update_community_stats()

        db.session.expire_all()
        assert db.session.get(Community, community.id).total_subscriptions_count == 1

    def test_a_local_community_whose_total_already_leads_is_left_alone(self, db_session):
        """The false arm of `:305-306`'s third condition.

        A local community's total counts remote subscribers too, so a total
        ABOVE the local count is the normal state and must not be pulled down.
        """
        instance, user, community, _ = _seed()
        make_community_member(user, community)
        community.total_subscriptions_count = 50
        db.session.commit()

        update_community_stats()

        db.session.expire_all()
        assert db.session.get(Community, community.id).total_subscriptions_count == 50

    def test_a_remote_community_keeps_its_total_untouched(self, db_session):
        """`:305`'s first condition, `is_local()`, taking its false arm.

        `make_community` leaves `ap_id` None, which makes `is_local()` true at
        app/models.py:796's first disjunct. Setting both `ap_id` and
        `ap_profile_id` to a remote host makes it false, and `profile_id()`
        (app/models.py:787) then returns a URI that does not start with
        SERVER_URL.
        """
        instance, user, community, _ = _seed()
        make_community_member(user, community)
        community.ap_id = 'microblogs@peer.example'
        community.ap_profile_id = 'https://peer.example/c/microblogs'
        community.total_subscriptions_count = 0
        db.session.commit()

        update_community_stats()

        db.session.expire_all()
        refreshed = db.session.get(Community, community.id)
        assert refreshed.subscriptions_count == 1
        assert refreshed.total_subscriptions_count == 0

    def test_a_failure_inside_the_task_propagates(self, db_session, monkeypatch):
        monkeypatch.setattr('app.shared.tasks.maintenance.utcnow', _boom)

        with pytest.raises(RuntimeError, match='the task itself failed'):
            update_community_stats()
```

- [ ] **Step 2: Run them**

```bash
./run_tests.sh tests/test_shared_tasks_maintenance_cleanup.py -v
```
Expected: 37 passed.

- [ ] **Step 3: Commit**

Subject: `test: cover update_community_stats and its three-condition local guard`

---

## Task 6: PC2 — observe `update_community_stats`'s in-loop commit, then fix it

**Files:**
- Modify: `app/shared/tasks/maintenance.py` (one line moved)
- Modify: `tests/test_shared_tasks_maintenance_cleanup.py`

**Interfaces:**
- Consumes: Task 5's tests, all of which must still pass afterwards.
- Produces: `:317`'s commit moved out of the loop.

`:317`'s `session.commit()` sits inside the `for community in communities:` loop opened at `:292`. The spec predicts two consequences. **Observe both before changing anything.** Sub-project 28's central prediction was disproved by a probe; treat this one the same way.

- [ ] **Step 1: Write the atomicity test and watch it FAIL against the current code**

```python
class TestUpdateCommunityStatsIsAtomic:
    """PC2: `:317`'s commit inside the loop at `:292`.

    With the commit inside the loop, a failure at community N leaves
    communities 1..N-1 committed, and `:319-321`'s handler rolls back only the
    current unit of work. The rollback READS as though it protects the task's
    whole effect. It does not.
    """

    def test_a_failure_partway_through_leaves_no_partial_writes(self, db_session, monkeypatch):
        import app.shared.tasks.maintenance as maintenance

        _, user, first, _ = _seed()
        second = make_community(name='second')
        make_community_member(user, first)
        make_community_member(user, second)
        first.subscriptions_count = 0
        second.subscriptions_count = 0
        db.session.commit()

        # Each community costs two text() calls, at `:309` and `:313`. Letting
        # two through and failing on the third puts the failure inside the
        # SECOND community, after the first has been fully processed.
        real_text = maintenance.text
        calls = {'n': 0}

        def _text_that_fails_on_the_second_community(sql):
            calls['n'] += 1
            if calls['n'] > 2:
                raise RuntimeError('the task itself failed')
            return real_text(sql)

        monkeypatch.setattr(maintenance, 'text', _text_that_fails_on_the_second_community)

        with pytest.raises(RuntimeError, match='the task itself failed'):
            update_community_stats()

        db.session.expire_all()
        counts = {c.name: c.subscriptions_count
                  for c in db.session.query(Community).all()}
        assert counts == {'microblogs': 0, 'second': 0}
```

Run it:

```bash
./run_tests.sh tests/test_shared_tasks_maintenance_cleanup.py::TestUpdateCommunityStatsIsAtomic -v
```

Expected: FAIL, with `microblogs` reading 1 rather than 0 — the first community's recount was committed before the second failed. **Paste the exact assertion output.** If it passes against the unmodified code, the prediction did not reproduce: stop, report that, and do not make the change.

- [ ] **Step 2: Observe the N+1 and paste the numbers**

Write this test and run it against the unmodified code, recording the count it prints:

```python
    def test_the_number_of_community_selects_does_not_scale_with_the_loop(self, db_session):
        """PC2's second consequence: `expire_on_commit` forces a re-SELECT.

        `expire_on_commit` defaults to True (fact 58), so each commit inside the
        loop expires every loaded Community and the next iteration's first
        attribute read reloads its row. Counting statements on `db.engine`
        catches the task's own connection, because `get_task_session()` binds to
        that same engine (app/utils.py:3673-3675).
        """
        import re

        from sqlalchemy import event

        _, user, first, _ = _seed()
        for name in ('second', 'third', 'fourth'):
            make_community(name=name)
        db.session.commit()

        statements = []

        def _record(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(db.engine, 'before_cursor_execute', _record)
        try:
            update_community_stats()
        finally:
            event.remove(db.engine, 'before_cursor_execute', _record)

        # A WORD BOUNDARY, not a substring. `:294-302`'s
        # `select(func.count()).select_from(CommunityMember)` emits
        # `FROM community_member`, which `' FROM community' in s.lower()` would
        # match -- an oracle that counts a statement it did not name.
        selects = [s for s in statements
                   if s.lstrip().upper().startswith('SELECT')
                   and re.search(r'\bfrom\s+community\b', s, re.IGNORECASE)]
        assert len(selects) == 1
```

Run it. Expected: FAIL against the unmodified code, with a count above 1. **Paste the number.** If it is 1, the N+1 prediction did not reproduce — record that in the report, keep the atomicity test as PC2's justification, and say in the report that the second consequence was not observed.

- [ ] **Step 3: Make the change**

Move `:317`'s commit out of the loop. The `for` body ends with `:313`'s assignment, which closes at `:315`; the commit becomes a single statement at the same indentation as the `for`:

```python
            community.post_reply_count = session.execute(text(
                'SELECT COUNT(*) as c FROM post_reply WHERE deleted is false and community_id = :community_id'
            ), {'community_id': community.id}).scalar()

        session.commit()
```

- [ ] **Step 4: Run both new tests and all of Task 5's**

```bash
./run_tests.sh tests/test_shared_tasks_maintenance_cleanup.py -v
```
Expected: 39 passed. Every Task 5 test still passes — the fix changes when the write lands, not what it computes.

- [ ] **Step 5: Commit**

Subject: `fix: commit update_community_stats once rather than per community`

The body says what was observed, not what was predicted: quote the atomicity failure and the select count from Steps 1 and 2, and record that one transaction now covers every community active in the last three days, holding their row locks for the run's duration.

---

## Task 7: `recalculate_user_attitudes` coverage and PC3

**Files:**
- Modify: `app/shared/tasks/maintenance.py:716`, `:737` (two statements deleted)
- Modify: `tests/test_shared_tasks_maintenance_cleanup.py`
- Read: `app/shared/tasks/maintenance.py:712-747`, `app/models.py:1348`, `:1419-1427`

**Interfaces:**
- Consumes: `_seed()`, `_boom`.
- Produces: `recalculate_user_attitudes` with no `processed` counter.

Two branch points: `:728`'s `for i in range(0, total_users, batch_size)` and `:734`'s `for user in users`. `batch_size` is 100 at `:715`, so the multi-batch path needs 101 users — too slow to seed. The loop's zero-iteration arm is reachable with no recently-seen users, and that plus one populated batch covers both arcs.

- [ ] **Step 1: Confirm the counter is dead, and show the grep**

```bash
awk 'NR>=712 && NR<=748' app/shared/tasks/maintenance.py | grep -n 'processed'
```

Expected: exactly two hits, an assignment and an increment, with no read. Paste the output. If anything reads it, PC3 is void — report that and skip to Step 3.

- [ ] **Step 2: Write the coverage tests**

```python
class TestRecalculateUserAttitudes:
    """`recalculate_user_attitudes:712` -- recompute attitude and post stats.

    `:721-723` selects users seen in the last day, `:728` batches them 100 at a
    time (`:715`), and `:735-736` call `recalculate_attitude` and
    `recalculate_post_stats` on each. Both model methods read `db.session`
    (app/models.py:1351, :1421), which is what `:719`'s `patch_db_session`
    redirects onto the task's own session.
    """

    def test_a_recently_seen_user_has_post_stats_recomputed(self, db_session):
        instance, user, community, post = _seed()
        make_post(community, user, 'https://peer.example/p/2')
        user.last_seen = utcnow()
        user.post_count = 99
        db.session.commit()

        recalculate_user_attitudes()

        db.session.expire_all()
        assert db.session.get(User, user.id).post_count == 2

    def test_a_user_not_seen_for_a_day_is_skipped(self, db_session):
        """`:728`'s zero-iteration arm: no eligible users, no batches."""
        instance, user, community, post = _seed()
        user.last_seen = utcnow() - timedelta(days=2)
        user.post_count = 99
        db.session.commit()

        recalculate_user_attitudes()

        db.session.expire_all()
        assert db.session.get(User, user.id).post_count == 99

    def test_deleted_posts_do_not_count(self, db_session):
        instance, user, community, post = _seed()
        gone = make_post(community, user, 'https://peer.example/p/3')
        gone.deleted = True
        user.last_seen = utcnow()
        db.session.commit()

        recalculate_user_attitudes()

        db.session.expire_all()
        assert db.session.get(User, user.id).post_count == 1

    def test_a_failure_inside_the_task_propagates(self, db_session, monkeypatch):
        monkeypatch.setattr('app.shared.tasks.maintenance.utcnow', _boom)

        with pytest.raises(RuntimeError, match='the task itself failed'):
            recalculate_user_attitudes()
```

- [ ] **Step 3: Run them**

```bash
./run_tests.sh tests/test_shared_tasks_maintenance_cleanup.py -v
```
Expected: 43 passed.

- [ ] **Step 4: Delete the dead counter**

Remove `processed = 0` at `:716` and `processed += 1` at `:737`. Nothing else changes; `:715`'s `batch_size = 100` stays.

- [ ] **Step 5: Re-run**

```bash
./run_tests.sh tests/test_shared_tasks_maintenance_cleanup.py -v
```
Expected: 43 passed, unchanged.

- [ ] **Step 6: Commit**

Two commits, because they are two different claims. First the tests, subject `test: cover recalculate_user_attitudes and its empty-batch arm`. Then the deletion, subject `refactor: drop recalculate_user_attitudes' unread counter`, with a body quoting the grep from Step 1.

---

## Task 8: `calculate_community_activity_stats` coverage

**Files:**
- Modify: `tests/test_shared_tasks_maintenance_cleanup.py`
- Read: `app/shared/tasks/maintenance.py:748-866`

**Interfaces:**
- Consumes: `_seed()`, `_boom`, and Task 1's probe result.
- Produces: the tests Task 9 will re-run after changing the SELECT.

**Do not change `app/shared/tasks/maintenance.py` in this task.** Task 9 owns PC1.

The temp table is filled from four sources: posts at `:772-779`, post replies at `:782-789`, post votes at `:792-801`, and post reply votes at `:804-814`. Each carries its own bot exclusion — `from_bot = False` for the two content sources, `u.bot = False` for the two vote sources. A test that seeds only posts covers the statements while proving nothing about the other three INSERTs, so seed all four.

If Task 1's probe reported that the task raises under this harness, stop and report rather than inventing a fixture.

- [ ] **Step 1: Write the tests**

```python
class TestCalculateCommunityActivityStats:
    """`calculate_community_activity_stats:748` -- four activity sources.

    `:763-769` builds a temporary table, `:772-814` fills it from posts, post
    replies, post votes and post reply votes, `:817-819` indexes it, `:825-837`
    aggregates over four windows, and `:843-856` writes the four columns back.

    EACH INSERT HAS ITS OWN BOT EXCLUSION: `from_bot = False` for posts and
    replies, `u.bot = False` for the two vote sources. A test that seeds only
    posts executes every statement while pinning one quarter of the behaviour,
    so these tests seed all four and then remove them one kind at a time.
    """

    def _community_with_one_activity_of_each_kind(self, when):
        instance, author, community, post = _seed()
        voter = make_user(instance, 'voter', local=True)
        reply_voter = make_user(instance, 'replyvoter', local=True)
        replier = make_user(instance, 'replier', local=True)
        reply = make_post_reply(post, replier)
        post_vote = make_post_vote(voter, post, 1.0)
        # A DIFFERENT actor from `voter`, deliberately. If one user casts both
        # votes, COUNT(DISTINCT tca.user_id) counts them once and EITHER vote
        # INSERT can be deleted outright with every test still green -- the
        # source is executed but not verified.
        reply_vote = make_post_reply_vote(reply_voter, reply, 1.0)
        post.posted_at = when
        reply.posted_at = when
        post_vote.created_at = when
        reply_vote.created_at = when
        community.last_active = utcnow()
        db.session.commit()
        return community, {author.id, replier.id, voter.id, reply_voter.id}

    def test_activity_inside_a_day_counts_in_every_window(self, db_session):
        community, actors = self._community_with_one_activity_of_each_kind(
            utcnow() - timedelta(hours=1))

        calculate_community_activity_stats()

        db.session.expire_all()
        refreshed = db.session.get(Community, community.id)
        assert (refreshed.active_daily, refreshed.active_weekly,
                refreshed.active_monthly, refreshed.active_6monthly) == (
            len(actors), len(actors), len(actors), len(actors))

    def test_activity_older_than_a_day_counts_only_in_the_wider_windows(self, db_session):
        community, actors = self._community_with_one_activity_of_each_kind(
            utcnow() - timedelta(days=2))

        calculate_community_activity_stats()

        db.session.expire_all()
        refreshed = db.session.get(Community, community.id)
        assert refreshed.active_daily == 0
        assert (refreshed.active_weekly, refreshed.active_monthly,
                refreshed.active_6monthly) == (len(actors), len(actors), len(actors))

    def test_a_bot_author_is_excluded(self, db_session):
        """`:777`'s `p.from_bot = False`, on the post INSERT."""
        instance, author, community, post = _seed()
        post.from_bot = True
        post.posted_at = utcnow() - timedelta(hours=1)
        community.last_active = utcnow()
        db.session.commit()

        calculate_community_activity_stats()

        db.session.expire_all()
        assert db.session.get(Community, community.id).active_daily == 0

    def test_a_bot_voter_is_excluded(self, db_session):
        """`:799`'s `u.bot = False`, on the post-vote INSERT.

        The post itself is seeded outside the window so the only candidate
        activity is the vote, which makes the assertion about the vote rather
        than about whatever else happens to be in range.
        """
        instance, author, community, post = _seed()
        bot = make_user(instance, 'botty', local=True)
        bot.bot = True
        vote = make_post_vote(bot, post, 1.0)
        vote.created_at = utcnow() - timedelta(hours=1)
        post.posted_at = utcnow() - timedelta(weeks=40)
        community.last_active = utcnow()
        db.session.commit()

        calculate_community_activity_stats()

        db.session.expire_all()
        assert db.session.get(Community, community.id).active_daily == 0

    def test_a_banned_community_is_not_updated(self, db_session):
        """`:834`'s `c.banned = FALSE`."""
        community, _ = self._community_with_one_activity_of_each_kind(
            utcnow() - timedelta(hours=1))
        community.banned = True
        community.active_daily = 77
        db.session.commit()

        calculate_community_activity_stats()

        db.session.expire_all()
        assert db.session.get(Community, community.id).active_daily == 77

    def test_a_failure_inside_the_task_propagates(self, db_session, monkeypatch):
        monkeypatch.setattr('app.shared.tasks.maintenance.utcnow', _boom)

        with pytest.raises(RuntimeError, match='the task itself failed'):
            calculate_community_activity_stats()
```

- [ ] **Step 2: Run them**

```bash
./run_tests.sh tests/test_shared_tasks_maintenance_cleanup.py -v
```
Expected: 51 passed.

The two window tests assert on `len(actors)` rather than a literal, because how many distinct users the four INSERTs contribute is a property of the seed rather than of the task. If the counts come out different from `len(actors)`, do not adjust the expectation to match — find out which source did not contribute and say so in your report.

- [ ] **Step 3: Commit**

Subject: `test: cover the four activity sources and four windows`

---

## Task 9: PC1 — observe the stale stats, then drive the aggregate from `community`

**Files:**
- Modify: `app/shared/tasks/maintenance.py:825-837`
- Modify: `tests/test_shared_tasks_maintenance_cleanup.py`

**Interfaces:**
- Consumes: Task 8's tests, all of which must still pass afterwards.
- Produces: a SELECT driven from `community` with an outer join.

- [ ] **Step 1: Write the test and watch it FAIL against the current code**

```python
class TestCommunityActivityStatsAreNotStale:
    """PC1: `:833`'s INNER JOIN drops communities with no activity.

    `:843-856`'s UPDATE runs once per row `:825-837` returns, and that SELECT
    reads FROM the temp table. A community that had no post, reply or vote in
    six months contributes no row, so the join drops it and its four columns
    keep whatever they last held. Nothing else in app/ writes them --
    app/cli.py:788 is a separate command.
    """

    def test_a_community_that_went_quiet_reads_zero(self, db_session):
        _, user, community, post = _seed()
        post.posted_at = utcnow() - timedelta(weeks=40)
        community.last_active = utcnow()
        community.active_daily = 5
        community.active_weekly = 5
        community.active_monthly = 5
        community.active_6monthly = 5
        db.session.commit()

        calculate_community_activity_stats()

        db.session.expire_all()
        refreshed = db.session.get(Community, community.id)
        assert (refreshed.active_daily, refreshed.active_weekly,
                refreshed.active_monthly, refreshed.active_6monthly) == (0, 0, 0, 0)
```

Run it:

```bash
./run_tests.sh tests/test_shared_tasks_maintenance_cleanup.py::TestCommunityActivityStatsAreNotStale -v
```

Expected: FAIL, reading `(5, 5, 5, 5)`. **Paste the exact assertion output.** If it passes against the unmodified code, PC1 did not reproduce: stop, report it, and make no change — the round then lands two production changes, which the spec says is the correct outcome.

- [ ] **Step 2: Make the change**

Replace the SELECT at `:825-837` so it is driven from `community` and outer-joins the temp table. The four `COUNT(DISTINCT CASE ...)` expressions are unchanged; only the FROM, the join, and the GROUP BY move:

```python
        stats_results = session.execute(text('''
            SELECT
                c.id AS community_id,
                COUNT(DISTINCT CASE WHEN tca.activity_date > :day THEN tca.user_id END) as active_daily,
                COUNT(DISTINCT CASE WHEN tca.activity_date > :week THEN tca.user_id END) as active_weekly,
                COUNT(DISTINCT CASE WHEN tca.activity_date > :month THEN tca.user_id END) as active_monthly,
                COUNT(DISTINCT CASE WHEN tca.activity_date > :half_year THEN tca.user_id END) as active_6monthly
            FROM "community" c
            LEFT JOIN temp_community_activity tca ON c.id = tca.community_id
            WHERE c.banned = FALSE
                AND c.last_active > :half_year
            GROUP BY c.id
        '''), {'day': day, 'week': week, 'month': month, 'half_year': half_year})
```

The eligibility filter is untouched: a banned community, or one whose `last_active` has fallen outside six months, still keeps its stale numbers. That narrowness is deliberate and the register entry says so.

- [ ] **Step 3: Run every test in the file**

```bash
./run_tests.sh tests/test_shared_tasks_maintenance_cleanup.py -v
```
Expected: 52 passed. Task 8's `test_a_banned_community_is_not_updated` is the one most at risk from this rewrite — it asserts a banned community keeps `active_daily = 77`, which the new `WHERE c.banned = FALSE` still guarantees. If it fails, the rewrite went wider than intended.

- [ ] **Step 4: Commit**

Subject: `fix: recompute activity stats for quiet communities instead of leaving them stale`

Body quotes the `(5, 5, 5, 5)` failure from Step 1 and states the scope limit: communities excluded by the eligibility filter are unchanged.

---

## Task 10: Measure coverage, close the residuals, set the floor

**Files:**
- Modify: `coverage_floors.ini`
- Modify: `tests/test_shared_tasks_maintenance_cleanup.py` (whatever the measurement shows is missing)

**Interfaces:**
- Consumes: every test written so far.
- Produces: a floor entry and a measured percentage.

- [ ] **Step 1: Bring the stack down, then measure**

```bash
./run_tests.sh --down
./run_tests.sh --cov=app.shared.tasks.maintenance --cov-branch \
    --cov-report=json:/app/coverage_maintenance.json \
    tests/test_shared_tasks_maintenance_cleanup.py
echo "exit: $?"
```

Do not pipe this command. If you must, read `${PIPESTATUS[0]}`.

- [ ] **Step 2: Retrieve the report and check its mtime**

```bash
podman cp pyfedi_test-runner:/app/coverage_maintenance.json /tmp/.../coverage_maintenance.json
stat -c '%y' /tmp/.../coverage_maintenance.json
```

The mtime must postdate the run. A stale file is the failure mode this campaign has hit before.

- [ ] **Step 3: List what is still missing, restricted to Group A's line ranges**

```bash
python3 - <<'EOF'
import json
d = json.load(open('/tmp/.../coverage_maintenance.json'))
f = d['files']['app/shared/tasks/maintenance.py']
group_a = [(25,43),(45,59),(61,74),(194,213),(283,325),(327,390),
           (392,406),(712,746),(748,866),(870,883)]
def in_a(n):
    return any(a <= n <= b for a, b in group_a)
print('missing statements in Group A:',
      sorted(n for n in f['missing_lines'] if in_a(n)))
print('partial branches in Group A:',
      sorted(k for k in f.get('missing_branches', []) if in_a(k[0])))
print('module percent_covered:', f['summary']['percent_covered'])
EOF
```

**Re-derive the ranges above against the current tree before running this.** The ranges have already been shifted once, for Task 7's deletion of two statements at old `:716` and old `:737` — that is why the last three read `(712,746)`, `(748,866)` and `(870,883)` rather than their original values. Task 9's SELECT rewrite changes the length of `calculate_community_activity_stats` again, after these numbers were written. Derive every range from `grep -n '^def ' app/shared/tasks/maintenance.py` at the moment you run this, and treat the list above as a starting point rather than an answer.

- [ ] **Step 4: Close whatever is left**

For each missing statement or partial branch, write the test that reaches it. If a line proves unreachable, do not mark it `# pragma: no branch` on your own judgment — write the proof into your report and let the reviewer try to defeat it (fact 160). Before closing anything, check whether an earlier sub-project already proved it unreachable (fact 159).

- [ ] **Step 5: Add the floor**

Append to `coverage_floors.ini`, keeping the existing grouping:

```ini
app/shared/tasks/maintenance.py = <the measured integer, rounded DOWN>
```

Round down. A floor above the measured value fails the next run; the ratchet only ever rises.

- [ ] **Step 6: Commit**

Subject: `test: give maintenance.py its first coverage floor`

Body states the measured percentage, the number of Group A statements now covered, and that Groups B, C and D remain.

---

## Task 11: Mutation testing

**Files:**
- Modify: none permanently. Every mutation is applied and restored.

**Interfaces:**
- Consumes: the finished test file.
- Produces: a transcript, one entry per mutation.

**Every line number in the table below is advisory and pre-shift.** Tasks 6, 7 and 9 all edited `app/shared/tasks/maintenance.py` — Task 7 deleted two statements, so everything below them moved up by two. Re-derive each site against the tree as it stands now, and read the dry-run's produced line before applying anything.

Mutations run **one at a time**. For each: dry-run without `-i` and read the produced line first, apply, run, restore, then assert an empty `git diff -- app/` and the expected `wc -l`. Restore before any point where you might stop and report. Paste every dry-run line and every failure.

- [ ] **Step 1: Run these mutations, in this order**

| # | Site | Mutation | Expected |
|---|------|----------|----------|
| 1 | `cleanup_send_queue`'s cutoff | `days=7` → `days=70` | killed by the boundary test |
| 2 | `cleanup_old_notifications`'s first cutoff | `days=90` → `days=900` | killed |
| 3 | `cleanup_old_notifications`'s second cutoff | `days=365` → `days=90` | killed by `test_the_two_cutoffs_are_different` |
| 4 | `cleanup_old_activitypub_logs`'s cutoff | `days=3` → `days=30` | killed |
| 5 | `cleanup_old_read_posts`'s default | `get_setting('read_posts_cutoff', 180)` → `get_setting('read_posts_cutoff', 18)` | killed |
| 6 | `unban_expired_users`'s comparison | `banned_until < :cutoff` → `banned_until > :cutoff` | killed |
| 7 | `cleanup_old_voting_data`'s local guard | `local_months != -1` → `local_months == -1` | killed by the two asymmetric tests |
| 8 | `cleanup_old_voting_data`'s remote guard | `remote_months != -1` → `remote_months == -1` | killed |
| 9 | `cleanup_old_voting_data`'s local instance filter | `instance_id = :instance_id` → `instance_id != :instance_id` (local block only) | killed |
| 10 | `update_community_stats`'s bot exclusion | `User.bot == False` → `User.bot == True` | killed |
| 11 | `update_community_stats`'s banned-member exclusion | `CommunityMember.is_banned == False` → `== True` | killed |
| 12 | `update_community_stats`'s `is_local()` conjunct | delete `community.is_local() and` | killed by `test_a_remote_community_keeps_its_total_untouched` |
| 13 | `update_community_stats`'s total comparison | `<` → `>` | killed by `test_a_local_community_whose_total_already_leads_is_left_alone` |
| 14 | PC2's commit placement | re-indent the commit back inside the loop | killed by `test_a_failure_partway_through_leaves_no_partial_writes` |
| 15 | PC1's join | `LEFT JOIN` → `INNER JOIN` | killed by `test_a_community_that_went_quiet_reads_zero` |
| 16 | `calculate_community_activity_stats`'s banned filter | `c.banned = FALSE` → `c.banned IS NOT NULL` | killed |
| 17 | `calculate_community_activity_stats`'s post bot filter | `p.from_bot = False` → `p.from_bot IS NOT NULL` | killed |
| 18 | `calculate_community_activity_stats`'s voter bot filter | `u.bot = False` → `u.bot IS NOT NULL` (post-vote INSERT) | killed |
| 19 | `recalculate_user_attitudes`'s window, `User.last_seen > utcnow() - timedelta(days=1)` | `days=1` → `days=100` | killed |

- [ ] **Step 2: For every survivor, decide which of two things it is**

A survivor with a proof is information about the code — record the proof and call it an equivalent mutant. A survivor without one is a hole in the tests — write the test that kills it. Do not record a survivor as equivalent without showing why no test using this call site could kill it.

- [ ] **Step 3: Restore and verify**

```bash
git diff --stat -- app/
wc -l app/shared/tasks/maintenance.py
```

The diff must be empty and the line count must match what Tasks 6, 7 and 9 left behind.

- [ ] **Step 4: Commit the transcript**

The transcript goes in your report file, which is gitignored. Nothing is committed in this task unless Step 2 produced new tests, in which case commit those with subject `test: kill the surviving mutants in maintenance Group A`.

---

## Task 12: Register the findings and the facts

**Files:**
- Modify: `docs/superpowers/findings-register.md`
- Modify: `tests/README.md`

**Interfaces:**
- Consumes: everything observed in Tasks 1 through 11.
- Produces: entries from D342 and facts from 161.

- [ ] **Step 1: Confirm the next free numbers**

```bash
grep -o 'D3[0-9][0-9]' docs/superpowers/findings-register.md | sort -u | tail -3
grep -o '^\*\*1[0-9][0-9]\.' tests/README.md | tail -3
```

The plan says D342 and 161; verify rather than trusting it. Sub-project 27 committed a register entry citing a base commit 175 commits off, and a "next free number" that skipped two unallocated numbers.

- [ ] **Step 2: Write the findings**

One entry each, from D342:

- **The in-loop commit in `update_community_stats`** — what was observed in Task 6 Step 1, what changed, and the transaction-size consequence. If Task 6 Step 2's N+1 did not reproduce, say so explicitly rather than repeating the prediction.
- **The stale activity stats** — what was observed in Task 9 Step 1, and the scope limit: communities excluded by `c.banned = FALSE AND c.last_active > :half_year` are still not reset.
- **The dead `processed` counter** — with the grep that proved it dead.
- **`calculate_community_activity_stats` indexes a temp table** one aggregate scan reads. An observation, not a fix.
- **Two docstrings contradict their code** — `:26` against `:33-34`'s `RevokedToken` delete, and `:46` against `:50`'s `get_setting`. Registered rather than fixed; the approved production scope was three changes.
- **Raw `text()` DELETEs bypass the identity map** in `cleanup_old_read_posts` and `cleanup_old_voting_data`. Harmless as written because these tasks load nothing first; recorded so a future round that adds a query above one of them knows.
- **`maintenance.py`'s remaining three groups** — B, C and D, with their function lists and statement counts from the spec, so the next round does not re-derive them.

- [ ] **Step 3: Verify facts 156 and 157 rather than correcting them**

An earlier commit in this round claimed `tests/README.md:5181-5182`'s citations were off by one and "corrected" them in the plan and spec. **That claim was wrong and has been reverted.** `app/utils.py:3685` is `if has_request_context():` and `:3688` is the `return` inside it, exactly as facts 156 and 157 already say. The error came from reading an unnumbered `sed -n 'A,Bp'` range and counting output lines.

Open both lines with numbered output — `awk 'NR==3685||NR==3688 {printf "%d\t[%s]\n",NR,$0}' app/utils.py` — confirm the facts stand, and make no change to them. Then record the episode itself as one of Step 4's new facts: a citation verified by counting lines in an unnumbered range is not verified, and this round produced a wrong "correction" that way before catching it.

- [ ] **Step 4: Write the new facts, from 161**

At minimum:
- Task 1's probe result about `CREATE TEMPORARY TABLE ... ON COMMIT DROP` under this harness.
- That `tests/conftest.py`'s `db_session` fixture DELETEs and commits rather than rolling back, so a commit inside a test is real. This corrects a belief the spec itself carried in draft.
- The `monkeypatch.setattr('app.shared.tasks.maintenance.utcnow', _boom)` idiom for reaching a task's `except` arm without touching the factories, and the one task it does not work for.
- Whatever Task 6 Step 2's select count actually showed.

- [ ] **Step 5: Commit**

Subject: `docs: register the maintenance Group A findings and facts`

---

## Self-Review

**Spec coverage.** Every spec section maps to a task: the four-group decomposition to this plan's scope and Task 12's register entry; PC1 to Task 9; PC2 to Task 6; PC3 to Task 7; the five register-only findings to Task 12 Step 2; the per-task three-test shape to Tasks 1 through 8; the fresh-query oracle to the harness section and to every assertion; the floor to Task 10; the mutations to Task 11; the facts-156-and-157 correction to Task 12 Step 3. The spec's `ON COMMIT DROP` question is Task 1's whole purpose.

**Placeholder scan.** No task says "add appropriate tests" or "handle edge cases". Every code step carries the code. Task 10 Step 4 is the one step whose content depends on a measurement, and it says what to do with each possible result rather than deferring the decision.

**Type consistency.** `_seed()` returns `(instance, user, community, post)` and every caller unpacks four values. `_boom` takes `*args, **kwargs` because it substitutes for both `utcnow` (no arguments) and `text` (one). The test file imports `Tag`, `RevokedToken` and `SendQueue` from `app.models`, which Task 1's import block includes, and `set_setting` is imported locally inside the one test that uses it rather than at module scope.

**One risk the plan carries deliberately.** Tasks 6 and 9 each begin with a test that must FAIL. If either passes against unmodified code, the implementer is told to stop and report rather than to proceed — because the alternative is the shape of sub-project 28, where a production change was made on a premise that a probe later disproved.

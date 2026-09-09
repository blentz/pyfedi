# Sub-project 30: `maintenance.py` Group B Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Take the eight content-lifecycle tasks in `app/shared/tasks/maintenance.py` to 100% statement and branch coverage, raise the module's floor from 24, and land three production changes — each observed before it is made.

**Architecture:** One new test file, `tests/test_shared_tasks_maintenance_lifecycle.py`, covering Group B only. Two of these tasks delete through `app/shared/post.py`'s `delete_post` and one builds a `boto3` client; both are *arranged* rather than exercised, so the round stays inside its own module. Twenty-three branch points against Group A's seven.

**Tech Stack:** Flask, SQLAlchemy 2.0.52, Celery (eager in tests), pytest, PostgreSQL in a podman container.

**Spec:** `docs/superpowers/specs/2026-09-09-coverage-maintenance-b-30-design.md`

## Global Constraints

- **Delete nothing the task did not create.** `git checkout -- app/` is permitted only as a mutation-restore step.
- **There is no host Python with flask or pytest.** Everything runs through `./run_tests.sh <pytest args>`, which is `podman compose exec -T test-runner pytest "$@"` (`run_tests.sh:85`).
- **Only the controller runs the full suite**, one pytest session at a time, in the foreground.
- **`pytest` exits 1 on a session timeout and `run_tests.sh` propagates it.** A shell pipeline eats the status — read `${PIPESTATUS[0]}`, or do not pipe.
- **Coverage takes the dotted module form**: `--cov=app.shared.tasks.maintenance`. A path form collects nothing, writes no JSON, and exits 0 — a silent green failure.
- **A coverage run leaves its JSON in the HOST repo root**, because `compose.test.yaml` bind-mounts `./:/app:z`. Retrieve with `podman cp` if you prefer, but the file is already in the working tree and must be cleaned up — it is yours (fact 168).
- **Run `./run_tests.sh --down` before any measured run.** `pytest.ini:28` caps a session at 600s and the suite ran 346s at the end of sub-project 29 — the closest it has been to the cap in several rounds.
- **Test counts come from pytest's own collection output**, never `grep -c '^def test_'`.
- **No ordered assertions over rows a query planner returned.** Compare sets, or sort explicitly in Python.
- **Every line number is re-derived against the current tree, with numbered output** — `awk 'NR>=X && NR<=Y {printf "%d\t%s\n",NR,$0}' FILE`. Never count lines of an unnumbered `sed -n 'X,Yp'` range, and never read `grep -n` numbers from piped or already-extracted output as file line numbers. Both habits produced wrong "verified" citations in sub-project 29, one of them a confident correction that was itself wrong (fact 165).
- **Where prose describes a statement, cite that statement's line**, not the `def` above it, not the `if` guarding it, not the continuation that closes it.
- **An edit falsifies prose the same task wrote minutes earlier.** Three times in sub-project 29 a correct change made a docstring false. Re-derive every citation AFTER your diff is final, and sweep the whole file rather than only the lines you wrote (fact 166).
- **A docstring must not claim a proof the test does not deliver.**
- **Commit with `git commit -F <file>`, never `-m`.** Lowercase `type:` subject prefix, normal English prose in the body.
- **Commit trailers, in this order:**
  ```
  Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01BF7kVN2spWD86NYKWyGrVn
  ```
- **Mutations run one at a time:** dry-run without `-i` and read the produced line first, apply, run, restore, then assert an empty `git diff -- app/` and the expected `wc -l`. Restore before any point where you might stop and report.
- **PC1, PC2 and PC3 are predictions, not observations.** Observe the defect and paste what happened before changing anything. A prediction that does not reproduce becomes a registered finding and the change is dropped. **PC2 is expected to possibly not reproduce** — see Task 8.

---

## File Structure

| File | Responsibility |
|------|----------------|
| `tests/test_shared_tasks_maintenance_lifecycle.py` | **Create.** Every test in this round. Named for Group B; Group A's file is `tests/test_shared_tasks_maintenance_cleanup.py` and is not touched. |
| `app/shared/tasks/maintenance.py` | **Modify.** Three production changes: `archive_old_users`' filter, `pwn_bots`' session wrapper, two `@celery.task` decorators. |
| `coverage_floors.ini` | **Modify.** `app/shared/tasks/maintenance.py` raised from 24. |
| `tests/README.md` | **Modify.** New facts from 172. |
| `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` | **Modify.** New findings from D351. This is the campaign's register — it is NOT `docs/superpowers/findings-register.md`, which does not exist. |

## Harness facts that bind every task

**There is no fixture transaction.** `tests/conftest.py:137-202`'s `db_session` yields `db.session`, then DELETEs every row and commits. A `commit()` inside a test is real (fact 162).

**The task runs on its own connection.** `get_task_session()` returns `Session(bind=db.engine)` (`app/utils.py:3673-3675`). Rows a test seeds must be **committed** before the task runs, and an ORM attribute read afterwards is **stale** unless the test calls `db.session.expire_all()` first (fact 153).

**`archive_post` opens a THIRD session.** `app/utils.py:4946` calls `get_task_session()` again and `:4948` wraps its body in `patch_db_session`. Task 7 arranges around this rather than reasoning through it.

**The error-path idiom, and the two ways it fails.** A patched symbol must be *reached*, which needs two things: it must sit inside the `try`, and the statement using it must actually execute. `pwn_bots` fails the first — its cutoff is above the `try`. `remove_old_community_content` fails the second — its `utcnow()` is inside a `for` loop over communities with a retention policy, so with none seeded the loop body never runs and the task returns normally. Check both before writing any error-path test.

**Which task calls the clock where.** Six of the eight tasks call `utcnow()` inside their `try` — `process_expired_bans:80`, `remove_old_community_content:142`, `remove_old_bot_content:168`, `delete_old_soft_deleted_content:222`, `archive_old_posts:890`, `archive_old_users:938`. **`pwn_bots` computes its cutoff at `:1164`, one line ABOVE `:1165`'s `try:`**, so patching the clock there raises outside the handler and the test would pass even with the whole `except` clause deleted; its error-path test patches `text` and seeds a row so `:1167` is reached. Check which side of the `try` your patched symbol sits on before writing an error-path test. `utcnow` is bound into this module's namespace at `app/shared/tasks/maintenance.py:16`, so monkeypatching `app.shared.tasks.maintenance.utcnow` raises inside the `try` without touching `tests/factories.py`, which reaches `utcnow` through `app.models` (fact 163). **`archive_user` is the exception** — it takes a session and calls no clock; its error path is reached by passing a `user_id` with no row, which makes `:958`'s `.get()` return `None` and `:959`'s attribute read raise `AttributeError`.

**Config gates need config, not mocks.** `ARCHIVE_POSTS` (`config.py:184`, default **0**) and `BOT_CONTENT_RETENTION` (`config.py:190`, default 6) are read from `current_app.config`. **Capture the original value, set it, restore the captured value in a `finally`** — never restore a hardcoded default. Sub-project 29 shipped that bug and needed a review round to fix it.

**S3 is off by default.** `store_files_in_s3()` (`app/utils.py:4317-4319`) is true only when `S3_ACCESS_KEY`, `S3_ACCESS_SECRET` and `S3_ENDPOINT` are all non-empty; `config.py:103-107` defaults all three to `''`. So `archive_old_posts`' `s3 = None` arm is the default and its client arm needs all three set.

---

## Task 1: Open the file with `pwn_bots`

**Files:**
- Create: `tests/test_shared_tasks_maintenance_lifecycle.py`
- Read: `app/shared/tasks/maintenance.py:1161-1180`

**Interfaces:**
- Consumes: nothing.
- Produces: the module docstring, the import block, `_seed()`, `_boom`, and one passing test class. Every later task adds to this file.

`pwn_bots` is the simplest task in Group B — 12 statements, one branch point, no config gate, no external dependency. It also carries PC2's defect, which Task 8 owns; **do not fix it here.**

- [ ] **Step 1: Write the file's prelude**

```python
"""Group B of `app/shared/tasks/maintenance.py` -- the content-lifecycle tasks.

Sub-project 29 closed Group A, the ten tasks that need no transport, in
`tests/test_shared_tasks_maintenance_cleanup.py`. Group B is the eight that
move or destroy content:

  `process_expired_bans:76`            `remove_old_community_content:134`
  `remove_old_bot_content:160`         `delete_old_soft_deleted_content:215`
  `archive_old_posts:885`              `archive_old_users:933`
  `archive_user:957`                   `pwn_bots:1161`

TWENTY-THREE BRANCH POINTS AGAINST GROUP A'S SEVEN. Group A was straight-line
SQL in a repeated try/except skeleton. Group B branches on config values, on
whether a row still exists, on whether an image is shared between posts, and on
whether object storage is configured.

TWO TASKS DELETE THROUGH `app/shared/post.py`. `remove_old_community_content:150`
and `remove_old_bot_content:184` both call `delete_post`
(`app/shared/post.py:755`), which is a floored module at 40% with its own
federation behaviour. THIS FILE DOES NOT TEST `delete_post`. It tests which post
ids these two tasks hand over, by replacing `delete_post` in this module's
namespace with a recorder. Asserting on federation here would be testing another
module through a keyhole.

ONE TASK BUILDS A boto3 CLIENT. `archive_old_posts:911-918` constructs one when
`:910`'s `store_files_in_s3()` is true and passes it to `archive_post`. The
oracle is whether `archive_post` receives a client or `None` -- no S3 endpoint is
contacted, and `archive_post` is replaced by a recorder for the same reason
`delete_post` is.

THE TASK RUNS ON ITS OWN CONNECTION. `get_task_session()` returns
`Session(bind=db.engine)` (app/utils.py:3673-3675), so rows a test seeds must be
COMMITTED before the task runs, and an ORM attribute read afterwards is stale
unless the test calls `db.session.expire_all()` first (fact 153).
"""

from datetime import timedelta

import pytest

from app import db
from app.constants import NOTIF_UNBAN
from app.models import (
    BotChallenge, CommunityBan, CommunityMember, File, InstanceBan,
    Notification, Post, PostBookmark, PostReply, User, utcnow,
)
from app.shared.tasks.maintenance import (
    archive_old_posts, archive_old_users, archive_user,
    delete_old_soft_deleted_content, process_expired_bans, pwn_bots,
    remove_old_bot_content, remove_old_community_content,
)
from tests.factories import (
    make_community, make_community_ban, make_community_member, make_file,
    make_instance, make_instance_ban, make_post, make_post_reply, make_user,
)


def _boom(*args, **kwargs):
    """Raise where a task will catch it, to reach its `except` arm.

    THE CALLER PICKS THE SYMBOL, AND THE CHOICE MATTERS. Patching something
    the task calls BEFORE its `try` raises outside the handler, and the test
    then passes even if the whole `except` clause is deleted. Seven of Group
    B's eight tasks compute their cutoff inside the try, so patching `utcnow`
    reaches the handler; `pwn_bots` computes it at `:1164`, one line above
    `:1165`'s `try:`, so its error-path test patches `text` instead and seeds a
    row to make the loop body run.
    """
    raise RuntimeError('the task itself failed')


def _seed():
    """instance, a local user, a community, and one post -- all committed.

    Every factory here commits, so the task's own connection can see these
    rows. The first `make_instance` in a test receives id 1, because the
    `db_session` fixture resets sequences; tests that need a remote user build
    a second instance.
    """
    instance = make_instance('peer.example')
    user = make_user(instance, 'reader', local=True)
    community = make_community()
    post = make_post(community, user, 'https://peer.example/p/1')
    return instance, user, community, post


class _Recorder:
    """Replace a module-level callable and remember how it was called.

    Used for `delete_post` and `archive_post`, both of which belong to other
    modules this round does not test. `calls` holds one tuple of positional
    arguments per invocation.
    """

    def __init__(self, result=None):
        self.calls = []
        self.result = result

    def __call__(self, *args, **kwargs):
        self.calls.append(args)
        return self.result
```

- [ ] **Step 2: Write the `pwn_bots` tests**

```python
class TestPwnBots:
    """`pwn_bots:1161` -- everyone who ignored a bot challenge for 24h is a bot.

    `:1166` iterates challenges older than the cutoff whose `is_a_bot` is still
    NULL, `:1167` marks the user, and `:1170` marks the challenge. One branch
    point, the loop.

    `:1166` READS THROUGH `BotChallenge.query`, WHICH IS `db.session`, while
    `:1167` and `:1170` write through the task's own session, and the body is
    not wrapped in `patch_db_session`. That split is this round's PC2 and is
    NOT fixed by these tests -- they pass either way, which is itself the point
    Task 8 has to establish.
    """

    def test_a_challenge_ignored_past_the_cutoff_marks_the_user_a_bot(self, db_session):
        instance, user, _, _ = _seed()
        challenge = BotChallenge(uuid='c1', user_id=user.id,
                                 sent_at=utcnow() - timedelta(days=2))
        db.session.add(challenge)
        db.session.commit()

        pwn_bots()

        db.session.expire_all()
        refreshed = db.session.get(User, user.id)
        assert (refreshed.bot, refreshed.bot_override) == (True, True)
        assert db.session.get(BotChallenge, challenge.id).is_a_bot is True

    def test_a_challenge_inside_the_cutoff_is_left_alone(self, db_session):
        """The BOUNDARY. A deletion test alone passes against no cutoff at all."""
        instance, user, _, _ = _seed()
        challenge = BotChallenge(uuid='c2', user_id=user.id,
                                 sent_at=utcnow() - timedelta(hours=12))
        db.session.add(challenge)
        db.session.commit()

        pwn_bots()

        db.session.expire_all()
        assert db.session.get(User, user.id).bot is not True
        assert db.session.get(BotChallenge, challenge.id).is_a_bot is None

    def test_an_answered_challenge_is_left_alone(self, db_session):
        """`:1166`'s `is_a_bot == None` conjunct.

        A challenge someone answered has a non-NULL `is_a_bot`, and must not be
        reprocessed however old it is.
        """
        instance, user, _, _ = _seed()
        challenge = BotChallenge(uuid='c3', user_id=user.id,
                                 sent_at=utcnow() - timedelta(days=30),
                                 is_a_bot=False)
        db.session.add(challenge)
        db.session.commit()

        pwn_bots()

        db.session.expire_all()
        assert db.session.get(User, user.id).bot is not True
        assert db.session.get(BotChallenge, challenge.id).is_a_bot is False

    def test_a_failure_inside_the_task_propagates(self, db_session, monkeypatch):
        """`:1174-1176`'s handler, reached through `:1167`.

        NOT through `utcnow`. `:1164` computes the cutoff one line ABOVE
        `:1165`'s `try:`, so patching the clock raises before the handler
        exists and the test would pass even if the whole `except` clause were
        deleted. `pwn_bots` is the only task in this group ordering those two
        statements that way.

        A challenge must be seeded, because `:1167`'s `text(...)` is inside the
        loop and an empty result set never reaches it.
        """
        instance, user, _, _ = _seed()
        challenge = BotChallenge(uuid='c4', user_id=user.id,
                                 sent_at=utcnow() - timedelta(days=2))
        db.session.add(challenge)
        db.session.commit()
        monkeypatch.setattr('app.shared.tasks.maintenance.text', _boom)

        with pytest.raises(RuntimeError, match='the task itself failed'):
            pwn_bots()
```

- [ ] **Step 3: Run**

```bash
./run_tests.sh tests/test_shared_tasks_maintenance_lifecycle.py -v
```
Expected: 4 passed.

If `User.bot` or `User.bot_override` do not exist under those names, do not adjust the assertion to whatever does exist — read `:1167`'s UPDATE, use the columns it names, and say in your report that the plan's names were wrong.

- [ ] **Step 4: Commit**

Subject: `test: open the maintenance Group B file with pwn_bots`

---

## Task 2: `process_expired_bans`

**Files:**
- Modify: `tests/test_shared_tasks_maintenance_lifecycle.py`
- Read: `app/shared/tasks/maintenance.py:76-130`

**Interfaces:**
- Consumes: `_seed()`, `_boom`.
- Produces: nothing later tasks depend on.

The widest task in the group: 33 statements, four branch points — `:82`'s loop, `:88`'s `if community_membership_record:`, `:94`'s `if blocked and blocked.is_local():`, and `:120`'s second loop over instance bans.

`CommunityBan` has a **composite primary key** (`user_id`, `community_id`, `app/models.py:3576-3577`) and no `id`, so a test asserting the row is gone queries by both keys. `make_community_ban(user, community, banned_by=None, reason='', ban_until=None)` exists in `tests/factories.py:416`.

- [ ] **Step 1: Write the tests**

```python
class TestProcessExpiredBans:
    """`process_expired_bans:76` -- lift community bans whose term has run out.

    `:80` selects `CommunityBan` rows past `ban_until`; `:89` clears the
    membership's `is_banned`; `:94`'s guard decides whether a notification is
    written; `:116` deletes the ban row. A second loop at `:120` clears expired
    `InstanceBan` rows.

    `CommunityBan` has a COMPOSITE primary key (app/models.py:3576-3577) and no
    `id` column, so these tests query by both keys rather than by an id the
    factory never returned.
    """

    def _expired_ban(self, user, community):
        return make_community_ban(user, community,
                                  ban_until=utcnow() - timedelta(days=1))

    def _ban_row(self, user, community):
        return db.session.query(CommunityBan).filter_by(
            user_id=user.id, community_id=community.id).first()

    def test_an_expired_ban_is_deleted(self, db_session):
        instance, user, community, _ = _seed()
        self._expired_ban(user, community)

        process_expired_bans()

        db.session.expire_all()
        assert self._ban_row(user, community) is None

    def test_a_ban_still_running_survives(self, db_session):
        """The BOUNDARY -- `:80`'s `ban_until < utcnow()`."""
        instance, user, community, _ = _seed()
        make_community_ban(user, community,
                           ban_until=utcnow() + timedelta(days=1))

        process_expired_bans()

        db.session.expire_all()
        assert self._ban_row(user, community) is not None

    def test_a_permanent_ban_has_no_ban_until_and_survives(self, db_session):
        """`ban_until` is NULL for a permanent ban, and `NULL < timestamp` is
        UNKNOWN, which `WHERE` treats as false. This test pins the BEHAVIOUR --
        a permanent ban is not lifted -- rather than any one conjunct, and it
        would still pass if the comparison were written another way that also
        excluded NULLs.
        """
        instance, user, community, _ = _seed()
        make_community_ban(user, community, ban_until=None)

        process_expired_bans()

        db.session.expire_all()
        assert self._ban_row(user, community) is not None

    def test_the_membership_is_unbanned(self, db_session):
        """`:88`'s true arm -- a membership row exists, so `:89` clears it."""
        instance, user, community, _ = _seed()
        membership = make_community_member(user, community)
        membership.is_banned = True
        db.session.commit()
        self._expired_ban(user, community)

        process_expired_bans()

        db.session.expire_all()
        assert db.session.query(CommunityMember).filter_by(
            user_id=user.id, community_id=community.id).first().is_banned is False

    def test_a_ban_with_no_membership_row_still_clears(self, db_session):
        """`:88`'s FALSE arm. A user banned without ever having joined has no
        `CommunityMember` row, and the task must still delete the ban.
        """
        instance, user, community, _ = _seed()
        self._expired_ban(user, community)

        process_expired_bans()

        db.session.expire_all()
        assert self._ban_row(user, community) is None

    def test_a_local_user_is_notified(self, db_session):
        """`:94`'s true arm -- `:97-106` writes a NOTIF_UNBAN notification and
        `:107` increments the unread counter.
        """
        instance, user, community, _ = _seed()
        self._expired_ban(user, community)

        process_expired_bans()

        db.session.expire_all()
        notifications = db.session.query(Notification).filter_by(
            user_id=user.id, notif_type=NOTIF_UNBAN).all()
        assert len(notifications) == 1
        assert db.session.get(User, user.id).unread_notifications == 1

    def test_a_remote_user_is_not_notified(self, db_session):
        """`:94`'s false arm, via `is_local()`.

        `make_user` with `local=False` sets `ap_id`, and `User.is_local()`
        (app/models.py:1251-1252) is false once `ap_id` is set and does not
        start with SERVER_URL. The ban is still deleted -- only the
        notification is skipped.
        """
        local_instance = make_instance('local.example')
        remote_instance = make_instance('remote.example')
        remote_user = make_user(remote_instance, 'visitor')
        community = make_community()
        make_community_ban(remote_user, community,
                           ban_until=utcnow() - timedelta(days=1))

        process_expired_bans()

        db.session.expire_all()
        assert db.session.query(Notification).filter_by(
            user_id=remote_user.id).count() == 0
        assert db.session.query(CommunityBan).filter_by(
            user_id=remote_user.id, community_id=community.id).first() is None

    def test_an_expired_instance_ban_is_deleted(self, db_session):
        """`:119-123`'s second loop, which no community-ban test reaches."""
        instance, user, _, _ = _seed()
        ban = make_instance_ban(user, instance)
        ban.banned_until = utcnow() - timedelta(days=1)
        db.session.commit()

        process_expired_bans()

        db.session.expire_all()
        assert db.session.query(InstanceBan).filter_by(
            user_id=user.id, instance_id=instance.id).first() is None

    def test_a_permanent_instance_ban_survives(self, db_session):
        """`:119`'s `banned_until != None` conjunct, which -- unlike the
        community-ban case -- is written explicitly rather than left to SQL's
        three-valued logic. This test pins the behaviour; it cannot by itself
        prove the explicit conjunct is load-bearing, since the comparison beside
        it would exclude NULLs anyway.
        """
        instance, user, _, _ = _seed()
        make_instance_ban(user, instance)

        process_expired_bans()

        db.session.expire_all()
        assert db.session.query(InstanceBan).filter_by(
            user_id=user.id, instance_id=instance.id).first() is not None

    def test_a_failure_inside_the_task_propagates(self, db_session, monkeypatch):
        monkeypatch.setattr('app.shared.tasks.maintenance.utcnow', _boom)

        with pytest.raises(RuntimeError, match='the task itself failed'):
            process_expired_bans()
```

- [ ] **Step 2: Run**

```bash
./run_tests.sh tests/test_shared_tasks_maintenance_lifecycle.py -v
```
Expected: 14 passed.

- [ ] **Step 3: Decide the cache question and record it**

`:111-114` and `:121-122` call `cache.delete_memoized` six times. **These tests do not assert on those calls, deliberately** — the cache is a real Flask-Caching instance shared across the suite, and asserting on invalidation would couple this file to another subsystem's internals. Write one sentence in the class docstring saying so, so a later reader does not mistake the omission for an oversight, and note it in your report for the register.

- [ ] **Step 4: Commit**

Subject: `test: cover process_expired_bans and both of its loops`

---

## Task 3: `remove_old_community_content` and `remove_old_bot_content`

**Files:**
- Modify: `tests/test_shared_tasks_maintenance_lifecycle.py`
- Read: `app/shared/tasks/maintenance.py:134-157`, `:160-190`

**Interfaces:**
- Consumes: `_seed()`, `_boom`, `_Recorder`.
- Produces: nothing later tasks depend on.

Both call `delete_post`. **Replace it with a `_Recorder` in this module's namespace** — `monkeypatch.setattr('app.shared.tasks.maintenance.delete_post', recorder)` — and assert on which ids were handed over. Do not let a real `delete_post` run: it federates, and this round does not own that behaviour.

`Community.content_retention` defaults to **-1** (`app/models.py:581`), and `:138` selects only communities with `content_retention > 0`, so a test must set it.

`remove_old_bot_content`'s gate is `BOT_CONTENT_RETENTION` (`config.py:190`, default 6), and `:168`'s cutoff is `28 * bot_retention` days. `:179`'s batch size is 100, so the multi-batch arm needs 101 posts and is not worth seeding — cover the loop's single-batch and zero-iteration arms.

- [ ] **Step 1: Write the tests**

```python
class TestRemoveOldCommunityContent:
    """`remove_old_community_content:134` -- honour per-community retention.

    `:138` selects communities with `content_retention > 0` (the column
    defaults to -1, app/models.py:581), `:143-147` picks their posts older than
    the cutoff that are neither deleted nor sticky, and `:150` hands each id to
    `delete_post`.

    `delete_post` IS REPLACED BY A RECORDER IN THESE TESTS. It belongs to
    `app/shared/post.py`, a floored module at 40% with its own federation
    behaviour, and this round tests which ids reach it rather than what it then
    does.
    """

    def test_a_post_older_than_the_retention_window_is_handed_over(self, db_session, monkeypatch):
        recorder = _Recorder()
        monkeypatch.setattr('app.shared.tasks.maintenance.delete_post', recorder)
        instance, user, community, post = _seed()
        community.content_retention = 7
        post.posted_at = utcnow() - timedelta(days=8)
        db.session.commit()

        remove_old_community_content()

        assert [c[0] for c in recorder.calls] == [post.id]

    def test_a_post_inside_the_window_is_left_alone(self, db_session, monkeypatch):
        recorder = _Recorder()
        monkeypatch.setattr('app.shared.tasks.maintenance.delete_post', recorder)
        instance, user, community, post = _seed()
        community.content_retention = 7
        post.posted_at = utcnow() - timedelta(days=6)
        db.session.commit()

        remove_old_community_content()

        assert recorder.calls == []

    def test_a_community_with_no_retention_policy_is_skipped(self, db_session, monkeypatch):
        """`:138`'s filter. The column's default of -1 means "keep forever"."""
        recorder = _Recorder()
        monkeypatch.setattr('app.shared.tasks.maintenance.delete_post', recorder)
        instance, user, community, post = _seed()
        post.posted_at = utcnow() - timedelta(days=400)
        db.session.commit()

        remove_old_community_content()

        assert recorder.calls == []

    def test_a_sticky_or_already_deleted_post_is_skipped(self, db_session, monkeypatch):
        """`:143-147`'s `deleted=False, sticky=False` filter.

        Both are asserted in one test because each alone would leave the other
        conjunct unexercised, and the task's behaviour for both is the same.
        """
        recorder = _Recorder()
        monkeypatch.setattr('app.shared.tasks.maintenance.delete_post', recorder)
        instance, user, community, post = _seed()
        community.content_retention = 7
        post.posted_at = utcnow() - timedelta(days=8)
        post.sticky = True
        gone = make_post(community, user, 'https://peer.example/p/2')
        gone.posted_at = utcnow() - timedelta(days=8)
        gone.deleted = True
        db.session.commit()

        remove_old_community_content()

        assert recorder.calls == []

    def test_the_deletion_is_not_federated(self, db_session, monkeypatch):
        """`:150` passes False for `delete_post`'s SECOND parameter, which is
        `federate_deletion` (app/shared/post.py:755) and not a locality flag.

        A retention-policy deletion is deliberately not federated, so remote
        instances keep the post. `remove_old_bot_content:184` passes a different
        value for the same parameter; the two are separate policies, not an
        inconsistency.
        """
        recorder = _Recorder()
        monkeypatch.setattr('app.shared.tasks.maintenance.delete_post', recorder)
        instance, user, community, post = _seed()
        community.content_retention = 7
        post.posted_at = utcnow() - timedelta(days=8)
        db.session.commit()

        remove_old_community_content()

        assert recorder.calls[0][1] is False

    def test_a_failure_inside_the_task_propagates(self, db_session, monkeypatch):
        """`:142`'s cutoff, not `:138`'s query, is what must raise.

        `utcnow` is read once per community INSIDE the `for` loop at `:141`, so
        a community must be seeded with a retention policy or the loop body --
        and therefore `_boom` -- is never reached, and the task returns
        normally instead of propagating. This is the same trap as `pwn_bots`'
        in a different shape: there the patched symbol sat above the `try`,
        here it sits inside a loop that needs data to run.
        """
        instance, user, community, post = _seed()
        community.content_retention = 7
        db.session.commit()
        monkeypatch.setattr('app.shared.tasks.maintenance.utcnow', _boom)

        with pytest.raises(RuntimeError, match='the task itself failed'):
            remove_old_community_content()


class TestRemoveOldBotContent:
    """`remove_old_bot_content:160` -- delete replyless bot posts.

    `:167`'s `if bot_retention > 0:` gates the whole body on
    `BOT_CONTENT_RETENTION` (config.py:190, default 6, with -1 documented as
    "forever, no deletion"), and `:168`'s cutoff is 28 days per unit.
    `:170-175` selects non-deleted, non-sticky, replyless posts by bots, and
    `:179`'s loop batches them 100 at a time.

    THIS FUNCTION HAS NO `@celery.task` DECORATOR and never commits. Both are
    registered findings of this round; neither is fixed by these tests.
    """

    def _bot_post(self, community, user, ap_id, age_days):
        post = make_post(community, user, ap_id)
        post.from_bot = True
        post.reply_count = 0
        post.posted_at = utcnow() - timedelta(days=age_days)
        return post

    def test_an_old_replyless_bot_post_is_handed_over(self, db_session, monkeypatch):
        recorder = _Recorder()
        monkeypatch.setattr('app.shared.tasks.maintenance.delete_post', recorder)
        instance, user, community, post = _seed()
        bot_post = self._bot_post(community, user, 'https://peer.example/p/2',
                                  28 * 6 + 1)
        db.session.commit()

        remove_old_bot_content()

        assert [c[0] for c in recorder.calls] == [bot_post.id]

    def test_a_recent_bot_post_is_left_alone(self, db_session, monkeypatch):
        recorder = _Recorder()
        monkeypatch.setattr('app.shared.tasks.maintenance.delete_post', recorder)
        instance, user, community, post = _seed()
        self._bot_post(community, user, 'https://peer.example/p/2', 28 * 6 - 1)
        db.session.commit()

        remove_old_bot_content()

        assert recorder.calls == []

    def test_a_bot_post_with_replies_is_left_alone(self, db_session, monkeypatch):
        """`:174`'s `reply_count=0`."""
        recorder = _Recorder()
        monkeypatch.setattr('app.shared.tasks.maintenance.delete_post', recorder)
        instance, user, community, post = _seed()
        bot_post = self._bot_post(community, user, 'https://peer.example/p/2',
                                  28 * 6 + 1)
        bot_post.reply_count = 3
        db.session.commit()

        remove_old_bot_content()

        assert recorder.calls == []

    def test_a_human_post_is_left_alone(self, db_session, monkeypatch):
        """`:173`'s `from_bot=True`."""
        recorder = _Recorder()
        monkeypatch.setattr('app.shared.tasks.maintenance.delete_post', recorder)
        instance, user, community, post = _seed()
        post.posted_at = utcnow() - timedelta(days=28 * 6 + 1)
        post.reply_count = 0
        db.session.commit()

        remove_old_bot_content()

        assert recorder.calls == []

    def test_a_retention_of_minus_one_disables_the_task(self, db_session, monkeypatch, app):
        """`:167`'s FALSE arm. -1 is documented as "forever, no deletion"."""
        recorder = _Recorder()
        monkeypatch.setattr('app.shared.tasks.maintenance.delete_post', recorder)
        instance, user, community, post = _seed()
        self._bot_post(community, user, 'https://peer.example/p/2', 28 * 6 + 1)
        db.session.commit()
        original = app.config['BOT_CONTENT_RETENTION']
        app.config['BOT_CONTENT_RETENTION'] = -1

        try:
            remove_old_bot_content()
        finally:
            app.config['BOT_CONTENT_RETENTION'] = original

        assert recorder.calls == []

    def test_the_deletion_federates_for_a_local_author(self, db_session, monkeypatch):
        """`:184` passes `post.author.is_local()` for `federate_deletion`,
        where `remove_old_community_content:150` passes a constant False.
        """
        recorder = _Recorder()
        monkeypatch.setattr('app.shared.tasks.maintenance.delete_post', recorder)
        instance, user, community, post = _seed()
        self._bot_post(community, user, 'https://peer.example/p/2', 28 * 6 + 1)
        db.session.commit()

        remove_old_bot_content()

        assert recorder.calls[0][1] is True

    def test_a_failure_inside_the_task_propagates(self, db_session, monkeypatch):
        monkeypatch.setattr('app.shared.tasks.maintenance.utcnow', _boom)

        with pytest.raises(RuntimeError, match='the task itself failed'):
            remove_old_bot_content()
```

- [ ] **Step 2: Run**

```bash
./run_tests.sh tests/test_shared_tasks_maintenance_lifecycle.py -v
```
Expected: 27 passed.

`test_the_deletion_federates_for_a_local_author` asserts `True` because `_seed()`'s user is local. If it comes out `False`, do not flip the expectation — check whether `make_user(..., local=True)` still leaves `ap_id` None, and report what you found.

- [ ] **Step 3: Commit**

Subject: `test: cover both retention tasks and what they hand to delete_post`

---

## Task 4: `delete_old_soft_deleted_content`

**Files:**
- Modify: `tests/test_shared_tasks_maintenance_lifecycle.py`
- Read: `app/shared/tasks/maintenance.py:215-279`

**Interfaces:**
- Consumes: `_seed()`, `_boom`.
- Produces: nothing later tasks depend on.

Five branch points: `:252`'s loop, `:254`'s `if post and (post.image_id is None or post.image_id not in images_used_by_many_posts):`, `:267`'s loop, `:269`'s `if post_reply:`, `:271`'s `if not post_reply.has_replies(include_deleted=True):`.

`:254` is a **three-condition compound** and coverage.py sees one arc pair (fact 142) — branch coverage will read 100% with two of the three untested. Each gets its own test.

- [ ] **Step 1: Write the tests**

```python
class TestDeleteOldSoftDeletedContent:
    """`delete_old_soft_deleted_content:215` -- hard-delete after seven days.

    `:225-236` selects soft-deleted posts past the cutoff that are unbookmarked
    and either mod-deleted, retention-deleted (`deleted_by = 1`) or replyless.
    `:242-250` collects image ids shared by more than one post. `:254`'s
    compound then skips any post whose image is shared, because the cascade
    would fail -- the source comment at `:238-241` says so and defers the real
    fix.

    `:254` IS THREE CONDITIONS IN ONE ARC PAIR. coverage.py does not decompose
    conjunctions (fact 142), so branch coverage reads 100% with two of them
    untested. Each has its own test below.
    """

    def _soft_deleted_post(self, community, user, ap_id, age_days=8):
        post = make_post(community, user, ap_id)
        post.deleted = True
        post.deleted_by = 1
        post.reply_count = 0
        post.posted_at = utcnow() - timedelta(days=age_days)
        return post

    def test_an_old_soft_deleted_post_is_hard_deleted(self, db_session):
        instance, user, community, post = _seed()
        gone = self._soft_deleted_post(community, user, 'https://peer.example/p/2')
        db.session.commit()

        delete_old_soft_deleted_content()

        db.session.expire_all()
        assert db.session.get(Post, gone.id) is None

    def test_a_recently_deleted_post_survives(self, db_session):
        """The BOUNDARY -- `:228`'s `p.posted_at < :cutoff`, seven days."""
        instance, user, community, post = _seed()
        recent = self._soft_deleted_post(community, user,
                                         'https://peer.example/p/2', age_days=6)
        db.session.commit()

        delete_old_soft_deleted_content()

        db.session.expire_all()
        assert db.session.get(Post, recent.id) is not None

    def test_a_post_whose_image_is_shared_survives(self, db_session):
        """`:254`'s third condition, and the reason `:242-250` exists.

        Two posts referencing one File make the delete cascade fail, so the
        task deliberately skips them. The source comment at `:238-241` records
        that this is a workaround rather than a fix.
        """
        instance, user, community, post = _seed()
        shared = make_file(file_path='/static/shared.png')
        first = self._soft_deleted_post(community, user, 'https://peer.example/p/2')
        first.image_id = shared.id
        second = make_post(community, user, 'https://peer.example/p/3')
        second.image_id = shared.id
        db.session.commit()

        delete_old_soft_deleted_content()

        db.session.expire_all()
        assert db.session.get(Post, first.id) is not None

    def test_a_post_with_its_own_image_is_deleted(self, db_session):
        """`:254`'s second condition taking its false arm while the third takes
        its true arm -- the image exists but is not shared.
        """
        instance, user, community, post = _seed()
        own = make_file(file_path='/static/own.png')
        gone = self._soft_deleted_post(community, user, 'https://peer.example/p/2')
        gone.image_id = own.id
        db.session.commit()

        delete_old_soft_deleted_content()

        db.session.expire_all()
        assert db.session.get(Post, gone.id) is None

    def test_a_bookmarked_post_survives(self, db_session):
        """`:229-233`'s NOT EXISTS against post_bookmark."""
        instance, user, community, post = _seed()
        kept = self._soft_deleted_post(community, user, 'https://peer.example/p/2')
        db.session.commit()
        db.session.add(PostBookmark(post_id=kept.id, user_id=user.id))
        db.session.commit()

        delete_old_soft_deleted_content()

        db.session.expire_all()
        assert db.session.get(Post, kept.id) is not None

    def test_an_old_soft_deleted_reply_is_hard_deleted(self, db_session):
        """`:260-273`'s second pass, which no post test reaches."""
        instance, user, community, post = _seed()
        reply = make_post_reply(post, user)
        reply.deleted = True
        reply.posted_at = utcnow() - timedelta(days=8)
        db.session.commit()

        delete_old_soft_deleted_content()

        db.session.expire_all()
        assert db.session.get(PostReply, reply.id) is None

    def test_a_reply_with_children_survives(self, db_session):
        """`:271`'s `if not post_reply.has_replies(include_deleted=True):`.

        A deleted reply that still has children is kept, because removing it
        would orphan them.
        """
        instance, user, community, post = _seed()
        parent = make_post_reply(post, user)
        parent.deleted = True
        parent.posted_at = utcnow() - timedelta(days=8)
        child = make_post_reply(post, user)
        child.parent_id = parent.id
        db.session.commit()

        delete_old_soft_deleted_content()

        db.session.expire_all()
        assert db.session.get(PostReply, parent.id) is not None

    def test_a_failure_inside_the_task_propagates(self, db_session, monkeypatch):
        monkeypatch.setattr('app.shared.tasks.maintenance.utcnow', _boom)

        with pytest.raises(RuntimeError, match='the task itself failed'):
            delete_old_soft_deleted_content()
```

- [ ] **Step 2: Run**

```bash
./run_tests.sh tests/test_shared_tasks_maintenance_lifecycle.py -v
```
Expected: 35 passed.

Two schema details these tests rest on are **verified against the tree**, so use them as written rather than re-deriving: `PostBookmark` (`app/models.py:3865-3869`) carries `user_id`, `post_id` and `created_at`, and `PostReply.parent_id` is the column `has_replies` reads — `app/models.py:3289` queries `filter_by(parent_id=self.id)` through `db.session`, which `:220`'s `patch_db_session` has redirected onto the task's session by the time it runs.

- [ ] **Step 3: Commit**

Subject: `test: cover the soft-delete sweep and its shared-image guard`

---

## Task 5: `archive_user` and `archive_old_users` coverage

**Files:**
- Modify: `tests/test_shared_tasks_maintenance_lifecycle.py`
- Read: `app/shared/tasks/maintenance.py:933-954`, `:957-970`

**Interfaces:**
- Consumes: `_seed()`, `_boom`.
- Produces: the tests Task 6 re-runs after changing the filter.

**Do not change `app/shared/tasks/maintenance.py` in this task.** Task 6 owns PC1 and must first show a failing test against the unmodified filter.

`File.delete_from_disk(purge_cdn=True)` (`app/models.py:421`) touches the filesystem. `make_file(file_path=..., source_url=...)` builds a row whose `file_path` points nowhere; establish in Step 1 whether `delete_from_disk` tolerates a missing file, and if it does not, monkeypatch it with a recorder and say so in the docstring.

- [ ] **Step 1: Establish what `delete_from_disk` does with a nonexistent path**

Write a throwaway test that calls `archive_user` on a user with one `make_file` avatar and paste the result. If it raises, monkeypatch `File.delete_from_disk` for the whole class and record why. Remove the throwaway before committing.

- [ ] **Step 2: Write the tests**

```python
class TestArchiveUser:
    """`archive_user:957` -- drop a user's avatar and cover.

    `:959` and `:964` guard the two images INDEPENDENTLY, so this helper
    handles a user with only one. `archive_old_users:942`'s query does not --
    that mismatch is this round's PC1 and is NOT fixed here.
    """

    def test_both_images_are_removed(self, db_session):
        instance, user, _, _ = _seed()
        avatar = make_file(file_path='/static/avatar.png')
        cover = make_file(file_path='/static/cover.png')
        user.avatar_id = avatar.id
        user.cover_id = cover.id
        db.session.commit()

        archive_user(user.id, db.session)

        db.session.expire_all()
        refreshed = db.session.get(User, user.id)
        assert (refreshed.avatar_id, refreshed.cover_id) == (None, None)
        assert db.session.get(File, avatar.id) is None
        assert db.session.get(File, cover.id) is None

    def test_a_user_with_only_an_avatar_is_handled(self, db_session):
        """`:959` true, `:964` false. The helper copes; `archive_old_users`'
        query is what never sends it such a user.
        """
        instance, user, _, _ = _seed()
        avatar = make_file(file_path='/static/avatar.png')
        user.avatar_id = avatar.id
        db.session.commit()

        archive_user(user.id, db.session)

        db.session.expire_all()
        assert db.session.get(User, user.id).avatar_id is None

    def test_a_user_with_only_a_cover_is_handled(self, db_session):
        """`:959` false, `:964` true."""
        instance, user, _, _ = _seed()
        cover = make_file(file_path='/static/cover.png')
        user.cover_id = cover.id
        db.session.commit()

        archive_user(user.id, db.session)

        db.session.expire_all()
        assert db.session.get(User, user.id).cover_id is None


class TestArchiveOldUsers:
    """`archive_old_users:933` -- strip images from idle remote users.

    `:935` gates the whole body on `ARCHIVE_POSTS` (config.py:184, default 0),
    and `:938`'s cutoff is that value times 28 days. `:939-945` selects remote
    users idle past the cutoff, and `:947` hands each to `archive_user`.
    """

    def _idle_remote_user(self, name, *, avatar=True, cover=True, age_days=None):
        remote_instance = make_instance(f'{name}.example')
        user = make_user(remote_instance, name)
        if avatar:
            user.avatar_id = make_file(file_path=f'/static/{name}-a.png').id
        if cover:
            user.cover_id = make_file(file_path=f'/static/{name}-c.png').id
        user.last_seen = utcnow() - timedelta(days=age_days if age_days else 6 * 28 + 1)
        db.session.commit()
        return user

    def test_an_idle_remote_user_with_both_images_is_archived(self, db_session, app):
        make_instance('local.example')
        user = self._idle_remote_user('visitor')
        original = app.config['ARCHIVE_POSTS']
        app.config['ARCHIVE_POSTS'] = 6

        try:
            archive_old_users()
        finally:
            app.config['ARCHIVE_POSTS'] = original

        db.session.expire_all()
        refreshed = db.session.get(User, user.id)
        assert (refreshed.avatar_id, refreshed.cover_id) == (None, None)

    def test_a_recently_seen_user_is_skipped(self, db_session, app):
        """The BOUNDARY -- `:943`'s `u.last_seen < :cutoff`."""
        make_instance('local.example')
        user = self._idle_remote_user('recent', age_days=6 * 28 - 1)
        original = app.config['ARCHIVE_POSTS']
        app.config['ARCHIVE_POSTS'] = 6

        try:
            archive_old_users()
        finally:
            app.config['ARCHIVE_POSTS'] = original

        db.session.expire_all()
        assert db.session.get(User, user.id).avatar_id is not None

    def test_archiving_off_by_default_makes_the_task_a_no_op(self, db_session):
        """`:935`'s false arm. ARCHIVE_POSTS defaults to 0 (config.py:184), so
        this test sets no config at all -- the default IS the false arm.
        """
        make_instance('local.example')
        user = self._idle_remote_user('untouched')

        archive_old_users()

        db.session.expire_all()
        assert db.session.get(User, user.id).avatar_id is not None

    def test_a_local_user_is_skipped(self, db_session, app):
        """`:942`'s `u.ap_id IS NOT NULL`. A local user has no ap_id."""
        instance, user, _, _ = _seed()
        user.avatar_id = make_file(file_path='/static/local-a.png').id
        user.cover_id = make_file(file_path='/static/local-c.png').id
        user.last_seen = utcnow() - timedelta(days=6 * 28 + 1)
        db.session.commit()
        original = app.config['ARCHIVE_POSTS']
        app.config['ARCHIVE_POSTS'] = 6

        try:
            archive_old_users()
        finally:
            app.config['ARCHIVE_POSTS'] = original

        db.session.expire_all()
        assert db.session.get(User, user.id).avatar_id is not None

    def test_a_failure_inside_the_task_propagates(self, db_session, monkeypatch, app):
        monkeypatch.setattr('app.shared.tasks.maintenance.utcnow', _boom)
        original = app.config['ARCHIVE_POSTS']
        app.config['ARCHIVE_POSTS'] = 6

        try:
            with pytest.raises(RuntimeError, match='the task itself failed'):
                archive_old_users()
        finally:
            app.config['ARCHIVE_POSTS'] = original
```

- [ ] **Step 3: Run**

```bash
./run_tests.sh tests/test_shared_tasks_maintenance_lifecycle.py -v
```
Expected: 43 passed.

- [ ] **Step 4: Commit**

Subject: `test: cover the user-archiving pair and its config gate`

---

## Task 6: PC1 — observe the one-image gap, then widen the filter

**Files:**
- Modify: `app/shared/tasks/maintenance.py:942`
- Modify: `tests/test_shared_tasks_maintenance_lifecycle.py`

**Interfaces:**
- Consumes: Task 5's tests, all of which must still pass.
- Produces: an `OR` between the two image conditions.

**The test must FAIL against the unmodified code before you change anything.** If it passes, STOP, set your status to `DONE_WITH_CONCERNS`, and report — the round then lands two production changes, which is the correct outcome.

- [ ] **Step 1: Write the test and watch it fail**

```python
class TestArchiveOldUsersReachesOneImageUsers:
    """PC1: `:942` requires BOTH images, `archive_user` requires either.

    `:942` filters `u.avatar_id IS NOT NULL AND u.cover_id IS NOT NULL`, but
    `archive_user:959` and `:964` guard the two independently. A remote user
    with an avatar and no cover is never selected, however long they have been
    idle, though the helper that would process them handles that case.
    """

    def test_an_idle_remote_user_with_only_an_avatar_is_archived(self, db_session, app):
        make_instance('local.example')
        remote_instance = make_instance('remote.example')
        user = make_user(remote_instance, 'oneimage')
        avatar = make_file(file_path='/static/one-a.png')
        user.avatar_id = avatar.id
        user.last_seen = utcnow() - timedelta(days=6 * 28 + 1)
        db.session.commit()
        original = app.config['ARCHIVE_POSTS']
        app.config['ARCHIVE_POSTS'] = 6

        try:
            archive_old_users()
        finally:
            app.config['ARCHIVE_POSTS'] = original

        db.session.expire_all()
        assert db.session.get(User, user.id).avatar_id is None
```

Run:
```bash
./run_tests.sh "tests/test_shared_tasks_maintenance_lifecycle.py::TestArchiveOldUsersReachesOneImageUsers" -v
```
Expected: FAIL — the avatar is still attached. **Paste the exact assertion output.**

- [ ] **Step 2: Make the change**

`:942` becomes:

```sql
                    WHERE (u.avatar_id IS NOT NULL OR u.cover_id IS NOT NULL) AND u.ap_id IS NOT NULL
```

The parentheses matter: without them the `OR` would bind loosely and pull in local users. `u.last_seen < :cutoff` on the following line is untouched.

- [ ] **Step 3: Run the whole file**

```bash
./run_tests.sh tests/test_shared_tasks_maintenance_lifecycle.py -v
```
Expected: 44 passed. Task 5's `test_a_local_user_is_skipped` is the one most at risk from a mis-parenthesised `OR`; if it fails, the change went wider than intended.

- [ ] **Step 4: Re-derive citations, then commit**

Your edit changes one line's content but not the file's length. Confirm that with `wc -l` and re-check every citation into `archive_old_users` in the test file.

Subject: `fix: archive idle remote users that have only one of the two images`

Body quotes the Step 1 failure and states the scope limit: `:959` and `:964` are unchanged, so what is done to a selected user is the same.

---

## Task 7: `archive_old_posts`, including both S3 arms

**Files:**
- Modify: `tests/test_shared_tasks_maintenance_lifecycle.py`
- Read: `app/shared/tasks/maintenance.py:885-929`

**Interfaces:**
- Consumes: `_seed()`, `_boom`, `_Recorder`.
- Produces: nothing later tasks depend on.

Four branch points: `:887`'s config gate, `:910`'s `if store_files_in_s3():`, `:919`'s loop, `:922`'s `if s3:`.

**Replace `archive_post` with a `_Recorder`** — `monkeypatch.setattr('app.shared.tasks.maintenance.archive_post', recorder)`. The real one opens its own session (`app/utils.py:4946`) and would move files. The oracle for the S3 arms is **whether the recorder received a client or `None`**.

`store_files_in_s3()` (`app/utils.py:4317-4319`) is true only when all three of `S3_ACCESS_KEY`, `S3_ACCESS_SECRET` and `S3_ENDPOINT` are non-empty; `config.py:103-107` defaults all three to `''`. Constructing a `boto3` client makes no network call, so the true arm is safe to take.

- [ ] **Step 1: Write the tests**

```python
class TestArchiveOldPosts:
    """`archive_old_posts:885` -- archive old posts out of the main tables.

    `:887` gates the body on ARCHIVE_POSTS (config.py:184, default 0).
    `:891-907`'s query excludes stickied posts, private and unarchivable
    communities, and each community's hundred most recent posts. `:910` decides
    whether an S3 client is built, `:920` hands each id to `archive_post`, and
    `:923` closes the client.

    `archive_post` IS REPLACED BY A RECORDER. The real one opens its own task
    session (app/utils.py:4946) and moves files; this round tests which ids
    reach it and whether it got a client, not what it does with either.
    """

    def _past_the_recency_window(self, community, user):
        """Fill the community's hundred-most-recent window, then add one old post.

        `:900-906` excludes each community's hundred most recent posts by
        `created_at`. A community with a hundred posts or fewer therefore has
        NOTHING archivable, and every "this post is skipped" assertion below
        would pass whatever the task did. These tests seed a hundred recent
        posts to fill that window and one post old enough to fall outside it
        AND past `:896`'s cutoff.

        The hundred fillers are staggered a day apart so the ordering has no
        ties, and they are added in one `add_all` rather than a hundred
        committing factory calls.

        Returns the one post that is genuinely archivable.
        """
        now = utcnow()
        fillers = [
            Post(community_id=community.id, user_id=user.id,
                 instance_id=user.instance_id, title='filler',
                 ap_id=f'https://peer.example/fill/{i}',
                 created_at=now - timedelta(days=i),
                 posted_at=now - timedelta(days=i),
                 last_active=now - timedelta(days=i))
            for i in range(100)
        ]
        old = Post(community_id=community.id, user_id=user.id,
                   instance_id=user.instance_id, title='old',
                   ap_id='https://peer.example/old',
                   created_at=now - timedelta(days=6 * 28 + 1),
                   posted_at=now - timedelta(days=6 * 28 + 1),
                   last_active=now - timedelta(days=6 * 28 + 1))
        db.session.add_all(fillers + [old])
        db.session.commit()
        return old

    def test_a_post_beyond_the_recency_window_is_handed_over(self, db_session, monkeypatch, app):
        recorder = _Recorder()
        monkeypatch.setattr('app.shared.tasks.maintenance.archive_post', recorder)
        instance, user, community, post = _seed()
        old = self._past_the_recency_window(community, user)
        original = app.config['ARCHIVE_POSTS']
        app.config['ARCHIVE_POSTS'] = 6

        try:
            archive_old_posts()
        finally:
            app.config['ARCHIVE_POSTS'] = original

        assert [c[0] for c in recorder.calls] == [old.id]

    def test_a_community_inside_the_recency_window_archives_nothing(self, db_session, monkeypatch, app):
        """`:900-906`'s exclusion, and the test that stops the three filter
        tests below from passing vacuously.

        The same old post, in a community with only a handful of others, is not
        archived -- because it is still among that community's hundred most
        recent. Without this test a reader could not tell whether the filter
        tests below prove anything.
        """
        recorder = _Recorder()
        monkeypatch.setattr('app.shared.tasks.maintenance.archive_post', recorder)
        instance, user, community, post = _seed()
        old = make_post(community, user, 'https://peer.example/old')
        old.created_at = utcnow() - timedelta(days=6 * 28 + 1)
        db.session.commit()
        original = app.config['ARCHIVE_POSTS']
        app.config['ARCHIVE_POSTS'] = 6

        try:
            archive_old_posts()
        finally:
            app.config['ARCHIVE_POSTS'] = original

        assert recorder.calls == []

    def test_archiving_off_by_default_makes_the_task_a_no_op(self, db_session, monkeypatch):
        """`:887`'s false arm, which is the default configuration."""
        recorder = _Recorder()
        monkeypatch.setattr('app.shared.tasks.maintenance.archive_post', recorder)
        instance, user, community, post = _seed()
        self._past_the_recency_window(community, user)

        archive_old_posts()

        assert recorder.calls == []

    def test_a_sticky_post_is_skipped(self, db_session, monkeypatch, app):
        """`:897`'s `p.sticky = false`."""
        recorder = _Recorder()
        monkeypatch.setattr('app.shared.tasks.maintenance.archive_post', recorder)
        instance, user, community, post = _seed()
        old = self._past_the_recency_window(community, user)
        old.sticky = True
        db.session.commit()
        original = app.config['ARCHIVE_POSTS']
        app.config['ARCHIVE_POSTS'] = 6

        try:
            archive_old_posts()
        finally:
            app.config['ARCHIVE_POSTS'] = original

        assert recorder.calls == []

    def test_a_post_in_an_unarchivable_community_is_skipped(self, db_session, monkeypatch, app):
        """`:898`'s `c.can_be_archived = true` (app/models.py:588)."""
        recorder = _Recorder()
        monkeypatch.setattr('app.shared.tasks.maintenance.archive_post', recorder)
        instance, user, community, post = _seed()
        self._past_the_recency_window(community, user)
        community.can_be_archived = False
        db.session.commit()
        original = app.config['ARCHIVE_POSTS']
        app.config['ARCHIVE_POSTS'] = 6

        try:
            archive_old_posts()
        finally:
            app.config['ARCHIVE_POSTS'] = original

        assert recorder.calls == []

    def test_a_post_in_a_private_community_is_skipped(self, db_session, monkeypatch, app):
        """`:899`'s `c.private = false` (app/models.py:611)."""
        recorder = _Recorder()
        monkeypatch.setattr('app.shared.tasks.maintenance.archive_post', recorder)
        instance, user, community, post = _seed()
        self._past_the_recency_window(community, user)
        community.private = True
        db.session.commit()
        original = app.config['ARCHIVE_POSTS']
        app.config['ARCHIVE_POSTS'] = 6

        try:
            archive_old_posts()
        finally:
            app.config['ARCHIVE_POSTS'] = original

        assert recorder.calls == []

    def test_no_s3_client_is_built_when_object_storage_is_off(self, db_session, monkeypatch, app):
        """`:910`'s FALSE arm, which is the default configuration
        (config.py:103-107 default all three S3 settings to '').

        `archive_post` receives None, and `:922`'s `if s3:` is false so `:923`
        never runs.
        """
        recorder = _Recorder()
        monkeypatch.setattr('app.shared.tasks.maintenance.archive_post', recorder)
        instance, user, community, post = _seed()
        self._past_the_recency_window(community, user)
        original = app.config['ARCHIVE_POSTS']
        app.config['ARCHIVE_POSTS'] = 6

        try:
            archive_old_posts()
        finally:
            app.config['ARCHIVE_POSTS'] = original

        assert recorder.calls
        assert all(c[1] is None for c in recorder.calls)

    def test_an_s3_client_is_built_and_passed_when_configured(self, db_session, monkeypatch, app):
        """`:910`'s TRUE arm, and `:922`'s.

        Constructing a boto3 client makes no network call, so setting the three
        config values is enough and no endpoint is contacted. The oracle is
        that `archive_post` received something other than None.
        """
        recorder = _Recorder()
        monkeypatch.setattr('app.shared.tasks.maintenance.archive_post', recorder)
        instance, user, community, post = _seed()
        self._past_the_recency_window(community, user)
        originals = {k: app.config[k] for k in
                     ('ARCHIVE_POSTS', 'S3_ACCESS_KEY', 'S3_ACCESS_SECRET',
                      'S3_ENDPOINT', 'S3_REGION')}
        app.config['ARCHIVE_POSTS'] = 6
        app.config['S3_ACCESS_KEY'] = 'key'
        app.config['S3_ACCESS_SECRET'] = 'secret'
        app.config['S3_ENDPOINT'] = 'https://s3.example'
        app.config['S3_REGION'] = 'us-east-1'

        try:
            archive_old_posts()
        finally:
            for k, v in originals.items():
                app.config[k] = v

        assert recorder.calls
        assert all(c[1] is not None for c in recorder.calls)

    def test_a_failure_inside_the_task_propagates(self, db_session, monkeypatch, app):
        monkeypatch.setattr('app.shared.tasks.maintenance.utcnow', _boom)
        original = app.config['ARCHIVE_POSTS']
        app.config['ARCHIVE_POSTS'] = 6

        try:
            with pytest.raises(RuntimeError, match='the task itself failed'):
                archive_old_posts()
        finally:
            app.config['ARCHIVE_POSTS'] = original
```

- [ ] **Step 2: Run**

```bash
./run_tests.sh tests/test_shared_tasks_maintenance_lifecycle.py -v
```
Expected: 53 passed.

**Do not shorten `_past_the_recency_window`.** A hundred filler posts looks like a lot to seed, and it is the whole reason these tests measure anything: `:900-906` excludes each community's hundred most recent posts, so with fewer than that NOTHING is archivable and every "is skipped" assertion passes against a task that archives nothing. `test_a_community_inside_the_recency_window_archives_nothing` exists to prove that is what would otherwise happen. One `add_all` of 101 rows is a single round trip.

- [ ] **Step 3: Commit**

Subject: `test: cover archive_old_posts and both of its object-storage arms`

---

## Task 8: PC2 — try to observe `pwn_bots`' split session

**Files:**
- Possibly modify: `app/shared/tasks/maintenance.py:1165`
- Modify: `tests/test_shared_tasks_maintenance_lifecycle.py` (only if a test can discriminate)

**Interfaces:**
- Consumes: Task 1's `pwn_bots` tests.
- Produces: either a `patch_db_session` wrapper, or a register-only finding.

**This task is expected to possibly produce no change, and that is a success.** `pwn_bots:1166` reads through `BotChallenge.query` — bound to `db.session` — while `:1167` and `:1170` write through the task's own session, with no `patch_db_session` wrapper. Every sibling that mixes the two wraps: `remove_old_community_content:140`, `remove_old_bot_content:165`, `delete_old_soft_deleted_content:220`.

- [ ] **Step 1: Try to write a test that distinguishes patched from unpatched**

`tests/conftest.py:112` pushes an app context, so `BotChallenge.query` resolves and reads the same committed database the task writes to. Spend a bounded effort — no more than three attempts — trying to find an observable difference. Candidates worth trying, and worth recording as tried:

1. A challenge row committed by `db.session` but with the task's session holding an uncommitted conflicting write.
2. Asserting on which connection issues the SELECT, via a `before_cursor_execute` event listener on `db.engine` that records `conn`.
3. Adding the wrapper and checking whether any existing test changes behaviour.

- [ ] **Step 2: Report what you found, and take one of two paths**

**If you found a discriminating test:** it must FAIL against the unmodified code. Paste the failure, then wrap the body — `:1165`'s `try:` gains `with patch_db_session(session):` beneath it and the body indents one level — and re-run.

**If you could not:** stop. Do not add the wrapper. Write into your report exactly what you tried and why each attempt could not distinguish the two, and state that PC2 becomes a register-only finding. **This is the expected outcome and is not a failure of the task.** The round then lands two production changes rather than three, which the spec anticipates.

Sub-project 29 hit this same wall with `patch_db_session` and recorded it honestly rather than pretending a test proved it; that episode is fact 163's neighbour and this task follows it.

- [ ] **Step 3: Commit only if you changed something**

Subject if changed: `fix: read pwn_bots' challenges through the task's own session`
If unchanged, there is nothing to commit — the finding goes to Task 12.

---

## Task 9: PC3 — the two missing decorators

**Files:**
- Modify: `app/shared/tasks/maintenance.py:160`, `:1161`

**Interfaces:**
- Consumes: nothing.
- Produces: `remove_old_bot_content` and `pwn_bots` dispatchable with `.delay()`.

- [ ] **Step 1: Establish the asymmetry, and show it**

```bash
grep -n -B1 '^def ' app/shared/tasks/maintenance.py | grep -A1 '^[0-9]*-@celery.task' | head -40
```

Paste the output. Confirm that `remove_old_bot_content` and `pwn_bots` are the only two zero-argument module-level functions in this file without the decorator, and that `archive_user(user_id, session)` and `add_remote_community_from_post(post_data)` are undecorated because they take arguments and are helpers.

- [ ] **Step 2: Add the decorators**

Add `@celery.task` on the line above `def remove_old_bot_content():` and above `def pwn_bots():`.

- [ ] **Step 3: Run the file and confirm nothing changed behaviourally**

```bash
./run_tests.sh tests/test_shared_tasks_maintenance_lifecycle.py -v
```
Expected: the same count as Task 7 left (plus anything Task 8 added), all passing. Under `task_always_eager` a decorated task called directly still executes its body (fact 146), so every existing test must be unaffected. **If any test changes behaviour, that is the finding — report it rather than adapting the test.**

- [ ] **Step 4: Re-derive citations**

Adding two lines shifts everything below each one. `remove_old_bot_content` at `:160` pushes the rest of the file down by one; `pwn_bots` by one more. **Sweep the whole test file for citations into `maintenance.py` and correct every one that moved** — not only the ones you wrote. Sub-project 29 left five stale citations behind by skipping this sweep, and a later sweep found three more.

- [ ] **Step 5: Commit**

Subject: `refactor: give remove_old_bot_content and pwn_bots the celery task decorator`

Body must state the scope limit plainly: the decorator makes each function dispatchable with `.delay()`; it does **not** schedule either. `remove_old_bot_content` is called at `app/cli.py:911` and nowhere else, inside the synchronous `daily_maintenance` (`app/cli.py:884-885`); it is absent from `daily_maintenance_celery`'s import list at `app/cli.py:812-820`. `pwn_bots` is imported at `app/cli.py:40`. **This round does not edit `app/cli.py`**, so neither function starts running anywhere new.

---

## Task 10: Measure coverage, close residuals, raise the floor

**Files:**
- Modify: `coverage_floors.ini`
- Modify: `tests/test_shared_tasks_maintenance_lifecycle.py` (whatever the measurement shows is missing)

- [ ] **Step 1: Bring the stack down, then measure**

```bash
./run_tests.sh --down
./run_tests.sh --cov=app.shared.tasks.maintenance --cov-branch \
    --cov-report=json:/app/coverage_b30.json \
    tests/test_shared_tasks_maintenance_lifecycle.py \
    tests/test_shared_tasks_maintenance_cleanup.py
echo "exit: $?"
```

**Both files**, because the floor is for the whole module and Group A's coverage counts toward it. Do not pipe.

- [ ] **Step 2: Check the report's freshness**

The JSON appears in the host repo root because `/app` is bind-mounted (fact 168). `stat -c '%y' coverage_b30.json` and confirm the mtime postdates your run.

- [ ] **Step 3: List what is still missing inside Group B**

Derive the eight function ranges yourself from `grep -n '^def ' app/shared/tasks/maintenance.py` **at the moment you run this** — Tasks 6 and 9 have edited the file, and Task 9 added two lines. The eight functions are `process_expired_bans`, `remove_old_community_content`, `remove_old_bot_content`, `delete_old_soft_deleted_content`, `archive_old_posts`, `archive_old_users`, `archive_user`, `pwn_bots`.

```bash
python3 - <<'EOF'
import json
d = json.load(open('coverage_b30.json'))
f = d['files']['app/shared/tasks/maintenance.py']
group_b = []   # fill from the grep above: (start, end) per function
def in_b(n):
    return any(a <= n <= b for a, b in group_b)
print('missing statements in Group B:',
      sorted(n for n in f['missing_lines'] if in_b(n)))
print('partial branches in Group B:',
      sorted(k for k in f.get('missing_branches', []) if in_b(k[0])))
print('module percent_covered:', f['summary']['percent_covered'])
EOF
```

- [ ] **Step 4: Close whatever is left**

Write the test that reaches each missing statement or partial branch. **Do not mark anything `# pragma: no branch` on your own judgment** — write the proof into your report and let a reviewer try to defeat it (fact 160). Before closing anything, check whether an earlier sub-project already proved it unreachable (fact 159).

Task 7 covers `archive_old_posts:900-906`'s hundred-most-recent exclusion behaviourally, by seeding past it, so nothing in Group B is knowingly left unexercised going into this measurement.

- [ ] **Step 5: Raise the floor**

Edit `coverage_floors.ini`'s `app/shared/tasks/maintenance.py` line from `24` to the measured integer, **rounded DOWN**. Group A plus Group B is 303 of 634 statements, so expect roughly 48 — but write what you measured. Floors only ever rise.

- [ ] **Step 6: Delete the coverage JSON you created and commit**

```bash
rm coverage_b30.json
git status --porcelain -uall
```

Subject: `test: raise maintenance.py's floor for Group B`

---

## Task 11: Mutation testing

**Files:**
- Modify: none permanently. Every mutation is applied and restored.

**Every line number below is advisory and pre-shift.** Tasks 6 and 9 edited the file; Task 9 added two lines. Re-derive each site against the tree as it stands now, and read the dry-run's produced line before applying.

Mutations run **one at a time**: dry-run without `-i` and read the line, apply, run, restore with `git checkout -- app/`, then assert an empty `git diff -- app/` and the expected `wc -l`. Restore before any point where you might stop.

- [ ] **Step 1: Run these mutations, in this order**

| # | Site | Mutation | Expected |
|---|------|----------|----------|
| 1 | `pwn_bots`' cutoff | `days=1` → `days=100` | killed |
| 2 | `pwn_bots`' null guard | `BotChallenge.is_a_bot == None` → `!= None` | killed |
| 3 | `process_expired_bans`' comparison | `CommunityBan.ban_until < utcnow()` → `>` | killed |
| 4 | `process_expired_bans`' membership guard | `if community_membership_record:` → `if not community_membership_record:` | killed |
| 5 | `process_expired_bans`' locality conjunct | delete `and blocked.is_local()` | killed |
| 5b | `process_expired_bans`' ban deletion | re-indent `session.delete(expired_ban)` INTO the `if community_membership_record:` block above it | killed by `test_a_ban_with_no_membership_row_still_clears` -- an indentation mutant, which leaves the ban uncleared whenever no membership row exists |
| 6 | `process_expired_bans`' instance-ban null conjunct | delete `InstanceBan.banned_until != None,` | **expected to SURVIVE — equivalent.** `banned_until < utcnow()` already yields UNKNOWN for NULL. Record the proof. |
| 7 | `remove_old_community_content`'s retention filter | `Community.content_retention > 0` → `>= 0` | killed |
| 8 | `remove_old_community_content`'s sticky filter | `sticky=False` → `sticky=True` | killed |
| 9 | `remove_old_community_content`'s federation flag | `delete_post(post_id, False, ...)` → `True` | killed |
| 10 | `remove_old_bot_content`'s gate | `if bot_retention > 0:` → `>= 0` | killed |
| 11 | `remove_old_bot_content`'s cutoff multiplier | `28 * bot_retention` → `280 * bot_retention` | killed |
| 12 | `remove_old_bot_content`'s reply filter | `reply_count=0` → `reply_count=1` | killed |
| 13 | `remove_old_bot_content`'s federation flag | `post.author.is_local()` → `False` | killed |
| 14 | `delete_old_soft_deleted_content`'s cutoff | `days=7` → `days=70` | killed |
| 15 | `delete_old_soft_deleted_content`'s shared-image conjunct | delete `post.image_id not in images_used_by_many_posts` | killed |
| 16 | `delete_old_soft_deleted_content`'s child guard | `if not post_reply.has_replies(...)` → drop the `not` | killed |
| 17 | `archive_old_posts`' config gate | `> 0` → `>= 0` | killed |
| 18 | `archive_old_posts`' S3 guard | `if store_files_in_s3():` → `if not store_files_in_s3():` | killed |
| 19 | `archive_old_posts`' sticky filter | `p.sticky = false` → `p.sticky = true` | killed |
| 20 | `archive_old_users`' config gate | `> 0` → `>= 0` | killed |
| 21 | PC1's widened filter | the `OR` back to `AND` | killed by Task 6's test |
| 22 | `archive_user`'s avatar guard | `if user.avatar_id:` → `if not user.avatar_id:` | killed |
| 23 | `archive_user`'s cover guard | `if user.cover_id:` → `if not user.cover_id:` | killed |

- [ ] **Step 2: For every survivor, decide which of two things it is**

A survivor with a proof is information about the code — record the proof and call it an equivalent mutant. A survivor without one is a hole; write the test that kills it.

**Before recording any survivor as equivalent, check the site against the test this table names as its killer** (fact 171). A survivor at the wrong site looks exactly like a survivor at the right one, and sub-project 29 nearly recorded a false equivalence that way.

- [ ] **Step 3: Restore and verify**

```bash
git diff --stat -- app/
wc -l app/shared/tasks/maintenance.py
```

Empty diff, and the line count Tasks 6 and 9 left behind.

- [ ] **Step 4: Commit only if a survivor made you write a test**

Subject: `test: kill the surviving mutants in maintenance Group B`

---

## Task 12: Register the findings and the facts

**Files:**
- Modify: `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`
- Modify: `tests/README.md`

- [ ] **Step 1: Confirm the next free numbers**

```bash
grep -oE 'D3[0-9]{2}' docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md | sort -u | tail -3
grep -n 'Next free number' docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md | tail -1
grep -oE '^\*\*1[0-9][0-9]\.' tests/README.md | tail -3
```

The plan says D351 and fact 172; verify rather than trusting it.

- [ ] **Step 2: Write the findings, from D351**

- **The one-image archiving gap (PC1)** — what was observed in Task 6, what changed, and the scope limit that `:959`/`:964` are unchanged.
- **`pwn_bots`' split-session read (PC2)** — with the outcome Task 8 actually reached. If no test could distinguish it, say that plainly and say what was tried, rather than recording it as fixed or as unimportant.
- **The two missing decorators (PC3)** — with the scope limit that dispatchable is not scheduled, and that `app/cli.py` was not edited.
- **Four in-loop commits of D342's shape**, at `process_expired_bans:117`, `delete_old_soft_deleted_content:257` and `:273`, and `archive_user:970`. **Registered, not fixed**, with the reasoning: D342's own entry discloses that fixing that pattern cost an all-or-nothing starvation regression, and these four sites delete content rather than recompute counters, so a partial run fails differently and worse to get wrong.
- **`remove_old_bot_content` never commits** — no `session.commit()` anywhere in its body; it relies entirely on `delete_post`'s internal commits. Correct as written; recorded so a future edit adding a direct write knows the write would be lost.
- **The two `delete_post` call sites pass different federation policies, and both are defensible.** `delete_post`'s second parameter is `federate_deletion` (`app/shared/post.py:755`), not a locality flag. Recorded because the sites look inconsistent to a reader who has not opened the signature — which is how this round first read them.
- **`archive_old_posts:900-906` makes a community with a hundred posts or fewer entirely unarchivable**, which is a correct reading of the exclusion and a trap for anyone testing this task: three filter assertions would pass vacuously against a community that had nothing archivable to begin with. The plan's first draft contained exactly that trap and it was caught in the pre-flight scan rather than by a test. Register the shape, not just the line.
- **`process_expired_bans` invalidates six memoized caches** (`:111-114`, `:121-122`) and no test asserts on them, deliberately — asserting on a shared Flask-Caching instance would couple this file to another subsystem.
- **Groups C and D remain**, with their function lists and statement counts from the spec, so the next round does not re-derive them.

- [ ] **Step 3: Write the new facts, from 172**

At minimum:
- Whether `File.delete_from_disk` tolerates a nonexistent path, which Task 5 Step 1 established.
- The `_Recorder` idiom for a callable that belongs to another module — replacing it in the *calling* module's namespace, so the test asserts on the handover rather than on the callee's behaviour.
- Whatever Task 8 found about testing `patch_db_session` omissions in-process.
- That `boto3` client construction makes no network call, so an S3-configured arm can be taken in tests without contacting an endpoint.
- That `PostReply.has_replies` (`app/models.py:3287-3292`) reads through `db.session`, so it only sees the task's rows because `delete_old_soft_deleted_content:220` wrapped the body in `patch_db_session` — a helper whose correctness depends on its caller's wrapper.

- [ ] **Step 4: Commit**

Subject: `docs: register the maintenance Group B findings and facts`

---

## Self-Review

**Spec coverage.** Every spec section maps to a task: the eight-function table to Tasks 1-7; PC1 to Task 6; PC2 to Task 8; PC3 to Task 9; the five register-only findings to Task 12 Step 2; the `delete_post`-is-arranged rule to Tasks 3 and 7; the S3 arrangement to Task 7; the config capture-and-restore rule to Tasks 3, 5 and 7; the floor to Task 10; the mutations to Task 11; the facts to Task 12 Step 3.

**Placeholder scan.** No task says "add appropriate tests". Every code step carries its code. Task 10 Step 3's `group_b` list is deliberately left for the implementer to fill from a live `grep`, because Tasks 6 and 9 shift the file and a written range would be stale — the step says so and gives the command.

**Type consistency.** `_seed()` returns `(instance, user, community, post)` and every caller unpacks four. `_Recorder` is defined once in Task 1 and used in Tasks 3 and 7 with the same `calls` attribute holding positional-argument tuples. `_boom` takes `*args, **kwargs` because it substitutes for `utcnow` (no arguments). `archive_user(user_id, session)` is called with two arguments in Task 5, matching `app/shared/tasks/maintenance.py:957`.

**Three risks the plan carries deliberately.** Task 6 opens with a test that must FAIL and tells the implementer to stop if it passes. Task 8 is written to accept "no change" as its successful outcome. Task 5 Step 1 is a probe whose answer the plan does not presume, because `File.delete_from_disk` touching a missing path is not something this round has established.

# Coverage sub-project 41 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Take `app/shared/reply.py` Groups E and F — six functions, 105 statements and 58 arcs, all currently at zero coverage — to zero missing statements and zero missing branch arcs, and close two live API authorization defects found while scoping.

**Architecture:** One new test file, `tests/test_shared_reply_moderation.py`, class-per-function, modelled on `tests/test_shared_post_moderation.py` (which covers the direct twins) and reusing the harness `tests/test_shared_reply_interactions.py` established. Behaviour is pinned first, the two production fixes land after, and the pinning tests are inverted in the same commits as their fixes.

**Tech Stack:** Flask, SQLAlchemy 2.0.52, pytest, podman-compose, coverage.py with branch coverage.

**Spec:** `docs/superpowers/specs/2026-09-13-coverage-reply-ef-41-design.md` — read it, including the **AMENDED 2026-09-13** block on finding 3, which raised the round's production budget from one change to two.

## Global Constraints

- **There is NO host Python with flask or pytest.** Everything runs through `./run_tests.sh <args>`, which is `podman compose exec -T test-runner pytest "$@"` (`run_tests.sh:85`). **`run_tests.sh` has NO `--exec` flag** — passing one is rejected with pytest exit 4.
- **Container Python must be INLINED:** `podman-compose -f compose.test.yaml exec -T test-runner python -c "..."`. Staging a script through the host's `/tmp` and reading it in the container fails — different filesystems.
- **Coverage takes the DOTTED module form:** `--cov=app.shared.reply`. A path form collects nothing, writes no JSON and exits 0 — a silent green failure.
- **Write coverage JSON outside the repository** (`/tmp/...`); `/app` is bind-mounted.
- **`pytest` exits 1 on a session timeout and `run_tests.sh` propagates it.** A shell **pipeline** eats the status — read `${PIPESTATUS[0]}`, or do not pipe. A `>` redirect is safe.
- **Only the controller runs the full suite**, one pytest session at a time, in the foreground. No task runs it.
- **Test counts come from pytest's own collection output**, never from a number in this plan.
- **`git diff --quiet -- app/` is THE load-bearing tree check, not `wc -l`.** A same-line-count replacement is invisible to a line count.
- **`git checkout -- app/` is BANNED as a mutation restore in this round.** The round holds uncommitted production changes for part of its life, so HEAD is not the correct baseline; that idiom silently reverted a real fix in sub-project 40. Use a reverse `sed` and verify the line with numbered output.
- **Delete nothing the task did not create.**
- **Mutations:** one at a time, line-scoped `sed`, dry-run and read the line first, apply, run, restore, then assert `git diff --quiet -- app/` and the correct `wc -l`. **Restore before any point where you might stop and report.**
- **No duplicate test names.** This file holds class-based tests; the check must cover both forms: `grep -oE "^ *def (test_[a-z0-9_]+)" FILE | sed 's/^ *//' | sort | uniq -d` — the `0-9` matters.
- **No ordered assertions over rows a query planner returned.** Compare sets.
- **Every line number re-derived** with numbered output: `awk 'NR>=X && NR<=Y {printf "%d\t%s\n",NR,$0}' FILE`. **Verify every citation mechanically and paste the proof.**
- **Re-derive an inherited premise with a SECOND METHOD THAT FAILS DIFFERENTLY**, not a second grep. The interactive `grep` here is a `ugrep` wrapper carrying `--ignore-files` and silently skips gitignored paths; `/usr/bin/grep` does not.
- **A claim is graded where it is read.** Re-read each docstring you touch in full before committing and ask what premise your change invalidated elsewhere in it. Sweep the artefact in the same pass as the register.
- **Measurement-block checklist** for every pasted output: (a) does the label name the command or test that produced it, (b) does the paste support the conclusion beside it, (c) does any OTHER line in the same paste contradict it.
- **Dispatch no subagents** from within a task.
- Commit with `git commit -F <file>`, never `-m`. Lowercase `type:` subject prefix, normal English prose body. Trailers, last two lines, in this order:
  ```
  Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01BF7kVN2spWD86NYKWyGrVn
  ```

---

## File Structure

| File | Responsibility |
|---|---|
| `tests/test_shared_reply_moderation.py` | **Create.** All Group E and F tests. Module docstring carries the harness findings and the defect record; one class per production function. |
| `app/shared/reply.py` | **Modify, Task 7 only.** Two sites: `elif src == SRC_API: raise Exception('Does not have permission')` appended to `lock_post_reply` and `set_collapse_post_reply`. |
| `app/api/alpha/utils/reply.py` | **Modify, Task 7 only.** One site: an authorization guard in `post_reply_mark_as_answer`. |
| `coverage_floors.ini` | **Modify, Task 9 only.** Raise `app/shared/reply.py`. |
| `tests/README.md` | **Modify, Task 9 only.** Facts from 232. |
| `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` | **Modify, Task 9 only.** Register from D519, marker moved. |

Production line numbers below were re-derived at `903dab20`. **Re-derive them again in your own task** — Task 7 shifts every line after `:519`.

---

### Task 1: Harness probes and the file's spine

**Files:**
- Create: `tests/test_shared_reply_moderation.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `_seed_moderated_reply()`, `seed_moderator(s, user=None)`, `make_site_admin(user)`, `make_instance_admin(user, instance)`, `recording_task_selector()`. Every later task uses these names exactly.

- [ ] **Step 1: Probe, before writing any test**

Run each probe and paste its output into your report. **These are the plan's assumptions, and the plan expects at least one to be wrong.**

```bash
cd /home/blentz/git/pyfedi
awk 'NR>=414 && NR<=591 {printf "%d\t%s\n",NR,$0}' app/shared/reply.py
awk 'NR>=4782 && NR<=4800 {printf "%d\t%s\n",NR,$0}' app/utils.py
awk 'NR>=3564 && NR<=3585 {printf "%d\t%s\n",NR,$0}' app/utils.py
awk 'NR>=3908 && NR<=3935 {printf "%d\t%s\n",NR,$0}' app/models.py
```

Probe A — **does a factory user survive `get_recipient_language`?** `choose_answer:556` is `with force_locale(get_recipient_language(post_reply.user_id)):`. `get_recipient_language` reads `recipient.language_id` and then `Language.query.get(...)`. A factory user whose `language_id` points at no `Language` row would raise on `lang.code`. Write one throwaway test that calls `choose_answer` through SRC_API and records what happens. If it raises, the fix is to seed a `Language` row or clear `language_id`, and **that becomes a `tests/README.md` fact**.

Probe B — **does the `@>` cascade work on a factory-built path?** `lock_post_reply:503` is `where path @> ARRAY[:parent_id]`. `make_post_reply` does NOT set `path`. Seed a parent and a child whose `path` is `[0, parent.id, child.id]` — production's shape, `app/models.py:3053-3060` — and confirm the child's `replies_enabled` flips when the parent is locked. **A test that does not seed a descendant witnesses nothing about `:503`.**

Probe C — **what does `add_to_modlog:3570` type the row as?** It reads `actor.is_instance_admin() or actor.is_admin() or actor.is_staff()`. `tests/README.md` fact 214 records that an id-1 actor types as `admin` because `is_admin()` short-circuits. Confirm which user your seed makes id 1.

- [ ] **Step 2: Write the file's spine**

```python
"""Group E and F of app/shared/reply.py -- the moderator verbs and the
reply-only verbs.

WHAT THIS FILE COVERS, and every line number here was re-derived with
numbered output at the commit named in each task's report, never copied
from the plan:

    mod_remove_reply         :414   21 statements / 12 arcs
    mod_restore_reply        :450   21 / 12
    lock_post_reply          :486   24 / 14
    set_collapse_post_reply  :523   15 / 12
    choose_answer            :548   15 /  4
    unchoose_answer          :579    9 /  4

All six were at ZERO coverage when this file was created -- missing equal to
total for every one. There was no partial coverage to build on and no existing
test to read for the conventions, which is why the harness below is borrowed
wholesale from two files rather than derived here.

WHERE THE HARNESS COMES FROM. `tests/test_shared_post_moderation.py` covers
`lock_post`, `mod_remove_post` and `mod_restore_post` -- the direct twins -- and
`tests/test_shared_reply_interactions.py` covers Groups A and C of this module.
`seed_moderator`, `make_site_admin`, `make_instance_admin` and
`recording_task_selector` are transcribed from the former; `_seed_reply`'s
shape and the `make_site()` rule come from the latter.

THE `make_site()` RULE, in the form sub-project 40 corrected it to: a `Site`
row is needed for `render_template` OR for `can_downvote`, which reads
`Site.query.get(1)` at app/utils.py:2443 and dereferences it at `:2445` before
any source fork. Neither group here calls `can_downvote`, so a `Site` row is
needed only where a template renders.
"""

import pytest
from contextlib import contextmanager
from types import SimpleNamespace

from app import db
from app.constants import SRC_API, SRC_WEB
from app.models import ModLog, Notification, PostReply
from app.shared.reply import (
    choose_answer, lock_post_reply, mod_remove_reply, mod_restore_reply,
    set_collapse_post_reply, unchoose_answer,
)
from tests.factories import (
    bearer, make_community, make_community_member, make_instance, make_post,
    make_post_reply, make_site, make_user, web_ctx,
)


def _seed_moderated_reply(*, private=True, community_name='moderation'):
    """One instance, one local user, one community, one post, one reply.

    Modelled on `tests/test_shared_reply_interactions.py:507`'s `_seed_reply`
    and carrying its ordering constraints: `make_community` hardcodes
    `instance_id=1` and `user_id=1` (tests/factories.py:141-142) and
    tests/conftest.py:131 resets every sequence after each test, so the
    instance minted first lands on id 1 and the community resolves to it.

    `author` is the reply's author and `actor` is the user who will moderate
    it. They are DISTINCT, and that is load-bearing rather than tidy:
    `mod_remove_reply:426` is
    `reply.deleted_by = user.id if user.id != reply.user_id else -1`, so a
    fixture where the actor IS the author can never witness the `user.id` arm.

    `private=True` sets `community.private`, the federation lever register
    entry D393(d) identifies: it stops the eager Celery task bodies at their
    first guard so no test issues an outbound request.
    """
    instance = make_instance('local.example', software='piefed')
    author = make_user(instance, 'author', local=True)
    actor = make_user(instance, 'actor', local=True)
    community = make_community(community_name)
    community.private = private
    db.session.commit()
    post = make_post(community, author, 'https://local.example/p/1')
    reply = make_post_reply(post, author)
    db.session.commit()
    return SimpleNamespace(instance=instance, author=author, actor=actor,
                           community=community, post=post, reply=reply)


def seed_moderator(s, user=None):
    """Make `user` (default `s.actor`) a moderator of `s.community`.

    `Community.moderators()` (app/models.py:716-722) filters
    `is_banned == False`, so a banned CommunityMember is NOT a moderator --
    an arm worth pinning separately rather than assuming.
    """
    return make_community_member(user or s.actor, s.community, is_moderator=True)


def make_site_admin(user):
    """Give `user` a role named exactly 'Admin'.

    `User.is_admin()` (app/models.py:1259-1265) checks role NAMES, not
    permissions, so `grant_permission` cannot produce a site admin however it
    is called. The name must be the literal string 'Admin' -- sub-project 39
    lost a task to a helper that named the role 'role-4'.
    """
    from app.models import Role, user_role
    role = Role(name='Admin', weight=0)
    db.session.add(role)
    db.session.commit()
    db.session.execute(user_role.insert().values(user_id=user.id, role_id=role.id))
    db.session.commit()
    return role


def make_instance_admin(user, instance):
    """An InstanceRole making `user` an admin of `instance`.

    `Community.is_instance_admin(user)` (app/models.py:769-776) looks up
    InstanceRole by the COMMUNITY's instance_id, not the user's, so the
    instance passed here must be the one the community resolves to -- the
    first instance seeded, id 1.
    """
    from app.models import InstanceRole
    role = InstanceRole(instance_id=instance.id, user_id=user.id, role='admin')
    db.session.add(role)
    db.session.commit()
    return role


@contextmanager
def recording_task_selector():
    """Yield a list collecting every task key `app.shared.reply` federates.

    The six functions call `task_selector(...)` unqualified, so rebinding the
    name ON THE MODULE is what intercepts them -- patching
    `app.shared.tasks.task_selector` would not, because the `from ... import`
    at app/shared/reply.py:12 already bound the original into this module's
    globals.

    The recorder calls through to the original rather than stubbing it, so the
    permitted paths still do whatever they do; `_seed_moderated_reply` passes
    `private=True`, which stops every task body at its first guard.

    Restores in a `finally`: `app.shared.reply` is imported once per session,
    so a leaked patch would corrupt every test that ran after this one.
    """
    import app.shared.reply as reply_module
    calls = []
    original = reply_module.task_selector

    def recorder(task_key, **kwargs):
        calls.append(task_key)
        return original(task_key, **kwargs)

    reply_module.task_selector = recorder
    try:
        yield calls
    finally:
        reply_module.task_selector = original
```

- [ ] **Step 3: Write the first two tests**

```python
class TestModRemoveReply:
    """`mod_remove_reply` (app/shared/reply.py:414-447)."""

    def test_a_moderator_removes_a_reply_through_the_api(self, db_session):
        """`:421`'s false arm via `is_moderator`, and the writes below it.

        Asserts `deleted` AND `deleted_by`, because `:424` sets the flag on
        every path that gets past the guard and the flag alone cannot say
        which arm of `:426`'s conditional ran.
        """
        s = _seed_moderated_reply()
        seed_moderator(s)

        user_id, reply = mod_remove_reply(s.reply.id, 'spam', SRC_API,
                                          auth=bearer(s.actor))

        assert user_id == s.actor.id
        db.session.refresh(s.reply)
        assert s.reply.deleted is True
        assert s.reply.deleted_by == s.actor.id

    def test_an_unprivileged_user_is_refused_and_changes_nothing(self, db_session):
        """`:421`'s true arm -- all three disjuncts false -- and `:422`'s raise.

        THE RAISE IS NOT THE WITNESS ON ITS OWN. A crash is a weak kill, so
        this also asserts that `deleted` is still False and that no ModLog row
        was written: a mutant that ran the body and then raised would pass a
        `pytest.raises` alone.
        """
        s = _seed_moderated_reply()

        with pytest.raises(Exception, match='Does not have permission'):
            mod_remove_reply(s.reply.id, 'spam', SRC_API, auth=bearer(s.actor))

        db.session.refresh(s.reply)
        assert s.reply.deleted is False
        assert db.session.query(ModLog).count() == 0
```

- [ ] **Step 4: Run and confirm they pass**

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh tests/test_shared_reply_moderation.py -q > /tmp/t1.txt 2>&1
echo "exit=$?"
tail -3 /tmp/t1.txt
```

Take the count from that output. If a probe falsified something, fix the plan's assumption in your own docstring and **say so in the report** — the plan's predictions are not evidence.

- [ ] **Step 5: Commit**

```bash
cd /home/blentz/git/pyfedi
git diff --quiet -- app/ && echo CLEAN
git add tests/test_shared_reply_moderation.py
git commit -F <message-file>
```

Subject: `test: open the reply moderation file with its harness and probes`

---

### Task 2: `mod_remove_reply` to zero

**Files:**
- Modify: `tests/test_shared_reply_moderation.py` (class `TestModRemoveReply`)

**Interfaces:**
- Consumes: `_seed_moderated_reply`, `seed_moderator`, `make_site_admin`, `make_instance_admin`, `recording_task_selector` from Task 1.
- Produces: nothing later tasks consume.

**Target: 21 statements, 12 arcs, to zero.** Re-derive the function's extent yourself; do not trust `:414-447`.

- [ ] **Step 1: Enumerate the arcs before writing tests**

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh tests/test_shared_reply_moderation.py \
  --cov=app.shared.reply --cov-branch --cov-report=json:/tmp/t2.json -q > /tmp/t2.txt 2>&1
echo "exit=$?"
podman-compose -f compose.test.yaml exec -T test-runner python -c "
import json
f=json.load(open('/tmp/t2.json'))['files']['app/shared/reply.py']
R=range(414,450)
print('missing stmts', [l for l in f['missing_lines'] if l in R])
print('missing arcs', [p for p in f['missing_branches'] if p[0] in R or p[1] in R])
"
```

Work from that list. Each test below names the arcs it closes; **check the claim against the list rather than believing it.**

- [ ] **Step 2: Add the remaining tests**

```python
    def test_an_instance_admin_may_remove(self, db_session):
        """`:421`'s SECOND disjunct alone -- `is_instance_admin` -- with the
        first and third false.

        `:421` is three disjuncts scored by coverage.py as one arc pair, so
        each needs its own witness or mechanism (e) applies: two conditions
        exercised only in lockstep cannot detect a swap between them.
        """
        s = _seed_moderated_reply()
        make_instance_admin(s.actor, s.instance)

        mod_remove_reply(s.reply.id, 'spam', SRC_API, auth=bearer(s.actor))

        db.session.refresh(s.reply)
        assert s.reply.deleted is True

    def test_a_site_admin_who_is_neither_may_remove(self, db_session):
        """`:421`'s THIRD disjunct alone -- `user.is_admin_or_staff()`.

        This is the disjunct `mod_restore_reply:457` does NOT have, which is
        finding 2 in the spec; the paired test in `TestModRestoreReply` shows
        the same user refused there.
        """
        s = _seed_moderated_reply()
        make_site_admin(s.actor)

        mod_remove_reply(s.reply.id, 'spam', SRC_API, auth=bearer(s.actor))

        db.session.refresh(s.reply)
        assert s.reply.deleted is True

    def test_a_moderator_removing_their_own_reply_records_minus_one(self, db_session):
        """`:426`'s ELSE arm -- `user.id == reply.user_id` gives `-1`.

        The comment at `:425` says this makes the UI show 'removed' rather
        than 'deleted'. The sibling test above takes the other arm with a
        distinct actor, so the two together pin the conditional rather than
        the assignment.
        """
        s = _seed_moderated_reply()
        seed_moderator(s, user=s.author)

        mod_remove_reply(s.reply.id, 'spam', SRC_API, auth=bearer(s.author))

        db.session.refresh(s.reply)
        assert s.reply.deleted_by == -1

    def test_a_bot_authors_reply_does_not_move_the_post_counter(self, db_session):
        """`:427`'s false arm -- `reply.author.bot` true, so `:428` is skipped
        while `:429` still runs.

        THE TWO COUNTERS MUST BE SEEDED DISTINCT or this witnesses nothing: if
        both start at the same value, a mutant moving the wrong one is
        invisible. They are seeded 7 and 3 here and asserted separately.
        """
        s = _seed_moderated_reply()
        seed_moderator(s)
        s.author.bot = True
        s.post.reply_count = 7
        s.author.post_reply_count = 3
        db.session.commit()

        mod_remove_reply(s.reply.id, 'spam', SRC_API, auth=bearer(s.actor))

        db.session.refresh(s.post)
        db.session.refresh(s.author)
        assert s.post.reply_count == 7
        assert s.author.post_reply_count == 2

    def test_a_human_authors_reply_moves_both_counters(self, db_session):
        """`:427`'s true arm -- the same-mechanism positive control.

        Without it, a fixture in which no counter could ever move would
        produce the same untouched `reply_count` as the test above. Same
        seed, same distinct values, one lever moved.
        """
        s = _seed_moderated_reply()
        seed_moderator(s)
        s.post.reply_count = 7
        s.author.post_reply_count = 3
        db.session.commit()

        mod_remove_reply(s.reply.id, 'spam', SRC_API, auth=bearer(s.actor))

        db.session.refresh(s.post)
        db.session.refresh(s.author)
        assert s.post.reply_count == 6
        assert s.author.post_reply_count == 2

    def test_a_multi_element_path_decrements_the_ancestors_child_count(self, db_session):
        """`:430`'s true arm and the raw SQL at `:431-432`.

        The guard is D517's shape, `if reply.path and len(reply.path) > 1:`.
        The path seeded here is production's -- app/models.py:3053-3060 gives
        `[0, parent.id, reply.id]` -- so `tuple(reply.path[:-1])` is
        `(0, parent.id)`, a genuine multi-element IN operand.

        THE ASSERTION IS ON THE ANCESTOR, NOT THE REPLY. The reply's own
        `child_count` is untouched by this statement, so asserting on it would
        witness nothing; and a mutant dropping the `where` clause is caught
        only by a row the statement should NOT have touched, which is why the
        bystander below is seeded and asserted too.
        """
        s = _seed_moderated_reply()
        seed_moderator(s)
        parent = make_post_reply(s.post, s.author)
        bystander = make_post_reply(s.post, s.author)
        db.session.commit()
        parent.child_count = 5
        bystander.child_count = 9
        s.reply.path = [0, parent.id, s.reply.id]
        db.session.commit()

        mod_remove_reply(s.reply.id, 'spam', SRC_API, auth=bearer(s.actor))

        db.session.refresh(parent)
        db.session.refresh(bystander)
        assert parent.child_count == 4
        assert bystander.child_count == 9

    def test_the_web_arm_flashes_and_returns_none(self, db_session, app):
        """`:415`'s false arm, `:434`'s true arm, `:435`'s flash, `:447`.

        `make_site()` is required: the web arm reaches a flash, and the module
        docstring's rule says a Site row is needed wherever a template or a
        permission read touches it.
        """
        from flask import get_flashed_messages
        make_site()
        s = _seed_moderated_reply()
        seed_moderator(s)

        with web_ctx(s.actor):
            result = mod_remove_reply(s.reply.id, 'spam', SRC_WEB, auth=None)
            messages = get_flashed_messages()

        assert result is None
        assert 'Comment deleted.' in messages

    def test_the_modlog_row_names_the_delete_action(self, db_session):
        """`:437-440`'s add_to_modlog with the literal 'delete_post_reply'.

        Compared as a SET, never as an ordered list -- the campaign's rule
        about rows a query planner returned.
        """
        s = _seed_moderated_reply()
        seed_moderator(s)

        mod_remove_reply(s.reply.id, 'spam', SRC_API, auth=bearer(s.actor))

        actions = {row.action for row in db.session.query(ModLog).all()}
        assert actions == {'delete_post_reply'}

    def test_the_federation_task_is_selected(self, db_session):
        """`:442`'s task_selector call, intercepted on the module.

        `recording_task_selector` calls through rather than stubbing, so this
        also proves the call is reached on the permitted path rather than
        merely that a name exists.
        """
        s = _seed_moderated_reply()
        seed_moderator(s)

        with recording_task_selector() as calls:
            mod_remove_reply(s.reply.id, 'spam', SRC_API, auth=bearer(s.actor))

        assert 'delete_reply' in calls
```

- [ ] **Step 3: Re-measure and confirm the range is empty**

Re-run the Step 1 command. Both lists must be `[]` for the function's range. **Check them as lists**, not by inspecting a minimum and maximum.

- [ ] **Step 4: Duplicate-name check**

```bash
grep -oE "^ *def (test_[a-z0-9_]+)" tests/test_shared_reply_moderation.py | sed 's/^ *//' | sort | uniq -d
```

Empty output is the pass.

- [ ] **Step 5: Commit**

Subject: `test: cover mod_remove_reply's guard, counters and modlog`

---

### Task 3: `mod_restore_reply` to zero

**Files:**
- Modify: `tests/test_shared_reply_moderation.py` (new class `TestModRestoreReply`)

**Interfaces:**
- Consumes: Task 1's helpers. **Also `mod_remove_reply`**, to produce a removed reply through the production path.
- Produces: nothing.

**Target: 21 statements, 12 arcs.** `mod_restore_reply:456` filters `deleted=True`, so every test needs an already-removed reply.

- [ ] **Step 1: Add the class with its own `_removed` helper**

```python
class TestModRestoreReply:
    """`mod_restore_reply` (app/shared/reply.py:450-483).

    THE GUARD HAS TWO DISJUNCTS WHERE `mod_remove_reply`'S HAS THREE. `:457`
    is `is_moderator or is_instance_admin`; `:421` adds
    `user.is_admin_or_staff()`. So a site admin who is not a moderator can
    remove a comment and then cannot restore it. That is finding 2 in the
    spec, registered rather than fixed, and
    `test_a_site_admin_who_is_neither_is_refused` below is its witness --
    paired deliberately with `TestModRemoveReply`'s
    `test_a_site_admin_who_is_neither_may_remove`, which shows the same user
    permitted one line earlier in the file.
    """

    def _removed(self, *, bot=False):
        """A seeded reply already removed through `mod_remove_reply`.

        Routed through the production verb rather than set with the ORM, so
        `deleted_by` arrives holding a real actor id and a mutant clearing it
        is visible.
        """
        s = _seed_moderated_reply()
        seed_moderator(s)
        if bot:
            s.author.bot = True
        db.session.commit()
        mod_remove_reply(s.reply.id, 'spam', SRC_API, auth=bearer(s.actor))
        db.session.refresh(s.reply)
        return s
```

- [ ] **Step 2: Add the tests**

```python
    def test_a_moderator_restores_a_removed_reply(self, db_session):
        """`:457`'s false arm via `is_moderator`, `:460`-`:461`'s writes.

        Asserts `deleted_by` back to None as well as `deleted` to False: `:460`
        runs on every permitted path, so the flag alone cannot witness `:461`.
        """
        s = self._removed()

        user_id, reply = mod_restore_reply(s.reply.id, 'ok', SRC_API,
                                           auth=bearer(s.actor))

        assert user_id == s.actor.id
        db.session.refresh(s.reply)
        assert s.reply.deleted is False
        assert s.reply.deleted_by is None

    def test_an_instance_admin_may_restore(self, db_session):
        """`:457`'s SECOND disjunct alone, with the first false."""
        s = self._removed()
        other = make_user(s.instance, 'admin-user', local=True)
        db.session.commit()
        make_instance_admin(other, s.instance)

        mod_restore_reply(s.reply.id, 'ok', SRC_API, auth=bearer(other))

        db.session.refresh(s.reply)
        assert s.reply.deleted is False

    def test_a_site_admin_who_is_neither_is_refused(self, db_session):
        """`:457`'s true arm for a user `mod_remove_reply:421` WOULD admit.

        THIS TEST PINS FINDING 2 AND ASSERTS THE DIVERGENCE ON PURPOSE. The
        same user, with the same role, is permitted by
        `TestModRemoveReply::test_a_site_admin_who_is_neither_may_remove`. If
        a later round makes the two guards agree, THE EDIT OWED HERE IS TO
        INVERT THIS TEST: the restore must then succeed and `deleted` must
        read False. Its failure at that point is the fix landing, not a
        regression.
        """
        s = self._removed()
        other = make_user(s.instance, 'staffer', local=True)
        db.session.commit()
        make_site_admin(other)

        with pytest.raises(Exception, match='Does not have permission'):
            mod_restore_reply(s.reply.id, 'ok', SRC_API, auth=bearer(other))

        db.session.refresh(s.reply)
        assert s.reply.deleted is True

    def test_a_bot_authors_reply_does_not_move_the_post_counter(self, db_session):
        """`:462`'s false arm -- `:463` skipped, `:464` still runs."""
        s = self._removed(bot=True)
        s.post.reply_count = 7
        s.author.post_reply_count = 3
        db.session.commit()

        mod_restore_reply(s.reply.id, 'ok', SRC_API, auth=bearer(s.actor))

        db.session.refresh(s.post)
        db.session.refresh(s.author)
        assert s.post.reply_count == 7
        assert s.author.post_reply_count == 4

    def test_a_human_authors_reply_moves_both_counters(self, db_session):
        """`:462`'s true arm -- the same-mechanism positive control."""
        s = self._removed()
        s.post.reply_count = 7
        s.author.post_reply_count = 3
        db.session.commit()

        mod_restore_reply(s.reply.id, 'ok', SRC_API, auth=bearer(s.actor))

        db.session.refresh(s.post)
        db.session.refresh(s.author)
        assert s.post.reply_count == 8
        assert s.author.post_reply_count == 4

    def test_a_multi_element_path_increments_the_ancestors_child_count(self, db_session):
        """`:465`'s true arm and `:466-467`'s raw SQL.

        The mirror of `TestModRemoveReply`'s path test, with a BYSTANDER for
        the same reason: a mutant dropping the `where` clause is caught only
        by a row the statement should not have touched.
        """
        s = self._removed()
        parent = make_post_reply(s.post, s.author)
        bystander = make_post_reply(s.post, s.author)
        db.session.commit()
        parent.child_count = 5
        bystander.child_count = 9
        s.reply.path = [0, parent.id, s.reply.id]
        db.session.commit()

        mod_restore_reply(s.reply.id, 'ok', SRC_API, auth=bearer(s.actor))

        db.session.refresh(parent)
        db.session.refresh(bystander)
        assert parent.child_count == 6
        assert bystander.child_count == 9

    def test_the_web_arm_flashes_and_returns_none(self, db_session, app):
        """`:451`'s false arm, `:470`'s true arm, `:471`'s flash, `:483`."""
        from flask import get_flashed_messages
        make_site()
        s = self._removed()

        with web_ctx(s.actor):
            result = mod_restore_reply(s.reply.id, 'ok', SRC_WEB, auth=None)
            messages = get_flashed_messages()

        assert result is None
        assert 'Comment restored.' in messages

    def test_the_modlog_row_names_the_restore_action(self, db_session):
        """`:473-476`'s add_to_modlog with the literal 'restore_post_reply'.

        The set here holds TWO actions, because `_removed` wrote the delete
        row first. Asserting the set rather than a count is what makes the
        restore action's presence the witness.
        """
        s = self._removed()

        mod_restore_reply(s.reply.id, 'ok', SRC_API, auth=bearer(s.actor))

        actions = {row.action for row in db.session.query(ModLog).all()}
        assert actions == {'delete_post_reply', 'restore_post_reply'}

    def test_the_federation_task_is_selected(self, db_session):
        """`:478`'s task_selector call."""
        s = self._removed()

        with recording_task_selector() as calls:
            mod_restore_reply(s.reply.id, 'ok', SRC_API, auth=bearer(s.actor))

        assert 'restore_reply' in calls
```

- [ ] **Step 3: Re-measure `:450`-`:483`, both lists empty**
- [ ] **Step 4: Duplicate-name check**
- [ ] **Step 5: Commit**

Subject: `test: cover mod_restore_reply and pin its missing admin disjunct`

---

### Task 4: `lock_post_reply` to zero, including the `@>` cascade

**Files:**
- Modify: `tests/test_shared_reply_moderation.py` (new class `TestLockPostReply`)

**Interfaces:**
- Consumes: Task 1's helpers.
- Produces: the pinning test `test_an_unprivileged_api_caller_is_not_refused`, which **Task 7 inverts**.

**Target: 24 statements, 14 arcs.**

- [ ] **Step 1: Add the class**

```python
class TestLockPostReply:
    """`lock_post_reply` (app/shared/reply.py:486-520).

    THIS FUNCTION FAILS SILENTLY AND ITS TWIN DOES NOT. `:501`'s guard has no
    `else`, so an unauthorized SRC_API caller falls through to `:519-520` and
    receives `user.id, post_reply` with nothing changed -- a 200 carrying the
    unchanged object. `app/shared/post.py:968-969` is the same guard WITH
    `elif src == SRC_API: raise Exception('Does not have permission')`, and
    `move_post:999-1000` carries it too; both came from PC2 in sub-project 36.
    These two reply functions were left behind.

    That makes false-witness mechanism (a) acute here: THE RETURN VALUE IS THE
    SAME ON BOTH ARMS, so every test below asserts on state and never on the
    return.
    """

    def test_a_moderator_locks_a_reply_through_the_api(self, db_session):
        """`:494`'s true arm, `:501`'s true arm, `:502`'s assignment.

        Asserts `replies_enabled` rather than the return, because `:519-520`
        returns the same shape on the refused path.
        """
        s = _seed_moderated_reply()
        seed_moderator(s)
        s.reply.replies_enabled = True
        db.session.commit()

        user_id, reply = lock_post_reply(s.reply.id, True, SRC_API,
                                         auth=bearer(s.actor))

        assert user_id == s.actor.id
        db.session.refresh(s.reply)
        assert s.reply.replies_enabled is False

    def test_unlocking_sets_replies_enabled_back_to_true(self, db_session):
        """`:494`'s false arm, `:498`'s assignment and `:499`'s modlog_type."""
        s = _seed_moderated_reply()
        seed_moderator(s)
        s.reply.replies_enabled = False
        db.session.commit()

        lock_post_reply(s.reply.id, False, SRC_API, auth=bearer(s.actor))

        db.session.refresh(s.reply)
        assert s.reply.replies_enabled is True

    def test_locking_cascades_to_a_descendant(self, db_session):
        """`:503-504`'s containment query, the module's only `@>`.

        `where path @> ARRAY[:parent_id]` matches every reply whose `path`
        CONTAINS the locked reply's id, which is how a lock reaches a whole
        subtree. The descendant's path is production's shape --
        app/models.py:3053-3060, `[0, parent.id, child.id]` -- because
        `make_post_reply` does not set `path` at all.

        THE BYSTANDER IS THE WITNESS FOR THE `where` CLAUSE. A mutant dropping
        it would flip every reply in the table, and only a row that should NOT
        have changed catches that.
        """
        s = _seed_moderated_reply()
        seed_moderator(s)
        child = make_post_reply(s.post, s.author)
        bystander = make_post_reply(s.post, s.author)
        db.session.commit()
        child.path = [0, s.reply.id, child.id]
        bystander.path = [0, bystander.id]
        child.replies_enabled = True
        bystander.replies_enabled = True
        db.session.commit()

        lock_post_reply(s.reply.id, True, SRC_API, auth=bearer(s.actor))

        db.session.refresh(child)
        db.session.refresh(bystander)
        assert child.replies_enabled is False
        assert bystander.replies_enabled is True

    def test_an_instance_admin_may_lock(self, db_session):
        """`:501`'s SECOND disjunct alone, with the first false."""
        s = _seed_moderated_reply()
        make_instance_admin(s.actor, s.instance)
        s.reply.replies_enabled = True
        db.session.commit()

        lock_post_reply(s.reply.id, True, SRC_API, auth=bearer(s.actor))

        db.session.refresh(s.reply)
        assert s.reply.replies_enabled is False

    def test_an_unprivileged_api_caller_is_not_refused(self, db_session):
        """`:501`'s false arm -- AND IT ASSERTS THE DEFECT ON PURPOSE.

        Neither disjunct holds, so the whole body is skipped and control
        reaches `:519`. The call returns `user.id, post_reply` normally: no
        exception, no flash, nothing changed. The caller cannot tell this
        apart from a success.

        THE TWIN RAISES. `app/shared/post.py:968-969` is
        `elif src == SRC_API: raise Exception('Does not have permission')`,
        and `tests/test_shared_post_moderation.py`'s
        `test_an_unprivileged_user_changes_nothing` pins that shape with
        `pytest.raises`.

        WHOEVER PROPAGATES THAT FIX MUST EDIT THIS TEST. The edit owed is to
        INVERT it: wrap the call in
        `pytest.raises(Exception, match='Does not have permission')` and keep
        both state assertions. Its failure at that point is the fix landing,
        not a regression. Registered as finding 3 in the round's spec.

        THE STATE ASSERTIONS CARRY THE TEST, not the returned tuple. A mutant
        that ran the body and then returned the same tuple would pass a
        return-value assertion and is caught only by `replies_enabled` and the
        empty ModLog.
        """
        s = _seed_moderated_reply()
        s.reply.replies_enabled = True
        db.session.commit()

        user_id, reply = lock_post_reply(s.reply.id, True, SRC_API,
                                         auth=bearer(s.actor))

        assert user_id == s.actor.id
        db.session.refresh(s.reply)
        assert s.reply.replies_enabled is True
        assert db.session.query(ModLog).count() == 0

    def test_locking_writes_the_lock_action(self, db_session):
        """`:506-508`'s add_to_modlog with `:496`'s modlog_type."""
        s = _seed_moderated_reply()
        seed_moderator(s)

        lock_post_reply(s.reply.id, True, SRC_API, auth=bearer(s.actor))

        actions = {row.action for row in db.session.query(ModLog).all()}
        assert actions == {'lock_post_reply'}

    def test_unlocking_writes_the_unlock_action(self, db_session):
        """`:499`'s modlog_type on the false arm of `:494`.

        The counterpart of the test above. Together they prove `:506`'s
        argument is driven by `:494` rather than fixed.
        """
        s = _seed_moderated_reply()
        seed_moderator(s)

        lock_post_reply(s.reply.id, False, SRC_API, auth=bearer(s.actor))

        actions = {row.action for row in db.session.query(ModLog).all()}
        assert actions == {'unlock_post_reply'}

    def test_the_web_arm_flashes_when_locking(self, db_session, app):
        """`:487`'s false arm, `:510`'s true arm, `:511`-`:512`."""
        from flask import get_flashed_messages
        make_site()
        s = _seed_moderated_reply()
        seed_moderator(s)

        with web_ctx(s.actor):
            lock_post_reply(s.reply.id, True, SRC_WEB, auth=None)
            messages = get_flashed_messages()

        assert 'Comment has been locked.' in messages

    def test_the_web_arm_flashes_a_different_message_when_unlocking(self, db_session, app):
        """`:510`'s false arm, `:515`-`:516`.

        Paired with the test above so `:510`'s two arms are witnessed by
        different message text rather than by the same assertion twice.
        """
        from flask import get_flashed_messages
        make_site()
        s = _seed_moderated_reply()
        seed_moderator(s)

        with web_ctx(s.actor):
            lock_post_reply(s.reply.id, False, SRC_WEB, auth=None)
            messages = get_flashed_messages()

        assert 'Comment has been unlocked.' in messages

    def test_the_api_arm_selects_the_lock_task_without_flashing(self, db_session):
        """`:511`'s false arm and `:513`'s task_selector.

        `:511` guards only the flash; `:513` runs on both arms of it. The
        API call reaches `:513` with `:511` false, which is the arc no web
        test can take.
        """
        s = _seed_moderated_reply()
        seed_moderator(s)

        with recording_task_selector() as calls:
            lock_post_reply(s.reply.id, True, SRC_API, auth=bearer(s.actor))

        assert 'lock_post_reply' in calls

    def test_the_api_arm_selects_the_unlock_task(self, db_session):
        """`:515`'s false arm and `:517`'s task_selector."""
        s = _seed_moderated_reply()
        seed_moderator(s)

        with recording_task_selector() as calls:
            lock_post_reply(s.reply.id, False, SRC_API, auth=bearer(s.actor))

        assert 'unlock_post_reply' in calls
```

- [ ] **Step 2: Re-measure `:486`-`:520`, both lists empty**
- [ ] **Step 3: Duplicate-name check**
- [ ] **Step 4: Commit**

Subject: `test: cover lock_post_reply including its descendant cascade`

---

### Task 5: `set_collapse_post_reply` to zero

**Files:**
- Modify: `tests/test_shared_reply_moderation.py` (new class `TestSetCollapsePostReply`)

**Interfaces:**
- Consumes: Task 1's helpers.
- Produces: the pinning test `test_an_unprivileged_api_caller_is_not_refused_either`, which **Task 7 inverts**.

**Target: 15 statements, 12 arcs.**

- [ ] **Step 1: Add the class**

```python
class TestSetCollapsePostReply:
    """`set_collapse_post_reply` (app/shared/reply.py:523-545).

    THE SECOND SILENT-FAILURE FUNCTION, and its guard differs from
    `lock_post_reply`'s by one disjunct: `:531` is
    `is_moderator or is_instance_admin or user.is_admin_or_staff()` where
    `:501` has only the first two. So a site admin who is not a moderator can
    make a comment collapsible but cannot lock it -- finding 2's second
    instance, and `test_a_site_admin_who_is_neither_may_collapse` below is
    paired with `TestLockPostReply`'s refusal to witness it.

    `:538` and `:542` are COMMENTED-OUT `task_selector` calls, so this
    function federates nothing. A test asserting an empty recorder would
    witness the comment rather than the code; none is written.

    THE COLUMN DEFAULT IS A FALSE-WITNESS TRAP HERE AND EVERY TEST BELOW
    SEEDS AROUND IT. `PostReply.collapsible` defaults to **True**
    (app/models.py:2931) and `PostReply.new` sets it to
    `user.id != post.user_id` (`:3006`), so a test that asserts
    `collapsible is True` without seeding False first would pass with `:532`
    deleted -- mechanism (a), asserting on state something else set
    unconditionally. Every test here writes the opposite value before acting.
    """

    def test_a_moderator_makes_a_reply_collapsible(self, db_session):
        """`:531`'s true arm via `is_moderator`, `:532`'s assignment."""
        s = _seed_moderated_reply()
        seed_moderator(s)
        s.reply.collapsible = False
        db.session.commit()

        user_id, reply = set_collapse_post_reply(s.reply.id, True, SRC_API,
                                                 auth=bearer(s.actor))

        assert user_id == s.actor.id
        db.session.refresh(s.reply)
        assert s.reply.collapsible is True

    def test_clearing_collapsible_sets_it_back_to_false(self, db_session):
        """`:532` with the other argument, and `:539`'s else arm."""
        s = _seed_moderated_reply()
        seed_moderator(s)
        s.reply.collapsible = True
        db.session.commit()

        set_collapse_post_reply(s.reply.id, False, SRC_API, auth=bearer(s.actor))

        db.session.refresh(s.reply)
        assert s.reply.collapsible is False

    def test_an_instance_admin_may_collapse(self, db_session):
        """`:531`'s SECOND disjunct alone."""
        s = _seed_moderated_reply()
        make_instance_admin(s.actor, s.instance)
        s.reply.collapsible = False
        db.session.commit()

        set_collapse_post_reply(s.reply.id, True, SRC_API, auth=bearer(s.actor))

        db.session.refresh(s.reply)
        assert s.reply.collapsible is True

    def test_a_site_admin_who_is_neither_may_collapse(self, db_session):
        """`:531`'s THIRD disjunct -- the one `lock_post_reply:501` lacks.

        Paired with `TestLockPostReply`'s unprivileged test: the same role
        that silently does nothing there succeeds here, which is the
        divergence finding 2 registers.
        """
        s = _seed_moderated_reply()
        make_site_admin(s.actor)
        s.reply.collapsible = False
        db.session.commit()

        set_collapse_post_reply(s.reply.id, True, SRC_API, auth=bearer(s.actor))

        db.session.refresh(s.reply)
        assert s.reply.collapsible is True

    def test_an_unprivileged_api_caller_is_not_refused_either(self, db_session):
        """`:531`'s false arm -- ASSERTS THE DEFECT ON PURPOSE.

        The same silent failure as `lock_post_reply`'s, and the same fix-edit
        obligation: when
        `elif src == SRC_API: raise Exception('Does not have permission')` is
        transcribed from `app/shared/post.py:968-969`, THE EDIT OWED HERE IS
        TO INVERT THIS TEST -- wrap the call in `pytest.raises` and keep the
        state assertion. Its failure then is the fix landing.

        `collapsible` is seeded False and asserted False, and `:533`'s commit
        is the only write in the body, so the state assertion is what
        discriminates; the returned tuple is identical on both arms.
        """
        s = _seed_moderated_reply()
        s.reply.collapsible = False
        db.session.commit()

        user_id, reply = set_collapse_post_reply(s.reply.id, True, SRC_API,
                                                 auth=bearer(s.actor))

        assert user_id == s.actor.id
        db.session.refresh(s.reply)
        assert s.reply.collapsible is False

    def test_the_web_arm_flashes_the_collapsible_message(self, db_session, app):
        """`:524`'s false arm, `:535`'s true arm, `:536`-`:537`."""
        from flask import get_flashed_messages
        make_site()
        s = _seed_moderated_reply()
        seed_moderator(s)

        with web_ctx(s.actor):
            set_collapse_post_reply(s.reply.id, True, SRC_WEB, auth=None)
            messages = get_flashed_messages()

        assert 'Comment is collapsible.' in messages

    def test_the_web_arm_flashes_the_other_message_when_clearing(self, db_session, app):
        """`:535`'s false arm, `:540`-`:541`."""
        from flask import get_flashed_messages
        make_site()
        s = _seed_moderated_reply()
        seed_moderator(s)

        with web_ctx(s.actor):
            set_collapse_post_reply(s.reply.id, False, SRC_WEB, auth=None)
            messages = get_flashed_messages()

        assert 'Comment will not be collapsed when loading the post.' in messages
```

- [ ] **Step 2: Re-measure `:523`-`:545`, both lists empty**
- [ ] **Step 3: Duplicate-name check**
- [ ] **Step 4: Commit**

Subject: `test: cover set_collapse_post_reply and its extra admin disjunct`

---

### Task 6: `choose_answer` and `unchoose_answer` to zero, and pin the API hole

**Files:**
- Modify: `tests/test_shared_reply_moderation.py` (new class `TestChooseAnswer`)

**Interfaces:**
- Consumes: Task 1's helpers, and Probe A's finding about `get_recipient_language`.
- Produces: the pinning test `test_any_authenticated_api_user_may_mark_any_comment_as_the_answer`, which **Task 7 inverts**.

**Target: 24 statements, 8 arcs across the pair.**

- [ ] **Step 1: Add the class**

```python
class TestChooseAnswer:
    """`choose_answer` (app/shared/reply.py:548-576) and `unchoose_answer`
    (`:579-591`).

    NEITHER FUNCTION CONTAINS A PERMISSION CHECK. Both establish `user` from
    the source fork and then act. The WEB route guards --
    app/post/routes.py:2443 and `:2453` require
    `current_user.is_admin_or_staff() or post_reply.user_id == current_user.id
    or post_reply.community.is_moderator()` -- but the API path does not:
    app/api/alpha/routes.py:983 calls `post_reply_mark_as_answer`
    (app/api/alpha/utils/reply.py:687-697), which calls `authorise_api_user`
    and dispatches straight through. `authorise_api_user` establishes WHO the
    caller is and says nothing about what they may do.

    `force_locale(get_recipient_language(post_reply.user_id))` wraps the title
    at `:556`; what a factory user needs to survive that path is Task 1's
    Probe A and is recorded in tests/README.md.
    """

    def test_choosing_an_answer_sets_the_flag_and_notifies_the_author(self, db_session):
        """`:554`-`:571` -- the flag, the Notification and the unread counter.

        THREE ASSERTIONS BECAUSE `:555` ALONE WITNESSES ALMOST NOTHING: a
        mutant deleting `:564`-`:570` leaves `answer` true and the test green.
        The notification's `user_id` is asserted to be the AUTHOR's rather
        than the actor's, which is what `:565` claims and what a mutant
        swapping the two operands would break.
        """
        s = _seed_moderated_reply()
        s.author.unread_notifications = 4
        db.session.commit()

        user_id, reply = choose_answer(s.reply.id, SRC_API, auth=bearer(s.actor))

        assert user_id == s.actor.id
        db.session.refresh(s.reply)
        db.session.refresh(s.author)
        assert s.reply.answer is True
        assert s.author.unread_notifications == 5
        notifications = db.session.query(Notification).all()
        assert {n.user_id for n in notifications} == {s.author.id}
        assert {n.author_id for n in notifications} == {s.actor.id}

    def test_unchoosing_clears_the_flag_and_notifies_nobody(self, db_session):
        """`:585`-`:587`, and the absence of a notification.

        The positive control for the emptiness is the test above: it proves a
        Notification CAN be written by this fixture, so the zero here is
        `unchoose_answer` not writing one rather than a broken seed.
        """
        s = _seed_moderated_reply()
        s.reply.answer = True
        db.session.commit()

        user_id, reply = unchoose_answer(s.reply.id, SRC_API, auth=bearer(s.actor))

        assert user_id == s.actor.id
        db.session.refresh(s.reply)
        assert s.reply.answer is False
        assert db.session.query(Notification).count() == 0

    def test_any_authenticated_api_user_may_mark_any_comment_as_the_answer(self, db_session):
        """THIS TEST ASSERTS A LIVE AUTHORIZATION DEFECT ON PURPOSE.

        `stranger` is not the reply's author, not a moderator of its
        community, not an instance admin and not site staff. The web route
        would refuse them at app/post/routes.py:2443. The API path does not
        check at all, so the call succeeds and the comment is marked as the
        accepted answer by someone with no relationship to it.

        WHOEVER CLOSES THIS MUST EDIT THIS TEST. The fix belongs at
        app/api/alpha/utils/reply.py:687, mirroring the web route's three-way
        guard, so that `choose_answer` stays a plain verb and both entry
        points agree. THE EDIT OWED HERE IS TO INVERT THIS TEST: the call must
        then be refused and `answer` must stay False. Its failure at that
        point is the fix landing, not a regression.

        THE WITNESS IS `answer` BEING TRUE, not the return value: `:575`-`:576`
        return the same shape whoever calls.
        """
        s = _seed_moderated_reply()
        stranger = make_user(s.instance, 'stranger', local=True)
        db.session.commit()

        user_id, reply = choose_answer(s.reply.id, SRC_API, auth=bearer(stranger))

        assert user_id == stranger.id
        db.session.refresh(s.reply)
        assert s.reply.answer is True

    def test_the_web_arm_reads_current_user(self, db_session, app):
        """`:549`'s false arm and `:552`, for both functions.

        Returns None on the web arm because `:575` guards the return.
        """
        make_site()
        s = _seed_moderated_reply()

        with web_ctx(s.actor):
            result = choose_answer(s.reply.id, SRC_WEB, auth=None)

        assert result is None
        db.session.refresh(s.reply)
        assert s.reply.answer is True

    def test_the_web_arm_of_unchoose_reads_current_user(self, db_session, app):
        """`:580`'s false arm and `:583`, and `:591`'s guarded return."""
        make_site()
        s = _seed_moderated_reply()
        s.reply.answer = True
        db.session.commit()

        with web_ctx(s.actor):
            result = unchoose_answer(s.reply.id, SRC_WEB, auth=None)

        assert result is None
        db.session.refresh(s.reply)
        assert s.reply.answer is False

    def test_both_verbs_select_their_federation_task(self, db_session):
        """`:573` and `:589`.

        One test for both because the two calls are independent and neither
        has a branch; splitting them would add a test without adding a
        witness.
        """
        s = _seed_moderated_reply()

        with recording_task_selector() as calls:
            choose_answer(s.reply.id, SRC_API, auth=bearer(s.actor))
            unchoose_answer(s.reply.id, SRC_API, auth=bearer(s.actor))

        assert 'choose_answer' in calls
        assert 'unchoose_answer' in calls
```

- [ ] **Step 2: Re-measure `:548`-`:591`, both lists empty**
- [ ] **Step 3: Re-measure the WHOLE of Groups E and F**

```bash
podman-compose -f compose.test.yaml exec -T test-runner python -c "
import json
f=json.load(open('/tmp/t6.json'))['files']['app/shared/reply.py']
E=range(414,486); F=range(486,592)
ml=f['missing_lines']; mb=f['missing_branches']
print('E missing stmts', [l for l in ml if l in E])
print('F missing stmts', [l for l in ml if l in F])
print('E missing arcs', [p for p in mb if p[0] in E or p[1] in E])
print('F missing arcs', [p for p in mb if p[0] in F or p[1] in F])
"
```

All four lists must be `[]`. **Check them as lists.**

- [ ] **Step 4: Duplicate-name check**
- [ ] **Step 5: Commit**

Subject: `test: cover the answer verbs and pin their missing api authorization`

---

### Task 7: The two production fixes, and invert the three pins

**Files:**
- Modify: `app/api/alpha/utils/reply.py` (in `post_reply_mark_as_answer`)
- Modify: `app/shared/reply.py` (two sites)
- Modify: `tests/test_shared_reply_moderation.py` (invert three tests)

**Interfaces:**
- Consumes: the three pinning tests from Tasks 4, 5 and 6, each of which states the edit owed in its own docstring.
- Produces: nothing.

**This is the only task that may touch `app/`.** Both changes land with the failing observation pasted first.

- [ ] **Step 1: Paste the failing observation for each defect**

Run the three pinning tests and paste their current PASSING output. That is the defect in executable form and it is the before-picture:

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh tests/test_shared_reply_moderation.py -q \
  -k "not_refused or any_authenticated_api_user" > /tmp/t7pre.txt 2>&1
echo "exit=$?"
tail -4 /tmp/t7pre.txt
```

- [ ] **Step 2: Propagate PC2's refusal to the two Group F functions**

Re-derive both line numbers first. Transcribe from `app/shared/post.py:968-969`, verified with numbered output rather than from memory:

```python
            task_selector('unlock_post_reply', user_id=user.id, post_reply_id=post_reply_id)
    elif src == SRC_API:
        raise Exception('Does not have permission')

    if src == SRC_API:
        return user.id, post_reply
```

The same two lines go at the end of `set_collapse_post_reply`'s guarded block. **Indentation is load-bearing:** the `elif` must align with the `if` at `:501` and `:531`, not with the statements inside them.

- [ ] **Step 3: Guard the answer API entry point**

`app/api/alpha/utils/reply.py`, in `post_reply_mark_as_answer`, after `authorise_api_user` and before the dispatch. Mirror the web route at `app/post/routes.py:2443`:

```python
    reply = db.session.query(PostReply).get(reply_id)
    user = User.query.get(user_id)
    if not (user.is_admin_or_staff() or reply.user_id == user.id
            or reply.community.is_moderator(user)):
        raise Exception('Does not have permission')
```

**Both dependencies were verified at source while this plan was written, so confirm rather than discover them:** `app/api/alpha/utils/reply.py:10` already imports `PostReply` and `User`, so no new import is needed; and `Community.is_moderator(self, user=None)` (`app/models.py:736`) takes an optional user. The web route calls it with no argument, which defaults to `current_user`; there is no `current_user` on the API path, so the user MUST be passed explicitly. Re-derive both with numbered output anyway and paste the proof.

- [ ] **Step 4: Invert the three pinning tests**

Each docstring names its own edit. For the two Group F tests, wrap the call and keep the state assertions:

```python
        with pytest.raises(Exception, match='Does not have permission'):
            lock_post_reply(s.reply.id, True, SRC_API, auth=bearer(s.actor))

        db.session.refresh(s.reply)
        assert s.reply.replies_enabled is True
        assert db.session.query(ModLog).count() == 0
```

For the answer test, the refusal is raised by the API wrapper rather than by `choose_answer`, so the inverted test must call **the wrapper**, not the verb. Rewrite it to exercise `post_reply_mark_as_answer` and assert `answer` stays False.

**Rewrite each docstring to say the defect WAS fixed, in the past tense, and what the test now witnesses.** A retraction says what was claimed, that it was false or is now stale, and why — never a silent rewrite.

- [ ] **Step 5: Sweep for invalidated premises**

The three docstrings you just edited are not the only places these claims stand. The module docstring, `TestLockPostReply`'s class docstring and `TestSetCollapsePostReply`'s class docstring all assert the silent-failure behaviour. **Re-read each in full and correct every claim your change invalidated.** Sub-project 40 hit this defect six times, every one inside a correction written to fix a different instance of it.

- [ ] **Step 6: Run the file and confirm**

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh tests/test_shared_reply_moderation.py -q > /tmp/t7.txt 2>&1
echo "exit=$?"
tail -3 /tmp/t7.txt
git diff --quiet -- app/ || echo "app/ modified, as this task intends"
```

Also run `tests/test_shared_post_moderation.py` and `tests/test_shared_reply_interactions.py` — the `app/shared/reply.py` edit is in a module both import.

- [ ] **Step 7: Commit**

Subject: `fix: refuse unauthorized api callers in three reply verbs`

Body must state: what each defect was, that the fix is transcription from the twin module for two of the three, the failing observation, and that three tests were inverted per their own stated obligations.

---

### Task 8: The mutation pass

**Files:**
- Modify: `tests/test_shared_reply_moderation.py` (only to close holes)

**Interfaces:**
- Consumes: everything above.
- Produces: the survivor inventory Task 9 registers.

- [ ] **Step 1: Derive the statement and compound lists mechanically**

`ast.walk`, never by reading. Sub-project 39's controller predicted 11 `BoolOp` nodes and 26 operands; the AST reported 27 and 58.

```bash
cd /home/blentz/git/pyfedi
podman-compose -f compose.test.yaml exec -T test-runner python -c "
import ast
src = open('app/shared/reply.py').read()
tree = ast.parse(src)
targets = {'mod_remove_reply','mod_restore_reply','lock_post_reply',
           'set_collapse_post_reply','choose_answer','unchoose_answer'}
for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name in targets]:
    stmts = sorted({s.lineno for s in ast.walk(fn) if isinstance(s, ast.stmt)})
    bools = [(b.lineno, len(b.values)) for b in ast.walk(fn) if isinstance(b, ast.BoolOp)]
    print(fn.name, 'stmts', len(stmts), stmts)
    print(fn.name, 'BoolOps', bools)
"
```

- [ ] **Step 2: Run the pass, one mutation at a time**

For each: dry-run the `sed`, read the line with numbered output, apply, run the file, **restore with a REVERSE `sed`** — `git checkout -- app/` is banned this round — then assert `git diff --quiet -- app/` and the correct `wc -l`. **Restore before any point where you might stop and report.**

- [ ] **Step 3: The two mandatory mutations**

Beyond the statement list:

1. Neutralise `lock_post_reply`'s permission guard so it never refuses.
2. Neutralise `mod_restore_reply`'s guard likewise.

**Say which regime you measured in.** Task 7's propagation means `lock_post_reply`'s mutant is now killable by the raise as well as by a state assertion; before it, only state could kill. A pass that does not name the regime has not measured anything.

- [ ] **Step 4: Classify every survivor**

A crash kill is not a kill unless a viable non-crashing variant of the same fault also dies. An operator can be structurally void. **Fix-catching is not a unique kill.** A test closing no arc and no statement earns its place by a unique kill against a fault-direction mutant, or by pinning a registered defect with the fix-edit obligation stated in the test itself.

Close what is worth closing; record the rest with reproduction recipes. **Non-failures are evidence.** Arithmetic must reconcile: killed + survivors = total, and survivors must break down into named categories that sum.

- [ ] **Step 5: Commit**

Subject: `test: close the holes the reply moderation mutation pass found`

---

### Task 9: Measure, ratchet, register

**Files:**
- Modify: `coverage_floors.ini`
- Modify: `tests/README.md`
- Modify: `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`

**DO NOT RUN THE FULL SUITE.** The controller runs it.

- [ ] **Step 1: Measure suite-scoped**

Confirm the file list with `ls`, not from this plan. Sub-project 40's Task 8 found that its brief's glob was wrong and that a file outside the `test_shared_` namespace was closing seven statements — **three measurements at different scopes, not one.**

- [ ] **Step 2: Verify Groups E and F at zero, arcs checked PAIRWISE**

Print both lists in full. Expect roughly 126 statements and 68 arcs still missing module-wide — Groups B and D are untouched.

- [ ] **Step 3: Raise the floor**

`app/shared/reply.py` is currently **36**. Raise it to `floor(percent_covered)` from the JSON.

- [ ] **Step 4: Re-measure Groups B and D**

Sub-project 42 must inherit a number. Report per-function statements and arcs from coverage.py's own function table.

- [ ] **Step 5: `tests/README.md` facts from 232**

At minimum: what `force_locale(get_recipient_language(...))` needs from a factory user (Probe A); how to witness `lock_post_reply`'s `@>` cascade and why a bystander is required; and that `set_collapse_post_reply`'s two `task_selector` calls are commented out.

- [ ] **Step 6: Register findings from D519**

At minimum:

- **The answer-verb API authorization hole** — found, pinned and FIXED this round, with the route evidence on both sides.
- **PC2's unpropagated refusal** — `lock_post_reply` and `set_collapse_post_reply` fell through where `lock_post:968-969` and `move_post:999-1000` raise. FIXED this round. **The transferable finding is the shape:** a fix applied to one twin and not the other is invisible to every instrument this campaign runs — it creates no missing arc, no failing test and no surviving mutant. Only reading the twins side by side finds it.
- **The permission-disjunct divergences** — `mod_remove_reply`/`set_collapse_post_reply` admit `is_admin_or_staff` where `mod_restore_reply`/`lock_post_reply` do not, and `lock_post:953` calls `community.is_admin_or_staff(user)` where `lock_post_reply:501` calls `community.is_instance_admin(user)`. Registered, not fixed.
- **The counter divergence** — `delete_reply` moves three counters inside the bot guard where `mod_remove_reply` moves one.
- Everything Task 8's mutation pass turned up, **with recipes**: `.superpowers/sdd/` is deleted when the round ends, so a survivor recorded only there is indistinguishable later from work never done.

Move the marker to the next free number, taken as the maximum over every marker in the file.

- [ ] **Step 7: Commit**

Subject: `test: raise the reply floor and register sub-project 41's findings`

---

## Self-Review

**Spec coverage.** Goal → Tasks 2-6. Harness → Task 1. Finding 1 → pinned Task 6, fixed Task 7. Finding 2 → pinned Tasks 2, 3, 5; registered Task 9. Finding 3 → pinned Tasks 4, 5; fixed Task 7; registered Task 9. Finding 4 → pinned Task 2; registered Task 9. Five false-witness mechanisms → named in the docstrings that rely on them. Verification → Task 8, including both mandatory mutations and the regime note. Success criteria → Task 9, plus the controller's full-suite run. No spec requirement is unassigned.

**Placeholder scan.** No TBD, no "similar to Task N", no "add appropriate error handling". Every code step carries real code. Task 7's Step 3 deliberately says *check the imports and the signature* rather than asserting them, because those are facts the task must verify rather than inherit — that is an instruction, not a placeholder.

**Type consistency.** `_seed_moderated_reply` returns a `SimpleNamespace` with `instance`, `author`, `actor`, `community`, `post`, `reply`, and every task uses those names. `seed_moderator(s, user=None)` defaults to `s.actor`. `recording_task_selector()` yields a list of task-key strings. The three pinning tests named in Tasks 4, 5 and 6 are the three Task 7 inverts, by the same names.

**One risk stated plainly.** Every line number in this plan was re-derived at `903dab20`, but Task 7 inserts lines into `app/shared/reply.py`, so Tasks 8 and 9 must re-derive everything after `:519` rather than trusting anything above.

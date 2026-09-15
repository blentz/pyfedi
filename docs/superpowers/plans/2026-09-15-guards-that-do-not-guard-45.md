# Sub-project 45: two guards that do not guard, and community.py's membership group

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair two live guards that announce a refusal and then do not refuse, close `app/shared/site.py`, and cover `app/shared/community.py`'s membership group — 90 statements and 57 arcs together.

**Architecture:** Each guard is pinned against today's behaviour first, with a test that asserts the refusal *fails* — then fixed in one line, then the pin inverted. The coverage half is six functions, four of which are the fifth instance of a block/unblock twin pair this campaign has already closed four times, so the harness transfers.

**Tech Stack:** Python 3.13, Flask, SQLAlchemy 2.0.52, pytest, podman-compose. No host Python has flask or pytest.

**Spec:** `docs/superpowers/specs/2026-09-15-guards-that-do-not-guard-45-design.md` (committed `e124c532`).

---

## Global Constraints

Every task's requirements implicitly include this section. These are not style preferences. Each line is here because it cost this campaign a defect, a retraction, or a day.

### The rule this round turns on

**A refusal that does not refuse is invisible to coverage.** Both defects here execute every line they occupy. Only an assertion about the *outcome* — was the row created, did the member actually leave — catches them. When you write a test for a guard, **assert what the guard was supposed to prevent**, not merely that the message appeared.

### Unreachability: the lesson this campaign paid for twice

**A line no *input* can reach is not always a line no *test* can reach.** Sub-project 43 ruled two lines equivalent because no production caller reached them, when a legitimate third argument value did; that ruling was retracted as **D564**. **This round contains two more instances of that exact shape** — see Task 6 — and the remedy is precedented three times over. Do not rule them unreachable.

Distinguish that from the case that *does* stand: sub-projects 19 and 20 ruled two handlers unreachable because the callee cannot raise on that path, and that ruling holds.

**`tests/README.md` fact 75 catalogues the causes. It has NO cause 4(c)** — cause 4 has only (a) and (b), and a label that does not exist propagated through two sub-projects before anyone re-read the source. **Cause 8 is narrowly scoped to a `try`/`except` whose body can never run**; a bare `if <cond>: raise` is not cause 8 however unreachable it is. **Name the cause that actually fits, and if none does, say so plainly and argue the shape on its merits.** Two citations were corrected in sub-project 44 — one for bending a quotation to fit, one for naming a cause that does not exist.

### Choosing a measurement oracle

**Measure the oracle; never name it.** A module's coverage is often not produced by the test file named after it. Sub-project 43 twice prescribed files that did not execute the function under test, turning every mutant there into a survivor. **`subscribe_community` is already at 5 missing statements of 29**, so something already covers most of it — find out what before concluding anything from a gap.

### Patching: rebind on the module that uses the name

`app/shared/community.py:12` is `from app import db, cache, plugins`, `:20` is `from app.shared.tasks import task_selector`, and `:23-27` imports `communities_banned_from`, `favorite_communities`, `blocked_communities` and others from `app.utils`. Each `from ... import` binds the name into `app.shared.community`'s own globals at import time, so **patching `app.utils.communities_banned_from` would NOT intercept** — rebind `app.shared.community.communities_banned_from`. The same applies to `app.shared.site`.

### Running anything

- **There is no host Python with flask or pytest.** Everything goes through `./run_tests.sh` (= `podman compose exec -T test-runner pytest "$@"`). There is no `--exec` flag.
- `compose.test.yaml:67` bind-mounts `./:/app:z`, so **repo files ARE shared with the container**. Only `/tmp` is not. Inline container Python as `podman-compose -f compose.test.yaml exec -T test-runner python -c "..."`.
- `run_tests.sh:83` runs `flask db upgrade` before every invocation.
- **Only the controller runs the full suite**, one pytest session at a time. **Never kill a running pytest** — teardown will not run and the test database is left corrupt, presenting as ~87 failures with `psycopg2.errors.UniqueViolation` on `site_pkey` that look like a code failure. Recover with `./run_tests.sh --down`. Runs have taken **360s to 687s** on a loaded machine.
- The suite needs `-o session_timeout=1800` as a command-line override. **`pytest.ini` must NOT be edited.**
- **`pytest` exits 1 on session timeout** and a pipeline eats the status — read `${PIPESTATUS[0]}`, or do not pipe.

### Measuring

- **Coverage takes the DOTTED module form** (`--cov=app.shared.community`). A path form collects nothing, writes no JSON, and exits 0 — a silent green failure.
- Write coverage JSON outside the repo; it lands in the **container's** `/tmp`.
- **`tests/check_coverage_floors.py` takes TWO arguments**, fails closed on one, and **counts a floored module absent from the report as 0.0**, so the floors check must run against a **`--cov=app`** JSON.
- **Test counts come from pytest's own output.** **A count is a claim** — publish the derivation beside it.

### Editing and verifying

- **Delete nothing the task did not create.**
- **`git checkout -- app/` is BANNED** while the round holds an uncommitted production change. Reverse edits by hand and re-read the restored line.
- **`git diff --quiet -- app/` is THE tree check**, not `wc -l`.
- **Verify against the COMMIT OBJECT, never the working tree**: `git show HEAD:<path>`, commit-to-commit `git diff --numstat`, and always `git status --porcelain`. A `git commit --amend` without staging changes the message only while every working-tree check passes. **And `--amend` targets HEAD** — a fix round on an earlier task's commit after a later task has landed will amend the **wrong commit**. If your commit is not HEAD, commit on top instead and say so.
- Re-derive every line number with `awk 'NR>=X && NR<=Y {printf "%d\t%s\n",NR,$0}' FILE`. **Never an `awk` that assigns to a field.**
- **`/usr/bin/grep`, not the interactive `grep`.**
- **No duplicate test names:** `/usr/bin/grep -oE "^ *def (test_[a-z0-9_]+)" FILE | sed 's/^ *//' | sort | uniq -d`. Use `^ *def test_` — class-nested tests make a `^def test_` grep return 0.
- **No ordered assertions over rows a query planner returned.** Compare sets.
- **A production change mid-round REOPENS COVERAGE.** Tasks 2 and 5 change production; Task 7 re-measures against the result.

### Evidence

- **An equivalence claim needs a proof of unkillability, never a failure to kill.**
- **A crash kill is not a kill** unless a viable non-crashing variant of the same fault also dies.
- **Fix-catching is not a unique kill.**
- **The five false-witness mechanisms:** (a) asserting on state something else sets unconditionally; (b) a fixture coincidence; (c) emptiness with no same-mechanism positive control; (d) an input taking the same path under both arms; (e) two conditions exercised only in lockstep.

### Fixtures

- **The id-1 admin trap.** `app/models.py:1259-1261` returns True from `is_admin` for id 1, and `tests/conftest.py:131-132` resets every sequence after every test, so **the first user minted is id 1 deterministically**. Burn the seat with a live `assert burn.id == 1`.
- **`make_community`** (`tests/factories.py:124`) hardcodes `instance_id=1, user_id=1`, so an Instance and a User must already occupy id 1 when it is called. The id-1 burn satisfies both.
- `make_community_member(user, community, is_moderator=False)` at `tests/factories.py:384`. `ban_user_from_community(user, community)` at `:397`. `make_user(instance, name, local=False, with_keys=False)` at `:41`. `web_ctx(app, user, query_string='')` at `:1216`, `@contextmanager`-decorated. `bearer(user)` at `:1228`.

### Committing

- `git commit -F <file>`, never `-m`. Lowercase `type:` subject prefix, prose body.
- Trailers, the last two lines, **a fixed campaign literal — NOT the model running the task**:
  ```
  Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01BF7kVN2spWD86NYKWyGrVn
  ```

### Scope

- **Production changes this round: exactly two** — Task 2's `return` and Task 5's `and`. Everything else that surfaces is **registered, not fixed**. The user's standing policy is that the register backlog waits until coverage is complete; defects inside the round's own target modules are still pinned and fixed.
- Register from **D595**. `tests/README.md` facts from **256**.

---

## File Structure

**Created:**

| File | Responsibility |
|---|---|
| `tests/test_shared_site.py` | `block_remote_instance`, `unblock_remote_instance`, and the local-instance guard. |
| `tests/test_shared_community_membership.py` | `join_community`, `leave_community`, `block_community`, `unblock_community`, `subscribe_community`, `favorite_community`. |

**Modified:**

| File | Change |
|---|---|
| `app/shared/site.py:19` | Gains `return`. **Task 2 only.** |
| `app/shared/community.py:60` | `or` becomes `and`. **Task 5 only.** |
| `coverage_floors.ini` | One new entry, for `site.py`. **Task 7 only.** |
| `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` | Entries from D595. **Task 9 only.** |
| `tests/README.md` | Facts from 256. **Task 9 only.** |

Two test files, one per module. `community.py`'s membership group gets its own file rather than joining a future `test_shared_community_*.py` sprawl, because three further groups follow and each should own its file.

---

### Task 1: Pin `app/shared/site.py`, including the guard that does not guard

**Files:**
- Create: `tests/test_shared_site.py`
- Read: `app/shared/site.py` (whole, 49 lines)

**Interfaces:**
- Consumes: nothing.
- Produces: `_seed_blocker()` returning a `SimpleNamespace` with `.blocker` (a local `User` that is not id 1), `.local` (the `Instance` at id 1) and `.remote` (an `Instance` that is not id 1), used again by Task 2.

**Target:** `missing_lines [19, 32]`, `missing_branches [[16,19],[22,29],[29,32],[39,46]]` — 2 statements, 4 arcs, at 86.957%.

**The defect this task pins.** `app/shared/site.py:14-23`:

```
14    if instance_id == 1:
15        msg = 'You cannot block the local instance.'
16        if src == SRC_API:
17            raise Exception(msg)
18        else:
19            flash(_(msg), 'error')          <- no return
21    existing = db.session.query(InstanceBlock).filter_by(user_id=user_id, instance_id=instance_id).first()
22    if not existing:
23        db.session.add(InstanceBlock(user_id=user_id, instance_id=instance_id))
```

The API arm raises. **The web arm flashes and falls through, so `:23` creates the block the message just refused.** Both twins return — `app/shared/user.py:29-31` and `app/shared/domain.py` — so `site.py` is the outlier.

**Write the pin so its inversion is a visible edit**: a docstring saying PINS A DEFECT, and an assertion that the `InstanceBlock` **is** created.

- [ ] **Step 1: Write the file**

```python
"""`block_remote_instance` and `unblock_remote_instance` (app/shared/site.py:11-49).

The module was at 86.957% before this file: 2 statements and 4 arcs missing.

ONE TEST HERE PINS A DEFECT. app/shared/site.py:14-19 refuses to block the
local instance -- the API arm raises at :17, the web arm flashes at :19 --
but the web arm does NOT return, so control falls to :21 and :23 creates the
block anyway. Both twins return: app/shared/user.py:29-31's
block_another_user flashes then returns, and app/shared/domain.py does the
same. That test asserts today's wrong behaviour and is INVERTED by the task
that adds the return. It says PINS A DEFECT in its docstring.

The defect is reachable through ordinary routes rather than only by a
crafted call: app/post/routes.py:1480 passes post.instance_id and
app/user/routes.py:881 passes user.instance_id, and app/shared/post.py:218
builds a Post with instance_id=user.instance_id, which is 1 for a local
user. Clicking "block instance" on a local post blocks your own instance.
"""
from types import SimpleNamespace

import pytest
from flask import get_flashed_messages

from app import db
from app.constants import SRC_API, SRC_WEB
from app.models import Instance, InstanceBlock
from app.shared.site import block_remote_instance, unblock_remote_instance
from tests.factories import bearer, make_instance, make_user, web_ctx


def _seed_blocker():
    """A local user who is not id 1, and a remote Instance that is not id 1.

    The first user minted in any test is id 1 deterministically --
    tests/conftest.py:131-132 runs `SELECT setval(c.oid, 1, false)` over every
    sequence after each test -- and `User.is_admin` (app/models.py:1259-1261)
    returns True for id 1 regardless of roles. Nothing in THIS module reads
    is_admin, but the burn is kept so a later test added to this file cannot
    inherit the trap silently.

    The burn ALSO makes the first Instance row id 1, which is what
    `block_remote_instance`'s :14 guard tests against -- so `local` below is
    genuinely the local instance and `remote` genuinely is not.
    """
    local = make_instance('test.piefed.local', software='piefed')
    assert local.id == 1, 'instance id 1 is the local instance; re-derive if this moved'

    burn = make_user(local, 'burn-the-id-1-seat', local=True)
    assert burn.id == 1, 'the id-1 admin trap moved; re-derive before trusting this'

    blocker = make_user(local, 'blocker', local=True)
    remote = make_instance('peer.example')
    db.session.commit()
    return SimpleNamespace(blocker=blocker, local=local, remote=remote)


def test_block_remote_instance_api_creates_the_block_and_returns_the_blocker(app, db_session):
    s = _seed_blocker()

    returned = block_remote_instance(s.remote.id, SRC_API, bearer(s.blocker))

    assert returned == s.blocker.id
    assert db.session.query(InstanceBlock).filter_by(
        user_id=s.blocker.id, instance_id=s.remote.id).count() == 1


def test_block_remote_instance_web_creates_the_block_and_returns_none(app, db_session):
    """:32 returns None and leaves the flash to its caller.

    Both the return value and the row are asserted: a function that created
    nothing would also return None.
    """
    s = _seed_blocker()

    with web_ctx(app, s.blocker):
        returned = block_remote_instance(s.remote.id, SRC_WEB)

    assert returned is None
    assert db.session.query(InstanceBlock).filter_by(
        user_id=s.blocker.id, instance_id=s.remote.id).count() == 1


def test_block_remote_instance_is_idempotent(app, db_session):
    """:22's `if not existing` -- the false arm. The second call must not add
    a second row and must still return the blocker's id from :30."""
    s = _seed_blocker()
    block_remote_instance(s.remote.id, SRC_API, bearer(s.blocker))

    returned = block_remote_instance(s.remote.id, SRC_API, bearer(s.blocker))

    assert returned == s.blocker.id
    assert db.session.query(InstanceBlock).count() == 1


def test_block_remote_instance_api_refuses_the_local_instance(app, db_session):
    """:16-17. The API arm raises and never reaches :21.

    THIS IS THE CONTROL, not a pin: it is the arm that already refuses
    correctly and must keep refusing after the web arm is fixed. The row
    count is asserted too, because a raise that happened AFTER the insert
    would still satisfy pytest.raises.
    """
    s = _seed_blocker()

    with pytest.raises(Exception, match='cannot block the local instance'):
        block_remote_instance(1, SRC_API, bearer(s.blocker))

    assert db.session.query(InstanceBlock).count() == 0


def test_block_remote_instance_web_blocks_the_local_instance_anyway(app, db_session):
    """PINS A DEFECT. :18-19 flashes the refusal and does NOT return, so :23
    creates the block the message just said was impossible.

    BOTH halves are asserted, and the second is the defect: the flash is what
    makes this look correct to a reader, and the row is what proves it is
    not. Asserting only the flash would pass against the fixed code too.

    THIS ASSERTION IS INVERTED by the task that adds the return.
    """
    s = _seed_blocker()

    with web_ctx(app, s.blocker):
        block_remote_instance(1, SRC_WEB)
        flashed = get_flashed_messages()

    assert flashed == ['You cannot block the local instance.']
    assert db.session.query(InstanceBlock).filter_by(
        user_id=s.blocker.id, instance_id=1).count() == 1


def test_unblock_remote_instance_api_removes_the_block(app, db_session):
    s = _seed_blocker()
    db.session.add(InstanceBlock(user_id=s.blocker.id, instance_id=s.remote.id))
    db.session.commit()

    returned = unblock_remote_instance(s.remote.id, SRC_API, bearer(s.blocker))

    assert returned == s.blocker.id
    assert db.session.query(InstanceBlock).count() == 0


def test_unblock_remote_instance_web_removes_the_block_and_returns_none(app, db_session):
    s = _seed_blocker()
    db.session.add(InstanceBlock(user_id=s.blocker.id, instance_id=s.remote.id))
    db.session.commit()

    with web_ctx(app, s.blocker):
        returned = unblock_remote_instance(s.remote.id, SRC_WEB)

    assert returned is None
    assert db.session.query(InstanceBlock).count() == 0


def test_unblock_remote_instance_leaves_another_users_block_alone(app, db_session):
    """:38's filter is on BOTH user_id and instance_id.

    THE BYSTANDER'S ROW IS SEEDED FIRST, DELIBERATELY. `.first()` at :38 has
    no ORDER BY, so a heap scan over two rows inserted in one transaction
    returns them in insertion order. If the subject's row were seeded first, a
    mutant narrowing the filter to instance_id alone would return and delete
    that same row, and this assertion would pass either way -- false-witness
    mechanism (b). Seeded this way round, the mutant deletes the bystander's
    row and the assertion fails, which is the kill. Do not "tidy" the order.
    """
    s = _seed_blocker()
    bystander = make_user(s.local, 'bystander', local=True)
    db.session.add(InstanceBlock(user_id=bystander.id, instance_id=s.remote.id))
    db.session.add(InstanceBlock(user_id=s.blocker.id, instance_id=s.remote.id))
    db.session.commit()

    unblock_remote_instance(s.remote.id, SRC_API, bearer(s.blocker))
    db.session.expire_all()

    owners = {b.user_id for b in db.session.query(InstanceBlock).all()}
    assert owners == {bystander.id}


def test_unblock_remote_instance_is_idempotent(app, db_session):
    """:39's `if existing` -- the false arm. No block exists, so the body is
    skipped and :47 still returns the caller's id."""
    s = _seed_blocker()

    returned = unblock_remote_instance(s.remote.id, SRC_API, bearer(s.blocker))

    assert returned == s.blocker.id
    assert db.session.query(InstanceBlock).count() == 0
```

- [ ] **Step 2: Run, and treat every failure as a finding**

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh tests/test_shared_site.py -v
echo "PYTEST_EXIT=$?"
```

**`test_block_remote_instance_web_blocks_the_local_instance_anyway` must PASS.** It asserts today's wrong behaviour. If it fails, the defect is not what this plan says and Task 2's fix is built on it — **stop and report**.

If `make_instance('test.piefed.local')` does not produce id 1, read `tests/factories.py:34-38` and say what you found; the whole file's `instance_id == 1` reasoning depends on it.

- [ ] **Step 3: Measure the module to zero**

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh tests/test_shared_site.py --cov=app.shared.site \
    --cov-report=json:/tmp/site.json -q
echo "PYTEST_EXIT=$?"
podman-compose -f compose.test.yaml exec -T test-runner python -c "
import json
d = json.load(open('/tmp/site.json'))['files']['app/shared/site.py']
print('percent', d['summary']['percent_covered'])
print('missing_lines   ', d['missing_lines'])
print('missing_branches', d['missing_branches'])
"
```

Note the **dotted** `--cov=app.shared.site`. **First check whether anything else already covers this module** — `/usr/bin/grep -rln "block_remote_instance" tests/` — and if so include those files and say so, because a module's coverage is often not produced by the file named after it.

Both lists must be `[]`.

- [ ] **Step 4: Checks and commit**

```bash
cd /home/blentz/git/pyfedi
/usr/bin/grep -oE "^ *def (test_[a-z0-9_]+)" tests/test_shared_site.py | sed 's/^ *//' | sort | uniq -d
git diff --quiet -- app/ && echo "TREE CLEAN under app/" || echo "TREE DIRTY under app/"
git add tests/test_shared_site.py
git commit -F <message-file>
git status --porcelain
git show HEAD:tests/test_shared_site.py | /usr/bin/grep -cE "^ *def test_"
```

Subject: `test: cover block_remote_instance and unblock_remote_instance`. Body: the module was at 86.957; that one test pins the fall-through and will be inverted; the deliberate bystander-first seeding order and why.

---

### Task 2: Add the missing `return` — production change one

**Files:**
- Modify: `app/shared/site.py:19`
- Modify: `tests/test_shared_site.py` (invert one test)

**Interfaces:**
- Consumes: Task 1's pin and control.
- Produces: `app/shared/site.py` refusing the local instance on both arms.

- [ ] **Step 1: Re-derive the lines before touching them**

```bash
cd /home/blentz/git/pyfedi
awk 'NR>=11 && NR<=25 {printf "%d\t%s\n",NR,$0}' app/shared/site.py
```

Use what this prints, never a number from this plan.

- [ ] **Step 2: Make the edit**

Add `return` immediately after `:19`'s `flash(...)`, at the same indentation as the `flash` call:

```python
        if src == SRC_API:
            raise Exception(msg)
        else:
            flash(_(msg), 'error')
            return
```

**From here until this task commits, `git checkout -- app/` is BANNED.** It would silently revert the fix — this exact command destroyed a real fix in an earlier sub-project. Reverse edits by hand and re-read the restored line with `awk`.

- [ ] **Step 3: Confirm the edit and nothing else**

```bash
cd /home/blentz/git/pyfedi
git diff -- app/shared/site.py
```

Expected: one line added, nothing removed, no whitespace change elsewhere.

- [ ] **Step 4: Watch exactly one test fail**

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh tests/test_shared_site.py -v
echo "PYTEST_EXIT=$?"
```

Expected: exactly **one** failure, `test_block_remote_instance_web_blocks_the_local_instance_anyway`, on its `InstanceBlock` count assertion. **`test_block_remote_instance_api_refuses_the_local_instance` must keep passing** — it is the control, and the fix does not touch the API arm.

**A different count or a different test means stop and report.**

- [ ] **Step 5: Invert the pin**

Rename to `test_block_remote_instance_web_refuses_the_local_instance`, rewrite the docstring in the present tense, and assert the block was **not** created:

```python
    assert flashed == ['You cannot block the local instance.']
    assert db.session.query(InstanceBlock).count() == 0
```

**Keep the flash assertion.** An empty-count assertion alone is false-witness mechanism (c) — emptiness with no positive control. The flash proves the function reached `:19` rather than failing earlier, and the sibling tests that DO create blocks are the same-mechanism control proving insertion works in this harness. Say that in the docstring.

Also rewrite the module docstring's "ONE TEST HERE PINS A DEFECT" paragraph: say what `:19` read before, that the `return` was added in this round, and that the test below is the inverted pin.

- [ ] **Step 6: Green, then re-measure**

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh tests/test_shared_site.py -v
echo "PYTEST_EXIT=$?"
./run_tests.sh tests/test_shared_site.py --cov=app.shared.site \
    --cov-report=json:/tmp/site2.json -q
podman-compose -f compose.test.yaml exec -T test-runner python -c "
import json
d = json.load(open('/tmp/site2.json'))['files']['app/shared/site.py']
print('percent', d['summary']['percent_covered'])
print('missing_lines   ', d['missing_lines'])
print('missing_branches', d['missing_branches'])
"
```

**A production change reopens coverage.** The new `return` is a new statement and `:19`'s block now has an exit — if a gap appears, close it and say so.

- [ ] **Step 7: Sweep for collateral**

```bash
cd /home/blentz/git/pyfedi
/usr/bin/grep -rln "block_remote_instance" tests/
```

Run every file this returns. Seven production call sites pass `SRC_WEB`; if any test asserted the old fall-through, it will fail now and that is a finding, not an obstacle.

- [ ] **Step 8: Commit**

Subject: `fix: return after refusing to block the local instance`. Body: what `:19` did, that both twins return, that the block was created despite the message, the route chain that makes it reachable, and that the inverted test was written first against the old behaviour.

---

### Task 3: `block_community` and `unblock_community`

**Files:**
- Create: `tests/test_shared_community_membership.py`
- Read: `app/shared/community.py:85-118`

**Interfaces:**
- Consumes: nothing.
- Produces: `_seed_member()` returning a `SimpleNamespace` with `.user`, `.community` and `.instance`, used by Tasks 4 and 6 in the same file.

**Target:** `block_community` 11 statements / 6 arcs; `unblock_community` 11 / 6. **22 / 12.**

**These are the fifth instance of a shape this campaign has closed four times** — `block_another_user`, `block_domain`, `block_remote_instance`, and now this. `tests/test_shared_domain.py` is the model: the `src` fork, `bearer`, `web_ctx`, the id-1 burn, and the deliberate bystander-first seeding order. **Read it before writing.**

**The shape** (`app/shared/community.py:85-118`):

```
:86   if src == SRC_API: user_id = authorise_api_user(auth)  else  current_user.id
:91   existing = CommunityBlock.query.filter_by(user_id=user_id, community_id=community_id).first()
:92   if not existing:                    /  :110  if existing_block:
:93-95    add + commit + cache.delete_memoized(blocked_communities, user_id)
:111-113  delete + commit + same
:97-100 / :115-118  if src == SRC_API: return user_id  else: return None
```

- [ ] **Step 1: Write the file**

Module docstring must state the group, its measured figures, and that four of its six functions are twins of already-closed modules. Then `_seed_member()`:

```python
def _seed_member():
    """A local user who is not id 1, and a Community.

    ORDER MATTERS. make_community (tests/factories.py:124) hardcodes
    instance_id=1 and user_id=1, so an Instance and a User must already
    occupy id 1 when it is called. The instance and the id-1 burn below fill
    both seats, which is why the burn is load-bearing twice over: it keeps
    `user` off the id-1 admin shortcut (app/models.py:1259-1261) AND gives
    the community an owner that exists.
    """
    instance = make_instance('test.piefed.local', software='piefed')
    burn = make_user(instance, 'burn-the-id-1-seat', local=True)
    assert burn.id == 1, 'the id-1 admin trap moved; re-derive before trusting this'

    user = make_user(instance, 'member', local=True)
    community = make_community('microblogs')
    db.session.commit()
    return SimpleNamespace(user=user, community=community, instance=instance)
```

Then eight tests, mirroring `tests/test_shared_domain.py` one for one: API creates and returns the id; web creates and returns None; block is idempotent (`:92`'s false arm); a block is scoped to the calling user (seed a bystander's row **first**, per the note above, and compare owners as a **set**); and the four unblock equivalents including `:110`'s false arm.

- [ ] **Step 2: Run and measure**

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh tests/test_shared_community_membership.py -v
echo "PYTEST_EXIT=$?"
./run_tests.sh tests/test_shared_community_membership.py --cov=app.shared.community \
    --cov-report=json:/tmp/comm.json -q
podman-compose -f compose.test.yaml exec -T test-runner python -c "
import json
fns = json.load(open('/tmp/comm.json'))['files']['app/shared/community.py']['functions']
for name in ('block_community','unblock_community'):
    k = [x for x in fns if x.endswith(name)][0]
    print(name, fns[k]['missing_lines'], fns[k]['missing_branches'])
"
```

Both must print `[] []`. The module's other functions stay open; later tasks take them.

- [ ] **Step 3: Checks and commit**

Duplicate-name check with `^ *def test_`, `git diff --quiet -- app/`, commit, then verify against the commit object.

Subject: `test: cover block_community and unblock_community`.

---

### Task 4: `join_community` and `leave_community`, pinning the De Morgan guard

**Files:**
- Modify: `tests/test_shared_community_membership.py` (append)
- Read: `app/shared/community.py:32-82`

**Interfaces:**
- Consumes: `_seed_member()` from Task 3.
- Produces: nothing.

**Target:** `join_community` 15 / 10; `leave_community` 16 / 8. **31 / 18.**

**`join_community` has a shape no closed sibling prepares** (`:32-53`):

```
:33   if src == SRC_API: user_id = authorise_api_user(auth)
:36   send_async = not (current_app.debug or src == SRC_WEB)
:38   sync_retval = task_selector('join_community', send_async, user_id=..., community_id=..., src=src)
:40   if send_async or sync_retval is True:
:41-46    insert CommunityMember if not already a member
:48   if src == SRC_API: return user_id
:50   elif src == SRC_PLD: return sync_retval      <- a THIRD return value
:52   else: return
```

`:36` is a compound and `:40` is a compound — **four operands between them, so four tests or some stay deletable.** `SRC_WEB` forces `send_async` False; `SRC_API` with `current_app.debug` False forces it True. `:50`'s `SRC_PLD` arm returns `sync_retval` itself, which no twin does.

**Patch `task_selector` by rebinding on `app.shared.community`** — `:20`'s `from app.shared.tasks import task_selector` already bound it into that module's globals, so patching `app.shared.tasks` would not intercept. Its return value is what `:40` reads, so the double must return a controllable value.

**`leave_community:57-82` carries the defect this round fixes second.** `:60` is `if not cm.is_owner or not cm.is_moderator:` — by De Morgan `not (owner and moderator)`, so the leave path runs unless the member is **both**. `CommunityMember.is_moderator` and `.is_owner` are independent booleans defaulting False (`app/models.py:3514-3515`), and federated moderators are created with `is_moderator=True` and no `is_owner` (`app/activitypub/util.py:959`). **So a plain moderator leaves freely and `:75`'s "step down first" message never fires for them.**

Write these:

- **`:59`'s `.one()`** raises `NoResultFound` for a non-member, where the siblings use `.first()`. One test, and **register the divergence**.
- **`:68`'s `if src == SRC_WEB and not bulk_leave:`** — two operands, two tests.
- **PINS A DEFECT:** a member with `is_moderator=True, is_owner=False` calls `leave_community` and **succeeds** — assert the `CommunityMember` row is gone and no exception was raised. That is the defect; Task 5 inverts it.
- **The control:** a member who is neither owner nor moderator leaves successfully, and **must keep passing after the fix**.
- **The other control:** a member who is **both** owner and moderator is refused today and after — `SRC_API` raises at `:73`, the web arm flashes at `:75` and returns.

- [ ] **Step 1: Append the tests**

Follow Task 3's conventions. Every defect-pinning test says PINS A DEFECT and names what Task 5 will invert.

- [ ] **Step 2: Run and measure both functions to `[] []`**

Same command shape as Task 3, naming `join_community` and `leave_community`.

- [ ] **Step 3: Checks and commit**

Subject: `test: cover join_community and leave_community, pinning the moderator guard`. Body: `join_community`'s sync/async fork and its third return value; `:59`'s `.one()` divergence; and that one test asserts a moderator leaves freely and will be inverted.

---

### Task 5: Fix the De Morgan guard — production change two

**Files:**
- Modify: `app/shared/community.py:60`
- Modify: `tests/test_shared_community_membership.py` (invert one test)

**Interfaces:**
- Consumes: Task 4's pin and its two controls.
- Produces: `app/shared/community.py` refusing any owner or moderator.

- [ ] **Step 1: Re-derive**

```bash
cd /home/blentz/git/pyfedi
awk 'NR>=57 && NR<=78 {printf "%d\t%s\n",NR,$0}' app/shared/community.py
```

- [ ] **Step 2: Make the edit**

`:60` becomes:

```python
    if not cm.is_owner and not cm.is_moderator:
```

One word. **`git checkout -- app/` is BANNED for the rest of this task.**

- [ ] **Step 3: Confirm the edit**

```bash
cd /home/blentz/git/pyfedi
git diff -- app/shared/community.py
```

Expected: one line changed, `or` to `and`, nothing else.

- [ ] **Step 4: Watch exactly one test fail**

Expected: the moderator pin fails. **Both controls must keep passing** — the plain member still leaves, the owner-and-moderator is still refused. **A different set means stop and report.**

- [ ] **Step 5: Invert the pin**

Rename to say the moderator is refused, rewrite the docstring in the present tense, and assert that the `CommunityMember` row **survives** and that the refusal happened — `pytest.raises(Exception, match='Step down as a moderator')` on the API arm, or the flash text on the web arm. **Assert the row survives as well as the refusal**: a raise alone would also satisfy a mutant that raised before deleting.

Update the module docstring's pin paragraph to describe history.

- [ ] **Step 6: Green, re-measure, sweep**

Re-measure `leave_community` — the fix changes which arc is taken and **a production change reopens coverage**. Then `/usr/bin/grep -rln "leave_community" tests/` and run what it returns; `bulk_leave` callers elsewhere may depend on the old behaviour, and if one does that is a finding.

- [ ] **Step 7: Commit**

Subject: `fix: refuse any owner or moderator leaving a community, not only both`. Body: the De Morgan reading, that `is_owner` and `is_moderator` are independent and federated moderators get only the latter, so the guard was dead for the population it names.

---

### Task 6: `subscribe_community` and `favorite_community`

**Files:**
- Modify: `tests/test_shared_community_membership.py` (append)
- Read: `app/shared/community.py:394-484`

**Interfaces:**
- Consumes: `_seed_member()` from Task 3.
- Produces: nothing.

**Target:** `subscribe_community` 5 / 5; `favorite_community` 30 / 18. **35 / 23.**

**THIS TASK CONTAINS TWO INSTANCES OF THE SHAPE THIS CAMPAIGN RETRACTED A RULING OVER. Read this section twice before writing.**

`subscribe_community:394-438`:

```
:398  if src == SRC_WEB:
:399      subscribe = False if community.notify_new_posts(user_id) else True
:401  existing_notification = NotificationSubscription.query.filter_by(
          entity_id=community_id, user_id=user_id, type=NOTIF_COMMUNITY).first()
:403  if subscribe == False:
:404      if existing_notification: delete
:407      else:  msg = 'A subscription for this community did not exist.'
:409          if src == SRC_API: raise
:411          else: flash(_(msg))          <- :412
:414  else:
:415      if existing_notification:  msg = '... already existed.'
:417          if src == SRC_API: raise
:419          else: flash(_(msg))          <- :420
```

**On `SRC_WEB`, `:399` recomputes `subscribe` from the same row `:401` queries**, so the two are in lockstep: `subscribe == False` exactly when `existing_notification` is truthy. `:412` needs `subscribe False` **and** no row — contradictory. `:420` needs `subscribe True` **and** a row — contradictory. On `SRC_API`, `:409` and `:417` take their raise arms.

**So `:412` and `:420` are unreachable from either production caller — and they are NOT unreachable by a test.** Pass a **third** `src` value. `SRC_PLD = 4` (`app/constants.py:94`) is neither `SRC_WEB` nor `SRC_API`, so `:398`'s override is skipped and the caller's `subscribe` argument stands, and `:409`/`:417` take their else arms.

**`favorite_community:441-484` is the identical shape** — `:445-446` overrides from `favorite_communities(user_id)`, `:448` queries `CommunityFavorite`, and `:458`/`:466` are the same two unreachable flashes.

**The remedy is precedented three times.** `tests/test_shared_post_interactions.py:577` is `test_a_third_source_reaches_the_flash_branches_the_web_arm_cannot`; `tests/test_shared_reply_interactions.py:1018-1065` is the reply twin; sub-project 43's D564 retraction added the user twin. **Read the first one** — it uses `web_ctx` even for `SRC_PLD`, because `:396`'s else-arm reads `current_user.id` regardless of the source constant, and it asserts on flash **content** rather than on the return value alone.

**Every such test's docstring must say plainly that `SRC_PLD` is used purely to reach those statements and is not how production calls the function.** That disclosure is the campaign's convention and the reason these tests are legitimate rather than fabricated.

**Also cover:**

- `:422` and `:468`'s `if community_id in communities_banned_from(user_id):` — its rows come from `CommunityBan`, and `tests/factories.py:397`'s `ban_user_from_community(user, community)` creates them. Both arms, both functions.
- `:395` and `:442`'s `.filter_by(id=community_id, banned=False).one()` — a banned community raises `NoResultFound`.
- `:435-438` and `:481-484`'s return fork: `SRC_API` returns `user_id`, the web arm renders `community/_notification_toggle.html`.
- **`:479`'s `cache.delete_memoized(favorite_communities, user_id)` runs unconditionally**, outside the if/else, where `subscribe_community` has no such call at all. **Register the asymmetry**; do not fix it.

**`subscribe_community` is already at 5 missing statements of 29**, so something else already covers most of it. **Find what** — `/usr/bin/grep -rln "subscribe_community" tests/` — include it in every measurement, and report what you found. A gap measured against the wrong oracle is not a gap.

- [ ] **Step 1: Find the existing oracle, then append the tests**

- [ ] **Step 2: Run and measure both functions to `[] []`**, naming every oracle file in the command.

- [ ] **Step 3: Checks and commit**

Subject: `test: cover subscribe_community and favorite_community`. Body: the lockstep between the override and the lookup; that `SRC_PLD` reaches the two flash arms neither production caller can; and the `cache.delete_memoized` asymmetry.

---

### Task 7: Floors and the full suite — CONTROLLER ONLY

**Files:**
- Modify: `coverage_floors.ini`

**Only the controller runs this task.** One pytest session at a time, never killed.

- [ ] **Step 1: Full suite with whole-app coverage**

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh -o session_timeout=1800 --cov=app --cov-report=json:/tmp/sp45.json -q
echo "PYTEST_EXIT=$?"
```

`--cov=app`, not a narrow form — the floors checker counts an absent module as 0.0. Do not pipe.

- [ ] **Step 2: Read the modules as lists**

```bash
cd /home/blentz/git/pyfedi
podman-compose -f compose.test.yaml exec -T test-runner python -c "
import json, math
files = json.load(open('/tmp/sp45.json'))['files']
for p in ('app/shared/site.py','app/shared/community.py','app/shared/post.py',
          'app/shared/reply.py','app/shared/user.py','app/shared/domain.py'):
    d = files[p]; s = d['summary']
    print('%-28s %9.5f floor %3d' % (p, s['percent_covered'], math.floor(s['percent_covered'])))
    print('   lines', d['missing_lines'][:12], 'branches', d['missing_branches'][:8])
fns = files['app/shared/community.py']['functions']
for name in ('join_community','leave_community','block_community','unblock_community',
             'subscribe_community','favorite_community'):
    k = [x for x in fns if x.endswith(name)][0]
    print('  ', name, fns[k]['missing_lines'], fns[k]['missing_branches'])
"
```

`site.py` must be `[]`/`[]`. Group A's six functions must each be `[]`/`[]`. `post.py`, `reply.py`, `user.py` and `domain.py` must all still be 100 — **a regression in a closed module is a stop-and-report.**

- [ ] **Step 3: Add the one floor entry**

Only `site.py` gets a floor, at `floor(percent_covered)` from Step 2. **`community.py` gets NO floor this round** — it is one group of four, and a floor would ratchet against work not yet done. Record its measured percentage for the register instead.

```ini
app/shared/domain.py = 100
app/shared/site.py = <measured>
```

26 floors total.

- [ ] **Step 4: Full suite again with the floors check chained**

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh -o session_timeout=1800 --cov=app --cov-report=json:/tmp/sp45.json -q \
  && podman-compose -f compose.test.yaml exec -T test-runner \
       python tests/check_coverage_floors.py /tmp/sp45.json coverage_floors.ini
echo "CHAIN_EXIT=$?"
```

Expected: `All 26 module floors met.` Record pytest's own passed/skipped/duration.

- [ ] **Step 5: Commit**

Subject: `test: add a coverage floor for app/shared/site.py`. Body: the measured value, the empty lists, community.py's measured percentage and why it gets no floor.

---

### Task 8: Mutation pass

**Files:** none permanently. Every mutation is reversed before the next.

**Scope by the STATEMENT list, not the arc table.** Derive mechanically:

```bash
cd /home/blentz/git/pyfedi
podman-compose -f compose.test.yaml exec -T test-runner python -c "
import ast
for path in ('/app/app/shared/site.py','/app/app/shared/community.py'):
    tree = ast.parse(open(path).read())
    stmts = sorted({n.lineno for n in ast.walk(tree) if isinstance(n, ast.stmt)})
    compounds = sorted({n.lineno for n in ast.walk(tree) if isinstance(n, ast.BoolOp)})
    print(path, 'STATEMENTS', len(stmts), stmts)
    print(path, 'COMPOUNDS', len(compounds), compounds)
"
```

**Publish that command and its complete raw output beside every count.** For `community.py`, **mutate only Group A's six functions** — the other eleven are not covered yet and every mutant there would be a meaningless survivor. Say in the report which line ranges you scoped to and why.

- [ ] **Step 1: Measure each oracle before using it**

Confirm your chosen files reproduce Task 7's figures for `site.py` and for Group A's six functions. **An oracle that does not execute a function turns every mutant there into a survivor** — this happened twice in sub-project 43.

- [ ] **Step 2: Run the pass**

Apply one edit, run that module's oracle, record KILLED or SURVIVED, **reverse by hand**, re-read the restored line with `awk`. **`git checkout -- app/` is banned for this whole task** — `app/shared/site.py` and `app/shared/community.py` both carry a fix from this round.

Rules: a crash kill is not a kill unless a viable non-crashing variant also dies; fix-catching is not a unique kill; an operator can be structurally void; **an equivalence claim needs a proof of unkillability**; name the fact 75 cause that fits or say none does.

- [ ] **Step 3: The two mandatory mutations**

1. **Delete the `return` added at `site.py:19`.** The inverted pin from Task 2 must fail. If nothing dies, the fix is decorative.
2. **Restore `community.py:60`'s `or`.** The inverted pin from Task 5 must fail, and **both controls must keep passing** — if a control dies too, it was not the control it claimed to be.

- [ ] **Step 4: Prove the tree is clean**

```bash
cd /home/blentz/git/pyfedi
git diff --quiet -- app/ && echo "TREE CLEAN under app/" || echo "TREE DIRTY under app/"
git status --porcelain
awk 'NR>=17 && NR<=21 {printf "%d\t%s\n",NR,$0}' app/shared/site.py
awk 'NR==60 {printf "%d\t%s\n",NR,$0}' app/shared/community.py
```

The two `awk` calls confirm both of this round's fixes survived the pass.

No commit for this task.

---

### Task 9: Register the findings

**Files:**
- Modify: `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`
- Modify: `tests/README.md`

**Paths and conventions.** The register is that file — **not** `docs/superpowers/findings-register.md`, which does not exist. Rows are `| D### | location | description | status | evidence |`. The live next number is the **maximum** over every marker, per the file's own note:

```bash
cd /home/blentz/git/pyfedi
/usr/bin/grep -o 'Next free number: D[0-9]*' docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md | sort -u -t D -k2 -n | tail -1
```

Expected `D595`. **Do not edit an older marker** — append a new one. `tests/README.md` facts are `**NNN. HEADING`; derive the highest **numerically** (`| tr -d '*.' | sort -n | tail -1`) because a lexical sort returns `100`. Expected 255, so start at **256**.

- [ ] **Step 1: Register**

1. **`app/shared/site.py:19` — FIXED.** The refusal flashed and fell through, creating the block. Both twins return. Give the route chain that makes it reachable — `app/post/routes.py:1480` → `post.instance_id` → `app/shared/post.py:218` → `user.instance_id` = 1 for a local user.
2. **`app/shared/community.py:60` — FIXED.** The De Morgan reading, the independent booleans, and that federated moderators get `is_moderator` without `is_owner`, so the guard was dead for the population it names.
3. **The pattern.** Three consecutive rounds, three refusals that do not refuse — with sub-project 43's `if SRC_WEB:` and sub-project 44's mis-nested ban refusal. **It is invisible to coverage**: every line executes, and only an assertion about the outcome catches it. Register it as a defect class.
4. **`leave_community:59` uses `.one()`** where its siblings use `.first()`.
5. **`favorite_community:479`'s unconditional `cache.delete_memoized`**, which `subscribe_community` lacks entirely.
6. **`subscribe_community:412`/`:420` and `favorite_community:458`/`:466`** — reachable only through a third `src` value, the fourth and fifth instances of the shape D564 was retracted over. Record that they were **covered, not ruled unreachable**.
7. **`app/shared/community.py`'s measured percentage after Group A**, and that no floor was set because three groups remain.
8. **Every survivor from Task 8**, each with line, mutation and why nothing killed it.

- [ ] **Step 2: Facts from 256**

Candidates, all verified this round — add the genuinely new ones and **re-derive any count**:

- A guard that flashes without returning is invisible to coverage; assert what the guard was supposed to prevent.
- `not A or not B` is `not (A and B)` — a De Morgan slip makes a two-flag guard fire only when both flags are set, which is the opposite of the usual intent.
- `make_community` hardcodes `instance_id=1, user_id=1`, so the id-1 burn is load-bearing twice: it dodges the admin trap and supplies the community's owner.
- Instance id 1 is the local instance throughout this codebase.

- [ ] **Step 3: Commit**

Subject: `docs: register sub-project 45's findings and the membership test facts`. Body: how many entries, which were fixed versus pinned, and the D-range used.

---

## Success criteria

- Both guards fixed, each pinned first and each pin inverted, with **all three controls** still passing unchanged.
- `app/shared/site.py` at `missing_lines []` and `missing_branches []`, checked as lists, with a **new** `coverage_floors.ini` entry at its measured value. **26 floors total.**
- Group A's six functions each at `[]`/`[]`, with any line ruled unreachable carrying a **named** fact 75 cause and a proof — or an explicit statement that none fits.
- **No floor for `app/shared/community.py`.**
- **No regression in the five closed modules**: `post.py`, `reply.py`, `user.py`, `domain.py`, `site.py`.
- Full suite green, floors check chained with `&&` and **both** arguments against a `--cov=app` JSON.
- Findings registered from **D595**; `tests/README.md` facts from **256**.
- **Exactly two production changes**: the `return` and the `and`.

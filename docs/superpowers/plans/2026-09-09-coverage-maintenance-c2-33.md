# Coverage sub-project 33 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Take `monitor_healthy_instances`' two identity phases and the task's own outer handler to zero missing statements, and fix three production defects — closing `app/shared/tasks/maintenance.py` entirely.

**Architecture:** One new test file, `tests/test_shared_tasks_maintenance_identity.py`. No respx: every helper these blocks call is imported at module scope, so tests replace the names directly. Three production changes, each landing as its own commit with its own failing observation.

**Tech Stack:** pytest, SQLAlchemy 2.0.52, httpx, Flask, Celery. Tests run only through `./run_tests.sh`.

**Spec:** `docs/superpowers/specs/2026-09-09-coverage-maintenance-c2-33-design.md`

## Global Constraints

- **Delete nothing the task did not create.** `git checkout -- app/` is permitted ONLY as a restore step after a mutation probe.
- **Restore any probe before any point where you might stop.** A process that dies mid-probe cannot restore; this campaign has had one do exactly that, and the controller had to clean it up.
- **There is NO host Python with flask or pytest.** Everything runs through `./run_tests.sh` = `podman compose exec -T test-runner pytest "$@"` (`run_tests.sh:85`).
- **`pytest` exits 1 on a session timeout and `run_tests.sh` propagates it. A shell pipeline eats the exit status** — do not pipe, or read `${PIPESTATUS[0]}`.
- **Coverage takes the dotted module form** `--cov=app.shared.tasks.maintenance`. A path form collects nothing, writes no JSON, and exits 0 — a silent green failure.
- **Write coverage JSON outside the repository.** `/app` is bind-mounted, so `--cov-report=json:coverage.json` leaves an artifact in the tree. Use `json:/tmp/<name>.json`.
- **Write only to the files a task names, plus its report file.** Create, modify or delete nothing else anywhere on the filesystem.
- **Test counts come from pytest's own collection output**, never `grep -c '^def test_'`.
- Before believing a failure that looks environmental, run `./run_tests.sh --down` and retry once.
- **Mutations:** one at a time, dry-run without `-i` and read the produced line first, apply, run, restore, then assert an empty `git diff -- app/` and the expected `wc -l`. Paste every dry-run line and every failure.
- Commit with `git commit -F <file>`, never `-m`. Lowercase `type:` subject prefix, body in normal English prose.
- Commit trailers, last two lines, in this order:
  `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`
  `Claude-Session: https://claude.ai/code/session_01BF7kVN2spWD86NYKWyGrVn`
- **No ordered assertions over rows a query planner returned.** `:540`, `:658` and `:720` all iterate queries; compare sets.
- **Seeded instances must not be id 1.** `:543` excludes it and the `db_session` teardown resets sequences, so the first `Instance` a test seeds becomes exactly that id.
- Re-derive every line number against the current tree with **numbered output** (`awk 'NR>=X && NR<=Y {printf "%d\t%s\n",NR,$0}' FILE`); never count lines of an unnumbered `sed -n 'X,Yp'` range.
- Where prose describes a **statement**, cite that statement's line — not the `if` guarding it, not the `def` above it.
- **Every test must be able to fail**, and its docstring must name the single-line regression it catches **and the arm**. An oracle over a field several arms write pins nothing — `failures` is written from six arms in this function.
- `app/shared/tasks/maintenance.py` is **1216 lines** at the start of this plan. The module floor is **89**.

---

### Task 1: Open the file, probe the failure mode, cover the outer handler

**Files:**
- Create: `tests/test_shared_tasks_maintenance_identity.py`
- Read: `app/shared/tasks/maintenance.py:534-547`, `:736-741`

**Interfaces:**
- Produces: `_seed_instance`, `_Recorder`, `_response`, `_quiet_http_half`, `_site_payload`, `_mbin_payload`, and the module docstring every later task extends.

**The probe answers a question every later task depends on, and this round predicts the OPPOSITE of the last two.** Sub-projects 31 and 32 both found unmatched requests being *swallowed* into failure branches. Here `get_request` raises, and the `finally`'s `if response:` at `:689` should raise `UnboundLocalError` **before** `except Exception` at `:685` can absorb anything — so a forgotten patch should kill the task rather than quietly redirect it. **Confirm by observation and record the result. Do not assume it.** If the probe contradicts the prediction, that is the most valuable thing you can report.

- [ ] **Step 1: Write the prelude**

```python
"""Group C's identity phases in `app/shared/tasks/maintenance.py`.

Sub-projects 29, 30, 31 and 32 closed the rest of this module. This file
covers what was left: the two blocks of `monitor_healthy_instances` gated on
`instance.software`, plus the task's own outer handler.

  Lemmy/PieFed admin roles and custom emoji  `:637-691`
  MBIN admin roles                           `:698-733`
  the task's outer handler                   `:736-738`

ENTRY IS GATED ON `software`. `:637` needs 'lemmy', 'piefed' or 'pylova';
`:698` needs 'mbin'. Sub-project 32's fixtures used `make_instance`'s
'mastodon' default, so neither block ran and both `if` statements were
covered only on their false arms. Every fixture here sets a matching value --
which means a test that sets `software` and forgets about these blocks enters
them anyway. Sub-project 32 learned that when a version test entered the
Lemmy block by accident and crashed.

THE HTTP HALF RUNS FIRST. An instance with `software` set still goes through
discovery and the fetch block above. `_quiet_http_half` neutralises them so a
test observes only the identity phases.

THE OUTER HANDLER IS REACHED THROUGH `:547`. `instance_banned` is called at
loop level, outside every `try`, so a raise there is the one failure that
reaches `:736` rather than being caught per-instance.

`get_request` RAISES, unlike `get_request_instance` which returns a synthetic
500. Task 1 probed what that means for a test that forgets to patch it.
"""

from datetime import timedelta

import httpx
import pytest

from app import db
from app.models import Emoji, Instance, InstanceRole, User, utcnow
from app.shared.tasks.maintenance import monitor_healthy_instances
from tests.factories import make_instance, make_user


class _Recorder:
    """`calls` holds one `(args, kwargs)` tuple per invocation.

    So `c[0][0]` is the first positional argument. Same shape as the other
    maintenance test files' recorders; kept identical so a reader moving
    between them does not have to re-learn it.
    """

    def __init__(self, result=None):
        self.calls = []
        self.result = result

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


def _response(status_code=200, payload=None):
    """A real `httpx.Response`, because production calls `.json()` and `.close()`."""
    if payload is None:
        return httpx.Response(status_code=status_code)
    return httpx.Response(status_code=status_code, json=payload)


def _seed_instance(domain, software='mastodon'):
    """Seed an instance the task will actually see.

    `monitor_healthy_instances:543` filters `Instance.id != 1`, and the
    `db_session` teardown resets every sequence with
    `SELECT setval(c.oid, 1, false)` (`tests/conftest.py:131`), so the FIRST
    instance a test seeds lands on exactly the id the task excludes.

    The reserved row absorbs that id and is excluded by STATE rather than by
    id -- dormant and gone_forever, with `start_trying_again` a year out -- so
    it stays invisible wherever it actually lands.
    """
    if db.session.query(Instance).filter_by(domain='reserved-id-one.example').first() is None:
        reserved = Instance(domain='reserved-id-one.example', software='mastodon')
        reserved.dormant = True
        reserved.gone_forever = True
        reserved.start_trying_again = utcnow() + timedelta(days=365)
        db.session.add(reserved)
        db.session.commit()
    return make_instance(domain, software=software)


def _quiet_http_half(monkeypatch):
    """Neutralise the fetch and discovery blocks above the identity phases.

    Every instance here has `software` set, so the HTTP half runs first. A 404
    from `get_request_instance` leaves the instance online -- two failure
    increments, well under `:609`'s threshold of 5 -- and drives no state the
    identity assertions read.
    """
    monkeypatch.setattr(
        'app.shared.tasks.maintenance.get_request_instance',
        lambda *args, **kwargs: _response(404))


def _site_payload(*actor_ids, emojis=None):
    """A Lemmy `/api/v3/site` body: admins, and optionally custom emoji.

    `:645` reads `admin['person']['actor_id']`; `:668` reads
    `emoji['custom_emoji']` and `emoji['keywords']`.
    """
    return {
        'admins': [{'person': {'actor_id': a}} for a in actor_ids],
        'custom_emojis': emojis if emojis is not None else [],
    }


def _emoji(shortcode, url='https://peer.example/e.png', category='cat', keywords=('happy',)):
    """One entry for `_site_payload`'s `custom_emojis` list."""
    return {
        'custom_emoji': {'shortcode': shortcode, 'image_url': url, 'category': category},
        'keywords': [{'keyword': k} for k in keywords],
    }


def _mbin_payload(*items):
    """An MBIN `/api/users/admins` body. `:705` reads `instance_data['items']`."""
    return {'items': list(items)}
```

- [ ] **Step 2: Write the throwaway probe**

Add this temporarily. It is deleted before the commit.

```python
class TestProbeDeleteMe:
    def test_what_an_unpatched_get_request_does(self, db_session, monkeypatch):
        _quiet_http_half(monkeypatch)
        instance = _seed_instance('probe.example', software='lemmy')
        db.session.commit()

        try:
            monitor_healthy_instances()
            print('PROBE: task returned normally')
        except Exception as exc:
            print(f'PROBE: task raised {type(exc).__name__}: {exc}')

        db.session.expire_all()
        reloaded = db.session.query(Instance).filter_by(domain='probe.example').first()
        print(f'PROBE: failures={reloaded.failures} dormant={reloaded.dormant}')
```

Run:
```bash
./run_tests.sh tests/test_shared_tasks_maintenance_identity.py -v -s -p no:randomly
```

**Paste the exact output.** The prediction is `UnboundLocalError` escaping to `:736`. Record what actually happens; the module docstring states whichever it is.

- [ ] **Step 3: Cover the outer handler**

```python
class TestTheTaskLevelHandler:
    """`monitor_healthy_instances:736-738` -- the task's own `except`.

    `:739`'s `finally` and `:740`'s `session.close()` were already covered:
    every call reaches them. `:736-738` had never run, because every failure
    inside the loop is caught per-instance. `:547`'s `instance_banned` call is
    the exception -- it sits at loop level, outside every `try`, so a raise
    there is the one that reaches the task handler.
    """

    def test_a_raising_ban_check_rolls_back_and_re_raises(self, db_session, monkeypatch):
        """`:738`'s `raise`. The task does not swallow -- Celery must see it."""
        def _boom(domain):
            raise RuntimeError('ban check exploded')

        monkeypatch.setattr('app.shared.tasks.maintenance.instance_banned', _boom)
        _quiet_http_half(monkeypatch)
        _seed_instance('peer.example', software='lemmy')
        db.session.commit()

        with pytest.raises(RuntimeError, match='ban check exploded'):
            monitor_healthy_instances()
```

- [ ] **Step 4: Delete the probe class, run, and commit**

```bash
./run_tests.sh tests/test_shared_tasks_maintenance_identity.py -v -p no:randomly
```
Expected: 1 passed. Confirm `TestProbeDeleteMe` is gone from the committed file, checking the committed blob rather than the editor.

Subject: `test: open the maintenance identity file and cover the task handler`

Body records what the probe established, in its own words.

---

### Task 2: DC1 — bind `response` before both `try` blocks

**Files:**
- Modify: `app/shared/tasks/maintenance.py:638`, `:688-690`, `:699`, `:730-732`
- Modify: `tests/test_shared_tasks_maintenance_identity.py`

**Interfaces:**
- Consumes: `_seed_instance`, `_quiet_http_half`, `_response` from Task 1.
- Produces: both identity blocks surviving a raising `get_request`.

**The test must FAIL against the unmodified code before you change anything.**

- [ ] **Step 1: Write the test and watch it fail**

```python
class TestIdentityPhaseFailures:
    """The two identity blocks' error handling."""

    def test_a_raising_request_does_not_end_the_whole_sweep(self, db_session, monkeypatch):
        """DC1: `:689` reads `response`, which `:639` may never have bound.

        `get_request` RAISES, unlike `get_request_instance`. The `except` at
        `:685` catches the original and then the `finally` at `:688-690`
        raises `UnboundLocalError`, which that handler has already run and
        cannot catch. It escapes to `:736`, rolls back and re-raises, so one
        instance's failure ends the sweep for every other instance.

        The oracle is that BOTH instances were touched, compared as a set:
        `:540` returns planner-ordered rows.
        """
        def _raise(*args, **kwargs):
            raise httpx.HTTPError('transport down')

        monkeypatch.setattr('app.shared.tasks.maintenance.get_request', _raise)
        _quiet_http_half(monkeypatch)
        for domain in ('one.example', 'two.example'):
            _seed_instance(domain, software='lemmy')
        db.session.commit()

        monitor_healthy_instances()

        db.session.expire_all()
        touched = {i.domain for i in db.session.query(Instance).all() if i.failures > 2}
        assert touched == {'one.example', 'two.example'}
```

The `> 2` threshold is deliberate: `_quiet_http_half` already drives two increments per instance through the HTTP half, so a third only appears if `:687`'s handler ran. **Derive that number yourself from the code before trusting it** — if the HTTP half contributes a different count, use what you measure and say so.

Run:
```bash
./run_tests.sh "tests/test_shared_tasks_maintenance_identity.py::TestIdentityPhaseFailures::test_a_raising_request_does_not_end_the_whole_sweep" -v
```
Expected: FAIL with `UnboundLocalError`. **Paste the exact output.** If it passes, STOP, make no production change, set your status to `DONE_WITH_CONCERNS` and report.

- [ ] **Step 2: Make the change**

At `:638`, bind before the `try` and guard the close:

```python
            if instance.online() and (instance.software == 'lemmy' or instance.software == 'piefed' or instance.software == 'pylova'):
                response = None
                try:
                    response = get_request(f'https://{instance.domain}/api/v3/site')
                    ...unchanged...
                finally:
                    if response is not None:
                        response.close()
                session.commit()
```

The same shape at `:699` for the MBIN block.

**Use `is not None`, not the truthiness test the surrounding code uses.** Sub-project 32 chose that deliberately at its two sites in this function, and this change matches it rather than copying the weaker local idiom.

- [ ] **Step 3: Run, re-derive citations, commit**

```bash
./run_tests.sh tests/test_shared_tasks_maintenance_identity.py -v -p no:randomly
```
Expected: 2 passed.

Your edit adds four lines: 1189 → wait, `maintenance.py` is **1216**; confirm with `wc -l` and expect **1220**. **Sweep the whole file's citations, not only the ones you wrote** — everything below `:638` moves, and both this file and `tests/test_shared_tasks_maintenance_health.py` cite lines in that range.

**DC1 changes the harness's own failure mode.** After this, a raising `get_request` is caught by `:685`/`:727` and becomes a failure increment rather than a crash. Update the module docstring to describe pre-fix and post-fix behaviour separately rather than as one state.

Subject: `fix: stop a raising request from ending monitor_healthy_instances' sweep`

---

### Task 3: Lemmy/PieFed — the admin-role creation path

**Files:**
- Modify: `tests/test_shared_tasks_maintenance_identity.py`
- Read: `app/shared/tasks/maintenance.py`, the Lemmy block **as Task 2 left it** — re-derive

**Interfaces:**
- Consumes: `_seed_instance`, `_quiet_http_half`, `_Recorder`, `_response`, `_site_payload` from Task 1.
- Produces: nothing later tasks depend on.

**Do not modify `app/shared/tasks/maintenance.py` in this task.**

- [ ] **Step 1: Write the tests**

```python
class TestLemmyAdminRoles:
    """The Lemmy/PieFed block's admin reconciliation.

    `:637` forks on `software`; `:640` on the response; `:644` walks
    `instance_data['admins']`; `:646` requires an http(s) scheme; `:648`
    resolves the actor and `:649` skips one that is already an admin;
    `:650-655` creates the role.

    `find_actor_or_create` is imported at `maintenance.py:13`, so the
    namespace idiom reaches it. `InstanceRole` has a composite primary key
    `(instance_id, user_id)` (`app/models.py:167-168`), so a duplicate add
    raises rather than silently doubling -- `:649`'s guard is what prevents
    that.
    """

    def _lemmy(self, monkeypatch, payload, actor=None):
        _quiet_http_half(monkeypatch)
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request',
            _Recorder(result=_response(200, payload)))
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.find_actor_or_create',
            lambda profile_id, **kwargs: actor)

    def test_a_listed_admin_gets_an_instance_role(self, db_session, monkeypatch):
        """`:655`'s `session.add`. Delete it and no role exists."""
        instance = _seed_instance('peer.example', software='lemmy')
        admin = make_user(instance, 'adminuser')
        self._lemmy(monkeypatch, _site_payload(admin.ap_profile_id), actor=admin)

        monitor_healthy_instances()

        db.session.expire_all()
        roles = db.session.query(InstanceRole).filter_by(instance_id=instance.id).all()
        assert {(r.user_id, r.role) for r in roles} == {(admin.id, 'admin')}

    def test_a_non_http_actor_id_is_skipped(self, db_session, monkeypatch):
        """`:646`'s false arm. An acct: URI creates no role and is never resolved.

        The oracle is that `find_actor_or_create` was NOT called -- asserting
        only that no role exists would hold if the resolver returned None too.
        """
        instance = _seed_instance('peer.example', software='lemmy')
        resolver = _Recorder(result=None)
        _quiet_http_half(monkeypatch)
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request',
            _Recorder(result=_response(200, _site_payload('acct:admin@peer.example'))))
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.find_actor_or_create', resolver)

        monitor_healthy_instances()

        assert resolver.calls == []

    def test_an_unresolvable_actor_creates_no_role(self, db_session, monkeypatch):
        """`:649`'s `user and ...` conjunct, false because the resolver returned None."""
        instance = _seed_instance('peer.example', software='lemmy')
        self._lemmy(
            monkeypatch, _site_payload('https://peer.example/users/ghost'), actor=None)

        monitor_healthy_instances()

        db.session.expire_all()
        assert db.session.query(InstanceRole).filter_by(
            instance_id=instance.id).count() == 0

    def test_an_existing_admin_is_not_added_twice(self, db_session, monkeypatch):
        """`:649`'s second conjunct. Without it the composite PK collides.

        The role is seeded first, so `user_is_admin` is already true. A second
        `session.add` for the same `(instance_id, user_id)` would raise, which
        `:685` would swallow into a failure increment -- so the oracle checks
        BOTH that one role exists and that no failure was recorded by this
        block.
        """
        instance = _seed_instance('peer.example', software='lemmy')
        admin = make_user(instance, 'adminuser')
        db.session.add(InstanceRole(
            instance_id=instance.id, user_id=admin.id, role='admin'))
        db.session.commit()
        before = instance.failures
        self._lemmy(monkeypatch, _site_payload(admin.ap_profile_id), actor=admin)

        monitor_healthy_instances()

        db.session.expire_all()
        reloaded = db.session.query(Instance).filter_by(domain='peer.example').first()
        assert db.session.query(InstanceRole).filter_by(
            instance_id=instance.id).count() == 1
        assert reloaded.failures == before + 2

    def test_a_non_200_site_response_creates_no_role(self, db_session, monkeypatch):
        """`:640`'s false arm."""
        instance = _seed_instance('peer.example', software='lemmy')
        admin = make_user(instance, 'adminuser')
        _quiet_http_half(monkeypatch)
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request',
            _Recorder(result=_response(503)))
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.find_actor_or_create',
            lambda profile_id, **kwargs: admin)

        monitor_healthy_instances()

        db.session.expire_all()
        assert db.session.query(InstanceRole).filter_by(
            instance_id=instance.id).count() == 0
```

The `before + 2` in the duplicate test is the HTTP half's own contribution. **Derive it yourself and correct the plan in your report if it differs.**

- [ ] **Step 2: Run and commit**

```bash
./run_tests.sh tests/test_shared_tasks_maintenance_identity.py -v -p no:randomly
```

Subject: `test: cover the Lemmy admin-role creation path`

---

### Task 4: Lemmy/PieFed — the admin-role removal path

**Files:**
- Modify: `tests/test_shared_tasks_maintenance_identity.py`
- Read: `app/shared/tasks/maintenance.py`, the removal loop **as Task 2 left it** — re-derive

**Interfaces:**
- Consumes: everything from Task 1.
- Produces: the fixture Task 5 uses to demonstrate DC3's row sets are unchanged.

**Do not modify `app/shared/tasks/maintenance.py` in this task.** DC3 lives in this loop and belongs to Task 5; write these tests against the code as it stands.

- [ ] **Step 1: Write the tests**

```python
    def test_an_admin_no_longer_listed_loses_the_role(self, db_session, monkeypatch):
        """`:664`'s `.delete()`. The departing admin's role goes."""
        instance = _seed_instance('peer.example', software='lemmy')
        staying = make_user(instance, 'staying')
        leaving = make_user(instance, 'leaving')
        for user in (staying, leaving):
            db.session.add(InstanceRole(
                instance_id=instance.id, user_id=user.id, role='admin'))
        db.session.commit()
        self._lemmy(monkeypatch, _site_payload(staying.ap_profile_id), actor=staying)

        monitor_healthy_instances()

        db.session.expire_all()
        remaining = {
            r.user_id for r in
            db.session.query(InstanceRole).filter_by(instance_id=instance.id).all()}
        assert remaining == {staying.id}

    def test_a_still_listed_admin_keeps_the_role(self, db_session, monkeypatch):
        """`:659`'s false arm -- the profile IS in the listed set, so no delete.

        This is the companion the removal test needs: without it, a mutation
        that deletes unconditionally would still satisfy the test above.
        """
        instance = _seed_instance('peer.example', software='lemmy')
        staying = make_user(instance, 'staying')
        db.session.add(InstanceRole(
            instance_id=instance.id, user_id=staying.id, role='admin'))
        db.session.commit()
        self._lemmy(monkeypatch, _site_payload(staying.ap_profile_id), actor=staying)

        monitor_healthy_instances()

        db.session.expire_all()
        assert db.session.query(InstanceRole).filter_by(
            instance_id=instance.id, user_id=staying.id).count() == 1
```

**Note the comparison `:659` performs.** `:647` appends `profile_id.lower()`, and `:659` reads `instance_admin.user.profile_id()`, which does **not** lowercase (`app/models.py:1454-1456`) — unlike `Community.profile_id()` which does (`:787-789`). It works because `ap_profile_id` is stored lowercased at creation (`app/activitypub/util.py:1233`) and `make_user` writes an all-lowercase URI. **Do not build a test that depends on the asymmetry**, and say in your report that you checked it.

- [ ] **Step 2: Run and commit**

```bash
./run_tests.sh tests/test_shared_tasks_maintenance_identity.py -v -p no:randomly
```

Subject: `test: cover the Lemmy admin-role removal path`

---

### Task 5: DC3 — collect the roles to remove before deleting them

**Files:**
- Modify: `app/shared/tasks/maintenance.py`, the removal loop in the Lemmy block
- Modify: `tests/test_shared_tasks_maintenance_identity.py`

**Interfaces:**
- Consumes: Task 4's two tests, both of which must still pass unchanged.
- Produces: a removal loop that does not mutate the result set it iterates.

**This is the least obvious of the round's three changes and the plan says so rather than treating it as routine.**

The loop iterates `session.query(InstanceRole).filter_by(instance_id=instance.id)` and issues a bulk `Query.delete()` against rows that result set contains — mutating a collection mid-iteration and desynchronizing the session's identity map from the database in one stroke.

**The ordering is the risk.** The adds at `:650-655` are still pending in the session when the removal query runs, so the removal's view of "current admin roles" depends on whether those pending adds are visible to it. A fix that collects first must not change which rows the removal considers.

- [ ] **Step 1: Demonstrate the row sets, BEFORE changing anything**

Write a temporary probe that records which `(instance_id, user_id)` pairs the removal deletes, on a fixture with **both** a surviving admin and a departing one, and run it against the unmodified code. Then apply the change and run it again. **The two sets must be identical.** Paste both.

If they differ, **STOP.** Do not make the change; report the difference and set your status to `DONE_WITH_CONCERNS`. Deleting a legitimate admin role is worse than the defect being repaired.

Delete the probe before committing.

- [ ] **Step 2: Make the change**

```python
                        # Remove old admin roles
                        stale_roles = [
                            instance_admin for instance_admin
                            in session.query(InstanceRole).filter_by(instance_id=instance.id)
                            if instance_admin.user.profile_id() not in admin_profile_ids
                        ]
                        for instance_admin in stale_roles:
                            session.query(InstanceRole).filter(
                                InstanceRole.user_id == instance_admin.user_id,
                                InstanceRole.instance_id == instance.id,
                                InstanceRole.role == 'admin'
                            ).delete()
```

Two things changed and both are deliberate. The list comprehension drains the query before any delete runs, so nothing is mutated mid-iteration. And `instance_admin.user.id` becomes `instance_admin.user_id`, which reads the column already loaded rather than forcing the `user` relationship — the MBIN block at `:723` already does it that way, so this makes the two consistent.

**Do not touch the MBIN block's equivalent loop in this task.** It has the same shape; whether it needs the same fix is a question for the round's register, not a change to make on momentum.

- [ ] **Step 3: Run, sweep citations, commit**

```bash
./run_tests.sh tests/test_shared_tasks_maintenance_identity.py -v -p no:randomly
```
Task 4's two tests must pass unchanged. Confirm `wc -l` and sweep the whole file's citations.

Subject: `fix: drain the admin-role query before deleting from it`

Body quotes both row sets from Step 1 and states that they are identical.

---

### Task 6: Lemmy/PieFed — the custom-emoji refresh

**Files:**
- Modify: `tests/test_shared_tasks_maintenance_identity.py`
- Read: `app/shared/tasks/maintenance.py`, the emoji block **as Tasks 2 and 5 left it** — re-derive

**Interfaces:**
- Consumes: `_site_payload`, `_emoji` from Task 1.
- Produces: nothing later tasks depend on.

**Do not modify `app/shared/tasks/maintenance.py` in this task.** DC2 lives just below this block and belongs to Task 7.

- [ ] **Step 1: Write the tests**

```python
class TestLemmyCustomEmoji:
    """`:667-683` -- refresh the instance's custom emoji.

    `:667` skips the whole block for a banned domain; `:668` walks
    `custom_emojis`; `:673` forks on whether a row with that token already
    exists for this instance.

    `Emoji` has NO unique constraint on `(instance_id, token)`
    (`app/models.py:4378-4384`), so `:671`'s lookup is the only thing
    preventing duplicates.
    """

    def _lemmy(self, monkeypatch, payload, actor=None):
        _quiet_http_half(monkeypatch)
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request',
            _Recorder(result=_response(200, payload)))
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.find_actor_or_create',
            lambda profile_id, **kwargs: actor)

    def test_a_new_emoji_is_created(self, db_session, monkeypatch):
        """`:678-682`'s create arm."""
        instance = _seed_instance('peer.example', software='lemmy')
        self._lemmy(monkeypatch, _site_payload(emojis=[
            _emoji('blobcat', url='https://peer.example/blob.png',
                   category='blobs', keywords=('happy', 'cat'))]))

        monitor_healthy_instances()

        db.session.expire_all()
        rows = db.session.query(Emoji).filter_by(instance_id=instance.id).all()
        assert len(rows) == 1
        assert rows[0].token == ':blobcat:'
        assert rows[0].url == 'https://peer.example/blob.png'
        assert rows[0].category == 'blobs'
        assert rows[0].aliases == 'happy cat'

    def test_an_existing_emoji_is_updated_not_duplicated(self, db_session, monkeypatch):
        """`:673`'s true arm and `:674-676`. The token has no unique constraint,
        so a broken lookup would duplicate rather than raise.
        """
        instance = _seed_instance('peer.example', software='lemmy')
        db.session.add(Emoji(
            instance_id=instance.id, token=':blobcat:',
            url='https://peer.example/old.png', category='old', aliases='stale'))
        db.session.commit()
        self._lemmy(monkeypatch, _site_payload(emojis=[
            _emoji('blobcat', url='https://peer.example/new.png',
                   category='blobs', keywords=('happy',))]))

        monitor_healthy_instances()

        db.session.expire_all()
        rows = db.session.query(Emoji).filter_by(instance_id=instance.id).all()
        assert len(rows) == 1
        assert rows[0].url == 'https://peer.example/new.png'
        assert rows[0].category == 'blobs'
        assert rows[0].aliases == 'happy'

    def test_a_banned_domain_skips_the_emoji_refresh(self, db_session, monkeypatch):
        """`:667`'s false arm. Admin roles are still reconciled above it --
        only the emoji block is skipped -- so the oracle is the absence of an
        Emoji row, not the absence of all work.
        """
        instance = _seed_instance('peer.example', software='lemmy')
        self._lemmy(monkeypatch, _site_payload(emojis=[_emoji('blobcat')]))
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.instance_banned', lambda domain: True)

        monitor_healthy_instances()

        db.session.expire_all()
        assert db.session.query(Emoji).filter_by(instance_id=instance.id).count() == 0
```

**`:547` also calls `instance_banned`**, and patching it to return True there makes the task `continue` before reaching any identity block — which would make the banned-domain test pass for entirely the wrong reason. **Check what `:547` does with your patch before trusting the test**, and if it short-circuits, patch by domain so `:547` sees False and `:667` sees True. Report which you needed.

- [ ] **Step 2: Run and commit**

```bash
./run_tests.sh tests/test_shared_tasks_maintenance_identity.py -v -p no:randomly
```

Subject: `test: cover the Lemmy custom-emoji refresh`

---

### Task 7: DC2 — invalidate the emoji cache only on a 200

**Files:**
- Modify: `app/shared/tasks/maintenance.py`, the `cache.delete_memoized` call
- Modify: `tests/test_shared_tasks_maintenance_identity.py`

**Interfaces:**
- Consumes: `_Recorder`, `_response` from Task 1.
- Produces: `cache.delete_memoized` firing only inside the 200 guard.

**The test must FAIL against the unmodified code before you change anything.**

`cache.delete_memoized(get_emoji_replacements)` sits inside the `try` but **outside** the `if response and response.status_code == 200:` guard, so a 404 or 503 from a Lemmy instance invalidates the whole site's emoji replacements.

- [ ] **Step 1: Write the test and watch it fail**

```python
    def test_a_non_200_does_not_invalidate_the_emoji_cache(self, db_session, monkeypatch):
        """DC2: `cache.delete_memoized` sits outside `:640`'s guard.

        A 404 from one Lemmy instance discards the whole site's emoji
        replacements. The oracle records the call rather than observing the
        cache, because `CACHE_TYPE` is `NullCache` under test
        (`tests/conftest.py:68`) and an invalidation there is unobservable.
        """
        recorder = _Recorder()
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.cache.delete_memoized', recorder)
        _quiet_http_half(monkeypatch)
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request',
            _Recorder(result=_response(404)))
        _seed_instance('peer.example', software='lemmy')
        db.session.commit()

        monitor_healthy_instances()

        assert recorder.calls == []
```

Run it. Expected: FAIL — one recorded call. **Paste the exact output.**

**Patching `cache.delete_memoized` mutates the shared `cache` object**, not a module-local name. `monkeypatch` restores it, but check nothing else in the run depends on it, and say so in your report.

- [ ] **Step 2: Make the change**

Move the `cache.delete_memoized(get_emoji_replacements)` line inside the `if response and response.status_code == 200:` block, at the same indentation as the emoji loop above it. Nothing else changes.

- [ ] **Step 3: Add the companion, run, commit**

A test proving it still fires on the success path, so the fix cannot be "delete the line":

```python
    def test_a_200_does_invalidate_the_emoji_cache(self, db_session, monkeypatch):
        """The other side of DC2. Without this, deleting the call entirely
        would satisfy the test above.
        """
        recorder = _Recorder()
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.cache.delete_memoized', recorder)
        self._lemmy(monkeypatch, _site_payload(emojis=[_emoji('blobcat')]))
        _seed_instance('peer.example', software='lemmy')
        db.session.commit()

        monitor_healthy_instances()

        assert len(recorder.calls) == 1
```

```bash
./run_tests.sh tests/test_shared_tasks_maintenance_identity.py -v -p no:randomly
```
Confirm `wc -l` and sweep citations.

Subject: `fix: invalidate the emoji cache only when the site call succeeded`

---

### Task 8: The MBIN block

**Files:**
- Modify: `tests/test_shared_tasks_maintenance_identity.py`
- Read: `app/shared/tasks/maintenance.py`, the MBIN block **as Tasks 2, 5 and 7 left it** — re-derive

**Interfaces:**
- Consumes: `_seed_instance`, `_quiet_http_half`, `_Recorder`, `_response`, `_mbin_payload` from Task 1.
- Produces: nothing later tasks depend on.

**Do not modify `app/shared/tasks/maintenance.py` in this task.**

The MBIN block reconciles roles only — no emoji — and resolves users from the local database rather than creating them, which is what the comment above `:698` explains.

- [ ] **Step 1: Write the tests**

```python
class TestMbinAdminRoles:
    """`:698-733` -- MBIN admin reconciliation.

    `:698` forks on `software == 'mbin'`; `:701` on the response; `:705` walks
    `instance_data['items']`; `:706` reads the username defensively; `:707` is
    a COMPOUND -- `username and (isAdmin or isGlobalModerator)` -- which
    coverage sees as one arc pair, so each conjunct needs its own test;
    `:708` looks the user up locally and `:709` skips one that is not known;
    `:711` skips one already an admin; `:720-726` removes stale roles.

    Unlike the Lemmy block this never creates a User: the API response does
    not carry enough to build one.
    """

    def _mbin(self, monkeypatch, payload):
        _quiet_http_half(monkeypatch)
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request',
            _Recorder(result=_response(200, payload)))

    def test_a_listed_admin_gets_an_instance_role(self, db_session, monkeypatch):
        """`:717`'s `session.add`."""
        instance = _seed_instance('peer.example', software='mbin')
        admin = make_user(instance, 'adminuser')
        self._mbin(monkeypatch, _mbin_payload(
            {'username': 'adminuser', 'isAdmin': True}))

        monitor_healthy_instances()

        db.session.expire_all()
        roles = db.session.query(InstanceRole).filter_by(instance_id=instance.id).all()
        assert {r.user_id for r in roles} == {admin.id}

    def test_a_global_moderator_also_gets_the_role(self, db_session, monkeypatch):
        """`:707`'s second disjunct, alone. `isAdmin` is absent."""
        instance = _seed_instance('peer.example', software='mbin')
        admin = make_user(instance, 'moduser')
        self._mbin(monkeypatch, _mbin_payload(
            {'username': 'moduser', 'isGlobalModerator': True}))

        monitor_healthy_instances()

        db.session.expire_all()
        assert db.session.query(InstanceRole).filter_by(
            instance_id=instance.id, user_id=admin.id).count() == 1

    def test_a_plain_user_gets_no_role(self, db_session, monkeypatch):
        """`:707`'s false arm -- neither flag set."""
        instance = _seed_instance('peer.example', software='mbin')
        make_user(instance, 'plainuser')
        self._mbin(monkeypatch, _mbin_payload({'username': 'plainuser'}))

        monitor_healthy_instances()

        db.session.expire_all()
        assert db.session.query(InstanceRole).filter_by(
            instance_id=instance.id).count() == 0

    def test_an_item_without_a_username_is_skipped(self, db_session, monkeypatch):
        """`:706`'s else -- the key is absent, so `username` is None and
        `:707`'s first conjunct is false. Without `:706`'s guard this raises
        `KeyError` into `:727`.
        """
        instance = _seed_instance('peer.example', software='mbin')
        before = instance.failures
        self._mbin(monkeypatch, _mbin_payload({'isAdmin': True}))

        monitor_healthy_instances()

        db.session.expire_all()
        reloaded = db.session.query(Instance).filter_by(domain='peer.example').first()
        assert db.session.query(InstanceRole).filter_by(
            instance_id=instance.id).count() == 0
        assert reloaded.failures == before + 2

    def test_an_unknown_username_gets_no_role(self, db_session, monkeypatch):
        """`:709`'s false arm -- listed as admin but not in our database."""
        instance = _seed_instance('peer.example', software='mbin')
        self._mbin(monkeypatch, _mbin_payload(
            {'username': 'stranger', 'isAdmin': True}))

        monitor_healthy_instances()

        db.session.expire_all()
        assert db.session.query(InstanceRole).filter_by(
            instance_id=instance.id).count() == 0

    def test_an_admin_no_longer_listed_loses_the_role(self, db_session, monkeypatch):
        """`:726`'s `.delete()`."""
        instance = _seed_instance('peer.example', software='mbin')
        staying = make_user(instance, 'staying')
        leaving = make_user(instance, 'leaving')
        for user in (staying, leaving):
            db.session.add(InstanceRole(
                instance_id=instance.id, user_id=user.id, role='admin'))
        db.session.commit()
        self._mbin(monkeypatch, _mbin_payload(
            {'username': 'staying', 'isAdmin': True}))

        monitor_healthy_instances()

        db.session.expire_all()
        remaining = {
            r.user_id for r in
            db.session.query(InstanceRole).filter_by(instance_id=instance.id).all()}
        assert remaining == {staying.id}

    def test_a_non_200_admins_response_creates_no_role(self, db_session, monkeypatch):
        """`:701`'s false arm."""
        instance = _seed_instance('peer.example', software='mbin')
        make_user(instance, 'adminuser')
        _quiet_http_half(monkeypatch)
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request',
            _Recorder(result=_response(503)))

        monitor_healthy_instances()

        db.session.expire_all()
        assert db.session.query(InstanceRole).filter_by(
            instance_id=instance.id).count() == 0
```

The `before + 2` figures are the HTTP half's contribution. **Derive them yourself.**

- [ ] **Step 2: Run and commit**

```bash
./run_tests.sh tests/test_shared_tasks_maintenance_identity.py -v -p no:randomly
```

Subject: `test: cover the MBIN admin-role reconciliation`

---

### Task 9: Measure coverage, close residuals, raise the floor

**Files:**
- Modify: `coverage_floors.ini`
- Modify: `tests/test_shared_tasks_maintenance_identity.py` (whatever the measurement shows is missing)

- [ ] **Step 1: Bring the stack down, then measure**

```bash
./run_tests.sh --down
./run_tests.sh --cov=app.shared.tasks.maintenance --cov-branch \
    --cov-report=json:/tmp/c33.json \
    tests/test_shared_tasks_maintenance_identity.py \
    tests/test_shared_tasks_maintenance_health.py \
    tests/test_shared_tasks_maintenance_external.py \
    tests/test_shared_tasks_maintenance_lifecycle.py \
    tests/test_shared_tasks_maintenance_cleanup.py
echo "exit: $?"
```

**All five files**, because the floor is for the whole module. Do not pipe.

- [ ] **Step 2: List what is still missing**

Derive the block boundaries yourself from `grep -n '^def \|^@celery.task' app/shared/tasks/maintenance.py` **at the moment you run this** — Tasks 2, 5 and 7 have all edited the file.

```bash
podman compose -f compose.test.yaml exec -T test-runner python3 - <<'EOF'
import json
d = json.load(open('/tmp/c33.json'))
f = d['files']['app/shared/tasks/maintenance.py']
print('missing statements:', sorted(f['missing_lines']))
print('missing branches:', sorted(f.get('missing_branches', [])))
print('percent_covered:', f['summary']['percent_covered'])
EOF
```

**This round should reach zero missing statements for the whole module.** If anything remains, name it and say whether it is closable.

- [ ] **Step 3: Close what is left, and report what you do not**

**Exhaust the oracles before calling anything unobservable.** Database state, exception propagation, call recording and **log output** are four different instruments — a previous round judged a branch unobservable on the first two and a reviewer defeated it with the third. **Do not mark anything `# pragma: no branch` on your own judgment.**

- [ ] **Step 4: Raise the floor**

Edit `coverage_floors.ini`'s `app/shared/tasks/maintenance.py` from `89` to the measured integer, **rounded DOWN**. `tests/check_coverage_floors.py:75` compares `entry['summary']['percent_covered']` — the combined statement-and-branch figure — not `percent_statements_covered`, which reads several points higher and would set an unholdable floor.

- [ ] **Step 5: Commit**

```bash
git status --porcelain -uall
```
Confirm no coverage JSON landed in the tree.

Subject: `test: raise maintenance.py's floor for the identity phases`

---

### Task 10: Mutation testing

**Files:**
- Modify: none permanently. Every mutation is applied and restored.

**Every line number below is advisory.** Tasks 2, 5 and 7 edited the file. Re-derive each site against the tree as it stands, and read the dry-run's produced line before applying.

**Give every site in this table its own row, and mutate every boundary in BOTH directions.** Sub-project 32's pass enumerated a site in its own prose and then never mutated it, and mutated a cutoff only in the direction its tests already caught; the final review found both holes.

- [ ] **Step 1: Run these mutations, in this order**

| # | Site | Mutation | Expected |
|---|------|----------|----------|
| 1 | the Lemmy `software` fork | drop `or instance.software == 'pylova'` | **check** — no test seeds pylova |
| 2 | same | drop `or instance.software == 'piefed'` | **check** — no test seeds piefed |
| 3 | the Lemmy response guard | `== 200` → `!= 200` | killed |
| 4 | same | drop the `response and` conjunct | **check** — needs a falsy response |
| 5 | the scheme check | drop `or profile_id.startswith('http://')` | **check** — no test uses plain http |
| 6 | same | `startswith('https://')` → `endswith` | killed |
| 7 | `:649`'s guard | drop the `user and` conjunct | killed by the unresolvable-actor test |
| 8 | same | drop the `not instance.user_is_admin(...)` conjunct | killed by the already-admin test |
| 9 | the removal condition | drop the `not in` negation | killed by the still-listed test |
| 10 | DC3's comprehension | restore the delete inside the loop | **check which test kills it** |
| 11 | the emoji ban gate | drop the `not` | killed by the banned-domain test |
| 12 | the emoji existence fork | `if existing_emoji:` → `if not existing_emoji:` | killed |
| 13 | DC2's placement | move `cache.delete_memoized` back outside the guard | killed by the non-200 test |
| 14 | DC1's Lemmy guard | `if response is not None:` → `if response is None:` | killed |
| 15 | DC1's MBIN guard | same | killed |
| 16 | the MBIN `software` fork | `== 'mbin'` → `!= 'mbin'` | killed |
| 17 | the MBIN response guard | `== 200` → `!= 200` | killed |
| 18 | `:706`'s username guard | drop the `if 'username' in item` conditional | killed by the no-username test |
| 19 | `:707`'s first conjunct | drop `username and` | **check** |
| 20 | `:707`'s admin flag | drop `item.get('isAdmin') or` | killed by the admin test |
| 21 | `:707`'s moderator flag | drop `or item.get('isGlobalModerator')` | killed by the moderator test |
| 22 | `:709`'s guard | `if user:` → `if not user:` | killed |
| 23 | `:711`'s guard | drop the `not` | killed |
| 24 | the MBIN removal condition | drop the `not in` | **check** — is there a still-listed companion? |
| 25 | the task handler | delete `raise` | killed by Task 1's test |

- [ ] **Step 2: For every survivor, decide which of two things it is**

A survivor **with a proof** is information about the code — record the proof and call it an equivalent mutant. A survivor **without one** is a hole; write the test that kills it and name the regression **and the arm**.

**Before recording any survivor as equivalent, check the site against the test this table names as its killer.** A survivor at the wrong site looks exactly like a survivor at the right one.

**Two individually-equivalent mutations can be jointly load-bearing** — a previous round found a pair of redundant `.lower()` calls where dropping either alone was unobservable and dropping both broke the behaviour.

- [ ] **Step 3: Restore and verify**

```bash
git diff --stat -- app/
wc -l app/shared/tasks/maintenance.py
```

Empty diff, and the line count Tasks 2, 5 and 7 left behind.

- [ ] **Step 4: Commit only if a survivor made you write a test**

Subject: `test: kill the surviving mutants in the maintenance identity phases`

---

### Task 11: Register the findings and the facts

**Files:**
- Modify: `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`
- Modify: `tests/README.md`

- [ ] **Step 1: Confirm the next free numbers**

```bash
grep -n 'Next free number' docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md | tail -1
grep -oE '^\*\*19[0-9]\.|^\*\*20[0-9]\.' tests/README.md | tail -3
```

The plan says D383 and fact 198; verify rather than trusting it.

- [ ] **Step 2: Write the findings, from D383**

- **DC1, the unbound `response`** — and that this is the *reachable* twin of the defect sub-project 32 fixed at two other sites in this function, already confirmed by observation before this round began. Record that it also closed the wrong response on later iterations, not only crashed on the first.
- **DC2, the emoji cache invalidation** — a non-200 from one instance discarding the whole site's replacements.
- **DC3, the drained removal query** — with the before-and-after row sets from Task 5 Step 1 quoted, since the change was permitted only on that evidence.
- **`:683`'s `session.commit()` inside the per-emoji loop** — one commit per emoji rather than per instance, on a path iterating an unbounded list from a remote server. The fifth in-loop commit this campaign has registered. Registered, not fixed.
- **`User.profile_id()` does not lowercase** where `Community.profile_id()` does — latent, not live, because `ap_profile_id` is stored lowercased at creation. Record why it is safe today and what would make it unsafe.
- **`:649`'s `user_is_admin` guard checks `role == 'admin'` specifically**, so a row with a different role value slips past it into a composite-PK collision.
- **The MBIN removal loop has DC3's shape and was deliberately not changed** — say so explicitly, with whatever Task 10's mutation 24 established about whether it is discriminated.
- **`check_instance_health`'s batched commit remains outstanding**, still nominated for a future round's production scope. It is not in this range and this round did not fix it.
- **The module is complete.** Record the final floor, the total test count across all five maintenance files, and what the campaign's next target should be — this is the last round in `app/shared/tasks/maintenance.py`, so the next sub-project starts somewhere new and this entry carries more weight than usual.

- [ ] **Step 3: Write the new facts, from 198**

- **Whatever Task 1's probe established**, in its own words — and that DC1 then changed it, so the file documents two states.
- **An instance's `software` value decides which blocks a test enters**, and a test that sets it for one reason enters them for all reasons. Sub-project 32 hit this by accident.
- **`_quiet_http_half`'s shape** — why an identity-phase test must neutralise the blocks above it, and what a 404 there contributes to `failures`.
- **Patching `cache.delete_memoized` mutates a shared object**, not a module-local name, and why the oracle records the call rather than observing the cache under `NullCache`.
- Anything the mutation pass turned up that generalises.

- [ ] **Step 4: Commit**

Subject: `docs: register the maintenance identity findings and close the module`

---

## Self-Review

**Spec coverage.** Every spec section maps to a task: the outer handler to Task 1; DC1 to Task 2; the Lemmy admin path to Tasks 3 and 4; DC3 to Task 5; the emoji refresh to Task 6; DC2 to Task 7; the MBIN block to Task 8; the floor to Task 9; the mutations to Task 10; the register-only findings and facts to Task 11. The spec's harness facts — `_seed_instance`, no ordered assertions, the compound-condition warning — are in Global Constraints and restated where they bite. The spec's `InstanceRole` composite-PK, `Emoji` unique-constraint and `User.profile_id()` notes each appear in the task whose tests depend on them.

**Placeholder scan.** No task says "add appropriate tests". Every code step carries its code. Task 9 Step 2's block boundaries are deliberately left for the implementer to derive from a live `grep`, because Tasks 2, 5 and 7 shift the file — the step says so and gives the command.

**Type consistency.** `_Recorder.calls` holds `(args, kwargs)` throughout and every caller indexes `c[0][0]`. `_response(status_code, payload)` is the single response constructor. `_site_payload(*actor_ids, emojis=None)` and `_emoji(shortcode, url, category, keywords)` compose; `_mbin_payload(*items)` takes raw dicts because the MBIN item shape varies per test. `_seed_instance(domain, software='mastodon')` and `_quiet_http_half(monkeypatch)` are used by every later task. Task 3's `_lemmy` helper and Task 6's are deliberately duplicated across two classes rather than shared — **a reviewer may flag that; the answer is that they belong to different classes with different fixtures, and merging them would need a base class this file does not otherwise want.**

**Four risks the plan carries deliberately.** Task 1's probe predicts the opposite of the last two rounds and may be wrong. Task 5 refuses its own change unless the row sets match. Task 2 changes the harness's failure mode mid-round, so the module docstring must describe two states. And six rows of Task 10's mutation table say "check" rather than predicting — where this plan does not know, it says so.

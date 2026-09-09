# Coverage sub-project 32 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Take the first half of `app/shared/tasks/maintenance.py`'s Group C to zero missing statements, and fix four production defects found while reading it.

**Architecture:** One new test file, `tests/test_shared_tasks_maintenance_health.py`, covering `sync_defederation_subscriptions`, `check_instance_health`, and `monitor_healthy_instances`' HTTP half. No respx: every helper these tasks call is imported at module scope, so tests replace the names directly and hand back constructed `httpx.Response` objects. Four production changes, each landing as its own commit with its own failing observation.

**Tech Stack:** pytest, SQLAlchemy 2.0.52, httpx, Flask, Celery. Tests run only through `./run_tests.sh`.

**Spec:** `docs/superpowers/specs/2026-09-09-coverage-maintenance-c-32-design.md`

## Global Constraints

- **Delete nothing the task did not create.** `git checkout -- app/` is permitted ONLY as a restore step after a mutation probe.
- **There is NO host Python with flask or pytest.** Everything runs through `./run_tests.sh`, which is `podman compose exec -T test-runner pytest "$@"` (`run_tests.sh:85`).
- **`pytest` exits 1 on a session timeout and `run_tests.sh` propagates it. A shell pipeline eats the exit status** — do not pipe, or read `${PIPESTATUS[0]}`.
- **Coverage takes the dotted module form** `--cov=app.shared.tasks.maintenance`. A path form collects nothing, writes no JSON, and exits 0 — a silent green failure.
- **Write coverage JSON outside the repository.** `/app` is bind-mounted, so `--cov-report=json:coverage.json` leaves an artifact in the working tree. Use `json:/tmp/<name>.json`.
- **Test counts come from pytest's own collection output**, never `grep -c '^def test_'`.
- Before believing a failure that looks environmental, run `./run_tests.sh --down` and retry once.
- **Mutations:** one at a time, dry-run without `-i` and read the produced line first, apply, run, restore, then assert an empty `git diff -- app/` and the expected `wc -l`. **Restore before any point where you might stop and report.** Paste every dry-run line and every failure.
- Commit with `git commit -F <file>`, never `-m`. Lowercase `type:` subject prefix, body in normal English prose.
- Commit trailers, last two lines, in this order:
  `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`
  `Claude-Session: https://claude.ai/code/session_01BF7kVN2spWD86NYKWyGrVn`
- **No ordered assertions over rows a query planner returned.** `:436`, `:446` and `:515` all return planner-ordered lists; compare sets.
- **Seeded instances must have `id != 1`.** `:449` and `:518` exclude it and the conftest fixtures reserve it.
- Re-derive every line number against the current tree with **numbered output** (`awk 'NR>=X && NR<=Y {printf "%d\t%s\n",NR,$0}' FILE`); never count lines of an unnumbered `sed -n 'X,Yp'` range.
- Where prose describes a **statement**, cite that statement's line — not the `if` guarding it, not the `def` above it.
- **Every test must be able to fail**, and its docstring must name the single-line regression it catches. An assertion that holds trivially proves nothing. Name the mechanism you actually checked.
- **Do not run any probe that holds an uncommitted write on a row another session touches** — it deadlocks Postgres and `pytest-timeout` cannot interrupt it.
- `app/shared/tasks/maintenance.py` is **1185 lines** at the start of this plan.

---

### Task 1: Open the file, cover `sync_defederation_subscriptions`, and probe the swallow

**Files:**
- Create: `tests/test_shared_tasks_maintenance_health.py`
- Read: `app/shared/tasks/maintenance.py:408-425`, `app/utils.py:189-196`

**Interfaces:**
- Produces: `_Recorder`, `_response`, and the module docstring every later task extends.

**The probe answers a question every later task depends on.** `get_request_instance` (`app/utils.py:190-196`) wraps its call in a bare `except:` and returns `httpx.Response(status_code=500)`. The `block_outbound_http` fixture (`tests/conftest.py:262`) is session-scoped autouse, so an unmatched request raises — but that raise is swallowed here and becomes a synthetic 500. **A test that forgets to patch `get_request_instance` therefore does not fail; it silently exercises the failure path.** This is fact 148's shape in a new place. Confirm it by observation and record the result; no later task may assume it.

- [ ] **Step 1: Write the prelude**

```python
"""Group C of `app/shared/tasks/maintenance.py` -- the instance-health tasks.

Sub-project 29 closed Group A in `tests/test_shared_tasks_maintenance_cleanup.py`,
30 closed Group B in `..._lifecycle.py`, and 31 closed Group D in
`..._external.py`. Group C is the last, and the only group no test reached at
all. This file covers its first half:

  `sync_defederation_subscriptions:409`   `check_instance_health:427`
  `monitor_healthy_instances:509`, HTTP half only

THIS FILE USES NO RESPX. Every helper these tasks call is imported at module
scope (`maintenance.py:13` and `:19`), so tests replace
`app.shared.tasks.maintenance.get_request_instance`, `.get_request`,
`.instance_banned` and `.download_defeds` directly and hand back constructed
`httpx.Response` objects. That avoids sub-project 31's central hazard and
`get_request`'s 3-to-10-second retry sleep (`app/utils.py:158-162`, `:173-177`)
in one move: no request reaches a transport, so neither can bite.

BUT THE HAZARD STILL EXISTS FOR A TEST THAT FORGETS THE PATCH.
`get_request_instance` (`app/utils.py:190-196`) catches EVERYTHING with a bare
`except:` -- including respx's `AllMockedAssertionError` from the autouse
`block_outbound_http` fixture -- and returns a synthetic
`httpx.Response(status_code=500)`. So an unpatched call does not fail the test;
it routes it into the failure path while the test believes it tested success.
Task 1 established this by observation.

THE IDENTITY HALF IS OUT OF SCOPE. `monitor_healthy_instances:606` needs
`instance.software` in {'lemmy', 'piefed', 'pylova'} and `:667` needs 'mbin'.
Every fixture here uses `make_instance`'s default, 'mastodon', so neither body
runs. Both `if` statements still evaluate, so this file covers their FALSE arms
and sub-project 33 owns the true ones.

INSTANCE 1 IS RESERVED. `:449` and `:518` both filter `Instance.id != 1`, and
the conftest fixtures seed `instance_id=1`. Seeded instances must not be it.

THE TASKS RUN ON THEIR OWN CONNECTION. `get_task_session()` returns
`Session(bind=db.engine)` (`app/utils.py:3673-3675`), so rows a test seeds must
be COMMITTED before the task runs, and an ORM attribute read afterwards is stale
unless the test calls `db.session.expire_all()` first (fact 153).
"""

from datetime import timedelta

import httpx
import pytest
from sqlalchemy import event

from app import db
from app.models import BannedInstances, DefederationSubscription, Instance, utcnow
from app.shared.tasks.maintenance import (
    check_instance_health,
    monitor_healthy_instances,
    sync_defederation_subscriptions,
)
from tests.factories import make_instance


class _Recorder:
    """`calls` holds one `(args, kwargs)` tuple per invocation.

    So `c[0][0]` is the first positional argument and `c[1]` the keywords. Same
    shape as the Group D file's recorder; kept identical so a reader moving
    between the two files does not have to re-learn it.
    """

    def __init__(self, result=None):
        self.calls = []
        self.result = result

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


def _response(status_code=200, payload=None):
    """A real `httpx.Response`, because production calls `.json()` and `.close()`.

    A stub would have to imitate both. Constructing the real class means the
    test exercises the same parsing production does.
    """
    if payload is None:
        return httpx.Response(status_code=status_code)
    return httpx.Response(status_code=status_code, json=payload)


NODEINFO_LINK = 'http://nodeinfo.diaspora.software/ns/schema/2.0'
```

- [ ] **Step 2: Write the throwaway probe**

Add this temporarily. It is deleted before the commit.

```python
class TestProbeDeleteMe:
    def test_what_an_unpatched_helper_does(self, db_session):
        instance = make_instance('probe.example')
        instance.dormant = True
        instance.gone_forever = False
        instance.start_trying_again = utcnow() - timedelta(days=1)
        db.session.commit()

        check_instance_health()

        db.session.expire_all()
        reloaded = db.session.query(Instance).filter_by(domain='probe.example').first()
        print(f'PROBE: task returned normally, failures={reloaded.failures}')

    def test_a_constructed_response_closes(self):
        r = _response(200, {'software': {'name': 'PieFed', 'version': '1.2.3'}})
        assert r.json()['software']['name'] == 'PieFed'
        r.close()
        print('PROBE: constructed Response.close() did not raise')
```

Run:
```bash
./run_tests.sh tests/test_shared_tasks_maintenance_health.py -v -s -p no:randomly
```

**Paste the exact output.** Two questions answered: whether an unpatched
`get_request_instance` swallows the blocked request into a synthetic 500 rather
than failing the test, and whether `.close()` on a constructed `httpx.Response`
is safe. **Every later task depends on the second.** If `.close()` raises,
`_response` needs a different construction and this plan's later tasks change
shape — report it and stop rather than working around it.

- [ ] **Step 3: Write the `sync_defederation_subscriptions` tests**

```python
class TestSyncDefederationSubscriptions:
    """`sync_defederation_subscriptions:409` -- refresh subscription-sourced bans.

    `:413` deletes every ban carrying a `subscription_id` and `:414` commits,
    then `:416` walks the subscriptions and `:417` hands each to
    `download_defeds`. `:419-421` rolls back and re-raises.

    `BannedInstances.subscription_id` is None for a ban a local admin placed
    (`app/models.py:70`), which is what `:413`'s WHERE clause distinguishes.
    """

    def test_subscription_bans_are_cleared_and_admin_bans_survive(self, db_session, monkeypatch):
        """`:413`'s WHERE clause. Deleting it would take the admin ban too."""
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.download_defeds', _Recorder())
        sub = DefederationSubscription(domain='sub.example')
        db.session.add(sub)
        db.session.commit()
        db.session.add(BannedInstances(domain='from-sub.example', subscription_id=sub.id))
        db.session.add(BannedInstances(domain='from-admin.example', subscription_id=None))
        db.session.commit()

        sync_defederation_subscriptions()

        db.session.expire_all()
        remaining = {b.domain for b in db.session.query(BannedInstances).all()}
        assert remaining == {'from-admin.example'}

    def test_every_subscription_is_handed_over_with_its_id_and_domain(self, db_session, monkeypatch):
        """`:417`'s call. The set comparison is deliberate: `:416` returns
        planner-ordered rows and this file does not assert an order over those.
        """
        recorder = _Recorder()
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.download_defeds', recorder)
        first = DefederationSubscription(domain='one.example')
        second = DefederationSubscription(domain='two.example')
        db.session.add_all([first, second])
        db.session.commit()
        expected = {(first.id, 'one.example'), (second.id, 'two.example')}

        sync_defederation_subscriptions()

        assert {(c[0][0], c[0][1]) for c in recorder.calls} == expected

    def test_a_failing_download_rolls_back_and_re_raises(self, db_session, monkeypatch):
        """`:419-421`. The task does not swallow -- Celery must see the failure.

        The raise comes from `download_defeds` at `:417`, INSIDE the `try` that
        opens at `:412`, which is what makes `:420`'s rollback reachable.
        """
        def _boom(*args, **kwargs):
            raise RuntimeError('defed download failed')

        monkeypatch.setattr('app.shared.tasks.maintenance.download_defeds', _boom)
        db.session.add(DefederationSubscription(domain='sub.example'))
        db.session.commit()

        with pytest.raises(RuntimeError, match='defed download failed'):
            sync_defederation_subscriptions()

    def test_no_subscriptions_is_not_an_error(self, db_session, monkeypatch):
        """`:416`'s loop over an empty result. The delete at `:413` still runs."""
        recorder = _Recorder()
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.download_defeds', recorder)
        db.session.add(BannedInstances(domain='stale.example', subscription_id=None))
        db.session.commit()

        sync_defederation_subscriptions()

        db.session.expire_all()
        assert recorder.calls == []
        assert db.session.query(BannedInstances).filter_by(
            domain='stale.example').first() is not None
```

- [ ] **Step 4: Delete the probe class, run, and commit**

```bash
./run_tests.sh tests/test_shared_tasks_maintenance_health.py -v -p no:randomly
```
Expected: 4 passed. Confirm `TestProbeDeleteMe` is gone from the committed file.

Subject: `test: open the maintenance Group C file with the defederation sync`

Body records what the probe established, in its own words.

---

### Task 2: DC1 — try to observe `monitor_healthy_instances`' missing session

**Files:**
- Possibly modify: `app/shared/tasks/maintenance.py:509-709`
- Modify: `tests/test_shared_tasks_maintenance_health.py`

**Interfaces:**
- Consumes: `_Recorder`, `_response` from Task 1.
- Produces: either a `patch_db_session` wrapper, or a register-only finding.

**This task may correctly produce no change.** Sub-project 31's PC2 was investigated and correctly not made. If the discriminator cannot separate the two behaviours, that is an anticipated outcome: the round lands three production changes and DC1 becomes a register-only finding. **Do not add the wrapper because it "matches `check_instance_health`."** Consistency is a reason to register a finding, not to change production code on no evidence.

**Precondition, check it first.** `patch_db_session` short-circuits when `has_request_context()` (`app/utils.py:3685`, returning at `:3688`). The `app` fixture pushes an app context, not a request context, so the guard is False and the wrapper takes effect. Without confirming this, a null result is ambiguous between "no defect" and "the instrument is blind."

- [ ] **Step 1: Write the discriminator**

`get_request_instance` (`app/utils.py:193-195`) does `instance.failures += 1`, `instance.update_dormant_gone()` and `db.session.commit()`. This test lets the real helper run and makes the request beneath it fail, so those writes actually happen.

```python
    def test_the_failure_bookkeeping_shares_the_task_s_connection(self, db_session, monkeypatch):
        """DC1: `get_request_instance` commits through `db.session`.

        `monitor_healthy_instances:511` opens its own session and `:515` loads
        the instances from it, but the task never wraps its body in
        `patch_db_session`. `check_instance_health:431` does. So the failure
        bookkeeping at `app/utils.py:193-195` runs against Flask-SQLAlchemy's
        session while the rows it mutates live in the task's.
        """
        def _raise(*args, **kwargs):
            raise httpx.HTTPError('transport down')

        monkeypatch.setattr('app.utils.get_request', _raise)
        instance = make_instance('peer.example')
        instance.nodeinfo_href = None
        db.session.commit()

        checkouts = []

        def _record(conn, cursor, statement, parameters, context, executemany):
            if 'instance' in statement.lower():
                checkouts.append(id(conn.connection))

        event.listen(db.engine, 'before_cursor_execute', _record)
        try:
            monitor_healthy_instances()
        finally:
            event.remove(db.engine, 'before_cursor_execute', _record)

        assert checkouts
        assert len(set(checkouts)) == 1
```

Run it against the unmodified code:
```bash
./run_tests.sh "tests/test_shared_tasks_maintenance_health.py::TestMonitorHealthyInstances::test_the_failure_bookkeeping_shares_the_task_s_connection" -v
```

**Bounded effort: no more than three attempts at a discriminator.** If three do not separate the behaviours, that is the answer.

- [ ] **Step 2: Take one of two paths, and report which**

**If it FAILS against unmodified code** — more than one checkout — the defect is real. Paste the failure, then wrap the body:

```python
@celery.task
def monitor_healthy_instances():
    """Check healthy instances to see if still healthy"""
    session = get_task_session()
    try:
        with patch_db_session(session):
            ...existing body from `HEADERS = ...` to the MBIN block, indented one level...

    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
```

`get_task_session` and `patch_db_session` are already imported at `maintenance.py:19-20`; no new import is needed. Re-run the whole file: every earlier test must still pass.

**If it PASSES against unmodified code** — one checkout already — **stop. Do not add the wrapper.** Write into your report what you tried and why it could not discriminate, and set your status to `DONE_WITH_CONCERNS`.

- [ ] **Step 3: Commit only if you changed something**

Subject if changed: `fix: give monitor_healthy_instances the task session its writes assume`

Body quotes the Step 1 failure and states the scope limit: no retry, no logging, nothing swallowed.

---

### Task 3: DC2 — bind `nodeinfo` and `node` before their `try` blocks

**Files:**
- Modify: `app/shared/tasks/maintenance.py:531-562`, `:564-592`
- Modify: `tests/test_shared_tasks_maintenance_health.py`

**Interfaces:**
- Consumes: `_response` from Task 1.
- Produces: `monitor_healthy_instances` surviving a raising `get_request_instance`.

**The test must FAIL against the unmodified code before you change anything.**

**This justification is weaker than the round's usual bar, and the plan says so rather than dressing it up.** In production the path needs `get_request_instance`'s own handler to fail at `app/utils.py:193-195`, because its bare `except:` swallows what the request raises. The failing observation is produced by patching that helper to raise, which tests robustness against a dependency's contract rather than a defect a user hits today. That is the same class of argument as sub-project 31's PC1.

- [ ] **Step 1: Write the test and watch it fail**

```python
    def test_a_raising_helper_does_not_end_the_whole_sweep(self, db_session, monkeypatch):
        """DC2: `:561` closes `nodeinfo`, which `:533` may never have bound.

        If `get_request_instance` raises, the `except` at `:557` catches it and
        then the `finally` at `:560-561` raises `UnboundLocalError` -- which
        that handler has already run and cannot catch. It escapes to `:705`,
        rolls back and re-raises, so one instance's failure ends the sweep for
        every other instance.

        The oracle is that BOTH instances were touched, compared as a set:
        `:515` returns planner-ordered rows and this file asserts no order over
        those.
        """
        def _raise(*args, **kwargs):
            raise RuntimeError('helper exploded')

        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance', _raise)
        for domain in ('one.example', 'two.example'):
            instance = make_instance(domain)
            instance.nodeinfo_href = None
        db.session.commit()

        monitor_healthy_instances()

        db.session.expire_all()
        touched = {i.domain for i in db.session.query(Instance).all() if i.failures > 0}
        assert touched == {'one.example', 'two.example'}
```

Run:
```bash
./run_tests.sh "tests/test_shared_tasks_maintenance_health.py::TestMonitorHealthyInstances::test_a_raising_helper_does_not_end_the_whole_sweep" -v
```
Expected: FAIL with `UnboundLocalError`. **Paste the exact output.** If it passes, STOP, make no production change, set your status to `DONE_WITH_CONCERNS` and report — that is a real finding, not a problem to work around.

- [ ] **Step 2: Make the change**

At `:531-562`, bind before the `try` and guard the close:

```python
            if not nodeinfo_href:
                nodeinfo = None
                try:
                    nodeinfo = get_request_instance(
                        f"https://{instance.domain}/.well-known/nodeinfo",
                        headers=HEADERS,
                        instance=instance
                    )
                    ...unchanged through the `except` arm...
                finally:
                    if nodeinfo is not None:
                        nodeinfo.close()
                session.commit()
```

At `:564-592`, the same shape for `node`:

```python
            if instance.nodeinfo_href:
                node = None
                try:
                    node = get_request_instance(instance.nodeinfo_href, headers=HEADERS, instance=instance)
                    ...unchanged through the `except` arm...
                finally:
                    if node is not None:
                        node.close()
```

Nothing else changes. No logging, nothing swallowed, no retry.

- [ ] **Step 3: Run the whole file and commit**

```bash
./run_tests.sh tests/test_shared_tasks_maintenance_health.py -v -p no:randomly
```

Your edit adds four lines. Confirm with `wc -l` and re-derive every citation below `:531` — **`check_instance_health` is above this function and unaffected, but every later line in `monitor_healthy_instances` shifts.**

Subject: `fix: stop a raising helper from ending monitor_healthy_instances' sweep`

---

### Task 4: `check_instance_health`, the gone-forever sweep

**Files:**
- Modify: `tests/test_shared_tasks_maintenance_health.py`
- Read: `app/shared/tasks/maintenance.py:427-443`

**Interfaces:**
- Consumes: `_response` from Task 1.
- Produces: nothing later tasks depend on.

**Do not modify `app/shared/tasks/maintenance.py` in this task.**

- [ ] **Step 1: Write the tests**

```python
class TestCheckInstanceHealthGoneForever:
    """`check_instance_health:427` -- first loop, `:435-443`.

    `:435` computes the cutoff, `:436-439` selects dormant instances whose
    `start_trying_again` is already past it, `:441-442` marks each
    `gone_forever`, and `:443` commits.

    The whole body sits inside `patch_db_session(session)` at `:431`, unlike
    `monitor_healthy_instances`. That is DC1's subject and is why this task's
    tests need no session gymnastics.
    """

    def _dormant(self, domain, days_ago):
        instance = make_instance(domain)
        instance.dormant = True
        instance.gone_forever = False
        instance.start_trying_again = utcnow() - timedelta(days=days_ago)
        return instance

    def test_a_long_dormant_instance_is_marked_gone(self, db_session, monkeypatch):
        """`:442`'s assignment. The recheck loop is neutralised so this test
        observes only the first loop: a raising helper would otherwise route
        into `:494-497` and change `failures`.
        """
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance',
            _Recorder(result=_response(500)))
        self._dormant('gone.example', days_ago=6)
        db.session.commit()

        check_instance_health()

        db.session.expire_all()
        assert db.session.query(Instance).filter_by(
            domain='gone.example').first().gone_forever is True

    def test_a_recently_dormant_instance_is_not_marked_gone(self, db_session, monkeypatch):
        """The BOUNDARY at `:438`: `start_trying_again` must be older than the
        five-day cutoff, not merely set.
        """
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance',
            _Recorder(result=_response(500)))
        self._dormant('recent.example', days_ago=4)
        db.session.commit()

        check_instance_health()

        db.session.expire_all()
        assert db.session.query(Instance).filter_by(
            domain='recent.example').first().gone_forever is False

    def test_a_live_instance_is_untouched_by_the_sweep(self, db_session, monkeypatch):
        """`:437`'s `dormant == True` filter. A live instance is not selected
        by either loop -- `:447` requires dormant as well.
        """
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance',
            _Recorder(result=_response(500)))
        instance = make_instance('live.example')
        instance.dormant = False
        instance.start_trying_again = utcnow() - timedelta(days=99)
        db.session.commit()

        check_instance_health()

        db.session.expire_all()
        reloaded = db.session.query(Instance).filter_by(domain='live.example').first()
        assert reloaded.gone_forever is False
        assert reloaded.failures == 0
```

- [ ] **Step 2: Run and commit**

```bash
./run_tests.sh tests/test_shared_tasks_maintenance_health.py -v -p no:randomly
```

Subject: `test: cover check_instance_health's gone-forever sweep`

---

### Task 5: `check_instance_health`, the recheck loop

**Files:**
- Modify: `tests/test_shared_tasks_maintenance_health.py`
- Read: `app/shared/tasks/maintenance.py:446-499`

**Interfaces:**
- Consumes: `_Recorder`, `_response`, `NODEINFO_LINK` from Task 1.
- Produces: nothing later tasks depend on.

**Do not modify `app/shared/tasks/maintenance.py` in this task.**

Five branch points: `:453`'s skip, `:458`'s href fork, `:460` and `:478`'s status checks, `:463`'s `software` key, `:482`'s `rel` match, and the handler at `:494`.

- [ ] **Step 1: Write the tests**

```python
class TestCheckInstanceHealthRecheck:
    """`check_instance_health`'s second loop, `:446-497`.

    `:446-450` selects dormant instances that are not yet gone and are not
    instance 1. `:453` skips banned domains and the flipboard.com literal.
    `:458` forks on whether a `nodeinfo_href` is already known: with one,
    `:459` fetches it and `:463-467` revives the instance; without, `:473`
    discovers one and `:481-491` walks the links. `:494-497` catches whatever
    either path raises, rolls back and counts a failure.

    `:469-470` and `:492-493` are `finally` blocks that close the response.
    Unlike `monitor_healthy_instances`, both names are bound before the `try`
    they belong to, so DC2's defect does not exist here.
    """

    def _dormant(self, domain, href=None):
        instance = make_instance(domain)
        instance.dormant = True
        instance.gone_forever = False
        instance.start_trying_again = utcnow() + timedelta(days=1)
        instance.nodeinfo_href = href
        return instance

    def test_a_known_href_returning_software_revives_the_instance(self, db_session, monkeypatch):
        """`:464-467`. Deleting `:467` leaves the instance dormant."""
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance',
            _Recorder(result=_response(200, {'software': {'name': 'PieFed', 'version': '1.2.3'}})))
        self._dormant('back.example', href='https://back.example/nodeinfo/2.0')
        db.session.commit()

        check_instance_health()

        db.session.expire_all()
        reloaded = db.session.query(Instance).filter_by(domain='back.example').first()
        assert reloaded.dormant is False
        assert reloaded.software == 'piefed'
        assert reloaded.version == '1.2.3'
        assert reloaded.failures == 0

    def test_a_document_without_software_leaves_the_instance_dormant(self, db_session, monkeypatch):
        """`:463`'s false arm. A 200 alone is not enough to revive."""
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance',
            _Recorder(result=_response(200, {'unexpected': True})))
        self._dormant('quiet.example', href='https://quiet.example/nodeinfo/2.0')
        db.session.commit()

        check_instance_health()

        db.session.expire_all()
        assert db.session.query(Instance).filter_by(
            domain='quiet.example').first().dormant is True

    def test_a_non_200_leaves_the_instance_dormant(self, db_session, monkeypatch):
        """`:460`'s false arm."""
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance',
            _Recorder(result=_response(503)))
        self._dormant('down.example', href='https://down.example/nodeinfo/2.0')
        db.session.commit()

        check_instance_health()

        db.session.expire_all()
        assert db.session.query(Instance).filter_by(
            domain='down.example').first().dormant is True

    def test_discovery_finds_an_href_and_revives_the_instance(self, db_session, monkeypatch):
        """`:471`'s else arm and `:487-490`. The instance has no known href, so
        `:473` asks well-known/nodeinfo and `:482`'s rel match supplies one.
        """
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance',
            _Recorder(result=_response(200, {'links': [
                {'rel': NODEINFO_LINK, 'href': 'https://found.example/nodeinfo/2.0'}]})))
        self._dormant('found.example', href=None)
        db.session.commit()

        check_instance_health()

        db.session.expire_all()
        reloaded = db.session.query(Instance).filter_by(domain='found.example').first()
        assert reloaded.nodeinfo_href == 'https://found.example/nodeinfo/2.0'
        assert reloaded.dormant is False

    def test_a_link_list_with_no_match_leaves_the_instance_dormant(self, db_session, monkeypatch):
        """`:482`'s false arm, taken for every link. `:481`'s loop ends without
        a break and nothing is assigned.
        """
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance',
            _Recorder(result=_response(200, {'links': [
                {'rel': 'https://example.invalid/other', 'href': 'https://x.example/y'}]})))
        self._dormant('nomatch.example', href=None)
        db.session.commit()

        check_instance_health()

        db.session.expire_all()
        reloaded = db.session.query(Instance).filter_by(domain='nomatch.example').first()
        assert reloaded.nodeinfo_href is None
        assert reloaded.dormant is True

    def test_a_banned_domain_is_skipped_before_any_request(self, db_session, monkeypatch):
        """`:453`'s true arm. The oracle is that no request was made at all --
        asserting only that the instance stayed dormant would hold anyway.
        """
        recorder = _Recorder(result=_response(200, {'software': {'name': 'PieFed', 'version': '1.0'}}))
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance', recorder)
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.instance_banned', lambda domain: True)
        self._dormant('banned.example', href='https://banned.example/nodeinfo/2.0')
        db.session.commit()

        check_instance_health()

        assert recorder.calls == []

    def test_a_raising_request_counts_a_failure_and_continues(self, db_session, monkeypatch):
        """`:494-497`'s handler. The set comparison is deliberate: `:446`
        returns planner-ordered rows.
        """
        def _raise(*args, **kwargs):
            raise RuntimeError('recheck exploded')

        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance', _raise)
        self._dormant('bad-one.example', href='https://bad-one.example/nodeinfo/2.0')
        self._dormant('bad-two.example', href='https://bad-two.example/nodeinfo/2.0')
        db.session.commit()

        check_instance_health()

        db.session.expire_all()
        counted = {i.domain for i in db.session.query(Instance).all() if i.failures == 1}
        assert counted == {'bad-one.example', 'bad-two.example'}
```

- [ ] **Step 2: Run and commit**

```bash
./run_tests.sh tests/test_shared_tasks_maintenance_health.py -v -p no:randomly
```

Subject: `test: cover check_instance_health's dormant recheck loop`

---

### Task 6: `monitor_healthy_instances`, the discovery block

**Files:**
- Modify: `tests/test_shared_tasks_maintenance_health.py`
- Read: `app/shared/tasks/maintenance.py:531-562` **as Task 3 left it** — re-derive, the lines have moved

**Interfaces:**
- Consumes: `_Recorder`, `_response`, `NODEINFO_LINK` from Task 1.
- Produces: nothing later tasks depend on.

**Do not modify `app/shared/tasks/maintenance.py` in this task.** DC3 lives in this block and belongs to Task 7; write these tests against the code as it stands and let Task 7 change it.

- [ ] **Step 1: Write the tests**

Add to the `TestMonitorHealthyInstances` class Tasks 2 and 3 opened.

```python
    def test_discovery_assigns_the_matching_href(self, db_session, monkeypatch):
        """`:547-551`. A rel in the recognised set supplies the href and clears
        the failure state.
        """
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance',
            _Recorder(result=_response(200, {'links': [
                {'rel': NODEINFO_LINK, 'href': 'https://peer.example/nodeinfo/2.0'}]})))
        instance = make_instance('peer.example')
        instance.nodeinfo_href = None
        instance.failures = 3
        db.session.commit()

        monitor_healthy_instances()

        db.session.expire_all()
        reloaded = db.session.query(Instance).filter_by(domain='peer.example').first()
        assert reloaded.nodeinfo_href == 'https://peer.example/nodeinfo/2.0'
        assert reloaded.failures == 0

    def test_a_non_dict_link_does_not_crash_discovery(self, db_session, monkeypatch):
        """`:542`'s `isinstance` guard, taking its false arm on a bare string.

        Without the guard `'rel' in links` would test substring membership on a
        str rather than key membership on a dict, and the entry would be
        mis-read rather than skipped.
        """
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance',
            _Recorder(result=_response(200, {'links': ['just-a-string']})))
        instance = make_instance('odd.example')
        instance.nodeinfo_href = None
        db.session.commit()

        monitor_healthy_instances()

        db.session.expire_all()
        assert db.session.query(Instance).filter_by(
            domain='odd.example').first().nodeinfo_href is None

    def test_a_non_200_discovery_counts_a_failure(self, db_session, monkeypatch):
        """`:554-556`'s `elif`. A 404 is logged and counted, not retried."""
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance',
            _Recorder(result=_response(404)))
        instance = make_instance('missing.example')
        instance.nodeinfo_href = None
        db.session.commit()

        monitor_healthy_instances()

        db.session.expire_all()
        assert db.session.query(Instance).filter_by(
            domain='missing.example').first().failures > 0
```

- [ ] **Step 2: Run and commit**

```bash
./run_tests.sh tests/test_shared_tasks_maintenance_health.py -v -p no:randomly
```

Subject: `test: cover monitor_healthy_instances' nodeinfo discovery`

---

### Task 7: DC3 — count one failure per document, not one per link

**Files:**
- Modify: `app/shared/tasks/maintenance.py`, the `else` arm inside the discovery loop
- Modify: `tests/test_shared_tasks_maintenance_health.py`

**Interfaces:**
- Consumes: `_Recorder`, `_response` from Task 1.
- Produces: the discovery loop counting one failure per unmatched document.

**The test must FAIL against the unmodified code before you change anything.**

`update_dormant_gone` (`app/models.py:146-150`) turns an instance dormant above 2 failures and gone above 7, so a counter driven by document shape rather than by reachability crosses real thresholds.

- [ ] **Step 1: Work out the arithmetic, then write the test**

With three non-matching links and no `nodeinfo_href`, control passes through the discovery loop and then, because no href was assigned, through the `else` arm below the fetch block that increments again. **Derive both numbers yourself from the code as Task 3 left it** and state them in your report before running — the expected values below are this plan's arithmetic, and if yours disagrees, yours wins.

```python
    def test_an_unmatched_document_counts_one_failure_not_one_per_link(self, db_session, monkeypatch):
        """DC3: the increment sits INSIDE `:541`'s per-link loop.

        Three unrelated links record three failures, so a document's shape --
        not the instance's reachability -- drives `update_dormant_gone`'s
        thresholds (`app/models.py:146-150`: dormant above 2, gone above 7).

        Two increments are expected in total: one for the unmatched document,
        and one from the no-href arm below the fetch block.
        """
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance',
            _Recorder(result=_response(200, {'links': [
                {'rel': 'https://example.invalid/a', 'href': 'https://x.example/a'},
                {'rel': 'https://example.invalid/b', 'href': 'https://x.example/b'},
                {'rel': 'https://example.invalid/c', 'href': 'https://x.example/c'},
            ]})))
        instance = make_instance('noisy.example')
        instance.nodeinfo_href = None
        instance.failures = 0
        db.session.commit()

        monitor_healthy_instances()

        db.session.expire_all()
        assert db.session.query(Instance).filter_by(
            domain='noisy.example').first().failures == 2
```

Run it. Expected: FAIL, with four failures rather than two. **Paste the exact output.** If the observed pre-fix number is not four, say so and correct the plan's arithmetic in your report rather than adjusting the assertion until it passes.

- [ ] **Step 2: Make the change**

Replace the loop's `else` arm with a flag checked after the loop:

```python
                        matched = False
                        for links in nodeinfo_json['links']:
                            if isinstance(links, dict) and 'rel' in links and links['rel'] in [
                                'http://nodeinfo.diaspora.software/ns/schema/2.0',
                                'https://nodeinfo.diaspora.software/ns/schema/2.0',
                                'http://nodeinfo.diaspora.software/ns/schema/2.1'
                            ]:
                                instance.nodeinfo_href = links['href']
                                instance.failures = 0
                                instance.dormant = False
                                instance.gone_forever = False
                                matched = True
                                break
                        if not matched:
                            instance.failures += 1
```

The equivalent `else` in `check_instance_health:481-491` has no such increment and is not touched.

- [ ] **Step 3: Run the whole file, re-derive citations, commit**

```bash
./run_tests.sh tests/test_shared_tasks_maintenance_health.py -v -p no:randomly
```

Your edit adds two lines. Confirm with `wc -l` and re-derive every citation below the change.

Subject: `fix: count one nodeinfo failure per document, not per link`

---

### Task 8: DC4 — compare versions numerically

**Files:**
- Modify: `app/shared/tasks/maintenance.py`, the Lemmy version check and a new module-level helper
- Modify: `tests/test_shared_tasks_maintenance_health.py`

**Interfaces:**
- Consumes: `_Recorder`, `_response`, `NODEINFO_LINK` from Task 1.
- Produces: `_version_at_least(version: str, minimum: str) -> bool` in `maintenance.py`.

**The test must FAIL against the unmodified code before you change anything.**

- [ ] **Step 1: Write the test and watch it fail**

The check discards a Lemmy instance's `nodeinfo_href` so discovery re-runs. As strings `'0.19.10' >= '0.19.4'` is False, so the newest instances keep the stale href — the opposite of the intent.

```python
    def test_a_lemmy_point_release_above_nine_is_rediscovered(self, db_session, monkeypatch):
        """DC4: the version check compares strings.

        `'0.19.10' >= '0.19.4'` is False lexically, so exactly the newer Lemmy
        instances this check exists to catch keep their stale
        `nodeinfo/2.0.json` href instead of rediscovering it.

        The oracle is the rewritten href. Asserting the instance is merely
        'healthy' would hold on both sides of the fix.
        """
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance',
            _Recorder(result=_response(200, {'links': [
                {'rel': NODEINFO_LINK, 'href': 'https://lemmy.example/nodeinfo/2.1'}]})))
        instance = make_instance('lemmy.example', software='lemmy')
        instance.version = '0.19.10'
        instance.nodeinfo_href = 'https://lemmy.example/nodeinfo/2.0.json'
        db.session.commit()

        monitor_healthy_instances()

        db.session.expire_all()
        assert db.session.query(Instance).filter_by(
            domain='lemmy.example').first().nodeinfo_href == 'https://lemmy.example/nodeinfo/2.1'
```

Run it. Expected: FAIL — the href is still the `2.0.json` one. **Paste the exact output.**

Note `make_instance(domain, software='lemmy')` overrides the factory's `'mastodon'` default (`tests/factories.py:34`). This is the one test in the file that does so, and it does not reach the identity phases because those need `instance.online()` **and** a request that this test's recorder does not serve — confirm that by observation and say so in your report.

- [ ] **Step 2: Add the helper**

Module level in `maintenance.py`, above the tasks:

```python
def _version_at_least(version: str, minimum: str) -> bool:
    """Compare dotted numeric versions numerically rather than lexically.

    `'0.19.10' >= '0.19.4'` is False as strings, which is backwards for every
    caller that means "this release or newer". Non-numeric suffixes compare as
    0 rather than raising, so a version like '1.2.3-rc1' does not end a sweep.
    """
    def _parts(value):
        parts = []
        for chunk in value.split('.'):
            digits = ''
            for char in chunk:
                if not char.isdigit():
                    break
                digits += char
            parts.append(int(digits) if digits else 0)
        return parts

    left, right = _parts(version), _parts(minimum)
    width = max(len(left), len(right))
    left += [0] * (width - len(left))
    right += [0] * (width - len(right))
    return left >= right
```

- [ ] **Step 3: Use it**

In the Lemmy check, replace `instance.version >= '0.19.4'` with
`_version_at_least(instance.version, '0.19.4')`. Nothing else in the condition changes.

- [ ] **Step 4: Cover the helper directly**

The helper is new code and needs its own tests, not only the integration above.

```python
class TestVersionAtLeast:
    """`_version_at_least` -- the comparison DC4 introduced."""

    def test_a_double_digit_patch_outranks_a_single_digit_one(self):
        """The defect that motivated the helper: lexically '0.19.10' < '0.19.4'."""
        assert _version_at_least('0.19.10', '0.19.4') is True

    def test_an_older_release_does_not_qualify(self):
        assert _version_at_least('0.18.9', '0.19.4') is False

    def test_an_exact_match_qualifies(self):
        assert _version_at_least('0.19.4', '0.19.4') is True

    def test_a_missing_segment_is_treated_as_zero(self):
        assert _version_at_least('1', '1.0.0') is True

    def test_a_non_numeric_suffix_does_not_raise(self):
        assert _version_at_least('1.2.3-rc1', '1.2.3') is True
```

Add `_version_at_least` to the file's import list.

- [ ] **Step 5: Run and commit**

```bash
./run_tests.sh tests/test_shared_tasks_maintenance_health.py -v -p no:randomly
```

Your edit adds the helper's lines plus one changed line. Confirm with `wc -l` and re-derive citations.

Subject: `fix: compare instance versions numerically, not as strings`

Body states that the string comparison mis-ordered exactly the releases the check targets, and that the helper is scoped to this call site rather than generalised.

---

### Task 9: `monitor_healthy_instances`, fetch and escalation

**Files:**
- Modify: `tests/test_shared_tasks_maintenance_health.py`
- Read: `app/shared/tasks/maintenance.py`, the fetch block and the no-href else arm **as Tasks 3, 7 and 8 left them** — re-derive

**Interfaces:**
- Consumes: `_Recorder`, `_response` from Task 1.
- Produces: nothing later tasks depend on.

**Do not modify `app/shared/tasks/maintenance.py` in this task.**

The escalation thresholds are boundaries: dormant above 5 failures, gone above 12. **Each needs a test on both sides**, not one comfortably past. A test seeding 99 failures cannot tell `> 5` from `>= 5`.

- [ ] **Step 1: Write the tests**

```python
    def test_a_healthy_node_document_clears_the_failure_state(self, db_session, monkeypatch):
        """The fetch block's 200 arm: software, version and the three flags."""
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance',
            _Recorder(result=_response(200, {'software': {'name': 'PieFed', 'version': '1.2.3'}})))
        instance = make_instance('healthy.example')
        instance.nodeinfo_href = 'https://healthy.example/nodeinfo/2.0'
        instance.failures = 4
        db.session.commit()

        monitor_healthy_instances()

        db.session.expire_all()
        reloaded = db.session.query(Instance).filter_by(domain='healthy.example').first()
        assert reloaded.software == 'piefed'
        assert reloaded.version == '1.2.3'
        assert reloaded.failures == 0
        assert reloaded.dormant is False

    def test_a_non_200_node_response_drops_the_href_and_counts_a_failure(self, db_session, monkeypatch):
        """The `elif ... >= 300` arm: the href is cleared so discovery re-runs
        next sweep, and `most_recent_attempt` is stamped.
        """
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance',
            _Recorder(result=_response(503)))
        instance = make_instance('flaky.example')
        instance.nodeinfo_href = 'https://flaky.example/nodeinfo/2.0'
        instance.failures = 0
        db.session.commit()

        monitor_healthy_instances()

        db.session.expire_all()
        reloaded = db.session.query(Instance).filter_by(domain='flaky.example').first()
        assert reloaded.nodeinfo_href is None
        assert reloaded.failures == 1
        assert reloaded.most_recent_attempt is not None

    def test_the_sixth_failure_turns_an_instance_dormant(self, db_session, monkeypatch):
        """The BOUNDARY: `> 5`, so five failures is not enough and six is.

        Seeded at 5, the sweep's own increment makes 6.
        """
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance',
            _Recorder(result=_response(503)))
        instance = make_instance('sixth.example')
        instance.nodeinfo_href = 'https://sixth.example/nodeinfo/2.0'
        instance.failures = 5
        db.session.commit()

        monitor_healthy_instances()

        db.session.expire_all()
        reloaded = db.session.query(Instance).filter_by(domain='sixth.example').first()
        assert reloaded.failures == 6
        assert reloaded.dormant is True
        assert reloaded.start_trying_again is not None

    def test_the_fifth_failure_does_not(self, db_session, monkeypatch):
        """The other side of the same boundary. Seeded at 4, ending at 5."""
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance',
            _Recorder(result=_response(503)))
        instance = make_instance('fifth.example')
        instance.nodeinfo_href = 'https://fifth.example/nodeinfo/2.0'
        instance.failures = 4
        db.session.commit()

        monitor_healthy_instances()

        db.session.expire_all()
        reloaded = db.session.query(Instance).filter_by(domain='fifth.example').first()
        assert reloaded.failures == 5
        assert reloaded.dormant is False

    def test_an_instance_with_no_href_after_discovery_escalates(self, db_session, monkeypatch):
        """The else arm below the fetch block, reached when discovery found
        nothing. This is a DIFFERENT path from the fetch block's failure arm
        and has its own threshold checks.
        """
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance',
            _Recorder(result=_response(200, {'links': []})))
        instance = make_instance('nohref.example')
        instance.nodeinfo_href = None
        instance.failures = 12
        db.session.commit()

        monitor_healthy_instances()

        db.session.expire_all()
        reloaded = db.session.query(Instance).filter_by(domain='nohref.example').first()
        assert reloaded.gone_forever is True

    def test_a_banned_domain_is_skipped_before_any_request(self, db_session, monkeypatch):
        """`:522`'s true arm. The oracle is that no request was made."""
        recorder = _Recorder(result=_response(200, {'software': {'name': 'PieFed', 'version': '1.0'}}))
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance', recorder)
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.instance_banned', lambda domain: True)
        instance = make_instance('banned.example')
        instance.nodeinfo_href = 'https://banned.example/nodeinfo/2.0'
        db.session.commit()

        monitor_healthy_instances()

        assert recorder.calls == []
```

- [ ] **Step 2: Run and commit**

```bash
./run_tests.sh tests/test_shared_tasks_maintenance_health.py -v -p no:randomly
```

Subject: `test: cover monitor_healthy_instances' fetch and failure escalation`

---

### Task 10: Measure coverage, close residuals, raise the floor

**Files:**
- Modify: `coverage_floors.ini`
- Modify: `tests/test_shared_tasks_maintenance_health.py` (whatever the measurement shows is missing)

- [ ] **Step 1: Bring the stack down, then measure**

```bash
./run_tests.sh --down
./run_tests.sh --cov=app.shared.tasks.maintenance --cov-branch \
    --cov-report=json:/tmp/c32.json \
    tests/test_shared_tasks_maintenance_health.py \
    tests/test_shared_tasks_maintenance_external.py \
    tests/test_shared_tasks_maintenance_lifecycle.py \
    tests/test_shared_tasks_maintenance_cleanup.py
echo "exit: $?"
```

**All four files**, because the floor is for the whole module. Do not pipe. The JSON goes to the container's `/tmp`, not the repo root.

- [ ] **Step 2: List what is still missing in this round's range**

Derive the three function ranges yourself from `grep -n '^def \|^@celery.task' app/shared/tasks/maintenance.py` **at the moment you run this** — Tasks 2, 3, 7 and 8 have all edited the file.

```bash
podman compose -f compose.test.yaml exec -T test-runner python3 - <<'EOF'
import json
d = json.load(open('/tmp/c32.json'))
f = d['files']['app/shared/tasks/maintenance.py']
in_scope = []   # fill from the grep above: (start, end) per function, decorator-inclusive
def hit(n):
    return any(a <= n <= b for a, b in in_scope)
print('missing statements:', sorted(n for n in f['missing_lines'] if hit(n)))
print('missing branches:', sorted(k for k in f.get('missing_branches', []) if hit(k[0])))
print('percent_covered:', f['summary']['percent_covered'])
EOF
```

- [ ] **Step 3: Close what is left, and report what you do not**

Write the test for anything you can close cheaply and safely, and report what you leave with the reason. **Do not mark anything `# pragma: no branch` on your own judgment** — write the proof into your report and let a reviewer try to defeat it.

**Before recording any branch as unobservable, exhaust the oracles.** Sub-project 31 judged one branch unobservable on database state and exception propagation, and a reviewer defeated it with a log side-channel: the surrounding handler logged only when it caught something. Database state, exception propagation, call recording and **log output** are four different instruments.

- [ ] **Step 4: Raise the floor**

Edit `coverage_floors.ini`'s `app/shared/tasks/maintenance.py` from `66` to the measured integer, **rounded DOWN**. `tests/check_coverage_floors.py:75` compares `entry['summary']['percent_covered']` — the combined statement-and-branch figure — not `percent_statements_covered`, which read four points higher last round and would set an unholdable floor.

Write what you measure.

- [ ] **Step 5: Commit**

```bash
git status --porcelain -uall
```
Confirm no coverage JSON landed in the tree.

Subject: `test: raise maintenance.py's floor for Group C's first half`

---

### Task 11: Mutation testing

**Files:**
- Modify: none permanently. Every mutation is applied and restored.

**Every line number below is advisory and stale.** Tasks 2, 3, 7 and 8 edited the file. Re-derive each site against the tree as it stands, and read the dry-run's produced line before applying.

One at a time: dry-run without `-i` and read the line, apply, run `./run_tests.sh tests/test_shared_tasks_maintenance_health.py`, restore with `git checkout -- app/`, then assert an empty `git diff -- app/` and the expected `wc -l`. **Restore before any point where you might stop.**

- [ ] **Step 1: Run these mutations, in this order**

| # | Site | Mutation | Expected |
|---|------|----------|----------|
| 1 | `sync_defederation_subscriptions`' DELETE | drop `WHERE subscription_id is not null` | killed by the admin-ban test |
| 2 | its loop | `session.query(DefederationSubscription).all()` → `.limit(1).all()` | killed by the two-subscription test |
| 3 | `check_instance_health`'s cutoff | `timedelta(days=5)` → `days=3` | killed by the four-day test |
| 4 | its dormant filter | `Instance.dormant == True` → `== False` | killed |
| 5 | its gone filter | `Instance.gone_forever == False` → `== True` | killed |
| 6 | its `id != 1` filter | `!=` → `==` | **check which test kills it** — every seeded instance has id != 1, so this may survive as a hole |
| 7 | its skip guard | `instance_banned(...) or ... == 'flipboard.com'` → drop the `or` clause | **check** — no test seeds flipboard.com |
| 8 | its status check | `node.status_code == 200` → `!= 200` | killed |
| 9 | its `software` key check | `'software' in node_json` → `not in` | killed |
| 10 | its rel match | drop one of the three schema URLs | **check which** — only one is exercised |
| 11 | DC1's wrapper, if Task 2 added one | remove `with patch_db_session(session):` | killed by Task 2's test |
| 12 | DC2's guard, `nodeinfo` | `if nodeinfo is not None:` → `if nodeinfo is None:` | killed |
| 13 | DC2's guard, `node` | same | killed |
| 14 | DC3's flag | `if not matched:` → `if matched:` | killed by the three-link test |
| 15 | DC4's helper | `left >= right` → `left > right` | killed by the exact-match test |
| 16 | DC4's padding | drop the `[0] * (width - len(left))` padding | killed by the missing-segment test |
| 17 | DC4's call site | `_version_at_least(instance.version, '0.19.4')` → `'0.20.0'` | killed |
| 18 | the isinstance guard | `isinstance(links, dict)` → `isinstance(links, str)` | killed |
| 19 | discovery's status check | `nodeinfo.status_code == 200` → `!= 200` | killed |
| 20 | the `elif >= 300` | `>= 300` → `>= 400` | **check** — the test uses 404, so a 3xx case may be missing |
| 21 | dormant threshold | `instance.failures > 5` → `>= 5` | killed by the fifth/sixth pair |
| 22 | gone threshold | `instance.failures > 12` → `>= 12` | **check** — needs a pair either side of 12 |
| 23 | the no-href else arm | delete its `instance.failures += 1` | killed |

- [ ] **Step 2: For every survivor, decide which of two things it is**

A survivor **with a proof** is information about the code — record the proof and call it an equivalent mutant. A survivor **without one** is a hole; write the test that kills it.

**Before recording any survivor as equivalent, check the site against the test this table names as its killer.** A survivor at the wrong site looks exactly like a survivor at the right one.

**Two individually-equivalent mutations can be jointly load-bearing.** Sub-project 31 found a pair of redundant `.lower()` calls where dropping either alone was unobservable and dropping both broke the behaviour — a shape single-line mutation cannot discriminate by construction. If you suspect one here, say so rather than recording two unrelated equivalences.

- [ ] **Step 3: Restore and verify**

```bash
git diff --stat -- app/
wc -l app/shared/tasks/maintenance.py
```

Empty diff, and the line count Tasks 2, 3, 7 and 8 left behind.

- [ ] **Step 4: Commit only if a survivor made you write a test**

Subject: `test: kill the surviving mutants in maintenance Group C`

---

### Task 12: Register the findings and the facts

**Files:**
- Modify: `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`
- Modify: `tests/README.md`

- [ ] **Step 1: Confirm the next free numbers**

```bash
grep -n 'Next free number' docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md | tail -1
grep -oE '^\*\*18[0-9]\.' tests/README.md | tail -3
```

The plan says D374 and fact 187; verify rather than trusting it.

- [ ] **Step 2: Write the findings, from D374**

- **DC1, `monitor_healthy_instances`' session** — with the outcome Task 2 actually reached. If the discriminator could not separate the behaviours, say so plainly and say what was tried, rather than recording it as fixed or as unimportant.
- **DC2, the unbound `finally` names** — and that its justification is robustness against `get_request_instance`'s contract rather than a defect users hit, because that helper's bare `except:` swallows what the request raises. Record that the **reachable** instances of this shape are the two sites that call `get_request` directly, which raises. Re-derive those two line numbers.
- **DC3, the per-link failure count** — and that `update_dormant_gone` (`app/models.py:146-150`) turns an instance dormant above 2 failures and gone above 7, so document shape was crossing real thresholds.
- **DC4, the string version comparison** — and that the helper is deliberately scoped to one call site rather than generalised.
- **`sync_defederation_subscriptions` deletes then re-downloads.** Every subscription-sourced ban is removed and committed before the re-download begins, so the instances are unbanned in between and a mid-run failure leaves the table empty. Registered, not fixed: the repair is a build-then-swap restructure, outside this round's approved scope. **Re-derive the two line numbers.**
- **`get_request_instance` swallows everything** — including `KeyboardInterrupt` and `SystemExit` — via a bare `except:` at `app/utils.py:192`, and returns a synthetic 500 that callers cannot distinguish from a real one. This is what makes the harness trap in Step 3 possible.
- **`check_instance_health` wraps its body in `patch_db_session` and `monitor_healthy_instances` does not**, whatever Task 2 concluded about whether that is observable.
- **Group C's remaining half** — the identity phases, their statements, branch points and four-phase structure — so sub-project 33 does not re-derive them. Use the decorator-inclusive convention and say so.

- [ ] **Step 3: Write the new facts, from 187**

- **A bare `except:` in a shared helper defeats the outbound-HTTP guard.** `block_outbound_http` (`tests/conftest.py:262`) is session-scoped autouse and makes an unmatched request raise, but `get_request_instance` swallows that raise and returns a synthetic 500. A test that forgets to patch the helper does not fail; it silently exercises the failure path. Cross-reference fact 148 and sub-project 31's respx finding — this is the same shape reached by a different route.
- **Replacing a request helper by name beats making the transport fail.** Every helper these tasks use is imported at module scope, so patching `app.shared.tasks.maintenance.<name>` reaches the call site without respx and without `get_request`'s 3-to-10-second retry sleep. Contrast a helper imported *inside* a function, which the namespace idiom cannot reach.
- **Two sibling tasks in one module can differ in whether they wrap in `patch_db_session`**, and whether that difference is observable depends on where the helper's writes land. Record what Task 2 measured.
- Whatever Task 1's probe established, in its own words.

- [ ] **Step 4: Commit**

Subject: `docs: register the maintenance Group C findings and facts`

---

## Self-Review

**Spec coverage.** Every spec section maps to a task: `sync_defederation_subscriptions` to Task 1; DC1 to Task 2; DC2 to Task 3; `check_instance_health`'s two loops to Tasks 4 and 5; the discovery block to Task 6; DC3 to Task 7; DC4 to Task 8; the fetch-and-escalation block to Task 9; the floor to Task 10; the mutations to Task 11; the register-only findings and facts to Task 12. The spec's no-respx decision is carried in Task 1's prelude and relied on by every later task. The `id != 1` and planner-order constraints are in Global Constraints and restated where they bite.

**Placeholder scan.** No task says "add appropriate tests". Every code step carries its code. Task 10 Step 2's `in_scope` list is deliberately left for the implementer to fill from a live `grep`, because Tasks 2, 3, 7 and 8 shift the file — the step says so and gives the command.

**Type consistency.** `_Recorder.calls` holds `(args, kwargs)` tuples throughout, and every caller indexes `c[0][0]` for the first positional argument. `_response(status_code=200, payload=None)` is the single response constructor, used with a payload where production calls `.json()` and without one where it only reads `.status_code`. `_version_at_least(version, minimum) -> bool` is the signature Task 8 produces and Task 11's mutations 15-17 probe. `NODEINFO_LINK` is defined once in Task 1 and used in Tasks 5, 6 and 8.

**Four risks the plan carries deliberately.** Task 1 is a probe whose answer the plan predicts but does not presume. Task 2 is written so that "no change" is a successful outcome. Task 3's justification is robustness rather than a live bug, and the plan says so instead of inflating it. Task 8 introduces new production code — a version helper — which is why it carries its own unit tests rather than relying on the integration test alone.

**One thing deliberately not planned.** Task 11's table marks six rows "check which test kills it" rather than predicting killed. Sub-project 31's table predicted a kill that was provably impossible and predicted a survival that a later task had already closed; both cost review time. Where this plan does not know, it says so.

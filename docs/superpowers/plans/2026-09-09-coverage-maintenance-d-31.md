# Sub-project 31: `maintenance.py` Group D Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Take the five external-service tasks in `app/shared/tasks/maintenance.py` to 100% statement and branch coverage, raise the module's floor from 47, and land three production changes — each observed before it is made.

**Architecture:** One new test file, `tests/test_shared_tasks_maintenance_external.py`. This is the module's first group to leave the database: three outbound HTTP calls arranged with respx, one S3 client stubbed, one real temporary directory. Helpers belonging to other modules are replaced by recorders rather than exercised.

**Tech Stack:** Flask, SQLAlchemy 2.0.52, Celery (eager in tests), pytest, respx, boto3, PostgreSQL in a podman container.

**Spec:** `docs/superpowers/specs/2026-09-09-coverage-maintenance-d-31-design.md`

## Global Constraints

- **Delete nothing the task did not create.** `git checkout -- app/` is permitted only as a mutation-restore step.
- **There is no host Python with flask or pytest.** Everything runs through `./run_tests.sh <pytest args>`, which is `podman compose exec -T test-runner pytest "$@"` (`run_tests.sh:85`).
- **Only the controller runs the full suite**, one pytest session at a time, in the foreground.
- **Do not pipe `./run_tests.sh`.** A pipeline eats its exit status — read `${PIPESTATUS[0]}`, or do not pipe.
- **Coverage takes the dotted module form**: `--cov=app.shared.tasks.maintenance`. A path form collects nothing, writes no JSON, and exits 0 — a silent green failure.
- **A coverage run leaves its JSON in the HOST repo root**, because `compose.test.yaml` bind-mounts `./:/app:z`. Delete it when done — it is yours (fact 168).
- **Run `./run_tests.sh --down` before any measured run.** `pytest.ini:28` caps a session at 600s and the suite ran **371s** at the end of sub-project 30 — the closest it has been.
- **Test counts come from pytest's own collection output**, never `grep -c '^def test_'`.
- **No ordered assertions over rows a query planner returned.** Compare sets, or sort explicitly in Python.
- **Every line number is re-derived against the current tree, with numbered output** — `awk 'NR>=X && NR<=Y {printf "%d\t%s\n",NR,$0}' FILE`. Never count lines of an unnumbered `sed -n 'X,Yp'` range, and never read `grep -n` numbers from piped or already-extracted output as file line numbers (fact 165).
- **Where prose describes a statement, cite that statement's line**, not the `def` above it, not the `if` guarding it, not the continuation that closes it.
- **An edit falsifies prose the same task wrote minutes earlier.** Re-derive every citation AFTER your diff is final, and sweep the whole file rather than only the lines you wrote (fact 166). **Two sweeps in sub-project 30 were claimed complete when they were not**; a sweep's completeness claim is worth exactly as much as the check that verified it (fact 178).
- **A docstring must not claim a proof the test does not deliver.**
- **Commit with `git commit -F <file>`, never `-m`.** Lowercase `type:` subject prefix, normal English prose.
- **Commit trailers, in this order:**
  ```
  Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01BF7kVN2spWD86NYKWyGrVn
  ```
- **Paste command output verbatim.** A report in sub-project 30 was caught hand-massaging a diff block into itself.
- **Mutations run one at a time:** dry-run without `-i` and read the produced line first, apply, run, restore, then assert an empty `git diff -- app/` and the expected `wc -l`.
- **Do not run any probe that holds an uncommitted write on a row another session touches.** That deadlocked Postgres in sub-project 30 and wedged the container stack until three backends were terminated by hand — `pytest-timeout` cannot interrupt a backend blocked on a row lock (fact 172).
- **PC1, PC2 and PC3 are predictions, not observations.** Observe each before changing anything. A prediction that does not reproduce becomes a registered finding and the change is dropped. **PC2 is expected to possibly not reproduce.**

---

## File Structure

| File | Responsibility |
|------|----------------|
| `tests/test_shared_tasks_maintenance_external.py` | **Create.** Every test in this round. Group A's file is `..._cleanup.py`, Group B's is `..._lifecycle.py`; this name leaves Group C its own. |
| `app/shared/tasks/maintenance.py` | **Modify.** Three production changes: `clean_up_tmp`'s directory parameter, `add_remote_communities`' session, `delete_from_s3`'s `finally`. |
| `coverage_floors.ini` | **Modify.** `app/shared/tasks/maintenance.py` raised from 47. |
| `tests/README.md` | **Modify.** New facts from 183. |
| `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` | **Modify.** New findings from D360. This is the campaign's register — it is NOT `docs/superpowers/findings-register.md`, which does not exist. |

## Harness facts that bind every task

**There is no fixture transaction.** `tests/conftest.py:137-202`'s `db_session` yields `db.session`, then DELETEs every row and commits. A `commit()` in a test is real (fact 162).

**Tasks that have a session run on their own connection.** `get_task_session()` returns `Session(bind=db.engine)` (`app/utils.py:3673-3675`). Seeded rows must be committed before the task runs, and an ORM attribute read afterwards is stale unless the test calls `db.session.expire_all()` first (fact 153). **`add_remote_communities` has no session at all today** — that is PC2.

**`http_mock` is the respx fixture** (`tests/conftest.py:336-343`), a `respx.mock(assert_all_called=True)` router. `assert_all_called=True` means a test registering a route it never exercises **fails** — which is the protection this round wants.

**THE TRAP OF THIS ROUND, and Task 1 establishes it before anything relies on it.** respx raises `respx.models.AllMockedAssertionError` for an unmatched request. Its MRO is `AllMockedAssertionError → AssertionError → Exception`. It is **not** an `httpx.HTTPError`. So:

- `add_remote_communities:1077` catches only `httpx.HTTPError`, so an unmatched request there **propagates and fails the test**.
- `refresh_instance_chooser:1010` catches bare `except Exception`, so an unmatched request there is **swallowed into the "Failed to connect" branch**, the existing `InstanceChooser` row is deleted, and the loop continues. A test that mistypes a URL would exercise the failure path while believing it exercised the success path.
- `:1046`'s outer `except Exception` does the same for anything else in that iteration.

This is fact 148's shape in a new place. **Task 1 must confirm it by observation and record the result**; no later task may assume either behaviour.

**`get_request` sleeps on retry.** `app/utils.py:158-162` and `:173-177` both `sleep(random.randint(3, 10))` before retrying. A test that drives `get_request` into its `httpx.ReadError` or `httpx.HTTPError` handler costs 3-10 seconds of wall clock. **Do not write such a test.** Reach failure paths through the caller's own handler instead, or by making the route return a non-200 status, which does not retry.

**`random.shuffle` at `:999`** makes node processing order nondeterministic. No test may assert on that order. Assert on the resulting set of rows.

---

## Task 1: Establish respx's failure mode, then open the file with `delete_from_s3`

**Files:**
- Create: `tests/test_shared_tasks_maintenance_external.py`
- Read: `app/shared/tasks/maintenance.py:1121-1135`, `app/utils.py:131-180`

**Interfaces:**
- Consumes: nothing.
- Produces: the module docstring, the import block, `_Recorder`, and one passing test class. Every later task adds to this file.

`delete_from_s3` is the simplest function in the group — 7 statements, **zero branch points**, no session, no error handling. It is also the only one needing no HTTP.

- [ ] **Step 1: Probe respx's failure mode through both call paths, and paste what happens**

Write two throwaway tests in the new file. Each registers a route for one URL and lets the task request a *different* one, so respx has no match:

```python
def test_probe_unmatched_through_refresh(db_session, http_mock):
    http_mock.post('https://api.fediverse.observer/').respond(
        200, json={'data': {'nodes': [{'domain': 'peer.example',
                                       'uptime_alltime': 99,
                                       'monthsmonitored': 12}]}})
    # No route for https://peer.example/api/alpha/site/instance_chooser
    refresh_instance_chooser()
    print('REFRESH RETURNED NORMALLY')


def test_probe_unmatched_through_add_remote(db_session, http_mock):
    http_mock.get('https://example.invalid/nothing').respond(200, json={})
    # No route for lemmy.world
    add_remote_communities()
    print('ADD_REMOTE RETURNED NORMALLY')
```

Run with `-s` so the prints show:

```bash
./run_tests.sh tests/test_shared_tasks_maintenance_external.py -v -s
```

**Paste the complete output.** The expectation is that the first returns normally (the assertion swallowed by `:1010`) and the second raises. **Whatever happens, record it** — this is the fact every later task depends on.

- [ ] **Step 2: Record the answer in your report, then delete both probes**

State plainly, for each call path: does an unmatched request fail the test, or is it swallowed? Name the handler that swallows it if so. **Remove both throwaway tests before committing** — deleting them is permitted, your task created them.

If the result contradicts the expectation above, say so and stop rather than adapting later tasks yourself; the controller rules on it.

- [ ] **Step 3: Write the file's prelude**

```python
"""Group D of `app/shared/tasks/maintenance.py` -- the external-service tasks.

Sub-project 29 closed Group A (no transport) in
`tests/test_shared_tasks_maintenance_cleanup.py`; sub-project 30 closed Group B
(content lifecycle) in `tests/test_shared_tasks_maintenance_lifecycle.py`.
Group D is the five that leave the database:

  `refresh_instance_chooser:975`   `add_remote_communities:1070`
  `add_remote_community_from_post:1101`
  `delete_from_s3:1121`            `clean_up_tmp:1139`

AN UNMATCHED RESPX REQUEST DOES NOT FAIL EVERY TEST IN THIS FILE, and which
tests it fails is the first thing this round established.
`respx.models.AllMockedAssertionError` descends from `AssertionError`, not from
`httpx.HTTPError`. So `add_remote_communities:1077`'s narrow
`except httpx.HTTPError` lets it through and the test fails, while
`refresh_instance_chooser:1010`'s bare `except Exception` SWALLOWS it into the
"Failed to connect" branch -- deleting the domain's InstanceChooser row and
continuing. A test there that mistypes a URL exercises the failure path while
believing it exercised the success path. That is fact 148's shape in a new
place.

GET_REQUEST SLEEPS ON RETRY. `app/utils.py:158-162` and `:173-177` each
`sleep(random.randint(3, 10))` before retrying, so a test driving `get_request`
into either handler costs 3-10 seconds. No test here does. Failure paths are
reached through the caller's own handler, or by returning a non-200 status,
which does not retry.

`random.shuffle` AT `:999` makes node order nondeterministic. No test asserts on
processing order; the oracle is the resulting set of rows.

HELPERS BELONGING TO OTHER MODULES ARE ARRANGED, NOT EXERCISED --
`search_for_community` (`app/community/util.py:34`), `find_language_or_create`
(`app/activitypub/util.py:384`) and `boto3.session.Session`. Each is replaced in
THIS module's namespace with a recorder, so the tests assert on the handover
rather than on another module's behaviour (fact 179).
"""

import os
import tempfile
import time

import pytest

from app import db
from app.models import InstanceChooser, Settings, utcnow
from app.shared.tasks.maintenance import (
    add_remote_communities, add_remote_community_from_post, clean_up_tmp,
    delete_from_s3, refresh_instance_chooser,
)


class _Recorder:
    """Replace a module-level callable and remember how it was called.

    `calls` holds one tuple of positional arguments per invocation. Used for
    helpers belonging to other modules, so a test asserts on what was handed
    over rather than on what the callee then did (fact 179).
    """

    def __init__(self, result=None):
        self.calls = []
        self.result = result

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


class _StubS3:
    """A boto3 client stand-in recording `delete_objects` and `close`.

    `boto3.session.Session().client(...)` makes no network call (fact 181), but
    `delete_objects` does. This records both calls and can be made to raise, so
    `delete_from_s3`'s failure path is reachable without an endpoint.
    """

    def __init__(self, raise_on_delete=None):
        self.deleted = []
        self.closed = False
        self.raise_on_delete = raise_on_delete

    def delete_objects(self, **kwargs):
        self.deleted.append(kwargs)
        if self.raise_on_delete:
            raise self.raise_on_delete

    def close(self):
        self.closed = True


class _StubBoto3Session:
    """Stands in for `boto3.session.Session`, returning a fixed client."""

    def __init__(self, client):
        self._client = client

    def __call__(self):
        return self

    def client(self, **kwargs):
        self.client_kwargs = kwargs
        return self._client
```

- [ ] **Step 4: Write the `delete_from_s3` tests**

```python
class TestDeleteFromS3:
    """`delete_from_s3:1121` -- delete a batch of keys from object storage.

    Seven statements, ZERO branch points, and the only task in this module with
    no `try`, no `except`, no `finally` and no session. `:1135`'s `s3.close()`
    is the last statement of the body, so a raise from `:1134`'s
    `delete_objects` skips it and leaks the client. That is this round's PC3 and
    is NOT fixed by these tests.
    """

    def _patch_boto3(self, monkeypatch, client):
        stub = _StubBoto3Session(client)
        monkeypatch.setattr('app.shared.tasks.maintenance.boto3',
                            type('B', (), {'session': type('S', (), {'Session': stub})})())
        return stub

    def test_the_keys_are_sent_as_a_delete_payload(self, db_session, monkeypatch):
        client = _StubS3()
        self._patch_boto3(monkeypatch, client)

        delete_from_s3(['a.png', 'b.png'])

        assert len(client.deleted) == 1
        payload = client.deleted[0]['Delete']
        assert [o['Key'] for o in payload['Objects']] == ['a.png', 'b.png']
        assert payload['Quiet'] is True

    def test_an_empty_list_still_issues_one_call(self, db_session, monkeypatch):
        """`:1123`'s comprehension over an empty list.

        The task does not guard against an empty batch, so it sends a delete
        with no objects. This test pins that as the current behaviour rather
        than asserting it is desirable -- the guard's absence is registered,
        not fixed.
        """
        client = _StubS3()
        self._patch_boto3(monkeypatch, client)

        delete_from_s3([])

        assert client.deleted[0]['Delete']['Objects'] == []

    def test_the_client_is_closed_on_the_success_path(self, db_session, monkeypatch):
        client = _StubS3()
        self._patch_boto3(monkeypatch, client)

        delete_from_s3(['a.png'])

        assert client.closed is True
```

- [ ] **Step 5: Run**

```bash
./run_tests.sh tests/test_shared_tasks_maintenance_external.py -v
```
Expected: 3 passed, with both probes removed.

If the `boto3` monkeypatch shape does not work, **do not invent a different production seam** — report what failed. The task under test reads `boto3.session.Session()` at `:1126`, and replacing the module attribute in this module's namespace is the intended approach.

- [ ] **Step 6: Commit**

Subject: `test: open the maintenance Group D file with delete_from_s3`

---

## Task 2: PC3 — observe the leaked client, then add the `finally`

**Files:**
- Modify: `app/shared/tasks/maintenance.py:1121-1135`
- Modify: `tests/test_shared_tasks_maintenance_external.py`

**Interfaces:**
- Consumes: `_StubS3`, `_StubBoto3Session` from Task 1.
- Produces: `delete_from_s3` closing its client on the failure path.

**The test must FAIL against the unmodified code before you change anything.** If it passes, STOP, set your status to `DONE_WITH_CONCERNS`, and report — the round then lands two production changes, which is the correct outcome.

- [ ] **Step 1: Write the test and watch it fail**

```python
    def test_the_client_is_closed_when_the_delete_raises(self, db_session, monkeypatch):
        """PC3: `:1135`'s `s3.close()` is the body's last statement, not a
        `finally`, so a raise from `:1134` skips it and leaks the client's
        connection pool.
        """
        client = _StubS3(raise_on_delete=RuntimeError('s3 is down'))
        self._patch_boto3(monkeypatch, client)

        with pytest.raises(RuntimeError, match='s3 is down'):
            delete_from_s3(['a.png'])

        assert client.closed is True
```

Run:
```bash
./run_tests.sh "tests/test_shared_tasks_maintenance_external.py::TestDeleteFromS3::test_the_client_is_closed_when_the_delete_raises" -v
```
Expected: FAIL on `assert client.closed is True` — the exception propagates but the close never runs. **Paste the exact output.**

- [ ] **Step 2: Make the change**

`:1122-1135` becomes:

```python
    delete_payload = {
        'Objects': [{'Key': key} for key in s3_files_to_delete],
        'Quiet': True  # Optional: if True, successful deletions are not returned
    }
    boto3_session = boto3.session.Session()
    s3 = boto3_session.client(
        service_name='s3',
        region_name=current_app.config['S3_REGION'],
        endpoint_url=current_app.config['S3_ENDPOINT'],
        aws_access_key_id=current_app.config['S3_ACCESS_KEY'],
        aws_secret_access_key=current_app.config['S3_ACCESS_SECRET'],
    )
    try:
        s3.delete_objects(Bucket=current_app.config['S3_BUCKET'], Delete=delete_payload)
    finally:
        s3.close()
```

**No `except` clause.** There is no session to roll back, and an unhandled exception already propagates to Celery. The only change is that the client closes on the way out.

- [ ] **Step 3: Run the whole file**

```bash
./run_tests.sh tests/test_shared_tasks_maintenance_external.py -v
```
Expected: 4 passed. The three Task 1 tests are unaffected — the success path is unchanged.

- [ ] **Step 4: Re-derive citations, then commit**

Your edit adds two lines. Confirm with `wc -l` and re-check every citation into `delete_from_s3` and below it in the test file. **`clean_up_tmp` sits below this function**, so its citations shift.

Subject: `fix: close delete_from_s3's client when the delete raises`

Body quotes the Step 1 failure and states the scope limit: no logging added, nothing swallowed, no retry.

---

## Task 3: PC1 — make `clean_up_tmp` testable, then cover it

**Files:**
- Modify: `app/shared/tasks/maintenance.py` (`clean_up_tmp`)
- Modify: `tests/test_shared_tasks_maintenance_external.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `clean_up_tmp(directory=None)`.

**This change's justification is weaker than this round's usual bar, and the plan says so rather than dressing it up.** It is a testability fix. The failing observation is a `TypeError` from a parameter that does not exist, not a wrong result users see. The argument for it is that the alternative is a test suite writing into the working tree on every run.

- [ ] **Step 1: Prove the resolved paths are identical, and paste the evidence**

```bash
podman compose -f compose.test.yaml exec -T test-runner python -c "
from app import create_app
from config import Config
import os
class T(Config):
    SERVER_NAME='test.piefed.local'
a = create_app(T)
print('cwd                :', os.getcwd())
print('relative resolves  :', os.path.abspath('app/static/tmp'))
print('root_path default  :', os.path.join(a.root_path, 'static', 'tmp'))
"
```

Paste the output. **The last two must be identical.** If they are not, PC1 changes shape: report it and stop rather than proceeding on a premise the evidence contradicts.

- [ ] **Step 2: Write the test and watch it fail**

```python
class TestCleanUpTmp:
    """`clean_up_tmp:1139` -- delete stale media from the tmp directory.

    `:1146` returns early when the directory is absent; `:1149` walks it;
    `:1151` skips non-files; `:1153` filters by extension; `:1155` filters by
    age; `:1158`'s bare `except` swallows a failed unlink.

    THE DIRECTORY IS A PARAMETER BECAUSE OF THIS ROUND. It was a hardcoded
    RELATIVE path, `'app/static/tmp'`, resolved against the process working
    directory -- which under the test container is `/app`, the bind-mounted
    repository. Covering this function meant creating real files inside the
    working tree. The default now resolves to the same absolute path production
    already used, which this round verified rather than assumed.
    """

    def _stale(self, directory, name, age_seconds):
        path = os.path.join(directory, name)
        with open(path, 'w') as fh:
            fh.write('x')
        old = time.time() - age_seconds
        os.utime(path, (old, old))
        return path

    def test_a_stale_image_is_removed(self, db_session):
        directory = tempfile.mkdtemp()
        path = self._stale(directory, 'old.jpg', 25 * 60 * 60)

        clean_up_tmp(directory)

        assert not os.path.exists(path)
```

Run:
```bash
./run_tests.sh "tests/test_shared_tasks_maintenance_external.py::TestCleanUpTmp::test_a_stale_image_is_removed" -v
```
Expected: FAIL with `TypeError` — `clean_up_tmp()` takes no arguments. **Paste the exact output.**

- [ ] **Step 3: Make the change**

Add the parameter and replace the hardcoded assignment:

```python
@celery.task
def clean_up_tmp(directory=None):
    DELETABLE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".mp3", ".mp4"}
    ONE_DAY = 24 * 60 * 60

    now = time.time()
    if directory is None:
        directory = os.path.join(current_app.root_path, 'static', 'tmp')
```

The rest of the body is unchanged.

- [ ] **Step 4: Write the remaining tests**

```python
    def test_a_recent_image_survives(self, db_session):
        """The BOUNDARY at `:1155` -- older than one day, not merely old."""
        directory = tempfile.mkdtemp()
        path = self._stale(directory, 'recent.jpg', 23 * 60 * 60)

        clean_up_tmp(directory)

        assert os.path.exists(path)

    def test_a_file_with_an_undeletable_extension_survives(self, db_session):
        """`:1153`'s extension filter. A .txt is not in DELETABLE_EXTENSIONS."""
        directory = tempfile.mkdtemp()
        path = self._stale(directory, 'old.txt', 25 * 60 * 60)

        clean_up_tmp(directory)

        assert os.path.exists(path)

    def test_the_extension_check_is_case_insensitive(self, db_session):
        """`:1152` lowercases the filename before splitting, so .JPG matches."""
        directory = tempfile.mkdtemp()
        path = self._stale(directory, 'OLD.JPG', 25 * 60 * 60)

        clean_up_tmp(directory)

        assert not os.path.exists(path)

    def test_a_subdirectory_is_skipped(self, db_session):
        """`:1151`'s `os.path.isfile` guard, taking its false arm."""
        directory = tempfile.mkdtemp()
        nested = os.path.join(directory, 'sub.jpg')
        os.mkdir(nested)

        clean_up_tmp(directory)

        assert os.path.isdir(nested)

    def test_a_missing_directory_returns_early(self, db_session):
        """`:1146`'s true arm. The task must not raise on a path that is gone."""
        directory = tempfile.mkdtemp()
        os.rmdir(directory)

        clean_up_tmp(directory)

    def test_a_failed_unlink_is_swallowed(self, db_session, monkeypatch):
        """`:1156-1159`'s bare `except Exception: pass`.

        The task continues rather than aborting the sweep when one file cannot
        be removed. This asserts the swallow, not that swallowing is right --
        a file the sweep cannot delete is registered, not fixed.
        """
        directory = tempfile.mkdtemp()
        path = self._stale(directory, 'locked.jpg', 25 * 60 * 60)

        def _refuse(_):
            raise PermissionError('nope')

        monkeypatch.setattr('app.shared.tasks.maintenance.os.remove', _refuse)

        clean_up_tmp(directory)

        assert os.path.exists(path)
```

- [ ] **Step 5: Run the whole file**

```bash
./run_tests.sh tests/test_shared_tasks_maintenance_external.py -v
```
Expected: 11 passed.

`tempfile.mkdtemp()` lands in the container's `/tmp`, not the repository (fact 154). These tests do not clean up their directories; the container is disposable and `--down` removes it. **If you find a reason that is wrong, say so** rather than adding teardown nobody asked for.

- [ ] **Step 6: Re-derive citations, then commit**

Your edit adds one line. Confirm with `wc -l` and sweep the test file for citations into `clean_up_tmp`.

Subject: `refactor: let clean_up_tmp take the directory it sweeps`

Body must state plainly: the default resolves to the same absolute path the relative form resolved to from the container's working directory — quote the Step 1 evidence — so production behaviour is unchanged, and all four call sites in `app/cli.py` (`:851`, `:876` via `.delay()`, `:936`, imports at `:819` and `:893`) pass no argument. Say that it also removes a latent dependency on the process working directory.

---

## Task 4: `add_remote_community_from_post`

**Files:**
- Modify: `tests/test_shared_tasks_maintenance_external.py`
- Read: `app/shared/tasks/maintenance.py:1101-1117`

**Interfaces:**
- Consumes: `_Recorder`.
- Produces: nothing later tasks depend on.

Four branch points: `:1102`'s `if 'url' in post_data:`, `:1110`'s `if len(community_lookup):`, `:1112`'s loop, `:1113`'s `if f"@{SERVER_NAME}" not in cl:`.

`search_for_community` (`app/community/util.py:34`) is imported **inside** the function at `:1111`, so patching `app.shared.tasks.maintenance.search_for_community` will not work — the name does not exist in this module's namespace. Patch `app.community.util.search_for_community` instead, and say so in the docstring: this is the one helper in the round that a namespace patch cannot reach.

- [ ] **Step 1: Write the tests**

```python
class TestAddRemoteCommunityFromPost:
    """`add_remote_community_from_post:1101` -- turn a post into a lookup.

    `:1102` forks on whether the post carries a url: with one, `:1104` extracts
    the domain and actor and builds a single `!name@server`; without, `:1108`
    regex-scans the body for the same shape. `:1113` skips anything on this
    instance, and `:1116`'s bare `except Exception: pass` swallows whatever
    `search_for_community` raises.

    `search_for_community` IS IMPORTED INSIDE THE FUNCTION at `:1111`, so it
    never enters this module's namespace and `app.shared.tasks.maintenance.
    search_for_community` does not exist to patch. These tests patch
    `app.community.util.search_for_community` at its source -- the one helper
    in this round the namespace idiom cannot reach.
    """

    def test_a_post_with_a_url_yields_one_lookup(self, db_session, monkeypatch):
        recorder = _Recorder()
        monkeypatch.setattr('app.community.util.search_for_community', recorder)

        add_remote_community_from_post({'url': 'https://peer.example/c/books'})

        assert [c[0][0] for c in recorder.calls] == ['!books@peer.example']

    def test_a_post_without_a_url_scans_the_body(self, db_session, monkeypatch):
        """`:1102`'s false arm and `:1108`'s regex."""
        recorder = _Recorder()
        monkeypatch.setattr('app.community.util.search_for_community', recorder)

        add_remote_community_from_post(
            {'body': 'try !books@peer.example and !film@other.example'})

        assert {c[0][0] for c in recorder.calls} == {
            '!books@peer.example', '!film@other.example'}

    def test_a_body_with_no_match_looks_up_nothing(self, db_session, monkeypatch):
        """`:1110`'s false arm."""
        recorder = _Recorder()
        monkeypatch.setattr('app.community.util.search_for_community', recorder)

        add_remote_community_from_post({'body': 'nothing here'})

        assert recorder.calls == []

    def test_a_community_on_this_instance_is_skipped(self, db_session, monkeypatch, app):
        """`:1113`'s guard against looking up our own communities."""
        recorder = _Recorder()
        monkeypatch.setattr('app.community.util.search_for_community', recorder)
        local = f"!books@{app.config['SERVER_NAME']}"

        add_remote_community_from_post({'body': f'see {local}'})

        assert recorder.calls == []

    def test_a_failing_lookup_is_swallowed(self, db_session, monkeypatch):
        """`:1116`'s bare `except Exception: pass`.

        This is fact 111's shape, which the campaign has reasoned about twice in
        `notes.py` and `pages.py`. The test asserts the swallow -- the task
        continues to the next candidate rather than aborting -- and does not
        claim the swallow is correct.
        """
        def _raise(*args, **kwargs):
            raise RuntimeError('lookup exploded')

        monkeypatch.setattr('app.community.util.search_for_community', _raise)

        add_remote_community_from_post({'body': '!books@peer.example'})
```

- [ ] **Step 2: Run**

```bash
./run_tests.sh tests/test_shared_tasks_maintenance_external.py -v
```
Expected: 16 passed.

`test_a_post_with_a_url_yields_one_lookup` depends on `extract_domain_and_actor` (`app/activitypub/util.py:622`) returning `('peer.example', 'books')` for that url. **Verify that by reading the function before writing the assertion.** If it returns something else, use what it returns and say so in your report — do not adjust the expectation to whatever makes the test green without checking.

- [ ] **Step 3: Commit**

Subject: `test: cover the community-lookup fork and its swallowed failure`

---

## Task 5: `add_remote_communities` coverage

**Files:**
- Modify: `tests/test_shared_tasks_maintenance_external.py`
- Read: `app/shared/tasks/maintenance.py:1070-1098`

**Interfaces:**
- Consumes: `_Recorder`, `http_mock`.
- Produces: the tests Task 6 re-runs after giving the function a session.

**Do not change `app/shared/tasks/maintenance.py` in this task.** Task 6 owns PC2.

Four branch points: `:1077`'s `except httpx.HTTPError`, `:1080`'s status check, `:1087`'s loop, `:1089`'s featured skip, `:1092`'s already-imported skip.

**An unmatched respx request here propagates**, because `:1077` catches only `httpx.HTTPError` and `AllMockedAssertionError` is an `AssertionError`. Task 1 established this; rely on it.

`add_remote_community_from_post` is called at `:1095`. Patch it in **this** module's namespace — unlike `search_for_community`, it is defined here, so `app.shared.tasks.maintenance.add_remote_community_from_post` exists.

- [ ] **Step 1: Write the tests**

```python
class TestAddRemoteCommunities:
    """`add_remote_communities:1070` -- import new communities from a feed.

    `:1072` fetches lemmy.world's newcommunities listing; `:1080` proceeds only
    on 200; `:1087` walks the posts oldest-first; `:1089` skips stickied posts
    and `:1092` skips ids already imported; `:1095` hands each survivor to
    `add_remote_community_from_post` and `:1098` records the high-water mark.

    THIS FUNCTION HAS NO SESSION. `:1085`'s `get_setting` and `:1098`'s
    `set_setting` both go through `db.session` (`app/utils.py:203-222`), with no
    `get_task_session()` and no `patch_db_session` -- alone among this module's
    tasks. That is this round's PC2 and is NOT fixed by these tests.

    An unmatched respx request DOES fail a test here: `:1077` catches only
    `httpx.HTTPError`, and respx raises an `AssertionError`.
    """

    LISTING = 'https://lemmy.world/api/v3/post/list'

    def _posts(self, *posts):
        return {'posts': [{'post': p} for p in posts]}

    def test_a_new_post_is_handed_over_and_recorded(self, db_session, monkeypatch, http_mock):
        recorder = _Recorder()
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.add_remote_community_from_post', recorder)
        http_mock.get(url__startswith=self.LISTING).respond(
            200, json=self._posts({'id': 7, 'featured_community': False,
                                   'url': 'https://peer.example/c/books'}))

        add_remote_communities()

        assert [c[0][0]['id'] for c in recorder.calls] == [7]
        from app.utils import get_setting
        assert get_setting('last_successful_import', 0) == 7

    def test_a_stickied_post_is_skipped(self, db_session, monkeypatch, http_mock):
        """`:1089`'s `featured_community` guard."""
        recorder = _Recorder()
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.add_remote_community_from_post', recorder)
        http_mock.get(url__startswith=self.LISTING).respond(
            200, json=self._posts({'id': 8, 'featured_community': True,
                                   'url': 'https://peer.example/c/books'}))

        add_remote_communities()

        assert recorder.calls == []

    def test_an_already_imported_post_is_skipped(self, db_session, monkeypatch, http_mock):
        """`:1092`'s high-water mark.

        `set_setting` is called directly to establish the mark, because the
        task only writes it after a successful hand-over and this test needs it
        set beforehand.
        """
        from app.utils import set_setting
        set_setting('last_successful_import', 20)
        recorder = _Recorder()
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.add_remote_community_from_post', recorder)
        http_mock.get(url__startswith=self.LISTING).respond(
            200, json=self._posts({'id': 15, 'featured_community': False,
                                   'url': 'https://peer.example/c/books'}))

        add_remote_communities()

        assert recorder.calls == []

    def test_a_non_200_response_does_nothing(self, db_session, monkeypatch, http_mock):
        """`:1080`'s false arm.

        A non-200 status returns without retrying -- unlike a transport error,
        which would send `get_request` into `app/utils.py:173-177`'s handler and
        cost a 3-to-10-second sleep.
        """
        recorder = _Recorder()
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.add_remote_community_from_post', recorder)
        http_mock.get(url__startswith=self.LISTING).respond(503)

        add_remote_communities()

        assert recorder.calls == []

    def test_posts_are_processed_oldest_first(self, db_session, monkeypatch, http_mock):
        """`:1087`'s `reversed(...)`.

        The listing is sorted newest-first, so the task reverses it to walk
        forward in time -- otherwise the high-water mark at `:1098` would be set
        to the newest id on the first iteration and every older post would then
        be skipped by `:1092`. This is call order, not planner order, so
        comparing a list is legitimate here.
        """
        recorder = _Recorder()
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.add_remote_community_from_post', recorder)
        http_mock.get(url__startswith=self.LISTING).respond(
            200, json=self._posts(
                {'id': 9, 'featured_community': False, 'url': 'https://peer.example/c/b'},
                {'id': 8, 'featured_community': False, 'url': 'https://peer.example/c/a'}))

        add_remote_communities()

        assert [c[0][0]['id'] for c in recorder.calls] == [8, 9]
```

- [ ] **Step 2: Run**

```bash
./run_tests.sh tests/test_shared_tasks_maintenance_external.py -v
```
Expected: 21 passed.

`:1077`'s `except httpx.HTTPError: return` arm is **not** covered by these tests, and reaching it through `get_request` would cost a 3-to-10-second sleep. **Leave it and say so in your report** — Task 7 measures what is missing and the controller rules on it. Do not add a slow test to close it without being asked.

- [ ] **Step 3: Commit**

Subject: `test: cover the new-community import and its two skip guards`

---

## Task 6: PC2 — try to observe the missing session

**Files:**
- Possibly modify: `app/shared/tasks/maintenance.py:1070-1098`
- Modify: `tests/test_shared_tasks_maintenance_external.py` (only if a test discriminates)

**Interfaces:**
- Consumes: Task 5's tests, all of which must still pass.
- Produces: either a task session and wrapper, or a register-only finding.

**This task may correctly produce no change.** Unlike sub-project 30's `pwn_bots`, this function has no task session to be split *from* — everything goes through `db.session` consistently. The discriminator may show one checkout either way.

- [ ] **Step 1: Try to find a discriminating test**

Use the instrument sub-project 30 used: a `before_cursor_execute` listener on `db.engine` recording `id(conn.connection)` per statement.

```python
    def test_the_reads_and_writes_share_one_connection_checkout(self, db_session, monkeypatch, http_mock):
        from sqlalchemy import event

        recorder = _Recorder()
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.add_remote_community_from_post', recorder)
        http_mock.get(url__startswith=self.LISTING).respond(
            200, json=self._posts({'id': 7, 'featured_community': False,
                                   'url': 'https://peer.example/c/books'}))

        checkouts = []

        def _record(conn, cursor, statement, parameters, context, executemany):
            if 'settings' in statement.lower():
                checkouts.append(id(conn.connection))

        event.listen(db.engine, 'before_cursor_execute', _record)
        try:
            add_remote_communities()
        finally:
            event.remove(db.engine, 'before_cursor_execute', _record)

        assert checkouts
        assert len(set(checkouts)) == 1
```

Run it against the unmodified code. **Bounded effort: no more than three attempts** at finding a discriminator.

- [ ] **Step 2: Take one of two paths, and report which**

**If the test FAILS against unmodified code** — more than one checkout — the defect is real. Paste the failure, then give the function a session and wrap its body:

```python
@celery.task
def add_remote_communities():
    session = get_task_session()
    try:
        with patch_db_session(session):
            ...existing body, indented two levels...
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
```

Note the existing body has its own `try/except httpx.HTTPError: return` at `:1071-1078`, which must stay inside. Re-run: all 21 earlier tests plus this one must pass.

**If it PASSES against unmodified code** — one checkout already — **stop. Do not add the session.** Write into your report what you tried and why it could not discriminate, and state that PC2 becomes a register-only finding. Set your status to `DONE_WITH_CONCERNS`. **This is an anticipated outcome, not a failure**: the round then lands two production changes, which the spec allows for.

Do not add the session because it "matches the siblings" — that is a reason to register a finding, not to change production code on no evidence.

- [ ] **Step 3: Commit only if you changed something**

Subject if changed: `fix: give add_remote_communities its own session`
If unchanged, nothing is committed and the finding goes to Task 8.

---

## Task 7: `refresh_instance_chooser`

**Files:**
- Modify: `tests/test_shared_tasks_maintenance_external.py`
- Read: `app/shared/tasks/maintenance.py:975-1066`

**Interfaces:**
- Consumes: `_Recorder`, `http_mock`.
- Produces: nothing later tasks depend on.

The largest function in the group: 60 statements, 11 branch points — `:986`'s status check, `:991`'s shape check, `:1002`'s loop, `:1014`, `:1018`, `:1026`, `:1031`, `:1043`, `:1050`, `:1057`, plus the two `except` arms at `:1010` and `:1046`.

**The trap.** `:1010` and `:1046` are bare `except Exception`, so **an unmatched respx request inside the loop is swallowed** and looks like a connection failure — deleting the domain's row and continuing. Task 1 established this. **Every test in this class that expects the success path must register a route for the chooser URL**, or it will silently exercise the failure path instead.

`find_language_or_create` (`app/activitypub/util.py:384`) is imported at module scope in `maintenance.py:13`, so `app.shared.tasks.maintenance.find_language_or_create` **does** exist and the namespace idiom reaches it.

- [ ] **Step 1: Write the tests**

```python
class TestRefreshInstanceChooser:
    """`refresh_instance_chooser:975` -- rebuild the instance-chooser table.

    `:984` asks fediverse.observer for PieFed nodes; `:986` and `:991` bail on a
    bad status or shape; `:999` shuffles; `:1002` walks the nodes, asking each
    for its own chooser document; `:1026` creates or updates a row; `:1040`'s
    else and `:1010`'s handler delete one; `:1056` prunes rows for domains the
    observer no longer lists.

    A TEST HERE THAT FORGETS A ROUTE DOES NOT FAIL -- IT SILENTLY TESTS THE
    FAILURE PATH. `:1010`'s bare `except Exception` catches respx's
    `AllMockedAssertionError` (an `AssertionError`, not an `httpx.HTTPError`),
    logs "Failed to connect", deletes the row and continues. Every success-path
    test below registers both routes for that reason.

    `:999`'s `random.shuffle` makes processing order nondeterministic; no test
    asserts on it. The oracle is the resulting set of rows.
    """

    OBSERVER = 'https://api.fediverse.observer/'

    def _nodes(self, *domains):
        return {'data': {'nodes': [
            {'domain': d, 'uptime_alltime': 99, 'monthsmonitored': 12}
            for d in domains]}}

    def _chooser(self):
        return {'nsfw': False, 'newbie_friendly': True, 'name': 'Peer'}

    def test_a_listed_domain_gets_a_row(self, db_session, http_mock):
        http_mock.post(self.OBSERVER).respond(200, json=self._nodes('peer.example'))
        http_mock.get('https://peer.example/api/alpha/site/instance_chooser').respond(
            200, json=self._chooser())

        refresh_instance_chooser()

        db.session.expire_all()
        rows = db.session.query(InstanceChooser).all()
        assert {r.domain for r in rows} == {'peer.example'}

    def test_the_uptime_and_months_are_folded_into_the_stored_data(self, db_session, http_mock):
        """`:1021-1022` copy two fields off the observer node into the chooser
        document before `:1038` stores the whole thing.
        """
        http_mock.post(self.OBSERVER).respond(200, json=self._nodes('peer.example'))
        http_mock.get('https://peer.example/api/alpha/site/instance_chooser').respond(
            200, json=self._chooser())

        refresh_instance_chooser()

        db.session.expire_all()
        row = db.session.query(InstanceChooser).filter_by(domain='peer.example').first()
        assert row.data['uptime'] == 99
        assert row.data['monthsmonitored'] == 12

    def test_an_existing_row_is_updated_rather_than_duplicated(self, db_session, http_mock):
        """`:1026`'s false arm -- the row already exists."""
        db.session.add(InstanceChooser(domain='peer.example', nsfw=True))
        db.session.commit()
        http_mock.post(self.OBSERVER).respond(200, json=self._nodes('peer.example'))
        http_mock.get('https://peer.example/api/alpha/site/instance_chooser').respond(
            200, json=self._chooser())

        refresh_instance_chooser()

        db.session.expire_all()
        rows = db.session.query(InstanceChooser).filter_by(domain='peer.example').all()
        assert len(rows) == 1
        assert rows[0].nsfw is False

    def test_a_chooser_404_removes_an_existing_row(self, db_session, http_mock):
        """`:1040`'s else arm and `:1043`'s guard."""
        db.session.add(InstanceChooser(domain='peer.example'))
        db.session.commit()
        http_mock.post(self.OBSERVER).respond(200, json=self._nodes('peer.example'))
        http_mock.get('https://peer.example/api/alpha/site/instance_chooser').respond(404)

        refresh_instance_chooser()

        db.session.expire_all()
        assert db.session.query(InstanceChooser).filter_by(
            domain='peer.example').first() is None

    def test_a_domain_the_observer_dropped_is_pruned(self, db_session, http_mock):
        """`:1056-1058` -- rows for domains absent from the observer response."""
        db.session.add(InstanceChooser(domain='gone.example'))
        db.session.commit()
        http_mock.post(self.OBSERVER).respond(200, json=self._nodes('peer.example'))
        http_mock.get('https://peer.example/api/alpha/site/instance_chooser').respond(
            200, json=self._chooser())

        refresh_instance_chooser()

        db.session.expire_all()
        assert {r.domain for r in db.session.query(InstanceChooser).all()} == {'peer.example'}

    def test_a_language_in_the_document_is_resolved(self, db_session, http_mock, monkeypatch):
        """`:1031`'s true arm. `find_language_or_create` is imported at module
        scope (`maintenance.py:13`), so the namespace idiom reaches it.
        """
        recorder = _Recorder(result=type('L', (), {'id': 42})())
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.find_language_or_create', recorder)
        doc = self._chooser()
        doc['language'] = {'id': 1, 'code': 'en', 'name': 'English'}
        http_mock.post(self.OBSERVER).respond(200, json=self._nodes('peer.example'))
        http_mock.get('https://peer.example/api/alpha/site/instance_chooser').respond(
            200, json=doc)

        refresh_instance_chooser()

        db.session.expire_all()
        row = db.session.query(InstanceChooser).filter_by(domain='peer.example').first()
        assert row.language_id == 42
        assert recorder.calls[0][0] == ('en', 'English')

    def test_an_observer_non_200_returns_early(self, db_session, http_mock):
        """`:986`'s true arm. No chooser route is registered, and none is
        requested -- if one were, `assert_all_called=True` would fail the test.
        """
        db.session.add(InstanceChooser(domain='kept.example'))
        db.session.commit()
        http_mock.post(self.OBSERVER).respond(503)

        refresh_instance_chooser()

        db.session.expire_all()
        assert db.session.query(InstanceChooser).filter_by(
            domain='kept.example').first() is not None

    def test_a_malformed_observer_response_returns_early(self, db_session, http_mock):
        """`:991`'s shape check -- 200 with no `data.nodes`."""
        db.session.add(InstanceChooser(domain='kept.example'))
        db.session.commit()
        http_mock.post(self.OBSERVER).respond(200, json={'unexpected': True})

        refresh_instance_chooser()

        db.session.expire_all()
        assert db.session.query(InstanceChooser).filter_by(
            domain='kept.example').first() is not None

    def test_a_failure_inside_the_task_propagates(self, db_session, monkeypatch, http_mock):
        """`:1062-1064`'s handler.

        Reached by making `random.shuffle` raise -- it is called at `:999`,
        inside `:977`'s `try` and before the per-domain loop, so the outer
        handler is the one that catches it. Patching a symbol used inside the
        loop would instead be swallowed by `:1046`.
        """
        def _boom(*args, **kwargs):
            raise RuntimeError('the task itself failed')

        monkeypatch.setattr('app.shared.tasks.maintenance.random.shuffle', _boom)
        http_mock.post(self.OBSERVER).respond(200, json=self._nodes('peer.example'))

        with pytest.raises(RuntimeError, match='the task itself failed'):
            refresh_instance_chooser()
```

- [ ] **Step 2: Run**

```bash
./run_tests.sh tests/test_shared_tasks_maintenance_external.py -v
```
Expected: 30 passed, or 31 if Task 6 added a test.

Two arms are deliberately not covered here and **you must say so in your report** rather than leaving the gap silent: `:1010`'s connection-failure handler and `:1046`'s outer per-domain handler. Both are reachable — the first by registering no chooser route, the second by making something inside the loop raise — but a test for the first is indistinguishable from a test that simply forgot a route, which is the trap this class is built around. **Task 8 measures what is missing and the controller rules on whether to close them.**

- [ ] **Step 3: Commit**

Subject: `test: cover the instance-chooser refresh and its prune`

---

## Task 8: Measure coverage, close residuals, raise the floor

**Files:**
- Modify: `coverage_floors.ini`
- Modify: `tests/test_shared_tasks_maintenance_external.py` (whatever the measurement shows is missing)

- [ ] **Step 1: Bring the stack down, then measure**

```bash
./run_tests.sh --down
./run_tests.sh --cov=app.shared.tasks.maintenance --cov-branch \
    --cov-report=json:/app/coverage_d31.json \
    tests/test_shared_tasks_maintenance_external.py \
    tests/test_shared_tasks_maintenance_lifecycle.py \
    tests/test_shared_tasks_maintenance_cleanup.py
echo "exit: $?"
```

**All three files**, because the floor is for the whole module and Groups A and B count toward it. Do not pipe.

- [ ] **Step 2: Check the report's freshness**

The JSON appears in the host repo root because `/app` is bind-mounted (fact 168). `stat -c '%y' coverage_d31.json` and confirm the mtime postdates your run.

- [ ] **Step 3: List what is still missing inside Group D**

Derive the five function ranges yourself from `grep -n '^def ' app/shared/tasks/maintenance.py` **at the moment you run this** — Tasks 2, 3 and possibly 6 have edited the file and each shifted the lines below it.

```bash
python3 - <<'EOF'
import json
d = json.load(open('coverage_d31.json'))
f = d['files']['app/shared/tasks/maintenance.py']
group_d = []   # fill from the grep above: (start, end) per function
def in_d(n):
    return any(a <= n <= b for a, b in group_d)
print('missing statements in Group D:',
      sorted(n for n in f['missing_lines'] if in_d(n)))
print('partial branches in Group D:',
      sorted(k for k in f.get('missing_branches', []) if in_d(k[0])))
print('module percent_covered:', f['summary']['percent_covered'])
EOF
```

- [ ] **Step 4: Close what is left, and report what you did not**

Three arms are known to be open going in, and each has a reason:

- **`add_remote_communities:1077`'s `except httpx.HTTPError: return`** — reachable only by driving `get_request` into a transport failure, which sleeps 3-10 seconds. Closing it costs suite time this round does not have.
- **`refresh_instance_chooser:1010`'s connection handler** — reachable by registering no chooser route, but such a test is indistinguishable from one that forgot a route.
- **`:1046`'s outer per-domain handler** — reachable by making something inside the loop raise.

**Write the test for any you can close cheaply and safely, and report the ones you leave with the reason.** Do not mark anything `# pragma: no branch` on your own judgment — write the proof into your report and let a reviewer try to defeat it (fact 160). Before closing anything, check whether an earlier sub-project already proved it unreachable (fact 159).

- [ ] **Step 5: Raise the floor**

Edit `coverage_floors.ini`'s `app/shared/tasks/maintenance.py` from `47` to the measured integer, **rounded DOWN**. `tests/check_coverage_floors.py:75` compares `entry['summary']['percent_covered']` — the combined statement-and-branch figure from a `--cov-branch` run, not `percent_statements_covered`, which reads higher and would set an unholdable floor.

Groups A, B and D together are 423 of 637 statements, so expect roughly 66 — but **write what you measure**. Floors only ever rise.

- [ ] **Step 6: Delete the coverage JSON and commit**

```bash
rm coverage_d31.json
git status --porcelain -uall
```

Subject: `test: raise maintenance.py's floor for Group D`

---

## Task 9: Mutation testing

**Files:**
- Modify: none permanently. Every mutation is applied and restored.

**Every line number below is advisory and stale.** Tasks 2, 3 and possibly 6 edited the file. Re-derive each site against the tree as it stands now, and read the dry-run's produced line before applying.

One at a time: dry-run without `-i` and read the line, apply, run `./run_tests.sh tests/test_shared_tasks_maintenance_external.py`, restore with `git checkout -- app/`, then assert an empty `git diff -- app/` and the expected `wc -l`. **Restore before any point where you might stop.**

- [ ] **Step 1: Run these mutations, in this order**

| # | Site | Mutation | Expected |
|---|------|----------|----------|
| 1 | `delete_from_s3`'s Quiet flag | `'Quiet': True` → `False` | killed |
| 2 | PC3's `finally` | `finally:` → `else:` | killed by the raising-delete test |
| 3 | `clean_up_tmp`'s age comparison | `now - mtime > ONE_DAY` → `< ONE_DAY` | killed |
| 4 | `clean_up_tmp`'s day constant | `24 * 60 * 60` → `24 * 60` | killed |
| 5 | `clean_up_tmp`'s isfile guard | `if os.path.isfile(file_path):` → `if not os.path.isfile(...)` | killed |
| 6 | `clean_up_tmp`'s case fold | `os.path.splitext(filename.lower())` → drop `.lower()` | **EXPECTED TO SURVIVE — equivalent.** `:1153` lowercases the extension again (`ext.lower() in DELETABLE_EXTENSIONS`), so `:1152`'s `.lower()` is redundant: `splitext('OLD.JPG')` gives `.JPG`, which `:1153` folds to `.jpg` regardless. Record the proof and register the redundancy. **Then run the real case-fold mutation:** drop `.lower()` from `:1153` instead, which the `.JPG` test does kill. |
| 7 | PC1's default | `os.path.join(current_app.root_path, 'static', 'tmp')` → `'app/static/tmp'` | **EXPECTED TO SURVIVE.** Every test passes an explicit directory, so the default branch is never taken. It is a hole, not an equivalence — the two resolve identically only when cwd is the repo root, which is the latent dependency PC1 removed. Write the killing test: call `clean_up_tmp()` with no argument against a `current_app.root_path` you control, or record why that cannot be arranged. |
| 8 | `add_remote_community_from_post`'s url fork | `if 'url' in post_data:` → `not in` | killed |
| 9 | its local-instance guard | `if f"@{SERVER_NAME}" not in cl:` → drop the `not` | killed |
| 10 | its lookup-count guard | `if len(community_lookup):` → `if not len(...)` | killed |
| 11 | `add_remote_communities`' status check | `== 200` → `!= 200` | killed |
| 12 | its featured guard | `if post_data['featured_community']:` → `if not ...` | killed |
| 13 | its high-water comparison | `<= last_successful_import` → `>=` | killed |
| 14 | its ordering | delete `reversed(...)` | killed by the oldest-first test |
| 15 | `refresh_instance_chooser`'s status check | `!= 200` → `== 200` | killed |
| 16 | its shape check | delete `or 'nodes' not in response_data['data']` | killed by the malformed-response test |
| 17 | its chooser status check | `== 200` → `!= 200` | killed |
| 18 | its create-or-update guard | `if not instance_chooser:` → drop the `not` | killed |
| 19 | its language guard | `if 'language' in chooser_data and ...` → drop the second conjunct | **check which test kills it** — the compound is one arc pair and coverage cannot decompose it (fact 173) |
| 20 | its prune condition | `if record.domain not in observer_domains:` → drop the `not` | killed |
| 21 | PC2's wrapper, if Task 6 added one | remove the `with patch_db_session(session):` | killed by Task 6's test |

- [ ] **Step 2: For every survivor, decide which of two things it is**

A survivor with a proof is information about the code — record the proof and call it an equivalent mutant. A survivor without one is a hole; write the test that kills it.

**Before recording any survivor as equivalent, check the site against the test this table names as its killer** (fact 171). A survivor at the wrong site looks exactly like a survivor at the right one.

- [ ] **Step 3: Restore and verify**

```bash
git diff --stat -- app/
wc -l app/shared/tasks/maintenance.py
```

Empty diff, and the line count Tasks 2, 3 and 6 left behind.

- [ ] **Step 4: Commit only if a survivor made you write a test**

Subject: `test: kill the surviving mutants in maintenance Group D`

---

## Task 10: Register the findings and the facts

**Files:**
- Modify: `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`
- Modify: `tests/README.md`

- [ ] **Step 1: Confirm the next free numbers**

```bash
grep -oE 'D3[0-9]{2}' docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md | sort -u | tail -3
grep -n 'Next free number' docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md | tail -1
grep -oE '^\*\*1[0-9][0-9]\.' tests/README.md | tail -3
```

The plan says D360 and fact 183; verify rather than trusting it.

- [ ] **Step 2: Write the findings, from D360**

- **PC1, `clean_up_tmp`'s directory.** Record that it was a *testability* fix rather than a user-visible repair, that the observation was a `TypeError` rather than a wrong result, and quote Task 3 Step 1's evidence that the default resolves to the same absolute path. Record that it also removed a latent working-directory dependency.
- **PC2, `add_remote_communities`' session** — with the outcome Task 6 actually reached. If no test could discriminate, say so plainly and say what was tried, rather than recording it as fixed or as unimportant.
- **PC3, `delete_from_s3`'s `finally`** — what was observed, and the scope limit that no logging, swallowing or retry was added.
- **`archive_old_posts:923-924` leaks its S3 client the same way**, at the end of a `try` rather than in a `finally`. Registered, not fixed: it belongs to Group B, which sub-project 30 closed. **Re-derive those two line numbers** — Tasks 2, 3 and 6 shifted the file.
- **A fifth in-loop commit**, at `refresh_instance_chooser:1052`. Same shape as D342 and D354's four. Registered on the same reasoning, plus one specific to this site: the loop *deletes* rows on failure, so a partial run leaves a partly-pruned table.
- **`add_remote_communities:1098` calls `set_setting` once per post**, each committing and invalidating a memoized cache, up to 50 times per run.
- **`:1116`'s bare `except Exception: pass`** around `search_for_community` — fact 111's shape, third instance after `notes.py` and `pages.py`.
- **`add_remote_communities:1077` catches only `httpx.HTTPError`**, narrower than every sibling. Correct as written if `get_request` raises nothing else; recorded so a future round widening `get_request` knows this site assumes otherwise.
- **`refresh_instance_chooser`'s nested handlers at `:1010` and `:1046`** both delete the existing row, which reads as redundancy and is not — the inner one `continue`s, the outer falls through to `:1052`'s commit.
- **`delete_from_s3` does not guard against an empty batch**, sending a delete with no objects.
- **`clean_up_tmp` lowercases twice.** `:1152` folds the whole filename before `os.path.splitext`, and `:1153` folds the resulting extension again. The first is redundant — dropping it is an equivalent mutant, which this round's mutation table predicted as a kill and was wrong about. Registered, not fixed: removing a redundant `.lower()` is a change with no observable effect, and this round's approved scope is three.
- **Group C remains** — `sync_defederation_subscriptions`, `check_instance_health` and `monitor_healthy_instances`, 196 statements and 47 branch points, two-thirds of it in one function. Record the numbers so the next round does not re-derive them.

- [ ] **Step 3: Write the new facts, from 183**

- **respx's unmatched-request failure is caught by a bare `except Exception`.** `AllMockedAssertionError` descends from `AssertionError`, not `httpx.HTTPError`, so `refresh_instance_chooser:1010` swallows it into a connection-failure branch while `add_remote_communities:1077`'s narrow catch lets it through. **Two functions in one module with opposite behaviour**, and a test in the first that forgets a route silently exercises the failure path. This is fact 148's shape in a new place — cross-reference it.
- **`get_request` sleeps 3-10 seconds on retry**, at `app/utils.py:158-162` and `:173-177`. A test driving it into either handler pays that cost; reach failure paths through the caller's handler or a non-200 status instead.
- **A helper imported *inside* a function cannot be patched in the calling module's namespace.** `search_for_community` is imported at `maintenance.py:1111`, so `app.shared.tasks.maintenance.search_for_community` does not exist; the patch must target `app.community.util.search_for_community`. Contrast `find_language_or_create`, imported at module scope at `:13`, which the namespace idiom does reach.
- **`tempfile.mkdtemp()` is the remedy only when the code under test is told where to look.** `clean_up_tmp` hardcoded a relative path, so fact 154's advice did not apply until PC1 made the directory a parameter. Record the distinction.
- Whatever Task 1's probe established, in its own words.

- [ ] **Step 4: Commit**

Subject: `docs: register the maintenance Group D findings and facts`

---

## Self-Review

**Spec coverage.** Every spec section maps to a task: the five-function table to Tasks 1-7; PC1 to Task 3; PC2 to Task 6; PC3 to Task 2; the respx trap to Task 1's probe and Task 7's class docstring; `random.shuffle` to Task 7; the helper-arrangement rule to Tasks 4, 5 and 7; the S3 stub to Task 1; the floor to Task 8; the mutations to Task 9; the register-only findings and facts to Task 10.

**Placeholder scan.** No task says "add appropriate tests". Every code step carries its code. Task 8 Step 3's `group_d` list is deliberately left for the implementer to fill from a live `grep`, because Tasks 2, 3 and 6 shift the file — the step says so and gives the command.

**Type consistency.** `_Recorder.calls` holds `(args, kwargs)` tuples throughout, and every caller indexes `c[0][0]` for the first positional argument. `_StubS3` exposes `deleted`, `closed` and `raise_on_delete`; `_StubBoto3Session` wraps it. `clean_up_tmp(directory=None)` is the signature Task 3 produces and Task 9's mutation 7 probes.

**Three risks the plan carries deliberately.** Task 1 is a probe whose answer the plan predicts but does not presume. Task 2 and Task 3 each open with a test that must FAIL, and Task 3's is a `TypeError` rather than a wrong assertion — the plan says why that is weaker and why it is still worth making. Task 6 is written so that "no change" is a successful outcome.

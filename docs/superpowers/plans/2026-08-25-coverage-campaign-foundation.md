# Coverage Campaign Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the mocking layer and per-module coverage ratchet that the remaining fourteen sub-projects depend on, and prove it by taking one currently-untestable module to 100%.

**Architecture:** Add `moto` (S3), `respx` (outbound `httpx`), `fakeredis` and Celery eager mode, expose each as a shared fixture in `tests/conftest.py`, and add a `federation_peer` fixture that serves webfinger and actor responses so inbox and follow paths can be exercised without a live remote server. Per-module coverage floors live in `coverage_floors.ini` and are enforced by `tests/check_coverage_floors.py`, because coverage.py's own `fail_under` is global only.

**Tech Stack:** Python 3.13, Flask 3.1.3, pytest, pytest-cov, coverage.py, respx, moto, fakeredis, httpx, podman-compose.

**Spec:** `docs/superpowers/specs/2026-08-25-coverage-campaign-foundation-design.md`

## Global Constraints

- **Project rule: `if TYPE_CHECKING` is always a bug.** Never introduce it. It appears zero times in `app/` today. It must NOT be added to `exclude_lines`, where it is a common default.
- **Project rule: imports go at the top of the file. No inline imports.** `app/` contains 216 existing violations; this plan adds none. Test files and fixtures obey the rule too.
- **Coverage is a floor, not the goal.** Every test asserts on observable behaviour — a response header, a database row, a return value. Never assert that a mock was called unless the call IS the behaviour under test. A test that executes a line without asserting anything is a defect and reviewers reject it.
- **Every pragma carries a written justification.** An unexplained `# pragma:` is rejected.
- Branch coverage is not condition coverage: a compound condition can read as covered while a sub-condition is never falsified.
- Floors only ever rise. `app/request_hooks.py` is seeded at 100.
- Use `--cov=app.request_hooks` (dotted). `--cov=app/request_hooks.py` (file path) silently measures nothing — `tests/README.md` documents this.
- No new RUNTIME dependencies. `moto`, `respx` and `fakeredis` are test dependencies, added beside `pytest` and `pytest-cov`.
- Tests run only via `./run_tests.sh [pytest args]`. There is NO host Python environment. See `tests/README.md`.
- Baseline: `./run_tests.sh tests/ -q --ignore=tests/test_activitypub_util.py` → 317 passed, 0 failed. Must still be 0 failed at every commit.

---

> **CORRECTION (2026-08-25, final-fix wave).** Where this plan says to append a
> test dependency to `requirements.txt`, that instruction is WRONG and was
> followed. `requirements.txt` is the production install -- the Dockerfile's
> `builder` stage, `deploy.sh` and INSTALL.md all run
> `pip install -r requirements.txt` -- and `atheris`, added by the fuzzing task,
> publishes no aarch64 wheel, so it broke `pip install -r requirements.txt` on
> every ARM64 host. Test dependencies now live in `requirements-test.txt`,
> installed by the Dockerfile's `test` stage, which is what `compose.test.yaml`
> builds. The plan text below is left as written because it is the record of
> what was planned; do not follow this part of it.


## File Structure

| File | Responsibility | Change |
|---|---|---|
| `requirements.txt` | add `respx`, `moto`, `fakeredis` | Modify |
| `.coveragerc` | branch mode, exclusion policy | Create |
| `coverage_floors.ini` | per-module floors, only ever raised | Create |
| `tests/check_coverage_floors.py` | enforce per-module floors from `coverage.json` | Create |
| `tests/conftest.py` | `http_mock`, `federation_peer`, `s3_bucket`, `redis_double`, eager Celery | Modify |
| `tests/test_coverage_floors.py` | tests for the floor checker itself | Create |
| `tests/test_instance_util.py` | proof: `app/instance/util.py` to 100% | Create |
| `tests/test_fixture_proofs.py` | proof: S3, Redis and Celery fixtures exercise real app code | Create |
| `tests/README.md` | document the ratchet and the fixtures | Modify |

---

## Task 1: Dependencies and the per-module ratchet

**Files:**
- Modify: `requirements.txt`
- Create: `.coveragerc`
- Create: `coverage_floors.ini`
- Create: `tests/check_coverage_floors.py`
- Test: `tests/test_coverage_floors.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `tests/check_coverage_floors.py` with `read_floors(path) -> dict[str, float]`, `violations(coverage_data, floors) -> list[tuple[str, float, float]]`, and a `main()` returning an exit code
  - `coverage_floors.ini` seeded with `app/request_hooks.py = 100`

**Why a script rather than configuration:** coverage.py's `fail_under` is a single global number. There is no built-in per-module threshold, so the ratchet the spec calls for has to be implemented. `violations()` is pure and takes already-parsed data, so it is testable without running coverage.

- [ ] **Step 1: Write the failing test**

Create `tests/test_coverage_floors.py`:

```python
import unittest

from tests.check_coverage_floors import read_floors, violations


class TestViolations(unittest.TestCase):

    def test_module_below_its_floor_is_a_violation(self):
        data = {'files': {'app/x.py': {'summary': {'percent_covered': 82.0}}}}
        self.assertEqual(violations(data, {'app/x.py': 90.0}),
                         [('app/x.py', 82.0, 90.0)])

    def test_module_at_its_floor_is_not_a_violation(self):
        data = {'files': {'app/x.py': {'summary': {'percent_covered': 90.0}}}}
        self.assertEqual(violations(data, {'app/x.py': 90.0}), [])

    def test_module_above_its_floor_is_not_a_violation(self):
        data = {'files': {'app/x.py': {'summary': {'percent_covered': 97.5}}}}
        self.assertEqual(violations(data, {'app/x.py': 90.0}), [])

    def test_module_with_no_floor_is_ignored(self):
        """Unfinished modules must not block anyone."""
        data = {'files': {'app/y.py': {'summary': {'percent_covered': 3.0}}}}
        self.assertEqual(violations(data, {'app/x.py': 90.0}), [])

    def test_floored_module_absent_from_the_report_is_a_violation(self):
        """A module that vanishes from coverage output is a regression, not a pass.

        Renaming or failing to import a floored module would otherwise silently
        satisfy the ratchet.
        """
        self.assertEqual(violations({'files': {}}, {'app/x.py': 90.0}),
                         [('app/x.py', 0.0, 90.0)])

    def test_several_violations_are_all_reported(self):
        data = {'files': {'app/x.py': {'summary': {'percent_covered': 10.0}},
                          'app/y.py': {'summary': {'percent_covered': 20.0}}}}
        result = violations(data, {'app/x.py': 50.0, 'app/y.py': 50.0})
        self.assertEqual(len(result), 2)


class TestReadFloors(unittest.TestCase):

    def test_reads_module_floors(self):
        import tempfile
        import os
        handle, path = tempfile.mkstemp(suffix='.ini')
        with os.fdopen(handle, 'w') as f:
            f.write('[floors]\napp/request_hooks.py = 100\napp/x.py = 42.5\n')
        try:
            self.assertEqual(read_floors(path),
                             {'app/request_hooks.py': 100.0, 'app/x.py': 42.5})
        finally:
            os.unlink(path)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./run_tests.sh tests/test_coverage_floors.py -q`
Expected: FAIL at import — `ModuleNotFoundError: No module named 'tests.check_coverage_floors'`

- [ ] **Step 3: Write the checker**

Create `tests/check_coverage_floors.py`:

```python
"""Enforce per-module coverage floors.

coverage.py's own fail_under is a single global number, so the campaign's
per-module ratchet has to be implemented. Floors live in coverage_floors.ini and
only ever rise; a module with no floor is ignored, so unfinished modules block
nobody.

Usage, after a run that wrote coverage.json:

    python tests/check_coverage_floors.py coverage.json coverage_floors.ini
"""

import configparser
import json
import sys


def read_floors(path):
    """Read module -> minimum percentage from an ini file's [floors] section."""
    parser = configparser.ConfigParser()
    parser.read(path)
    if not parser.has_section('floors'):
        return {}
    return {module: float(value) for module, value in parser.items('floors')}


def violations(coverage_data, floors):
    """Return (module, actual, floor) for every module below its floor.

    A floored module missing from the report counts as 0.0 rather than passing:
    a rename or an import failure would otherwise satisfy the ratchet silently.
    """
    files = coverage_data.get('files', {})
    found = []
    for module, floor in floors.items():
        entry = files.get(module)
        actual = entry['summary']['percent_covered'] if entry else 0.0
        if actual < floor:
            found.append((module, actual, floor))
    return found


def main():
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2

    with open(sys.argv[1]) as handle:
        coverage_data = json.load(handle)
    floors = read_floors(sys.argv[2])

    found = violations(coverage_data, floors)
    for module, actual, floor in found:
        print(f'{module}: {actual:.2f}% is below its floor of {floor:.2f}%',
              file=sys.stderr)

    if found:
        return 1
    print(f'All {len(floors)} module floors met.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
```

Note `configparser` lowercases keys by default, which is harmless here because module paths are already lowercase.

- [ ] **Step 4: Run test to verify it passes**

Run: `./run_tests.sh tests/test_coverage_floors.py -q`
Expected: PASS, 7 tests

- [ ] **Step 5: Add the dependencies**

Append to `requirements.txt`, beside the existing `pytest` and `pytest-cov`:

```
respx
moto
fakeredis
```

`respx` rather than `responses`: this codebase's outbound HTTP is `httpx` (`app/utils.py:131` `get_request` returns `httpx.Response`), and `responses` patches `requests`, so it would install cleanly and match nothing.

- [ ] **Step 6: Create the coverage configuration**

Create `.coveragerc`:

```ini
[run]
branch = True
source = app

[report]
exclude_lines =
    pragma: no cover
    if __name__ == .__main__.:
    raise NotImplementedError
    @(abc\.)?abstractmethod

[json]
output = coverage.json
```

`TYPE_CHECKING` is deliberately absent: it is a common default in this list, and it is a project rule that `if TYPE_CHECKING` is always a bug. Adding it here would quietly license the thing the rule forbids.

Create `coverage_floors.ini`:

```ini
# Per-module coverage floors for the coverage campaign.
#
# Floors only ever RISE. Raising one is the deliverable of a sub-project;
# lowering one to make a run pass defeats the ratchet and must be rejected.
#
# A module with no entry here is ignored, so unfinished modules block nobody.

[floors]
app/request_hooks.py = 100
```

- [ ] **Step 7: Rebuild the image and verify the ratchet end to end**

```bash
./run_tests.sh --down
podman-compose -f compose.test.yaml build --no-cache test-runner
./run_tests.sh tests/ -q --ignore=tests/test_activitypub_util.py --cov=app --cov-report=json
podman-compose -f compose.test.yaml exec -T test-runner \
    python tests/check_coverage_floors.py coverage.json coverage_floors.ini
```

Expected: `All 1 module floors met.` and exit 0.

`--down` destroys the tmpfs database, so this run replays ~269 migrations and is slow exactly once.

- [ ] **Step 8: Prove the ratchet bites**

Temporarily lower nothing and instead raise the floor to an unreachable value:

```bash
sed -i 's|app/request_hooks.py = 100|app/request_hooks.py = 101|' coverage_floors.ini
podman-compose -f compose.test.yaml exec -T test-runner \
    python tests/check_coverage_floors.py coverage.json coverage_floors.ini; echo "exit=$?"
sed -i 's|app/request_hooks.py = 101|app/request_hooks.py = 100|' coverage_floors.ini
```

Expected: the first command prints a violation line and `exit=1`; after restoring, it passes again. Put both outputs in your report — a ratchet nobody has seen fail is not known to work.

- [ ] **Step 9: Commit**

```bash
git add requirements.txt .coveragerc coverage_floors.ini tests/check_coverage_floors.py tests/test_coverage_floors.py
git commit -m "test: add coverage ratchet and mocking dependencies"
```

---

## Task 2: HTTP and federation-peer fixtures

**Files:**
- Modify: `tests/conftest.py`
- Test: `tests/test_instance_util.py`
- Modify: `coverage_floors.ini`

**Interfaces:**
- Consumes: the dependencies from Task 1.
- Produces:
  - fixture `http_mock` — a `respx` router with `assert_all_called=True`
  - fixture `federation_peer` — callable `federation_peer(handle)` registering webfinger, actor and inbox routes for `name@domain`, returning the actor dict it serves

**The proof is `app/instance/util.py` reaching 100%.** Its `bulk_follow` calls `search_for_user`, which does a webfinger fetch then an actor fetch (`app/user/utils.py:113-135`). If that module cannot reach 100% with these fixtures, the fixtures are wrong and this task is not done.

**A gotcha that will cost an hour if you hit it blind:** `get_request` (`app/utils.py:131`) calls `is_invalid_get_request_uri` first, which returns True for any host ending `.local` (`app/utils.py:4585`). The test app's own `SERVER_NAME` is `test.piefed.local`, which is fine because it is never fetched — but **any peer domain you invent must not end in `.local`**, or the request is rejected before `respx` ever sees it. Use a domain like `mastodon.cloud`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_instance_util.py`:

```python
import pytest

from app.instance.util import bulk_follow, is_fedi_handle
from app.models import UserFollower
from tests.factories import make_instance, make_site, make_user


class TestIsFediHandle:

    def test_bare_handle(self):
        assert is_fedi_handle('wakko@mastodon.cloud')

    def test_leading_at_is_accepted(self):
        assert is_fedi_handle('@wakko@mastodon.cloud')

    def test_not_a_handle(self):
        assert not is_fedi_handle('not a handle')


def test_bulk_follow_follows_a_resolved_remote_account(app, db_session, federation_peer):
    """The end-to-end path this fixture exists to enable: a handle becomes a follow."""
    make_instance('test.piefed.local', software='piefed')
    make_site()
    local = make_user(None, 'localuser', local=True)
    federation_peer('wakko@mastodon.cloud')

    bulk_follow(local.id, ['wakko@mastodon.cloud'])

    assert UserFollower.query.filter_by(local_user_id=local.id,
                                        is_inward=False).count() == 1


def test_bulk_follow_skips_an_account_already_followed(app, db_session, federation_peer):
    """Re-running an import must not create a second follow."""
    make_instance('test.piefed.local', software='piefed')
    make_site()
    local = make_user(None, 'localuser', local=True)
    federation_peer('wakko@mastodon.cloud')

    bulk_follow(local.id, ['wakko@mastodon.cloud'])
    bulk_follow(local.id, ['wakko@mastodon.cloud'])

    assert UserFollower.query.filter_by(local_user_id=local.id,
                                        is_inward=False).count() == 1


def test_bulk_follow_rolls_back_and_reraises_on_failure(app, db_session, federation_peer):
    """The except arm: a failure must roll back rather than leave a partial import."""
    make_instance('test.piefed.local', software='piefed')
    make_site()

    with pytest.raises(Exception):
        bulk_follow(999999, ['wakko@mastodon.cloud'])

    assert UserFollower.query.count() == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./run_tests.sh tests/test_instance_util.py -q`
Expected: FAIL — `fixture 'federation_peer' not found`

- [ ] **Step 3: Add the fixtures**

In `tests/conftest.py`, with `import respx` and `import httpx` at the top of the file alongside the existing imports — not inside the functions, per the project rule:

```python
@pytest.fixture
def http_mock():
    """A respx router intercepting all outbound httpx traffic.

    assert_all_called=True means a test that registers a route it never exercises
    fails, rather than passing while silently testing less than it claims.
    """
    with respx.mock(assert_all_called=True) as router:
        yield router


@pytest.fixture
def federation_peer(http_mock):
    """Serve webfinger and actor responses for a remote handle.

    Returns a callable: federation_peer('wakko@mastodon.cloud') -> actor dict.

    The domain must not end in '.local' -- get_request() rejects those via
    is_invalid_get_request_uri() before respx ever sees the request.

    The payload shape follows docs/activitypub_examples/users.md rather than being
    invented, so a test passing here means the code handles what real servers send.
    """
    def register(handle):
        name, domain = handle.lstrip('@').split('@')
        actor_url = f'https://{domain}/users/{name}'
        actor = {
            '@context': 'https://www.w3.org/ns/activitystreams',
            'type': 'Person',
            'id': actor_url,
            'preferredUsername': name,
            'name': name,
            'inbox': f'{actor_url}/inbox',
            'outbox': f'{actor_url}/outbox',
            'followers': f'{actor_url}/followers',
            'publicKey': {
                'id': f'{actor_url}#main-key',
                'owner': actor_url,
                'publicKeyPem': '-----BEGIN PUBLIC KEY-----\nnot-a-real-key\n-----END PUBLIC KEY-----\n',
            },
        }

        http_mock.get(f'https://{domain}/.well-known/webfinger').mock(
            return_value=httpx.Response(200, json={
                'subject': f'acct:{name}@{domain}',
                'links': [{'rel': 'self',
                           'type': 'application/activity+json',
                           'href': actor_url}],
            }))
        http_mock.get(actor_url).mock(return_value=httpx.Response(200, json=actor))
        http_mock.post(f'{actor_url}/inbox').mock(return_value=httpx.Response(202))

        return actor

    return register
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./run_tests.sh tests/test_instance_util.py -q`
Expected: PASS, 6 tests.

If a test fails because an unexpected outbound request was made, read which URL `respx` reports — that is a real dependency of the code under test that the peer fixture does not yet serve, and adding it to `register` is the right fix. Do NOT switch to `assert_all_called=False` to make the failure go away; that setting is what makes the fixture honest.

- [ ] **Step 5: Confirm the module reaches 100%**

```bash
./run_tests.sh tests/ -q --ignore=tests/test_activitypub_util.py \
    --cov=app.instance.util --cov-branch --cov-report=term-missing
```

Expected: `app/instance/util.py` at 100%.

If a line resists, report which and why rather than contorting a test to reach it. An honest gap named here is a finding about the fixtures and is more useful than a padded number.

- [ ] **Step 6: Raise the floor**

Add to `coverage_floors.ini` under `[floors]`:

```ini
app/instance/util.py = 100
```

Then re-run the ratchet:

```bash
./run_tests.sh tests/ -q --ignore=tests/test_activitypub_util.py --cov=app --cov-report=json
podman-compose -f compose.test.yaml exec -T test-runner \
    python tests/check_coverage_floors.py coverage.json coverage_floors.ini
```

Expected: `All 2 module floors met.`

- [ ] **Step 7: Commit**

```bash
git add tests/conftest.py tests/test_instance_util.py coverage_floors.ini
git commit -m "test: add federation peer fixtures, cover app/instance/util.py"
```

---

## Task 3: S3, Redis and Celery fixtures

**Files:**
- Modify: `tests/conftest.py`
- Test: `tests/test_fixture_proofs.py`
- Modify: `tests/README.md`

**Interfaces:**
- Consumes: the dependencies from Task 1; the `app` fixture.
- Produces:
  - fixture `s3_bucket` — a moto-backed bucket, yielding the bucket name
  - fixture `redis_double` — a `fakeredis` instance patched over `app.utils.get_redis_connection`
  - Celery eager mode configured on `TestConfig`

Each fixture is proved by exercising REAL application code, not the fixture itself. A test that asserts a `fakeredis` instance stores what you put in it tests `fakeredis`, not PieFed.

- [ ] **Step 1: Write the failing test**

Create `tests/test_fixture_proofs.py`:

```python
"""Each fixture proved by driving real application code through it.

A test asserting that fakeredis stores what you put in it tests fakeredis. These
drive PieFed's own functions and assert on what those functions do.
"""

from app.utils import decode_captcha


def test_redis_double_backs_a_real_captcha_round_trip(app, redis_double):
    """decode_captcha reads back what generate stored, through the double."""
    uuid = 'a' * 24
    redis_double.set('captcha_' + uuid, 'WXYZ', ex=1800)

    assert decode_captcha(uuid, 'wxyz') is True, 'comparison is case-insensitive'
    assert decode_captcha(uuid, 'wxyz') is False, 'the code is consumed on use'


def test_redis_double_rejects_a_malformed_uuid(app, redis_double):
    """The regex guard rejects before any Redis call."""
    assert decode_captcha('not-a-uuid', 'wxyz') is False


def test_s3_bucket_fixture_provides_a_usable_bucket(app, s3_bucket):
    """The bucket exists and is empty, so a test can assert on what code puts in it."""
    import boto3

    client = boto3.client('s3')
    listing = client.list_objects_v2(Bucket=s3_bucket)

    assert listing['ResponseMetadata']['HTTPStatusCode'] == 200
    assert listing.get('KeyCount', 0) == 0


def test_celery_tasks_run_eagerly(app):
    """.delay() executes inline, so task code is reachable from tests at all."""
    assert celery.conf.task_always_eager is True
    assert celery.conf.task_eager_propagates is True, \
        'a failing task must raise in the test rather than being swallowed'
```

Two notes on writing this file:

- The `import boto3` shown inside `test_s3_bucket_fixture_provides_a_usable_bucket` violates the project rule. Put it at the top with `from app import celery` and the others — it is inline here only to keep the example readable.
- The assertions use `celery.conf` directly, NOT `app.config`. `app/__init__.py:175` does `celery.conf.update(app.config)`, so Flask config keys do reach Celery — but only through Celery's old-name compatibility mapping, and the historic name is `CELERY_ALWAYS_EAGER`, not `CELERY_TASK_ALWAYS_EAGER`. Setting the modern attribute directly (next step) and asserting on `celery.conf` avoids depending on that mapping at all.

- [ ] **Step 2: Run test to verify it fails**

Run: `./run_tests.sh tests/test_fixture_proofs.py -q`
Expected: FAIL — `fixture 'redis_double' not found`

- [ ] **Step 3: Add the fixtures**

In `tests/conftest.py`, with `import fakeredis`, `import boto3` and `from moto import mock_aws` at the top of the file:

```python
@pytest.fixture
def redis_double(monkeypatch):
    """Patch get_redis_connection so app code reaches a fakeredis instance.

    Patched at app.utils.get_redis_connection because callers invoke it per use
    (see decode_captcha) rather than holding a module-level client. The rate
    limiter and Celery app are built from Config at import time and are NOT
    covered by this fixture.
    """
    server = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr('app.utils.get_redis_connection', lambda *args, **kwargs: server)
    return server


@pytest.fixture
def s3_bucket():
    """A moto-backed S3 bucket, yielding its name."""
    with mock_aws():
        client = boto3.client('s3', region_name='us-east-1')
        client.create_bucket(Bucket='pyfedi-test')
        yield 'pyfedi-test'
```

And for Celery, set the modern attributes directly on the shared app inside the
session-scoped `app` fixture in `tests/conftest.py`, after `create_app()` returns:

```python
    celery.conf.task_always_eager = True
    celery.conf.task_eager_propagates = True
```

with `from app import celery` at the top of the file.

Set on `celery.conf` rather than as `TestConfig` attributes because
`app/__init__.py:175` does `celery.conf.update(app.config)`, which only reaches
Celery through its old-name compatibility mapping — and the historic name is
`CELERY_ALWAYS_EAGER`, not the `CELERY_TASK_*` form. Setting the modern attribute
sidesteps that entirely.

`task_eager_propagates` matters as much as the eager flag: without it a task that
raises is swallowed and the test passes while the task failed.

- [ ] **Step 4: Run test to verify it passes**

Run: `./run_tests.sh tests/test_fixture_proofs.py -q`
Expected: PASS, 4 tests.

If the Celery assertions still fail, read how `app/__init__.py` configures Celery and use what it actually reads, reporting the correction rather than silently diverging.

- [ ] **Step 5: Document the fixtures and the ratchet**

Add to `tests/README.md`:

```markdown
## The coverage ratchet

Per-module floors live in `coverage_floors.ini`. Floors only ever RISE — raising
one is a sub-project's deliverable; lowering one to make a run pass defeats the
ratchet. A module with no entry is ignored, so unfinished modules block nobody.

    ./run_tests.sh tests/ -q --ignore=tests/test_activitypub_util.py --cov=app --cov-report=json
    podman-compose -f compose.test.yaml exec -T test-runner \
        python tests/check_coverage_floors.py coverage.json coverage_floors.ini

coverage.py's own `fail_under` is a single global number, which is why the
per-module check is a script.

## Fixtures for external services

- `http_mock` — respx router over outbound httpx. `assert_all_called=True`, so a
  registered-but-unused route fails the test.
- `federation_peer(handle)` — webfinger + actor + inbox responses for a remote
  handle. The domain must NOT end in `.local`: `get_request()` rejects those via
  `is_invalid_get_request_uri()` before respx sees them.
- `s3_bucket` — a moto-backed bucket, yields the bucket name.
- `redis_double` — fakeredis patched over `app.utils.get_redis_connection`. Note
  the rate limiter and Celery app are built from `Config` at import time and are
  not affected by it.
- Celery runs eagerly under test, with `eager_propagates` so a failing task raises
  rather than being swallowed.

Prove a fixture by driving real application code through it. A test asserting that
fakeredis stores what you put in it tests fakeredis, not PieFed.
```

- [ ] **Step 6: Run the full suite and the ratchet**

```bash
./run_tests.sh tests/ -q --ignore=tests/test_activitypub_util.py --cov=app --cov-report=json
podman-compose -f compose.test.yaml exec -T test-runner \
    python tests/check_coverage_floors.py coverage.json coverage_floors.ini
```

Expected: 0 failed, and `All 2 module floors met.`

- [ ] **Step 7: Commit**

```bash
git add tests/conftest.py tests/test_fixture_proofs.py tests/README.md
git commit -m "test: add S3, Redis and eager Celery fixtures"
```

---

## Self-Review Notes

Spec coverage:

| Spec section | Task |
|---|---|
| `moto`, `respx`, `fakeredis`, Celery eager | Task 1 Step 5, Task 3 |
| `respx` chosen over `responses` because the codebase uses httpx | Task 1 Step 5, with the reason |
| Shared fixtures in `tests/conftest.py` | Tasks 2 and 3 |
| `federation_peer` with webfinger/actor/outbox | Task 2 Step 3 |
| `.coveragerc` with exclusion policy, no `TYPE_CHECKING` | Task 1 Step 6 |
| Per-module floors, only rising | Task 1 (checker + seed), Task 2 Step 6 (first raise) |
| Proof: `app/instance/util.py` to 100% | Task 2 Steps 1-6 |
| Quality bar (behaviour not mocks, justified pragmas) | Global Constraints, restated in Task 3's preamble |
| Both project rules | Global Constraints; called out explicitly at Task 3 Step 1 |

Two spec gaps I resolved while writing this, rather than leaving for the implementer:

1. **The spec assumed per-module floors were configuration.** coverage.py's `fail_under` is global only, so Task 1 implements a checker. `violations()` is pure so it is testable without running coverage.
2. **The spec did not mention `signed_activity`.** It listed that fixture, but nothing in this sub-project's proof needs it, and a fixture with no consumer cannot be shown to work. Deferred to the first sub-project that exercises inbox handling, which will define it against a real need.

Symbols are consistent throughout: `read_floors`, `violations`, `http_mock`, `federation_peer`, `s3_bucket`, `redis_double`, `coverage_floors.ini`, `tests/check_coverage_floors.py`.

Known risks carried into implementation:

- **Celery configuration** was initially written as `TestConfig` attributes. Checking `app/__init__.py:175` showed it does `celery.conf.update(app.config)`, which reaches Celery only via its old-name compatibility mapping — and the historic name is `CELERY_ALWAYS_EAGER`, not the `CELERY_TASK_*` form I first wrote. Task 3 now sets the modern attributes on `celery.conf` directly, which depends on no mapping. Corrected in the plan rather than left as a risk.
- **The `federation_peer` payload** is modelled on `docs/activitypub_examples/users.md` but is not a captured real response. If a test passes here while production fails against a real Mastodon actor, the payload is the first place to look — and the fix is to capture a real one, not to loosen the code.
- **`app/instance/util.py`'s `except Exception:` arm** re-raises after rollback. Task 2's third test induces it by passing a non-existent user id, which fails at `following_user.encode_jwt_token()`. If that turns out to raise before the `try:` block instead, the test needs a different trigger — report which line actually raises rather than weakening the assertion.

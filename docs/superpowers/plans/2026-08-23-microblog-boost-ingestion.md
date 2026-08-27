# Microblog Boost Ingestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make PieFed ingest boosts (`Announce`) of microblog posts from Mastodon-style accounts that local users follow, record them, remove them on un-boost, and surface them in the feed — with real automated tests, not manual verification.

**Architecture:** `process_microblog_announce()` in `app/activitypub/util.py` is currently an empty stub that silently drops every such activity. It gains a trust gate that runs before any network I/O, a local-post short circuit, a top-level-only guard, and delegation to the existing `create_resolved_object()` for creation. Boosts are stored in the already-present but entirely unused `PostBoost` model and `Post.post_boosts` JSON cache. Inbox logic that currently lives inline in `app/activitypub/routes.py` is extracted into `util.py` functions so it can be tested without Redis or signature plumbing, following the existing `undo_vote()` precedent.

**Tech Stack:** Python 3, Flask 3.1.3, Flask-SQLAlchemy 3.1.1, SQLAlchemy 2.0, Celery, PostgreSQL, pytest/unittest, httpx.

**Spec:** `docs/superpowers/specs/2026-08-23-microblog-boost-ingestion-design.md`

## Global Constraints

- The trust gate MUST run before any outbound HTTP request. An unauthenticated remote party must never be able to make PieFed fetch a URL of their choosing. This is asserted by a test, not just by reading the code.
- Never read the acting actor from an inner object. Only `request_json['actor']` is HTTP-signature-verified. See the comment at `app/activitypub/routes.py:819`.
- Do not reimplement the `attributedTo` / domain-match impersonation check. `create_resolved_object()` already performs it at `app/activitypub/util.py:3680-3702`.
- No database migration. `PostBoost` and `Post.post_boosts` already exist via `migrations/versions/c831b9c7eee9_post_boost.py`.
- Every exit path calls `log_incoming_ap()` with a distinct reason string. A bare `return None` is a plan violation.
- At most one fetch **of the boosted object** per activity, and never before the trust gate. This is not zero I/O after the gate: `create_resolved_object` calls `find_actor_or_create`, which may fetch the `attributedTo` actor's profile. That is bounded and acceptable — it happens behind the gate, and `util.py:3797` rejects any `attributedTo` on a different host than the object, so a sender cannot use it to reach a host of their choosing.
- Boosts of replies are out of scope. A boosted object with a truthy `inReplyTo` is logged and ignored.
- New logic goes in `app/activitypub/util.py`, not inline in `routes.py`. Branches in `routes.py` stay thin enough to read at a glance, mirroring how `undo_vote()` is called at `routes.py:1723`.
- No new runtime dependencies. Remote fetches are stubbed by monkeypatching `remote_object_to_json`, not by adding an HTTP mocking library.
- Rebase onto current `origin/main` before starting. The local branch is 111 commits ahead and 109 behind, and these functions have moved before.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `compose.test.yaml` | Ephemeral Postgres and Redis for tests, via podman-compose | Create |
| `.env.test` | Test environment: test database and Redis URLs | Create |
| `run_tests.sh` | Bring up containers, migrate, run pytest | Create |
| `tests/conftest.py` | App, database, and truncation fixtures | Create |
| `tests/factories.py` | Builders for Instance, User, Community, Post, UserFollower | Create |
| `tests/README.md` | How to set up and run the database-backed tests | Create |
| `app/utils.py` | `boost_cache_entries()` pure shaping; feed `EXISTS` clause | Modify |
| `app/activitypub/util.py` | `announce_target_uri()`, `is_top_level()`, `announcer_is_followed()`, `record_boost()`, `remove_boost()`, `undo_boost()`, `process_announce_of_uri()`, real `process_microblog_announce()` | Modify |
| `app/models.py` | `Post.update_boost_cache()` | Modify |
| `app/activitypub/routes.py` | Delegate Announce dispatch and Undo/Announce to `util.py` | Modify |
| `tests/test_microblog_announce.py` | Pure helper tests | Create |
| `tests/test_boost_cache_entries.py` | Cache shaping tests | Create |
| `tests/test_boost_storage.py` | Database-backed boost storage tests | Create |
| `tests/test_process_microblog_announce.py` | Database-backed ingestion tests | Create |
| `tests/test_announce_dispatch.py` | Dispatch routing and undo tests | Create |
| `tests/test_feed_boost_visibility.py` | Feed SQL clause tests | Create |
| `FEDERATION.md` | Document boost ingestion | Modify |

`tests/test_activitypub_util.py` is **not** a model to copy — it requires live network access and a hardcoded username. Task 0 exists so that nothing else in this plan has to follow it.

---

## Task 0: Test fixtures

**Files:**
- Create: `compose.test.yaml`
- Create: `.env.test`
- Create: `run_tests.sh`
- Create: `tests/conftest.py`
- Create: `tests/factories.py`
- Create: `tests/README.md`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `./run_tests.sh [pytest args]` — the entry point for the whole suite
  - pytest fixture `app` — session-scoped Flask app bound to the test database
  - pytest fixture `db_session` — function-scoped, truncates all tables after each test
  - `tests/factories.py`: `make_instance(domain, software='mastodon') -> Instance`, `make_user(instance, name, local=False) -> User`, `make_community(name='microblogs') -> Community`, `make_post(community, user, ap_id) -> Post`, `make_follow(local_user, remote_user) -> UserFollower`

Three constraints drive this design:

1. Schema comes from `flask db upgrade`, never `db.create_all()`. SQLAlchemy-Searchable triggers and several indexes are created by migrations and would be absent otherwise, so `create_all()` would silently test a different schema than production runs.
2. `app/__init__.py:61-62` builds `limiter` and `celery` from `Config` at **import time**. Redis URLs must therefore be in the environment before `app` is imported — a `TestConfig` attribute is too late. Hence `.env.test` plus a runner script rather than configuration in Python alone.
3. `config.py:11` calls `load_dotenv('.env')`, which does **not** override already-exported variables. Exporting `.env.test` before pytest therefore coexists safely with the developer's dev `.env`.

Containers use tmpfs and non-default ports (5433, 6380) so they cannot collide with, or outlive, the dev stack in `compose.dev.yaml`.

- [ ] **Step 1: Write the smoke test that proves the fixtures work**

Create `tests/test_fixtures_smoke.py`:

```python
def test_database_is_reachable_and_empty(db_session):
    """The fixture gives each test a clean database"""
    from app.models import User
    assert User.query.count() == 0


def test_factories_build_a_followed_remote_user(db_session):
    """A remote user followed by a local user can be constructed"""
    from tests.factories import make_instance, make_user, make_follow
    from app.models import UserFollower

    remote_instance = make_instance('m.example')
    alice = make_user(remote_instance, 'alice')
    local = make_user(None, 'localuser', local=True)
    make_follow(local, alice)

    assert UserFollower.query.filter_by(remote_user_id=alice.id, is_inward=False).count() == 1


def test_truncation_between_tests(db_session):
    """Rows created by the previous test are gone"""
    from app.models import User
    assert User.query.count() == 0
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest tests/test_fixtures_smoke.py -v`
Expected: FAIL — `fixture 'db_session' not found`

- [ ] **Step 3: Write the compose file**

Create `compose.test.yaml`:

```yaml
# Ephemeral Postgres and Redis for the test suite.
#
# Data lives in tmpfs, so nothing survives `podman-compose down` and no state can
# leak between runs. Ports differ from compose.dev.yaml (5432/6379) so this stack
# can run alongside the dev stack without colliding.
services:

  test-db:
    image: docker.io/library/postgres:17
    environment:
      POSTGRES_USER: pyfedi
      POSTGRES_PASSWORD: pyfedi
      POSTGRES_DB: pyfedi_test
    command:
      - postgres
      - -c
      - fsync=off
      - -c
      - full_page_writes=off
      - -c
      - synchronous_commit=off
    tmpfs:
      - /var/lib/postgresql/data
    ports:
      - "127.0.0.1:5433:5432"
    networks:
      - pf_test_network

  test-redis:
    image: docker.io/library/redis:6.2
    ports:
      - "127.0.0.1:6380:6379"
    networks:
      - pf_test_network

  # Tests run in here, not on the host: this repo has no host Python environment
  # (no venv, no .env, and system python3 has no Flask). The Dockerfile's `builder`
  # target carries /venv with every requirement, including pytest. It is preferred
  # over `runtime` because it runs as root — avoiding __pycache__ permission
  # failures on the bind mount — and skips the tesseract layer tests do not need.
  test-runner:
    build:
      context: .
      target: builder
    depends_on:
      - test-db
      - test-redis
    env_file:
      - ./.env.test
    environment:
      PATH: /venv/bin:/usr/local/bin:/usr/bin:/bin
      PYTHONUNBUFFERED: "1"
    working_dir: /app
    volumes:
      - ./:/app:z
    command: ["sleep", "infinity"]
    networks:
      - pf_test_network

networks:
  pf_test_network:
    name: pf_test_network
    external: false
```

`fsync=off` and friends are safe here precisely because the data is disposable, and they make the truncation-per-test fixture noticeably cheaper.

`test-runner` idles on `sleep infinity` so `run_tests.sh` can `exec` into it repeatedly without paying container startup on every run.

Because `test-runner` reaches the database over the compose network rather than the published port, `.env.test` must name the **service hostnames**, not `127.0.0.1`. That is why the URLs below use `test-db` and `test-redis`.

- [ ] **Step 4: Write the test environment file**

Create `.env.test`:

```bash
# Environment for the test suite. Contains no secrets: everything here points at
# the disposable containers in compose.test.yaml.
#
# These are exported before pytest starts, because app/__init__.py builds the rate
# limiter and Celery app from Config at import time. config.py's load_dotenv() does
# not override already-exported variables, so this coexists with a dev .env.
SERVER_NAME=test.piefed.local
SECRET_KEY=test-secret-not-used-outside-tests
DATABASE_URL=postgresql+psycopg2://pyfedi:pyfedi@test-db:5432/pyfedi_test
TEST_DATABASE_URL=postgresql+psycopg2://pyfedi:pyfedi@test-db:5432/pyfedi_test
CACHE_TYPE=NullCache
CACHE_REDIS_URL=redis://test-redis:6379/1
CELERY_BROKER_URL=redis://test-redis:6379/0
RESULT_BACKEND=redis://test-redis:6379/0
```

Hostnames are the compose service names because tests execute inside `test-runner`, on the compose network. The published ports 5433 and 6380 exist for inspecting the databases from the host (psql, redis-cli), not for the test run itself.

- [ ] **Step 5: Write the runner**

Create `run_tests.sh`, and `chmod +x` it:

```bash
#!/usr/bin/env bash
# Run the test suite against disposable containers.
#
#   ./run_tests.sh                      # everything
#   ./run_tests.sh tests/test_foo.py -v # passed straight through to pytest
#   ./run_tests.sh --down               # stop and remove the containers
set -euo pipefail

cd "$(dirname "$0")"

COMPOSE="podman-compose -f compose.test.yaml"

if [ "${1:-}" = "--down" ]; then
    $COMPOSE down
    exit 0
fi

$COMPOSE up -d

echo "Waiting for Postgres..."
for _ in $(seq 1 60); do
    if $COMPOSE exec -T test-db pg_isready -U pyfedi -d pyfedi_test >/dev/null 2>&1; then
        break
    fi
    sleep 1
done
$COMPOSE exec -T test-db pg_isready -U pyfedi -d pyfedi_test >/dev/null

# Tests run inside test-runner: there is no host Python environment.
$COMPOSE exec -T test-runner flask db upgrade
exec $COMPOSE exec -T test-runner pytest "$@"
```

`flask db upgrade` is idempotent, so running the suite repeatedly is cheap after the first migrate. The containers are left running on purpose; `--down` disposes of them.

The environment is not exported by this script — `test-runner` gets it from `env_file` in the compose file, which reaches the process before Python starts. That is what matters, because `app/__init__.py` builds the rate limiter and Celery app from `Config` at import time.

The first run builds the `builder` image, which takes a few minutes. Subsequent runs reuse it.

- [ ] **Step 6: Write conftest.py**

Create `tests/conftest.py`:

```python
import os

import pytest

from config import Config

TEST_DATABASE_URL = os.environ.get('TEST_DATABASE_URL')


class TestConfig(Config):
    """Test configuration. Inherits the real Config so tests exercise real settings."""
    TESTING = True
    WTF_CSRF_ENABLED = False
    MAIL_SUPPRESS_SEND = True
    SQLALCHEMY_DATABASE_URI = TEST_DATABASE_URL
    CACHE_TYPE = 'NullCache'
    SERVER_NAME = 'test.piefed.local'


@pytest.fixture(scope='session')
def app():
    """A Flask app bound to the test database.

    Skips rather than fails when TEST_DATABASE_URL is unset, so a bare checkout
    can still run the pure-function tests. Use ./run_tests.sh for the full suite.
    """
    if not TEST_DATABASE_URL:
        pytest.skip('TEST_DATABASE_URL is not set; run ./run_tests.sh instead')

    # The db_session fixture truncates every table. Refuse to point that at a
    # database whose name does not mark it as disposable.
    if 'test' not in TEST_DATABASE_URL.rsplit('/', 1)[-1]:
        pytest.fail(f'TEST_DATABASE_URL must name a test database, got {TEST_DATABASE_URL!r}')

    from app import create_app
    application = create_app(TestConfig)
    with application.app_context():
        yield application


@pytest.fixture
def db_session(app):
    """Give each test a clean database.

    Truncates rather than rolling back a nested transaction: the code under test
    calls db.session.commit() in several places, which a rollback-based fixture
    would have to fight.
    """
    from app import db
    from sqlalchemy import text

    yield db.session

    db.session.rollback()
    table_names = ', '.join(f'"{table.name}"' for table in reversed(db.metadata.sorted_tables))
    db.session.execute(text(f'TRUNCATE TABLE {table_names} RESTART IDENTITY CASCADE'))
    db.session.commit()
```

- [ ] **Step 7: Write factories.py**

Create `tests/factories.py`:

```python
"""Builders for database rows used by the boost ingestion tests.

Each builder sets the minimum needed for a valid row and commits, so the object
has an id. If Postgres rejects an insert for a missing NOT NULL column, add that
column here rather than in the test.
"""

from app import db
from app.models import Community, Instance, Post, User, UserFollower, utcnow


def make_instance(domain: str, software: str = 'mastodon') -> Instance:
    instance = Instance(domain=domain, software=software, online=True)
    db.session.add(instance)
    db.session.commit()
    return instance


def make_user(instance, name: str, local: bool = False) -> User:
    """A local user has ap_id None; a remote user has a full actor URI."""
    user = User(
        user_name=name,
        email=f'{name}@example.com',
        instance_id=instance.id if instance else 1,
        verified=True,
        banned=False,
        ap_id=None if local else f'{name}@{instance.domain}',
        ap_profile_id=None if local else f'https://{instance.domain}/users/{name}',
        ap_public_url=None if local else f'https://{instance.domain}/users/{name}',
        ap_inbox_url=None if local else f'https://{instance.domain}/users/{name}/inbox',
    )
    db.session.add(user)
    db.session.commit()
    return user


def make_community(name: str = 'microblogs') -> Community:
    community = Community(
        name=name,
        title=name,
        instance_id=1,
        user_id=1,
        ap_profile_id=f'https://test.piefed.local/c/{name}',
        ap_public_url=f'https://test.piefed.local/c/{name}',
        ap_followers_url=f'https://test.piefed.local/c/{name}/followers',
        ap_domain='test.piefed.local',
        subscriptions_count=0,
        local_only=False,
        nsfw=False,
    )
    db.session.add(community)
    db.session.commit()
    return community


def make_post(community, user, ap_id: str, title: str = 'a post') -> Post:
    post = Post(
        community_id=community.id,
        user_id=user.id,
        title=title,
        ap_id=ap_id,
        instance_id=user.instance_id,
        posted_at=utcnow(),
        last_active=utcnow(),
        from_bot=False,
        nsfw=False,
        deleted=False,
    )
    db.session.add(post)
    db.session.commit()
    return post


def make_follow(local_user, remote_user) -> UserFollower:
    """local_user follows remote_user. is_inward False means outward: we follow them."""
    follow = UserFollower(
        local_user_id=local_user.id,
        remote_user_id=remote_user.id,
        is_accepted=True,
        is_inward=False,
    )
    db.session.add(follow)
    db.session.commit()
    return follow
```

- [ ] **Step 8: Write the setup documentation**

Create `tests/README.md`:

```markdown
# Tests

Run everything:

    ./run_tests.sh

Run a subset — arguments pass straight through to pytest:

    ./run_tests.sh tests/test_boost_storage.py -v

Dispose of the containers when you are done:

    ./run_tests.sh --down

## How it works

`run_tests.sh` starts the Postgres and Redis in `compose.test.yaml` with
podman-compose, waits for Postgres to accept connections, applies migrations, and
runs pytest. Container data lives in tmpfs on ports 5433 and 6380, so the test
stack neither collides with nor outlives the dev stack in `compose.dev.yaml`.

Environment comes from `.env.test`, exported before pytest starts. That matters
because `app/__init__.py` builds the rate limiter and Celery app from `Config` at
import time, so a `TestConfig` attribute would be set too late. `config.py` calls
`load_dotenv('.env')`, which does not override already-exported variables, so this
coexists with your dev `.env`.

The schema comes from `flask db upgrade`, not `db.create_all()`, because
SQLAlchemy-Searchable triggers and several indexes are created by migrations and
would otherwise be missing — tests would exercise a different schema than
production.

## Running pytest directly

There is no host Python environment for this repo, so pytest always runs inside
the `test-runner` container:

    podman-compose -f compose.test.yaml exec -T test-runner pytest tests/test_microblog_announce.py -v

Pure-function tests need no database, but they do need the environment — importing
`app.utils` pulls in `config.py`, which reads `SERVER_NAME` at import time and
raises without it. `test-runner` gets that environment from `.env.test`.

Database-backed tests skip, rather than fail, when `TEST_DATABASE_URL` is unset.

**Warning:** the `db_session` fixture truncates every table after each test.
`conftest.py` refuses to run if `TEST_DATABASE_URL` does not name a database with
"test" in it, but do not defeat that guard.

`tests/test_activitypub_util.py` predates this setup. It needs live network access
and a hardcoded username, and is excluded from the standard run.
```

- [ ] **Step 9: Bring up the stack and run the smoke test**

```bash
chmod +x run_tests.sh
./run_tests.sh tests/test_fixtures_smoke.py -v
```

Expected: PASS, 3 tests.

Two failure modes to expect and work through here, rather than treating them as plan defects:

- An insert rejected for a missing NOT NULL column — add that column to the relevant factory in `tests/factories.py` and re-run.
- `create_app()` failing on a service this plan did not anticipate. Read the traceback: if it needs another environment variable, add it to `.env.test`; if it needs another container, add it to `compose.test.yaml`. If it needs something that cannot be containerised, stop and report rather than stubbing out application startup.

- [ ] **Step 10: Verify the skip path works**

```bash
podman-compose -f compose.test.yaml exec -T -e TEST_DATABASE_URL= test-runner pytest tests/test_fixtures_smoke.py -v
```

Expected: 3 skipped, 0 failed. This is what keeps the pure-function tests runnable without a database.

- [ ] **Step 11: Verify teardown leaves nothing behind**

```bash
./run_tests.sh --down
podman ps -a --filter name=test-db --filter name=test-redis
```

Expected: no containers listed. Because the data is on tmpfs, nothing persists.

- [ ] **Step 12: Commit**

```bash
git add compose.test.yaml .env.test run_tests.sh tests/conftest.py tests/factories.py tests/README.md tests/test_fixtures_smoke.py
git commit -m "test: add containerised database fixtures and factories"
```

---

## Task 1: Pure helpers for reading an Announce

**Files:**
- Modify: `app/activitypub/util.py` (add two module-level functions immediately above `def process_microblog_announce`, currently line 3418)
- Test: `tests/test_microblog_announce.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `announce_target_uri(activity: dict) -> Union[str, None]`
  - `is_top_level(post_data: dict) -> bool`

- [ ] **Step 1: Write the failing test**

Create `tests/test_microblog_announce.py`:

```python
import unittest

from app.activitypub.util import announce_target_uri, is_top_level


class TestAnnounceTargetUri(unittest.TestCase):

    def test_string_object(self):
        """Mastodon sends the boosted object as a bare URI string"""
        self.assertEqual(
            announce_target_uri({'type': 'Announce', 'object': 'https://m.example/notes/1'}),
            'https://m.example/notes/1')

    def test_dict_object_uses_id(self):
        """Some platforms embed the object; its id is the URI"""
        self.assertEqual(
            announce_target_uri({'type': 'Announce', 'object': {'id': 'https://m.example/notes/2'}}),
            'https://m.example/notes/2')

    def test_missing_object(self):
        """An Announce with no object yields None rather than raising"""
        self.assertIsNone(announce_target_uri({'type': 'Announce'}))

    def test_empty_string_object(self):
        """An empty string is not a usable URI"""
        self.assertIsNone(announce_target_uri({'type': 'Announce', 'object': ''}))

    def test_dict_object_without_id(self):
        """An embedded object with no id yields None"""
        self.assertIsNone(announce_target_uri({'type': 'Announce', 'object': {'type': 'Note'}}))

    def test_dict_object_with_non_string_id(self):
        """A non-string id is rejected rather than returned"""
        self.assertIsNone(announce_target_uri({'type': 'Announce', 'object': {'id': 12345}}))

    def test_non_dict_activity(self):
        """A malformed activity yields None rather than raising"""
        self.assertIsNone(announce_target_uri('not a dict'))


class TestIsTopLevel(unittest.TestCase):

    def test_no_in_reply_to_key(self):
        """An object with no inReplyTo is top-level"""
        self.assertTrue(is_top_level({'type': 'Note', 'content': 'hello'}))

    def test_in_reply_to_none(self):
        """inReplyTo explicitly null is top-level"""
        self.assertTrue(is_top_level({'type': 'Note', 'inReplyTo': None}))

    def test_in_reply_to_empty_string(self):
        """inReplyTo as an empty string is top-level"""
        self.assertTrue(is_top_level({'type': 'Note', 'inReplyTo': ''}))

    def test_in_reply_to_set(self):
        """An object with a real inReplyTo is a reply, not top-level"""
        self.assertFalse(is_top_level({'type': 'Note', 'inReplyTo': 'https://m.example/notes/1'}))

    def test_non_dict(self):
        """Malformed data is not top-level"""
        self.assertFalse(is_top_level(None))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_microblog_announce.py -v`
Expected: FAIL at import — `ImportError: cannot import name 'announce_target_uri' from 'app.activitypub.util'`

- [ ] **Step 3: Write minimal implementation**

In `app/activitypub/util.py`, directly above `def process_microblog_announce`:

```python
def announce_target_uri(activity: dict) -> Union[str, None]:
    """Return the URI of the object an Announce (or Undo/Announce) refers to.

    Mastodon sends the object as a bare URI string. Some platforms embed the
    object, in which case its 'id' is the URI. Returns None if neither is usable.
    """
    if not isinstance(activity, dict):
        return None
    obj = activity.get('object')
    if isinstance(obj, str):
        return obj if obj else None
    if isinstance(obj, dict):
        obj_id = obj.get('id')
        return obj_id if isinstance(obj_id, str) and obj_id else None
    return None


def is_top_level(post_data: dict) -> bool:
    """True if a fetched object is a top-level post rather than a reply."""
    if not isinstance(post_data, dict):
        return False
    return not post_data.get('inReplyTo')
```

`Union` is already imported at `app/activitypub/util.py:10`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_microblog_announce.py -v`
Expected: PASS, 12 tests

- [ ] **Step 5: Commit**

```bash
git add tests/test_microblog_announce.py app/activitypub/util.py
git commit -m "feat: add pure helpers for reading Announce activities"
```

---

## Task 2: Boost cache shaping

**Files:**
- Modify: `app/utils.py` (add a module-level function near `microblog_content_to_title`, around line 990)
- Test: `tests/test_boost_cache_entries.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `boost_cache_entries(rows) -> list` — takes an iterable of `(user_id, ap_id, display_name, created_at)` tuples and returns the list of dicts stored in `Post.post_boosts`.

Separated from the database query so the JSON shape, which is where format bugs live, is testable without a database.

- [ ] **Step 1: Write the failing test**

Create `tests/test_boost_cache_entries.py`:

```python
import unittest
from datetime import datetime

from app.utils import boost_cache_entries


class TestBoostCacheEntries(unittest.TestCase):

    def test_single_remote_booster(self):
        """A remote user's ap_id and ISO timestamp are carried into the cache"""
        rows = [(7, 'alice@m.example', 'alice', datetime(2026, 8, 23, 12, 30, 0))]
        self.assertEqual(boost_cache_entries(rows), [{
            'user_id': 7,
            'ap_id': 'alice@m.example',
            'display_name': 'alice',
            'created_at': '2026-08-23T12:30:00',
        }])

    def test_local_booster_has_no_ap_id(self):
        """A local user has ap_id None; the cache stores an empty string"""
        rows = [(3, None, 'bob', datetime(2026, 8, 23, 9, 0, 0))]
        self.assertEqual(boost_cache_entries(rows)[0]['ap_id'], '')

    def test_missing_timestamp(self):
        """A null created_at becomes an empty string rather than raising"""
        rows = [(3, None, 'bob', None)]
        self.assertEqual(boost_cache_entries(rows)[0]['created_at'], '')

    def test_order_preserved(self):
        """Input order is preserved; the caller decides the ordering"""
        rows = [
            (1, None, 'first', datetime(2026, 8, 23, 10, 0, 0)),
            (2, None, 'second', datetime(2026, 8, 23, 11, 0, 0)),
        ]
        names = [entry['display_name'] for entry in boost_cache_entries(rows)]
        self.assertEqual(names, ['first', 'second'])

    def test_empty(self):
        """No boosts yields an empty list, not None"""
        self.assertEqual(boost_cache_entries([]), [])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_boost_cache_entries.py -v`
Expected: FAIL at import — `ImportError: cannot import name 'boost_cache_entries' from 'app.utils'`

- [ ] **Step 3: Write minimal implementation**

In `app/utils.py`:

```python
def boost_cache_entries(rows) -> list:
    """Shape (user_id, ap_id, display_name, created_at) rows into the post_boosts cache.

    Kept separate from the query so the stored JSON shape can be tested without
    a database. Input order is preserved; the caller chooses the ordering.
    """
    return [
        {
            'user_id': user_id,
            'ap_id': ap_id if ap_id else '',
            'display_name': display_name,
            'created_at': created_at.isoformat() if created_at else '',
        }
        for user_id, ap_id, display_name, created_at in rows
    ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_boost_cache_entries.py -v`
Expected: PASS, 5 tests

- [ ] **Step 5: Commit**

```bash
git add tests/test_boost_cache_entries.py app/utils.py
git commit -m "feat: add boost cache entry shaping"
```

---

## Task 3: Boost storage

**Files:**
- Modify: `app/models.py` — add `Post.update_boost_cache()` immediately after `Post.update_reaction_cache()`, which ends at line 2713
- Modify: `app/activitypub/util.py` — add `record_boost()` and `remove_boost()` above `announce_target_uri`; extend the model import at lines 27-30
- Test: `tests/test_boost_storage.py`

**Interfaces:**
- Consumes: `boost_cache_entries()` (Task 2); fixtures and factories (Task 0).
- Produces:
  - `Post.update_boost_cache() -> None`
  - `record_boost(post: Post, user: User) -> None`
  - `remove_boost(post: Post, user: User) -> None`

Idempotency uses query-then-insert rather than a unique constraint. A constraint would need a migration, and the duplicate suppression at `app/activitypub/routes.py:647` is a Redis key with a 90-second expiry — replay protection, not durable dedup. Redelivery after that window must not double-count.

- [ ] **Step 1: Write the failing test**

Create `tests/test_boost_storage.py`:

```python
import pytest

from tests.factories import make_community, make_follow, make_instance, make_post, make_user


@pytest.fixture
def boosted(db_session):
    """A post, and a remote user who can boost it."""
    from app.models import User
    instance = make_instance('m.example')
    author = make_user(instance, 'author')
    booster = make_user(instance, 'booster')
    community = make_community()
    post = make_post(community, author, 'https://m.example/notes/1')
    return post, booster


def test_record_boost_creates_one_row(db_session, boosted):
    """Recording a boost writes a PostBoost row"""
    from app.activitypub.util import record_boost
    from app.models import PostBoost
    post, booster = boosted

    record_boost(post, booster)

    assert PostBoost.query.filter_by(post_id=post.id, user_id=booster.id).count() == 1


def test_record_boost_is_idempotent(db_session, boosted):
    """Redelivery of the same Announce does not double-count"""
    from app.activitypub.util import record_boost
    from app.models import PostBoost
    post, booster = boosted

    record_boost(post, booster)
    record_boost(post, booster)

    assert PostBoost.query.filter_by(post_id=post.id).count() == 1


def test_record_boost_populates_cache(db_session, boosted):
    """The post_boosts cache is refreshed with the booster's details"""
    from app.activitypub.util import record_boost
    post, booster = boosted

    record_boost(post, booster)

    assert len(post.post_boosts) == 1
    assert post.post_boosts[0]['user_id'] == booster.id
    assert post.post_boosts[0]['display_name'] == 'booster'


def test_remove_boost_deletes_the_row(db_session, boosted):
    """Un-boosting removes the row and empties the cache"""
    from app.activitypub.util import record_boost, remove_boost
    from app.models import PostBoost
    post, booster = boosted

    record_boost(post, booster)
    remove_boost(post, booster)

    assert PostBoost.query.filter_by(post_id=post.id).count() == 0
    assert post.post_boosts == []


def test_remove_boost_when_absent_is_a_no_op(db_session, boosted):
    """A repeated Undo does not raise; remote instances re-send"""
    from app.activitypub.util import remove_boost
    post, booster = boosted

    remove_boost(post, booster)
    remove_boost(post, booster)


def test_two_boosters_both_recorded(db_session, boosted):
    """Idempotency is per user, not per post"""
    from app.activitypub.util import record_boost
    from app.models import PostBoost
    post, booster = boosted
    other = make_user(make_instance('other.example'), 'other')

    record_boost(post, booster)
    record_boost(post, other)

    assert PostBoost.query.filter_by(post_id=post.id).count() == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./run_tests.sh tests/test_boost_storage.py -v`
Expected: FAIL at import — `ImportError: cannot import name 'record_boost'`

- [ ] **Step 3: Add the cache method to Post**

In `app/models.py`, immediately after the end of `update_reaction_cache()` (line 2713):

```python
    def update_boost_cache(self):
        from app.utils import boost_cache_entries
        rows = db.session.query(PostBoost.user_id, User.ap_id, User.user_name, PostBoost.created_at). \
            join(User, User.id == PostBoost.user_id). \
            filter(PostBoost.post_id == self.id). \
            order_by(PostBoost.created_at.desc()).all()
        self.post_boosts = boost_cache_entries(rows)
```

`PostBoost` is defined later in the same module (line 4255) but resolves at call time. `update_reaction_cache` uses the same local-import-from-`app.utils` idiom.

- [ ] **Step 4: Add the import to util.py**

In `app/activitypub/util.py`, extend the `from app.models import ...` block at lines 27-30 with `UserFollower` and `PostBoost`. Neither is currently imported there.

- [ ] **Step 5: Add the write helpers to util.py**

```python
def record_boost(post: Post, user: User) -> None:
    """Record that `user` boosted `post`, idempotently, and refresh the cache.

    Idempotent by query-then-insert: the same Announce can be redelivered after
    the 90 second Redis duplicate window in routes.py has expired.
    """
    existing = db.session.query(PostBoost).filter_by(user_id=user.id, post_id=post.id).first()
    if existing:
        return
    db.session.add(PostBoost(user_id=user.id, post_id=post.id))
    db.session.commit()
    post.update_boost_cache()
    db.session.commit()


def remove_boost(post: Post, user: User) -> None:
    """Remove `user`'s boost of `post` and refresh the cache.

    A missing row is a successful no-op, not a failure — remote instances re-send.
    """
    existing = db.session.query(PostBoost).filter_by(user_id=user.id, post_id=post.id).first()
    if existing is None:
        return
    db.session.delete(existing)
    db.session.commit()
    post.update_boost_cache()
    db.session.commit()
```

- [ ] **Step 6: Run test to verify it passes**

Run: `./run_tests.sh tests/test_boost_storage.py -v`
Expected: PASS, 6 tests

- [ ] **Step 7: Commit**

```bash
git add app/models.py app/activitypub/util.py tests/test_boost_storage.py
git commit -m "feat: add boost recording and removal"
```

---

## Task 4: Implement process_microblog_announce

**Files:**
- Modify: `app/activitypub/util.py:3418-3435` — replace the stub body; add `announcer_is_followed()`
- Modify: `FEDERATION.md`
- Test: `tests/test_process_microblog_announce.py`

**Interfaces:**
- Consumes: `announce_target_uri()`, `is_top_level()` (Task 1); `record_boost()` (Task 3); existing `find_actor_or_create_cached()` (line 322), `remote_object_to_json()` (line 3621), `create_resolved_object()` (line 3678), `find_microblogging_community()` (line 4030), `log_incoming_ap()` (line 3957).
- Produces:
  - `announcer_is_followed(user_id: int) -> bool`
  - `process_microblog_announce(request_json, id, store_ap_json) -> Union[Post, None]` — signature unchanged, so the call site is unaffected.

- [ ] **Step 1: Write the failing test**

Create `tests/test_process_microblog_announce.py`:

```python
import pytest

from tests.factories import make_community, make_follow, make_instance, make_post, make_user


@pytest.fixture
def followed_booster(db_session):
    """A remote account that a local user follows."""
    instance = make_instance('m.example')
    booster = make_user(instance, 'booster')
    local = make_user(None, 'localuser', local=True)
    make_follow(local, booster)
    return booster


@pytest.fixture
def fetch_spy(monkeypatch):
    """Replace the remote fetch, and count how many times it is called."""
    calls = []

    def fake_fetch(uri):
        calls.append(uri)
        return fake_fetch.result

    fake_fetch.result = None
    monkeypatch.setattr('app.activitypub.util.remote_object_to_json', fake_fetch)
    return fake_fetch, calls


def announce(actor_uri, object_uri):
    return {
        'id': f'{actor_uri}/statuses/1/activity',
        'type': 'Announce',
        'actor': actor_uri,
        'object': object_uri,
    }


def test_unfollowed_actor_is_rejected_without_fetching(db_session, fetch_spy):
    """The trust gate runs before any network I/O"""
    from app.activitypub.util import process_microblog_announce
    _, calls = fetch_spy
    instance = make_instance('m.example')
    stranger = make_user(instance, 'stranger')

    result = process_microblog_announce(
        announce(stranger.ap_public_url, 'https://other.example/notes/9'), 'a1', False)

    assert result is None
    assert calls == [], 'no fetch may happen for an unfollowed actor'


def test_banned_actor_is_rejected_without_fetching(db_session, fetch_spy, followed_booster):
    """A banned actor is rejected even if followed"""
    from app import db
    from app.activitypub.util import process_microblog_announce
    _, calls = fetch_spy
    followed_booster.banned = True
    db.session.commit()

    result = process_microblog_announce(
        announce(followed_booster.ap_public_url, 'https://other.example/notes/9'), 'a2', False)

    assert result is None
    assert calls == []


def test_boosted_reply_is_ignored(db_session, fetch_spy, followed_booster):
    """A boosted reply is dropped; ancestor backfill is out of scope"""
    from app.activitypub.util import process_microblog_announce
    fake_fetch, calls = fetch_spy
    fake_fetch.result = {
        'id': 'https://other.example/notes/9',
        'type': 'Note',
        'inReplyTo': 'https://other.example/notes/8',
        'attributedTo': 'https://other.example/users/bob',
    }

    result = process_microblog_announce(
        announce(followed_booster.ap_public_url, 'https://other.example/notes/9'), 'a3', False)

    assert result is None
    assert len(calls) == 1, 'exactly one fetch per activity'


def test_existing_local_post_is_boosted_without_fetching(db_session, fetch_spy, followed_booster):
    """A boost of a post we already have short-circuits before the fetch"""
    from app.activitypub.util import process_microblog_announce
    from app.models import PostBoost
    _, calls = fetch_spy
    author = make_user(make_instance('other.example'), 'bob')
    post = make_post(make_community(), author, 'https://other.example/notes/7')

    result = process_microblog_announce(
        announce(followed_booster.ap_public_url, post.ap_id), 'a4', False)

    assert result is not None and result.id == post.id
    assert calls == [], 'a post we already have must not be fetched'
    assert PostBoost.query.filter_by(post_id=post.id, user_id=followed_booster.id).count() == 1


def test_redelivery_does_not_double_count(db_session, fetch_spy, followed_booster):
    """The same Announce arriving twice records one boost"""
    from app.activitypub.util import process_microblog_announce
    from app.models import PostBoost
    author = make_user(make_instance('other.example'), 'bob')
    post = make_post(make_community(), author, 'https://other.example/notes/7')
    activity = announce(followed_booster.ap_public_url, post.ap_id)

    process_microblog_announce(activity, 'a5', False)
    process_microblog_announce(activity, 'a5', False)

    assert PostBoost.query.filter_by(post_id=post.id).count() == 1


def test_announce_without_object_is_rejected(db_session, fetch_spy, followed_booster):
    """A malformed Announce is rejected without fetching"""
    from app.activitypub.util import process_microblog_announce
    _, calls = fetch_spy

    result = process_microblog_announce(
        {'id': 'a6', 'type': 'Announce', 'actor': followed_booster.ap_public_url}, 'a6', False)

    assert result is None
    assert calls == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./run_tests.sh tests/test_process_microblog_announce.py -v`
Expected: FAIL — every test returns `None` from the stub, so the assertions about `PostBoost` rows and the local short circuit fail.

- [ ] **Step 3: Add the trust gate helper**

In `app/activitypub/util.py`, above `record_boost`:

```python
def announcer_is_followed(user_id: int) -> bool:
    """True if at least one local user follows the remote user with this id.

    Called before any outbound fetch, so that an unfollowed remote party cannot
    make this instance request a URL of their choosing.
    """
    return db.session.query(UserFollower.id).filter(
        UserFollower.remote_user_id == user_id,
        UserFollower.is_inward == False).first() is not None
```

- [ ] **Step 4: Replace the stub body**

Replace the whole of `process_microblog_announce` — docstring and two-line body, lines 3418-3435 — with:

```python
def process_microblog_announce(request_json, id, store_ap_json) -> Union[Post, None]:
    """Ingest a boost of a microblog post from an account a local user follows.

    Top-level posts only. Boosted replies are ignored: backfilling absent
    ancestors would mean unbounded recursion against untrusted hosts.
    """
    saved_json = request_json if store_ap_json else None

    uri = announce_target_uri(request_json)
    if not uri:
        log_incoming_ap(id, APLOG_ANNOUNCE, APLOG_FAILURE, saved_json, 'Announce has no object URI')
        return None

    # Trust gate. Must stay above every network call in this function.
    announcer = find_actor_or_create_cached(request_json['actor'], create_if_not_found=False)
    if not announcer or not isinstance(announcer, User):
        log_incoming_ap(id, APLOG_ANNOUNCE, APLOG_IGNORED, saved_json, 'Announce actor is not a known user')
        return None
    if announcer.banned:
        log_incoming_ap(id, APLOG_ANNOUNCE, APLOG_IGNORED, saved_json, f'{announcer.ap_id} is banned')
        return None
    if not announcer_is_followed(announcer.id):
        log_incoming_ap(id, APLOG_ANNOUNCE, APLOG_IGNORED, saved_json, 'Announce from unfollowed actor')
        return None

    # Local posts carry a full ap_id from Post.generate_ap_id(), so this resolves
    # both already-ingested remote posts and posts authored on this instance.
    post = Post.get_by_ap_id(uri)
    if post:
        record_boost(post, announcer)
        return post

    post_data = remote_object_to_json(uri)
    if not post_data:
        log_incoming_ap(id, APLOG_ANNOUNCE, APLOG_FAILURE, saved_json, 'Could not fetch boosted object ' + uri)
        return None

    if not is_top_level(post_data):
        log_incoming_ap(id, APLOG_ANNOUNCE, APLOG_IGNORED, saved_json, 'Boosted object is a reply')
        return None

    # create_resolved_object performs the attributedTo / domain-match impersonation
    # check. Do not duplicate it here.
    resolved = create_resolved_object(uri, post_data, urlparse(uri).netloc,
                                      find_microblogging_community(), id, store_ap_json)
    if not isinstance(resolved, Post):
        log_incoming_ap(id, APLOG_ANNOUNCE, APLOG_IGNORED, saved_json, 'Boosted object did not resolve to a post')
        return None

    record_boost(resolved, announcer)
    return resolved
```

`urlparse` is already imported at line 11. The `APLOG_*` constants arrive via `from app.constants import *` at line 26.

- [ ] **Step 5: Run test to verify it passes**

Run: `./run_tests.sh tests/test_process_microblog_announce.py -v`
Expected: PASS, 6 tests

- [ ] **Step 6: Run everything so far**

Run: `./run_tests.sh tests/ -v --ignore=tests/test_activitypub_util.py`
Expected: no failures

- [ ] **Step 7: Document it**

In `FEDERATION.md`, under `## ActivityPub`, add:

```markdown
- Boosts (`Announce`) of top-level posts from microblogging platforms are ingested when
  the boosting account is followed by a local user. Boosted replies are not ingested.
```

- [ ] **Step 8: Commit**

```bash
git add app/activitypub/util.py FEDERATION.md tests/test_process_microblog_announce.py
git commit -m "feat: ingest boosts of microblog posts from followed accounts"
```

---

## Task 5: Announce dispatch, including boosts of local posts

**Files:**
- Modify: `app/activitypub/util.py` — add `process_announce_of_uri()`
- Modify: `app/activitypub/routes.py:862-875` — delegate to it
- Test: `tests/test_announce_dispatch.py`

**Interfaces:**
- Consumes: `process_microblog_announce()` (Task 4); existing `resolve_remote_post()` (line 3662).
- Produces: `process_announce_of_uri(request_json, community, id, store_ap_json) -> Union[Post, None]`

The early return at `routes.py:864-866` fires for any Announce whose object URL starts with this server's name, **before** the `community is None` dispatch at line 868. A followed account boosting a post authored here therefore never reaches `process_microblog_announce`. That is likely the most common boost a PieFed instance receives.

Extracting the dispatch into `util.py` fixes that and makes the ordering testable without Redis or the inbox plumbing.

- [ ] **Step 1: Write the failing test**

Create `tests/test_announce_dispatch.py`:

```python
import pytest

from tests.factories import make_community, make_follow, make_instance, make_post, make_user


@pytest.fixture
def followed_booster(db_session):
    instance = make_instance('m.example')
    booster = make_user(instance, 'booster')
    local = make_user(None, 'localuser', local=True)
    make_follow(local, booster)
    return booster


def announce(actor_uri, object_uri):
    return {
        'id': f'{actor_uri}/statuses/1/activity',
        'type': 'Announce',
        'actor': actor_uri,
        'object': object_uri,
    }


def test_local_post_boost_is_recorded(db_session, followed_booster, monkeypatch):
    """A followed account boosting a locally authored post records the boost"""
    from app.activitypub.util import process_announce_of_uri
    from app.models import PostBoost
    monkeypatch.setattr('app.activitypub.util.remote_object_to_json',
                        lambda uri: pytest.fail('must not fetch local content'))
    author = make_user(None, 'localauthor', local=True)
    post = make_post(make_community('news'), author, 'https://test.piefed.local/c/news/p/1/hello')

    result = process_announce_of_uri(announce(followed_booster.ap_public_url, post.ap_id),
                                     None, 'd1', False)

    assert result is not None and result.id == post.id
    assert PostBoost.query.filter_by(post_id=post.id, user_id=followed_booster.id).count() == 1


def test_community_announce_of_local_content_is_ignored(db_session, monkeypatch):
    """The community path still discards duplicates of local content"""
    from app.activitypub.util import process_announce_of_uri
    monkeypatch.setattr('app.activitypub.util.resolve_remote_post',
                        lambda *args, **kwargs: pytest.fail('must not resolve local content'))
    community = make_community('news')

    result = process_announce_of_uri(
        announce('https://m.example/users/booster', 'https://test.piefed.local/c/news/p/1/hello'),
        community, 'd2', False)

    assert result is None


def test_community_announce_of_remote_content_resolves(db_session, monkeypatch):
    """The community path still resolves remote content"""
    from app.activitypub.util import process_announce_of_uri
    seen = []
    monkeypatch.setattr('app.activitypub.util.resolve_remote_post',
                        lambda uri, community, announce_id, store: seen.append(uri) or 'resolved')
    community = make_community('news')

    result = process_announce_of_uri(
        announce('https://m.example/users/booster', 'https://other.example/notes/3'),
        community, 'd3', False)

    assert result == 'resolved'
    assert seen == ['https://other.example/notes/3']
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./run_tests.sh tests/test_announce_dispatch.py -v`
Expected: FAIL at import — `ImportError: cannot import name 'process_announce_of_uri'`

- [ ] **Step 3: Add the dispatch function to util.py**

In `app/activitypub/util.py`, immediately after `process_microblog_announce`:

```python
def process_announce_of_uri(request_json, community, id, store_ap_json) -> Union[Post, None]:
    """Route an Announce whose object is a bare URI.

    With no community, this is a microblog boost. Its object may be local content,
    in which case process_microblog_announce records the boost without fetching or
    creating anything — which is why the local-content check below applies only to
    the community path.
    """
    if community is None:
        return process_microblog_announce(request_json, id, store_ap_json)

    uri = announce_target_uri(request_json)
    if uri and uri.startswith('https://' + current_app.config['SERVER_NAME']):
        log_incoming_ap(id, APLOG_DUPLICATE, APLOG_IGNORED, request_json if store_ap_json else None,
                        'Activity about local content which is already present')
        return None

    return resolve_remote_post(uri, community, id, store_ap_json)
```

- [ ] **Step 4: Delegate from routes.py**

Replace lines 863-870 of `app/activitypub/routes.py`:

```python
                    if isinstance(request_json['object'], str):
                        if request_json['object'].startswith('https://' + current_app.config['SERVER_NAME']):
                            log_incoming_ap(id, APLOG_DUPLICATE, APLOG_IGNORED, saved_json, 'Activity about local content which is already present')
                            return
                        if community is None:
                            post = process_microblog_announce(request_json, id, store_ap_json)
                        else:
                            post = resolve_remote_post(request_json['object'], community, id, store_ap_json)
```

with:

```python
                    if isinstance(request_json['object'], str):
                        post = process_announce_of_uri(request_json, community, id, store_ap_json)
```

Then update the import at `app/activitypub/routes.py:22` to bring in `process_announce_of_uri`. `process_microblog_announce` and `resolve_remote_post` may no longer be referenced directly in `routes.py`; remove them from the import only if that is the case.

- [ ] **Step 5: Run test to verify it passes**

Run: `./run_tests.sh tests/test_announce_dispatch.py -v`
Expected: PASS, 3 tests

- [ ] **Step 6: Verify routes.py still imports**

Run: `python -c "import app.activitypub.routes"`
Expected: no output, exit 0

- [ ] **Step 7: Commit**

```bash
git add app/activitypub/util.py app/activitypub/routes.py tests/test_announce_dispatch.py
git commit -m "fix: record boosts of posts authored on this instance"
```

---

## Task 6: Handle Undo of a boost

**Files:**
- Modify: `app/activitypub/util.py` — add `undo_boost()` next to `undo_vote()` (line 3209)
- Modify: `app/activitypub/routes.py` — add a branch after the `Like`/`Dislike` branch, which ends at line 1731; extend the import at line 22
- Test: `tests/test_announce_dispatch.py` (append)

**Interfaces:**
- Consumes: `announce_target_uri()` (Task 1), `remove_boost()` (Task 3).
- Produces: `undo_boost(target_ap_id: str, user: User) -> Union[Post, None]` — returns the post whose boost was removed, or None. Mirrors `undo_vote()`'s shape so the routes branch stays thin.

There is currently no `Undo`/`Announce` branch at all. The chain starting at line 1645 handles only `Follow`, `Delete`, `Like`/`Dislike`, `Lock`, `Block`, and `ChooseAnswer`, so boost counts can only ever increase.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_announce_dispatch.py`:

```python
def test_undo_boost_removes_the_row(db_session, followed_booster):
    """Un-boosting removes the PostBoost row and returns the post"""
    from app.activitypub.util import record_boost, undo_boost
    from app.models import PostBoost
    author = make_user(make_instance('other.example'), 'bob')
    post = make_post(make_community(), author, 'https://other.example/notes/7')
    record_boost(post, followed_booster)

    result = undo_boost(post.ap_id, followed_booster)

    assert result is not None and result.id == post.id
    assert PostBoost.query.filter_by(post_id=post.id).count() == 0
    assert post.post_boosts == []


def test_undo_boost_for_unknown_post_returns_none(db_session, followed_booster):
    """An Undo for a post we do not have is ignored, not an error"""
    from app.activitypub.util import undo_boost

    assert undo_boost('https://other.example/notes/404', followed_booster) is None


def test_undo_boost_twice_is_a_no_op(db_session, followed_booster):
    """Remote instances re-send; the second Undo must not raise"""
    from app.activitypub.util import record_boost, undo_boost
    author = make_user(make_instance('other.example'), 'bob')
    post = make_post(make_community(), author, 'https://other.example/notes/7')
    record_boost(post, followed_booster)

    undo_boost(post.ap_id, followed_booster)
    assert undo_boost(post.ap_id, followed_booster) is not None


def test_undo_boost_only_removes_the_undoing_users_boost(db_session, followed_booster):
    """One user's Undo leaves another user's boost intact"""
    from app.activitypub.util import record_boost, undo_boost
    from app.models import PostBoost
    author = make_user(make_instance('other.example'), 'bob')
    other = make_user(make_instance('third.example'), 'carol')
    post = make_post(make_community(), author, 'https://other.example/notes/7')
    record_boost(post, followed_booster)
    record_boost(post, other)

    undo_boost(post.ap_id, followed_booster)

    assert PostBoost.query.filter_by(post_id=post.id).count() == 1
    assert PostBoost.query.filter_by(post_id=post.id, user_id=other.id).count() == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./run_tests.sh tests/test_announce_dispatch.py -v`
Expected: FAIL at import — `ImportError: cannot import name 'undo_boost'`

- [ ] **Step 3: Add undo_boost to util.py**

In `app/activitypub/util.py`, immediately after `undo_vote()` (which begins at line 3209):

```python
def undo_boost(target_ap_id: str, user: User) -> Union[Post, None]:
    """Remove `user`'s boost of the post at `target_ap_id`.

    Returns the post so the caller can log a result, mirroring undo_vote().
    A post with no boost from this user still returns the post: a repeated Undo
    is a successful no-op, not a failure.
    """
    if not target_ap_id:
        return None
    post = Post.get_by_ap_id(target_ap_id)
    if not post:
        return None
    remove_boost(post, user)
    return post
```

- [ ] **Step 4: Add the routes branch**

In `app/activitypub/routes.py`, immediately after the `Like`/`Dislike` branch's `return` (line 1731):

```python
                    if core_activity['object']['type'] == 'Announce':  # Undoing a boost from a microblogging platform
                        # `user` comes from the signed outer actor, resolved at line 838.
                        # Never read the actor from the inner object.
                        target_ap_id = announce_target_uri(core_activity['object'])
                        post = undo_boost(target_ap_id, user)
                        if post:
                            log_incoming_ap(id, APLOG_ANNOUNCE, APLOG_SUCCESS, saved_json)
                        else:
                            log_incoming_ap(id, APLOG_ANNOUNCE, APLOG_IGNORED, saved_json,
                                            'Unfound object for Undo Announce ' + str(target_ap_id))
                        return
```

Add `announce_target_uri` and `undo_boost` to the `from app.activitypub.util import ...` block at line 22.

No trust gate applies. Removing a boost is always safe, and gating it could strand rows if the announcer is unfollowed between the boost and the un-boost. No fetch occurs, because there is nothing to create.

- [ ] **Step 5: Run test to verify it passes**

Run: `./run_tests.sh tests/test_announce_dispatch.py -v`
Expected: PASS, 7 tests

- [ ] **Step 6: Verify routes.py still imports**

Run: `python -c "import app.activitypub.routes"`
Expected: no output, exit 0

- [ ] **Step 7: Commit**

```bash
git add app/activitypub/util.py app/activitypub/routes.py tests/test_announce_dispatch.py
git commit -m "feat: handle Undo of microblog boosts"
```

---

## Task 7: Surface boosted posts in the feed

**Files:**
- Modify: `app/utils.py:3250-3254`
- Test: `tests/test_feed_boost_visibility.py`

**Interfaces:**
- Consumes: the `post_boost` table populated by Task 3.
- Produces: no new symbols.

Without this, ingestion is invisible bookkeeping — the existing clause matches only on the post's **author**, so a boost by a followed account of a stranger's post matches nothing.

This sits in the hot feed path, so the change is gated on measurement.

- [ ] **Step 1: Write the failing test**

Create `tests/test_feed_boost_visibility.py`:

```python
"""The feed clause is a SQL string in app/utils.py, so it is tested as SQL.

This keeps the test independent of Flask-Login and the rest of the feed query.
"""

import pytest
from sqlalchemy import text

from tests.factories import make_community, make_follow, make_instance, make_post, make_user

BOOST_CLAUSE = """SELECT p.id FROM "post" as p WHERE
EXISTS (SELECT 1 FROM post_boost pb
        INNER JOIN user_follower uf2 ON uf2.remote_user_id = pb.user_id
        WHERE pb.post_id = p.id
        AND uf2.local_user_id = :local_user_id
        AND uf2.is_inward is false)"""


@pytest.fixture
def scenario(db_session):
    """A local user follows booster. Stranger authors a post. Booster boosts it."""
    from app.activitypub.util import record_boost
    instance = make_instance('m.example')
    booster = make_user(instance, 'booster')
    stranger = make_user(make_instance('other.example'), 'stranger')
    local = make_user(None, 'localuser', local=True)
    make_follow(local, booster)
    post = make_post(make_community(), stranger, 'https://other.example/notes/1')
    record_boost(post, booster)
    return local, post, stranger


def test_boosted_post_is_visible(db_session, scenario):
    """A post boosted by a followed account matches the clause"""
    from app import db
    local, post, _ = scenario

    ids = [row[0] for row in db.session.execute(text(BOOST_CLAUSE), {'local_user_id': local.id})]

    assert post.id in ids


def test_unboosted_post_is_not_visible(db_session, scenario):
    """A post nobody followed has boosted does not match"""
    from app import db
    local, _, stranger = scenario
    other_post = make_post(make_community('other'), stranger, 'https://other.example/notes/2')

    ids = [row[0] for row in db.session.execute(text(BOOST_CLAUSE), {'local_user_id': local.id})]

    assert other_post.id not in ids


def test_not_visible_to_a_user_who_follows_nobody(db_session, scenario):
    """The clause is scoped to the querying user's own follows"""
    from app import db
    _, post, _ = scenario
    someone_else = make_user(None, 'someoneelse', local=True)

    ids = [row[0] for row in db.session.execute(text(BOOST_CLAUSE),
                                                {'local_user_id': someone_else.id})]

    assert ids == []


def test_clause_matches_the_one_in_utils():
    """The tested SQL is the SQL the feed actually uses.

    Guards against the test drifting from app/utils.py.
    """
    import inspect
    from app import utils
    source = inspect.getsource(utils.get_deduped_post_ids)
    assert 'post_boost pb' in source
    assert 'uf2.remote_user_id = pb.user_id' in source
```

`get_deduped_post_ids` (`app/utils.py:3232`) is the function that builds this SQL.

- [ ] **Step 2: Run test to verify it fails**

Run: `./run_tests.sh tests/test_feed_boost_visibility.py -v`
Expected: the first three tests PASS (the clause is valid SQL on its own) and `test_clause_matches_the_one_in_utils` FAILS, because `app/utils.py` does not contain the clause yet.

- [ ] **Step 3: Capture the baseline query plan**

Before changing `app/utils.py`, capture the plan for a logged-in feed query on a **populated** database — the development database, not the truncated test one:

```bash
flask shell <<'EOF'
from app import db
from sqlalchemy import text
sql = """EXPLAIN ANALYZE SELECT p.id FROM "post" as p
INNER JOIN "community" as c on p.community_id = c.id
WHERE (c.show_all is true OR EXISTS (SELECT 1 FROM user_follower uf
      WHERE uf.local_user_id = :uid AND uf.remote_user_id = p.user_id AND is_inward is false))
AND c.banned is false ORDER BY p.posted_at DESC LIMIT 50"""
for row in db.session.execute(text(sql), {'uid': 1}):
    print(row[0])
EOF
```

Record the total execution time. Substitute a `local_user_id` that actually follows people.

- [ ] **Step 4: Add the EXISTS clause**

In `app/utils.py`, replace lines 3244-3248:

```python
    if current_user.is_authenticated and current_user.num_following and include_following:
        sources.append("""EXISTS (SELECT 1 FROM user_follower uf
                                  WHERE uf.local_user_id = :local_user_id
                                  AND uf.remote_user_id = p.user_id AND is_inward is false)""")
        params['local_user_id'] = current_user.id
```

with:

```python
    if current_user.is_authenticated and current_user.num_following and include_following:
        sources.append("""EXISTS (SELECT 1 FROM user_follower uf
                                  WHERE uf.local_user_id = :local_user_id
                                  AND uf.remote_user_id = p.user_id AND is_inward is false)""")
        sources.append("""EXISTS (SELECT 1 FROM post_boost pb
                                  INNER JOIN user_follower uf2 ON uf2.remote_user_id = pb.user_id
                                  WHERE pb.post_id = p.id
                                  AND uf2.local_user_id = :local_user_id
                                  AND uf2.is_inward is false)""")
        params['local_user_id'] = current_user.id
```

Both `post_boost.post_id` and `post_boost.user_id` are already indexed by `migrations/versions/c831b9c7eee9_post_boost.py`.

- [ ] **Step 5: Run test to verify it passes**

Run: `./run_tests.sh tests/test_feed_boost_visibility.py -v`
Expected: PASS, 4 tests

- [ ] **Step 6: Capture the new query plan**

Re-run Step 3's block with the second `EXISTS` added to the `WHERE` clause. Compare execution time against the baseline.

**Gate:** if the added clause degrades the query beyond what the team accepts on this dataset, stop and report the numbers rather than merging. Do not silently accept a regression in the feed path. Tasks 0-6 remain useful on their own if this happens.

- [ ] **Step 7: Verify end to end in the running app**

Log in as a user who follows the boosting account, load the home feed with the "subscribed" filter, and confirm a boosted post appears even though its author is not followed.

- [ ] **Step 8: Run the full suite**

Run: `./run_tests.sh tests/ -v --ignore=tests/test_activitypub_util.py`
Expected: no failures. `tests/test_activitypub_util.py` is excluded because it requires live network access and a hardcoded username.

- [ ] **Step 9: Commit**

```bash
git add app/utils.py tests/test_feed_boost_visibility.py
git commit -m "feat: surface posts boosted by followed accounts in the feed"
```

---

## Self-Review Notes

Spec coverage, section by section:

| Spec section | Task |
|---|---|
| Control flow steps 1-6 | Task 4 |
| Rejected alternatives | N/A — recorded, nothing to build |
| Data model and idempotency | Tasks 2, 3 |
| Undo / un-boost | Task 6 |
| Boosts of local posts | Task 5 |
| Feed surfacing | Task 7 |
| Error handling and logging | Task 4 (every branch logs) |
| Security considerations | Task 4 Steps 1-4 (trust gate asserted by test), Task 6 Step 4 |
| Testing | Task 0 provides fixtures; Tasks 1-7 each carry tests |

The spec's Testing section describes several behaviours as manually verified. Task 0 supersedes that: everything except the `EXPLAIN ANALYZE` gate and the final end-to-end feed check is now automated. The spec has been updated to match.

Symbols used consistently throughout: `announce_target_uri`, `is_top_level`, `boost_cache_entries`, `announcer_is_followed`, `record_boost`, `remove_boost`, `undo_boost`, `process_announce_of_uri`, `Post.update_boost_cache`.

Known risk carried into implementation: `tests/factories.py` sets the columns that are needed as far as the models show. If Postgres rejects an insert for a column this plan did not anticipate, add it to the factory — that is expected iteration, not a plan defect.

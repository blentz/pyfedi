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
runs pytest. Container data lives in tmpfs, so nothing survives `--down` and no
state leaks between runs.

Neither service publishes a host port. `test-runner` reaches them over the compose
network by name, so none is needed — and publishing one would stop two checkouts of
this repo (a git worktree, for instance) from running tests at the same time, since
the second stack could not bind the port. To inspect a running test database:

    podman-compose -f compose.test.yaml exec test-db psql -U pyfedi pyfedi_test
    podman-compose -f compose.test.yaml exec test-redis redis-cli

## Two things that will otherwise waste your time

**`--down` makes the next run slow.** It destroys the tmpfs volume, so the next
run replays all ~269 migrations against an empty database instead of the usual
no-op. Use it when you are finished, not between runs.

**podman-compose names the project after the directory.** A second checkout gets a
separate stack, and `./run_tests.sh --down` only stops the stack belonging to the
directory you run it from. If a run stalls waiting for Postgres, check `podman ps`
for another checkout's containers and stop that stack from its own directory.

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
`conftest.py`'s `is_disposable_database_url()` refuses to run unless the database
name (the last "/"-separated path segment, with any `?query`/`#fragment` stripped)
ends with `_test` — a bare substring match on "test" is not enough, since that
would also accept real database names like `attestation` or a URL whose query
string merely mentions "test". Do not defeat that guard. `tests/test_conftest_guard.py`
covers it.

`tests/test_activitypub_util.py` predates this setup. It needs live network access
and a hardcoded username, and is excluded from the standard run.

## Coverage

`app/request_hooks.py` is held at 100% branch coverage:

    ./run_tests.sh tests/ -q --ignore=tests/test_activitypub_util.py \
        --cov=app.request_hooks --cov-branch --cov-report=term-missing --cov-fail-under=100

Use the dotted module form (`--cov=app.request_hooks`), not a file path
(`--cov=app/request_hooks.py`). The file-path form reports `Module
app/request_hooks.py was never imported` and measures 0% in this environment
(pytest-cov against pytest 9.1.1 / coverage.py 7.15.4, no pyproject.toml /
pytest.ini / .coveragerc in this repo) even though the module is plainly
imported and exercised -- silently failing the gate for the wrong reason.

Branch coverage, not just line coverage: `after_request` is dense with
conditionals, and line coverage alone reports 100% while leaving whole branches
unexercised.

Coverage is a floor, not a target. A test that executes a line without asserting
anything raises the number and catches nothing.

The gate measures coverage.py BRANCH coverage, not CONDITION coverage: a compound
`if a and b` is "covered" once both the true and false outcome of the whole
expression have been observed, even if one of `a`/`b` is never independently
falsified. So 100% here does not mean every sub-condition has been shown to
matter -- read `--cov-report=term-missing` output with that in mind, and do not
over-read the number as-is. Two known cases in `app/request_hooks.py` where a
sub-condition of a compound branch is never independently falsified by this
suite: `request.path.startswith('/bootstrap/static/')` at lines 130 and 141, and
`"api/alpha/swagger" in request.path` / `not in request.path` at lines 147 and
166. (A third case, `response.status_code != 304` at line 150, is covered by
test_no_csp_header_on_a_304_response in tests/test_request_hooks.py -- listed
here as an example of the kind of gap this paragraph is warning about, and of
what closing one looks like.)

## The coverage ratchet

Per-module floors live in `coverage_floors.ini`. Floors only ever RISE; raising
one is a sub-project's deliverable, and lowering one to make a run pass defeats
the ratchet. A module with no entry is ignored, so unfinished modules block
nobody.

    ./run_tests.sh tests/ -q --ignore=tests/test_activitypub_util.py --cov=app --cov-report=json
    podman-compose -f compose.test.yaml exec -T test-runner \
        python tests/check_coverage_floors.py coverage.json coverage_floors.ini

coverage.py's own `fail_under` is a single global number, which is why the
per-module check is a script.

## Fixtures for external services

- `http_mock` — respx router over outbound httpx. `assert_all_called=True`, so a
  registered-but-unused route fails the test.
- `federation_peer(handle)` — webfinger + actor responses for a remote handle.
  The domain must NOT end in `.local`: `get_request()` rejects those via
  `is_invalid_get_request_uri()` before respx sees them. Pass
  `include_inbox=True` to also register the actor's inbox for a POST.
- `s3_bucket` — a moto-backed bucket, yields the bucket name.
- `redis_double` — fakeredis patched over `app.utils.get_redis_connection`. Note
  the rate limiter and Celery app are built from `Config` at import time and are
  not affected by it.
- Celery runs eagerly under test, with `eager_propagates` so a failing task raises
  rather than being swallowed. Configured in the `app` fixture, on `celery.conf`
  directly (**not** `TestConfig` attributes), setting BOTH the old- and
  new-style key names together in one `celery.conf.update(...)` call:
  `task_always_eager` / `CELERY_ALWAYS_EAGER` and `task_eager_propagates` /
  `CELERY_EAGER_PROPAGATES_EXCEPTIONS`. Two independent Celery quirks make
  anything simpler fragile:

  1. `config.py`'s `CELERY_BROKER_URL` is itself an old-style name, so by the
     time `app/__init__.py:175`'s `celery.conf.update(app.config)` has run,
     `celery.conf` already holds old-format keys. Celery's `Settings` object
     refuses to mix old- and new-format keys once a first real access
     finalizes it with one format dominant
     (`celery.exceptions.ImproperlyConfigured: "Cannot mix new setting names
     with old setting names"`) — setting only the modern `task_always_eager`
     name can raise exactly that.
  2. Setting only the *old*-style names is not reliably enough either:
     Celery's built-in defaults dict always carries a literal
     `task_always_eager` (and `task_eager_propagates`) key of its own, with
     value `False`. If anything else causes `celery.conf` to finalize before
     this fixture runs — e.g. a module-level `@celery.task`-decorated
     function, decorated at *import* time, and pytest imports every test
     module during collection before any fixture executes — a lookup by the
     new-style name finds Celery's own literal default first and never falls
     back to checking the old-style alias. The old-style write then silently
     has no effect, and whether the eager flag "works" ends up depending on
     which test files pytest happened to collect first. Observed concretely
     via `tests/test_instance_util.py`, whose `@celery.task def bulk_follow`
     (`app/instance/util.py`) triggers exactly this if it is imported before
     the `app` fixture has run.

  Providing both spellings in the same `update()` call sidesteps both
  problems: if this fixture is what finalizes `celery.conf`, Celery's own
  mixing check explicitly allows a setting provided under both names at once;
  if something else already finalized `celery.conf` first, the new-style
  write here lands as a literal key that a `ChainMap` lookup always checks
  before the defaults layer, so it wins regardless of collection order.

### Eager Celery makes outbound federation happen inline — and it fails silently

Read this before writing any test that federates.

With `task_always_eager`, `.delay()` no longer enqueues; it runs the task in this
process. `task_selector` calls `.delay()`, and `send_post_request` in turn calls
`post_request.delay()`, so a test that triggers a follow, a vote or a post really
does attempt the outbound HTTP POST during the test.

That POST does **not** fail loudly when it goes nowhere.
`app/activitypub/signature.py`'s `post_request` wraps the request in
`except Exception` and records the failure as an `ActivityPubLog` row instead of
propagating it. respx's "unexpected request" error is therefore swallowed, and a
test whose federation went nowhere still passes.

So a green test proves nothing about delivery on its own. To assert an activity
was actually sent, do one of:

- pass `include_inbox=True` to `federation_peer`, so `assert_all_called=True`
  fails the test if the POST never happened; or
- assert on the `ActivityPubLog` row the send produced.

`task_eager_propagates` does not help here: it re-raises what the *task* raises,
and `post_request` raises nothing.

Prove a fixture by driving real application code through it. A test asserting that
fakeredis stores what you put in it tests fakeredis, not PieFed.

## Known noise

Two things show up in normal runs that are not bugs in this setup and do not
need re-investigating:

- Two `DeprecationWarning`s from `ldap3`/`pyasn1` (`tagMap`/`typeMap` are
  deprecated) appear in every pytest run. They come from a transitive
  dependency pulled in by LDAP support, unrelated to this test setup.
- `./run_tests.sh --down` logs `StopSignal SIGTERM failed to stop container
  ...test-runner... resorting to SIGKILL`. `test-runner` idles on
  `sleep infinity`, which does not trap `SIGTERM`, so compose falls back to
  `SIGKILL` after its timeout. Cosmetic — the container still stops and no
  state persists (tmpfs).

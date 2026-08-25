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
(pytest-cov against pytest 9.1.1 / coverage.py 7.15.4) even though the module is
plainly imported and exercised -- silently failing the gate for the wrong
reason. This was re-checked with the `.coveragerc` this branch adds in place:
the warning still occurs, so the advice does not depend on config being absent.

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

## Fuzzing

Two modes, and the split is the point.

**The corpus replay** is an ordinary pytest file, `tests/test_utils_fuzz_corpus.py`.
It reads every file under `tests/fuzz/corpus/<target>/` and pushes it through the
matching property check in `tests/fuzz/harnesses.py`. Deterministic, no atheris,
runs in well under a second, and it is what makes a finding permanent.

**The campaign** is on demand and runs OUTSIDE the coverage run:

    podman-compose -f compose.test.yaml exec -T -w /app test-runner \
        python -m tests.fuzz.run_campaign is_valid_xml_utf8 -max_total_time=60

Targets: `is_valid_xml_utf8`, `sanitize_svg_bytes` and `allowlist_html`.
`-max_total_time` is the budget in seconds; every other libFuzzer flag passes
straight through.

Use `python -m`, from the repository root. Running the file by path puts
`tests/fuzz/` on `sys.path` instead of the root and the import of
`tests.fuzz.harnesses` fails.

**Never run a campaign under `--cov`.** atheris installs its own bytecode
instrumentation to guide mutation and coverage.py is already tracing; they must
not fight. They never meet, because nothing pytest collects imports atheris:
`harnesses.py` holds plain functions, and `run_campaign.py` is neither named
`test_*.py` nor holds test functions.

`run_campaign.py` instruments only `app` and `tests` (`atheris.instrument_imports(include=...)`).
Without instrumentation libFuzzer gets no feedback and degenerates into blind
random bytes -- it says so, with "no interesting inputs were found so far. Is the
code instrumented for coverage?", and the corpus never grows.

Two corpus directories, and which is which matters:

- `tests/fuzz/corpus/<target>/` is committed and hand-curated: named hostile
  seeds, plus a reproducer for every real finding. libFuzzer only READS it, and
  writes crash artifacts into it via `-artifact_prefix`.
- `tests/fuzz/.work/<target>/` is gitignored. libFuzzer writes every
  coverage-increasing unit there, and a later campaign resumes from it. One 60s
  run against `sanitize_svg_bytes` produces about a hundred of these; they do not
  belong in review.

**Findings are reported, not fixed.** A security fix in deployed software is the
project owner's decision. When a campaign finds something: rename the
`crash-<sha1>` artifact to something that says what it is, keep it in the
committed corpus, and add its id to `KNOWN_UNFIXED` in
`tests/test_utils_fuzz_corpus.py` with a reason naming the defect. Those are
`strict=True` xfails, so the day the defect is fixed they XPASS and fail the run
-- which is the signal to delete the entry, not to relax it. `KNOWN_UNFIXED` is
currently empty: it held six entries covering two `allowlist_html` defects, the
owner authorised fixing both, and the fix made all six XPASS. Their corpus files
are still replayed on every run, now as regression pins that pass.

A campaign stops at its first crash, so an unfixed defect blocks the search
behind it. There used to be a fourth target, `allowlist_html_past_known_defects`
-- the same target with those two defects suppressed, so the fuzzer could hunt
for a third. With both fixed, the suppression would only serve to swallow a
regression, so it and its target have been deleted. `allowlist_html` runs
honestly: 90,984 executions in 60s, `cov: 90 ft: 513`, no finding.

**Assert properties, not the absence of crashes.** "It did not crash" would pass
a sanitiser that returned its input unchanged. Each check in `harnesses.py`
asserts a security property instead, against a PARSED tree rather than raw bytes
-- every one of these functions may legally emit the text `javascript:` or
` onload=` as escaped character data, and a substring check reports that inert
text as a break-in within seconds of fuzzing.

Inertness cuts finer than "text vs markup". `allowlist_html`'s URL property is
asserted on ANCHORS only: `href` is on its `allowed_attrs` for every element, so
malformed input can leave `<img href="javascript:x">` standing, and that is
inert because HTML defines no `href` on `img`. `a` is the only element in
`allowed_tags` for which `href` is navigable. That distinction was found the
hard way -- the first campaign the fixed target could run flagged exactly that
`<img href>` at seed load.

`app.utils.sanitize_svg` is deliberately not a target. It opens a path, calls
`sanitize_svg_bytes` and writes the result back; all of its input handling is
`sanitize_svg_bytes`, which is fuzzed. Fuzzing a filesystem path would exercise
`open()`, not PieFed.

## The coverage ratchet

Per-module floors live in `coverage_floors.ini`. Floors only ever RISE; raising
one is a sub-project's deliverable, and lowering one to make a run pass defeats
the ratchet. A module with no entry is ignored, so unfinished modules block
nobody.

    ./run_tests.sh tests/ -q --ignore=tests/test_activitypub_util.py --cov=app --cov-report=json && \
    podman-compose -f compose.test.yaml exec -T test-runner \
        python tests/check_coverage_floors.py coverage.json coverage_floors.ini

**Run it as one `&&` chain, exactly as written.** `coverage.json` is gitignored,
so it is not cleaned up and it persists in the working tree between runs. A
pytest invocation that fails — or that never starts, e.g. a typo in a test path
— writes no new report, and the ratchet run afterwards will happily pass against
the *previous* run's file. A red suite followed by a green ratchet. Nothing
detects that automatically; the `&&` is what protects you. The checker prints
the report's absolute path and modification time on every run so you can see for
yourself which report was measured.

The checker fails closed. A missing, unreadable, section-less or empty
`coverage_floors.ini` exits 2 with an error rather than reporting "All 0 module
floors met." Exit codes: 0 met, 1 violated, 2 could not check.

coverage.py's own `fail_under` is a single global number, which is why the
per-module check is a script.

### Sub-project 1a: app/utils.py

Sub-project 1a (nine tasks, `tests/test_utils_*.py` plus
`tests/test_utils_context_globals.py`) covered `app/utils.py`'s pure functions,
its security-sensitive parsers (`allowlist_html`, `sanitize_svg_bytes`,
`is_valid_xml_utf8`, URL/domain helpers), and the functions that only need an
app or request context (`humanize_number`, `round_invisible_digits`,
`debug_checkpoint`, `localize_datetime`, `get_timezones`, `theme_list`,
`render_from_tpl`, `orjson_response`, `ensure_directory_exists`). It set
`app/utils.py`'s first coverage floor at 46% — the measured blended
statement-and-branch `percent_covered` from the full suite, rounded down —
not 100%, because the module also holds many DB-backed and network-backed
functions that sub-projects 1b and 1c are scoped to cover. Floors only rise:
1b and 1c raise this one further as they close those gaps, they do not lower
it. It is currently 49%: the client-IP fix (`tests/test_client_ip.py`) and the
`back()` / `inbox_domain()` de-duplication (`tests/test_redirect_back.py`,
`tests/test_instance_domain_lookup.py`) raised it from 46 to 48, and unifying
the ten `Referer`-handling routes onto `back()` behind one origin check
(`app.utils.is_safe_redirect_target`, `tests/test_safe_redirect_target.py`)
raised it to 49.

Seven functions were identified in Task 1 as out of reach for a pure/context-only
sub-project and moved out of scope, to be picked up by 1b or 1c:

| Function | Why it moved | Where |
|---|---|---|
| `jaccard_similarity` | calls `recently_upvoted_posts()` / `recently_upvoted_post_replies()`, both DB-backed; also memoises into a module-level `user2_cache` dict that persists across tests | 1b |
| `actor_contains_blocked_words` | calls `get_setting()`, which queries the `Setting` table | 1b |
| `user_ip_banned` | calls `banned_ip_addresses()`, DB-backed | 1b |
| `first_paragraph` | calls `allowlist_html()` with no `test_env`, which reaches `get_emoji_replacements()` and `fediverse_domains()` — both DB-backed | 1b |
| `is_image_url` | calls `mime_type_using_head()`, which performs a network HEAD request | 1c |
| `is_local_image_url` | calls `is_image_url()` — same network dependency | 1c |
| `download_defeds` | both arms call `download_defeds_worker`, which reaches the network through `retrieve_defederation_list` | 1c |

The fuzz corpus this sub-project built lives at `tests/fuzz/corpus/<target>/`
(committed, hand-curated) with working-set output in `tests/fuzz/.work/<target>/`
(gitignored); see "Fuzzing" above for how the two differ and how to run a
campaign.

## Fixtures for external services

- `http_mock` — respx router over outbound httpx. `assert_all_called=True`, so a
  registered-but-unused route fails the test.
- `federation_peer(handle)` — webfinger + actor responses for a remote handle.
  The domain must NOT end in `.local`: `get_request()` rejects those via
  `is_invalid_get_request_uri()` before respx sees them. Pass
  `include_inbox=True` to also register the actor's inbox for a POST.
- `s3_bucket` — a moto-backed bucket, yields the bucket name.
- `redis_double` — fakeredis patched over `get_redis_connection`. Patched at
  **four** binding sites, not one: `app.utils`, `app.main.routes`, `app.cli` and
  `app.activitypub.routes`. `from app.utils import get_redis_connection` binds a
  new name in the importing module at import time, so patching `app.utils` alone
  leaves those three pointing at the original and talking to the real, shared
  test Redis. Add any fifth such import to the fixture's list. Still not covered:
  `app.redis_client` — a module-level global assigned by `create_app()` and read
  via `from app import redis_client` in roughly 14 modules
  (`grep -rn 'from app import.*redis_client' app/` for the current set; not
  listed here because such a list rots). That is a far wider surface than
  `get_redis_connection`'s four bindings, so a sub-project needing Redis
  isolation across `app/` should plan to patch `app.redis_client` as well, and
  to widen this fixture rather than hand-roll its own. Also not covered: the
  rate limiter and Celery app, built from `Config` at import time.
- Celery runs eagerly under test, with `eager_propagates` so a failing task
  raises rather than being swallowed. Configured in the `app` fixture, on
  `celery.conf` directly (**not** `TestConfig` attributes), in the OLD key
  format. `config.py`'s `CELERY_BROKER_URL` and friends are already old-format
  names, so by the time `app/__init__.py:175`'s `celery.conf.update(app.config)`
  has run, `celery.conf` is old-format dominant. Celery refuses to mix formats:
  writing only the modern `task_always_eager` raises
  `celery.exceptions.ImproperlyConfigured: "Cannot mix new setting names with
  old setting names"` on the first *read* of the setting. The fixture supplies
  both spellings, but only the old ones are load-bearing — `detect_settings`
  explicitly tolerates a setting given under both names and converts the new one
  to the old key before storing it.
- `block_outbound_http` — session-scoped and autouse: an empty respx router, so
  any outbound **httpx** request no `http_mock` matched raises instead of
  reaching the network. **It blocks httpx and nothing else** — respx patches
  httpx's transports only. Three transports in `app/` are **not yet blocked**
  and do reach the real internet under this harness (all three verified by
  probe, not inferred):
  - `urllib` — `app/nntp/server.py:767` (`urllib.request.urlopen`). A probe
    against `https://example.com/` returned 200 with the fixture active.
  - `botocore`/`urllib3` (boto3) — ten modules: `app/cli.py`, `app/email.py`,
    `app/admin/util.py`, `app/community/util.py`, `app/utils.py`,
    `app/activitypub/util.py`, `app/main/routes.py`, `app/shared/post.py`,
    `app/shared/tasks/maintenance.py`, `app/shared/upload.py`. The `s3_bucket`
    fixture covers this, but it is function-scoped and opt-in: a test that
    drives S3 code without requesting it calls real AWS.
  - `smtplib` — `app/email.py:164-166`. `TestConfig`'s `MAIL_SUPPRESS_SEND`
    governs Flask-Mail, not this code path.

  If you are writing the harness for `app/nntp/`, an S3-using module or
  `app/email.py`, arrange your own isolation; do not assume this fixture covers
  you. Closing the gap properly means a socket-level block, which is a design
  change nobody has ruled on yet.

  Without it, eager Celery turns every federating test into real outbound
  timeouts (`tests/test_announce_dispatch.py` measured 1.15s with it, 119s
  without). respx consults routers in registration order, so this one is asked
  *first* and `http_mock` second; that is safe only because it registers zero
  routes and can never match. **Never add a route to it** — even a catch-all
  that logs would silently override every `http_mock` route in the suite.

### Eager Celery makes outbound federation happen inline — and it fails silently

Read this before writing any test that federates.

With `task_always_eager`, `.delay()` no longer enqueues; it runs the task in this
process. `task_selector` calls `.delay()`, and `send_post_request` in turn calls
`post_request.delay()`, so a test that triggers a follow, a vote or a post really
does try to federate during the test — the actor lookups it performs are live
outbound GETs, and the send is attempted right after them.

None of that fails loudly when it goes nowhere.
`app/activitypub/signature.py`'s `post_request` wraps the send in
`except Exception` and records the failure as an `ActivityPubLog` row instead of
propagating it. respx's "unexpected request" error is therefore swallowed, and a
test whose federation went nowhere still passes.

`task_eager_propagates` does not help here: it re-raises what the *task* raises,
and `post_request` raises nothing.

So a green test proves nothing about delivery on its own. To assert an activity
was actually sent:

- pass `include_inbox=True` to `federation_peer`, **and** build the sending actor
  with `make_user(..., with_keys=True)`. Both are required. Signing dereferences
  the sender's private key, so a keyless sender raises `'NoneType' object has no
  attribute 'encode'` before any HTTP request is attempted — the inbox route is
  then never called and you get an opaque `RESPX: some routes were not called!`
  at teardown, pointing nowhere near the cause. `make_user` leaves the keypair
  empty by default because generating one costs about a second.
- or assert on the `ActivityPubLog` row the send produced. This distinguishes a
  failed send (`result == 'failure'`, with the reason in `exception_message`)
  from no send at all (no row), which the route-count check cannot do.

`tests/test_fixture_proofs.py::test_delivery_can_be_proved_when_the_sender_has_keys`
is the worked example.

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

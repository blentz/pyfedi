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

## Things that will otherwise waste your time

**`--down` is for a wedged stack, not for speed.** It destroys the tmpfs volume
AND the containers, so the next run replays all ~269 migrations (about 8s) and
rebuilds the image. Measured 2026-08-31: a stack left up for eight hours ran the
full suite in 186s, while the run immediately after `--down` plus a rebuild took
260s. Tearing down to "get the speed back" is the wrong instinct -- see the next
note for what the teardown actually costs.

**The per-test teardown DELETEs rows; it no longer TRUNCATEs tables, and the
staleness reset that TRUNCATE needed is gone.** Changed 2026-09-06, commit
`ec98595c`. You should not need `--down` for speed; reach for it only to recover
a genuinely wedged stack.

The mechanism as it stands. `db_session` (`tests/conftest.py:137-202`) issues one
statement built once from the model metadata by `_teardown_sql`
(`tests/conftest.py:119-133`): `SET LOCAL session_replication_role = replica`,
then a `DELETE FROM` for every table in `db.metadata.sorted_tables`, then a
`setval` sweep over every sequence in the `public` schema. Each of the three
parts is load-bearing and none of them is interchangeable with the others:

- `session_replication_role = replica` disables FK triggers for the duration of
  the transaction. That is what `TRUNCATE ... CASCADE` used to buy, and it is
  not optional -- the schema's foreign keys are **not acyclic**, so there is no
  deletion order that satisfies them all. `SET LOCAL` ends at the `COMMIT`, so
  the next test sees constraints enforced normally.
- The `setval` sweep replaces `RESTART IDENTITY`. Fixtures hard-code
  `instance_id=1` and `user_id=1`, so resetting the sequences is not optional
  either -- facts 21, 61 and 89 all rest on ids restarting at 1 in every test,
  and they are all still true. Only the *mechanism* changed.
- `db.metadata.sorted_tables` rather than `pg_tables` leaves `alembic_version`
  alone; wiping it would strand `flask db upgrade`.

**`session_replication_role` IS A SUPERUSER GUC, AND THAT REQUIREMENT IS
LOAD-BEARING AND EASY TO MISS.** The teardown works because
`compose.test.yaml` sets `POSTGRES_USER: pyfedi`, which the official Postgres
image creates as the **cluster superuser**, and `.env.test` points both URLs at
that role. Point `TEST_DATABASE_URL` at some other `*_test` database reached
with an ordinary role -- which `is_disposable_database_url()` accepts, and which
this file describes as a supported thing to do -- and every `db_session` test
dies in teardown with `permission denied to set parameter
"session_replication_role"`. It fails loudly and immediately rather than
silently leaving foreign keys unenforced, which is why this is a note and not a
warning, but it is a constraint the podman stack satisfies by accident of the
image's defaults and nothing else in the repo states.

Measured 2026-09-06 on identical fresh stacks over the same 629-test subset:
**124.5ms mean teardown / 55.13s wall to 5.2ms mean teardown / 16.18s wall.**
Across a full run the teardown now totals about **16.5s**, and
it does **not drift** as the run proceeds (first hundred tests 6.5ms, last
hundred 5.8ms), so DELETE's dead tuples are not outrunning autovacuum.
**Say which run before quoting that as a percentage**: 16.5s is **3.7%** of the
447.32s plain full run `ec98595c` measured it against, and **7.2%** of the
228.46s full `--cov=app --cov-branch` run recorded on 2026-09-06. Both are true
and they are of different runs; the seconds are the portable figure.

**THE ONE RISK THIS ARRANGEMENT CARRIES, NAMED SO IT IS RECOGNISED RATHER THAN
RE-DISCOVERED: NOTHING RECLAIMS RELATION SPACE ANY MORE.** `DELETE FROM t` with
no `WHERE` scans the relation's **page high-water mark**, not its live rows.
`TRUNCATE` reset every relation to zero blocks after every test; `DELETE` never
shrinks one, and `VACUUM` only truncates *trailing* all-empty pages. The data
lives in tmpfs (`compose.test.yaml`) and survives for the life of the container
across every run that does not `--down`. And the `pg_class` probe that
diagnosed the 2026-08-31 slowdown was removed in the same commit, so there is
now no automatic signal at all.

- **Steady state is fine and that is measured, not assumed.** Ordinary tests
  insert tens of rows, and the teardown does not drift across a full run
  (6.5ms → 5.8ms). Nothing here is a reason to change how you write tests.
- **The trigger is a single outlier test.** One test that seeds tens of
  thousands of rows -- a pagination case, a backfill, a fuzz case -- extends
  those relations and their indexes permanently for the life of the container.
  Every remaining teardown in that run, and every teardown of every later run
  on the same stack, then seq-scans the extended pages.
- **The symptom is the one this whole section is about**: teardown cost rises
  for every test at once, so `--durations` stays flat and innocent while the
  run as a whole crawls. If you see that, this is the first thing to suspect,
  and `select pg_database_size('pyfedi_test');` is the one-line check the
  removed odometer used to do for you.
- **The remedy is `./run_tests.sh --down`**, which destroys the tmpfs volume
  and reclaims everything, at the cost of an ~8s migration replay and an image
  rebuild. **This is the exception to "`--down` is not for speed" at the top of
  this file**: that advice was written about the TRUNCATE arrangement and holds
  for the ordinary case, but `--down` is now the *only* thing that shrinks a
  relation.

**What was there before, and the figure in it that was false.** `db_session`
used to `TRUNCATE` all ~90 tables after every test. TRUNCATE allocates a fresh
relfilenode per table, so ~2735 teardowns per run churned the system catalog;
`run_tests.sh` probed `pg_total_relation_size('pg_class')` before each run and
restarted `test-db` once it passed a threshold, because a fresh database was the
only thing that recovered the speed. That probe has been **removed** -- with the
churn gone there is nothing for it to detect, and leaving it in would pay for an
8-second migration replay on nothing.

This document and `run_tests.sh` both claimed a single TRUNCATE cost "about
0.05ms against a fresh database" and ~124ms once degraded, "roughly 2500x".
**The 0.05ms figure is wrong and the ratio built on it was wrong with it.**
Measured directly 2026-09-06: truncating 90 tables on a genuinely fresh database
costs **76ms**. The degradation to ~124ms was real, but it was second-order --
the base cost always dominated, and 0.05ms was almost certainly measured against
**one** empty table rather than ninety. The practical consequence is that
TRUNCATE was never affordable here even at its best, so the fix was to stop
truncating rather than to keep resetting the database.

Still true from the old investigation, and worth not re-checking: user-table
bloat was never the cause (zero dead tuples across all 90 tables), coverage
instrumentation is not either (`--cov=app --cov-branch` costs about 1.4x), and
`VACUUM FULL` on `pg_class` did not recover TRUNCATE's speed.

Two alternatives that were measured and lost, recorded so nobody re-derives
them: truncating only the non-empty tables, found with an `EXISTS` probe,
measured **92.4ms**, and tracking dirty tables in-process with a
`before_cursor_execute` listener measured **135.3ms** -- both **worse** than the
124.5ms they were meant to beat, which is exactly why they are written down.
Both lose for the same reason: `TRUNCATE "user" CASCADE` re-expands to dozens of
tables, so subsetting buys nothing. **Provenance, because it is weaker than the
rest of this section's**: these two figures come from the same fresh stacks and
the same 629-test subset as the 124.5ms and 5.2ms above, but unlike those they
are **not** in `ec98595c`'s commit message -- they are recorded in the
sub-project 20 ledger, `.superpowers/sdd/2026-09-06-coverage-send-reply-20/progress.md`,
under its Task 7 entry, which is **gitignored**. A reader who cannot reach that
file has this paragraph and nothing else.

**A run over ten minutes is a broken environment, not a slow suite.**
`pytest.ini` sets `session_timeout = 600` alongside the per-test `timeout = 60`.
The per-test limit only ever catches ONE hung test; the session limit is what
catches a whole-run slowdown spread evenly over every teardown, which is what
the old TRUNCATE degradation was and which no `--durations` listing would have
shown. That particular cause is gone (see above), so if you hit the session
budget now, suspect the environment -- host CPU governor and power profile, and
`podman stats` -- not the tests. **A session timeout exits NON-ZERO -- pytest
exits 1 and `run_tests.sh` propagates it**, exactly as `pytest.ini:26-27` says.
Earlier revisions of this file claimed the opposite; that claim was measured
false on 2026-09-06 and the measurement is fact 118 below. What throws the
status away is **piping pytest's output**, which every campaign run does.
Still check the test count and the mtime of whatever the run was supposed to
write -- those catch more than a timeout does, including the silent case fact
117 records -- but check them **as well as** the exit code, not instead of it.

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

**Warning:** the `db_session` fixture deletes every row of every table after each
test, with FK triggers disabled -- so the *database* offers no resistance
whatever, and the only thing standing between that statement and a database you
care about is the name check below. `conftest.py`'s
`is_disposable_database_url()` refuses to run unless the database
name (the last "/"-separated path segment, with any `?query`/`#fragment` stripped)
ends with `_test` — a bare substring match on "test" is not enough, since that
would also accept real database names like `attestation` or a URL whose query
string merely mentions "test". Do not defeat that guard. `tests/test_conftest_guard.py`
covers it.

`tests/test_activitypub_util.py` used to be excluded from the standard run with
`--ignore=tests/test_activitypub_util.py`: it built its own app from its own
`TestConfig` (so no `db_session`, no `redis_double` and no
`block_outbound_http`), assumed a seeded developer database containing a user
named `rimuadmin`, and fetched a real community from the real piefed.social. It
now uses the shared fixtures like every other file here and runs in the standard
suite; **there is no `--ignore` any more.** Read its module docstring before
changing it: it records two findings about `find_actor_or_create_cached` that
the rewrite had to work around.

The dated plan documents under `docs/superpowers/plans/` still quote the old
`--ignore` command. Those are records of runs that really did use it, with the
pass counts of the day attached, so they are left alone -- the same rule this
file applies to any other citation anchored to a past reading.

## Auditing existing rows for a cross-host `ap_profile_id`

`app/cli.py`'s `find_cross_host_actors(session=None)` reports `User`, `Community`
and `Feed` rows whose `ap_profile_id` host disagrees with the row's own
`ap_domain`. It is read-only: it only queries and returns a list of
`(model_name, row_id, ap_profile_id, ap_domain)` tuples, and issues no `UPDATE`,
`DELETE`, or migration.

`actor_json_to_model`'s gate (see `app/activitypub/util.py`'s `host_of`) now
refuses to mint a new row like that; this function is how an existing database
is checked for rows that predate the gate. It reports and changes nothing --
what to do about anything it finds is a separate decision. Run it with:

    flask audit-cross-host-actors

or call `find_cross_host_actors` directly, the way `tests/test_audit_cross_host_actors.py`
does, to get the list back without printing it.

## Coverage

`app/request_hooks.py` is held at 100% branch coverage:

    ./run_tests.sh tests/ -q \
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

`atheris` is a TEST dependency and lives in `requirements-test.txt`, not
`requirements.txt`. `requirements.txt` is the production install path
(`Dockerfile`'s `builder` stage, `deploy.sh`, INSTALL.md) and atheris publishes
no aarch64 wheel, so an entry there makes `pip install -r requirements.txt` try
to build it from source -- needing clang with libFuzzer -- and fail outright on
any ARM64 host. The Dockerfile's `test` stage is `builder` plus
`requirements-test.txt`, and that is what `compose.test.yaml` builds, so the test
container still gets atheris while `runtime` (which copies `/venv` from
`builder`) does not. `tests/test_requirements_split.py` fails if that ever
inverts. `pytest`, `pytest-cov`, `pytest-timeout`, `respx`, `moto` and
`fakeredis` moved with it -- they install everywhere and were harmless, but a
rule with exceptions is not a rule.

Note that `compose.dev.yaml` still builds `builder`, so the DEV app stack no
longer carries pytest. Tests run through `./run_tests.sh` / `compose.test.yaml`,
which is where they always ran.

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
not fight. They never meet, because a bare `import atheris` installs no
instrumentation by itself -- only `atheris.instrument_imports()` (called from
`run_campaign.py`, which pytest never collects: it is neither named `test_*.py`
nor holds test functions) or `atheris.Fuzz()` turns on bytecode tracing.
`tests/test_link_parsers_fuzz.py` **does** `import atheris` at line 80 and
**is** collected by the coverage run -- but it only uses
`atheris.FuzzedDataProvider` as a structured bytes decoder (see Sub-project 1c
below) and calls neither `instrument_imports()` nor `Fuzz()`, so it installs no
instrumentation either. The conclusion still holds; the reason is that nothing
in the coverage run's collected files calls the two functions that actually
instrument, not that nothing collected imports atheris at all.

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

`src` is now scheme-checked too, and it is a SEPARATE decision from `href`, not
the same one widened. `app.utils.UNSAFE_SRC_SCHEMES` is `UNSAFE_URL_SCHEMES`
minus `data`: a `data:` href is a document the attacker wrote and navigates to,
while a `data:` src is decoded as an image in a non-scripted context and inline
`data:` images are ordinary federated content -- `sanitize_svg_bytes` already
keeps them for `image/jpeg|png|gif|webp|avif`. Both halves are pinned by
`TestSrcSchemeIsFilteredTheSameWayHrefIs` in `tests/test_allowlist_html.py`;
change one and that class must be changed with a reason. `UNSAFE_URL_SCHEMES`
is a blocklist and says so in its own comment, including why it is not an
allowlist yet.

`app.utils.sanitize_svg` is deliberately not a target. It opens a path, calls
`sanitize_svg_bytes` and writes the result back; all of its input handling is
`sanitize_svg_bytes`, which is fuzzed. Fuzzing a filesystem path would exercise
`open()`, not PieFed.

## The coverage ratchet

Per-module floors live in `coverage_floors.ini`. Floors only ever RISE; raising
one is a sub-project's deliverable, and lowering one to make a run pass defeats
the ratchet. A module with no entry is ignored, so unfinished modules block
nobody.

    ./run_tests.sh tests/ -q --cov=app --cov-report=json && \
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
it. It is currently 50%: the client-IP fix (`tests/test_client_ip.py`) and the
`back()` / `inbox_domain()` de-duplication (`tests/test_redirect_back.py`,
`tests/test_instance_domain_lookup.py`) raised it from 46 to 48, unifying
the ten `Referer`-handling routes onto `back()` behind one origin check
(`app.utils.is_safe_redirect_target`, `tests/test_safe_redirect_target.py`)
raised it to 49, and making that origin check's host rule admin-configurable
(`tests/test_redirect_policy.py`) raised it to 50. The microblog feed fix
(`tests/test_subscribed_feed_microblogs.py`, which drives `get_deduped_post_ids`
end to end rather than a copy of its SQL) took the measured figure to 60.1865
and the floor to 60 -- that raise banks unrecorded gains from the tasks between
as well as its own; the module measured 56.4300 immediately before it. Figures
here are quoted to four decimal places on purpose: 60.1865 rounds to 60.19 in
some places and 60.18 in others depending on the rounding, and two documents
disagreeing by a hundredth reads as a measurement error rather than a rounding
one.

Sub-project 1b-i's last task (the permission call-site audit,
`docs/superpowers/specs/2026-08-25-permission-callsite-audit.md`) re-measured it
at 60.2356. Rounded down that is still 60, so the floor did NOT rise there and
`coverage_floors.ini` was left alone -- the honest outcome of "set it to the
measured figure rounded DOWN" when the previous raise already banked the gain.
The floor was still exercised: set to 61 the ratchet exits 1 with
`app/utils.py: 60.24% is below its floor of 61.00%`, and back at 60 it exits 0
with `All 3 module floors met.` A floor nobody has seen fail is not a floor,
even on a task that does not raise it.

`app/models.py` measured 42.6908 in the same run and still has NO floor entry,
deliberately. Nothing has been scoped to cover it: 1a and 1b are `app/utils.py`
sub-projects, and the coverage `app/models.py` has today is incidental --
whatever the `app/utils.py` tests happened to drag in through fixtures. A floor
pinned to an incidental figure would break on an unrelated refactor that stopped
exercising a model path, which is the failure mode floors exist to avoid. It
should get its first floor from the sub-project that first targets it, measured
the same way. (An earlier note put this figure near 56.43; that number is
`app/utils.py`'s own reading immediately before the microblog-feed raise, not
`app/models.py`'s.)

`is_safe_redirect_target` MOVED CATEGORY with that last change (Ruling 17). It
used to be pure -- app config and string parsing, no database. It now reads the
`redirect_policy` setting through `get_setting`, and under the two
instance-matching policies queries the `Instance` table, so it is DB-backed and
belongs to 1b's category rather than 1a's. Its existing tests did not have to
move: they already ran under the `site` fixture. Anything that later partitions
`app/utils.py` by purity should count it on the DB side.

A consequence of that move, worth knowing before it is discovered: `back()`,
`referrer()` and `safe_redirect_target()` now touch the database on the redirect
path, so a request served while the session is in a failed state can raise out of
a helper that previously could not. The same-origin fast path returns before the
setting is read, so the exposure is limited to off-origin candidates.

### Sub-project 1b-ii: `app/utils.py` feed and query machinery

Sub-project 1b-ii (`tests/test_feed_sorts.py` -- which also carries the `top_*`
window tests, `tests/test_feed_visibility_filters.py`,
`tests/test_feed_display_preferences.py`, `tests/test_instance_stickies.py`,
`tests/test_possible_communities.py`, `tests/test_factories_feed.py`, plus new
factories in `tests/factories.py`) covered `app/utils.py`'s five feed and query
functions: `get_deduped_post_ids`, `post_ids_to_models`, `instance_sticky_posts`,
`get_instance_stickies` and `possible_communities`. It re-measured the module at
**67.1330%** (`percent_covered`, the blended statement+branch figure -- see "The
coverage ratchet" above) and raised the floor from 60 to **67**, rounded down. The
floor was proved to bite: set to 68 against that `coverage.json`, the ratchet
exits 1 with `app/utils.py: 67.13% is below its floor of 68.00%`; restored to 67 it
exits 0 with `All 3 module floors met.`

A fix round then closed two gaps the sub-project's own last-measurement claim had
missed -- see "Correcting a 100% claim" below -- taking the module to
**67.2558%** (still rounds down to 67, so the floor did not move again) and the
floor-bites proof was re-run against the new `coverage.json` with the same
result (68 fails naming the module, 67 passes).

The private-community fix (see "The private-community filter" below) took it to
**67.5178%** -- still 67 rounded down, so the floor stayed put a third time -- and
the bites proof was re-run once more against that `coverage.json`: at 68 the
ratchet exits 1 with `app/utils.py: 67.52% is below its floor of 68.00%`, and back
at 67 it exits 0 with `All 3 module floors met.`

The two private-community access-control fixes below ("The private-community
picker and post destination" and "The private community page") took it to
**67.6478%** -- 67 rounded down for a fourth time, so the floor still did not
move -- and the bites proof was re-run against that `coverage.json` too: at 68
the ratchet exits 1 with `app/utils.py: 67.65% is below its floor of 68.00%`, and
back at 67 it exits 0 with `All 3 module floors met.`

### Correcting a 100% claim

The sub-project's own design doc and task briefs stated all five target functions
were at 100% line and branch coverage except `possible_communities`'s two
documented-dead arms. That claim was carried forward across task reports rather
than re-derived, and it was wrong: `get_deduped_post_ids` had two real,
previously unnoticed gaps, on top of three legitimately out-of-scope ones. This
is the sixth hand-carried figure in this campaign to be wrong on re-derivation --
this time inside a brief rather than a report, which is exactly why "measure
fresh, never carry forward" is the standing rule.

The two real gaps, now closed:

- **The empty-`community_ids` early return** (`app/utils.py:3800-3801`). No
  earlier test called the function with an empty community list.
  `test_empty_community_ids_returns_an_empty_list_without_querying`
  (`tests/test_factories_feed.py`) covers it; neutralizing the guard makes the
  very next branch index `community_ids[0]` on an empty list and raise
  `IndexError`, which is what makes the mutation observable rather than merely
  changing a return value.
- **The Redis cache-HIT read path** (`app/utils.py:3809-3811`). Task 1 proved
  only the cache-WRITE side. `test_a_cached_result_id_is_served_without_reaching_the_database`
  primes a result_id's cached value through `redis_double` to something a live
  query could never produce, then asserts the STALE cached value comes back --
  discriminating a real cache hit from a test that would pass either way.
  `test_an_authenticated_call_with_an_empty_result_id_writes_no_cache_entry`
  closes the one remaining branch in this area (the False arm of the cache-key
  condition). It used to be named
  `..._still_writes_a_wasted_cache_entry` and asserted the OPPOSITE, proving the
  sub-project's suspected defect #1 was live: an authenticated call with an empty
  `result_id` wrote a key literally named `''` that the read path could never
  return. That defect is now FIXED -- see "The feed cache key" below -- so the
  test was replaced in place with the assertion that now holds, same call, same
  fixtures, same branch covered.

`get_deduped_post_ids` now carries exactly two documented-uncovered regions,
both legitimately out of this sub-project's scope: the `hashtag` filter
(`3841-3845`) and the unrecognized-`sort` fallthrough (`3957->3961`, mirrored in
`post_ids_to_models` at `3984->3986`). There used to be a third, the
private-community branch at `3868-3869` -- it stopped being uncovered when it
turned out to be a leak rather than a gap; see "The private-community filter"
below. Those line numbers moved by seven when the cache-key fix landed and again
when the private-community fix did, and are re-derived from `coverage.json`, not
carried forward. `instance_sticky_posts` and
`get_instance_stickies` are 100% statement and branch. `possible_communities`
carries its two documented-dead branches (`4362->4361`, `4369->4368`), unchanged.

### The feed cache key

`get_deduped_post_ids`'s Redis key is `feed:<user id>:<result_id>`, derived ONCE
into a local `cache_key` that governs both the read near the top of the function
and the write at the end, and set to `None` unless `result_id` is non-empty AND
the caller is authenticated. Read `tests/test_feed_cache.py` before changing any
of it; that file is the regression suite and carries the reasoning.

It was `result_id` alone, which is three defects in one line:

- **A cross-user leak.** `result_id` is CLIENT-CONTROLLED -- all three web callers
  read it from `?result_id=` and echo it back into the pagination links they
  render (`app/main/routes.py:83,161,163`; `app/feed/routes.py:419,484,487`;
  `app/topic/routes.py:38,107,109`), so a shared page-2 link is enough for two
  sessions to present the same one. The cached value is a list of post ids
  filtered for ONE viewer's authorisation, and `post_ids_to_models` re-filters
  nothing -- it renders whatever ids it is handed. So the key was, in effect, the
  access-control boundary for the whole feed, with no user component and a 24h
  TTL. It leaked in both directions: the second reader got the first reader's
  feed, and a chosen `result_id` seeded what a victim's next page would render.
- **Anonymous readers read it too.** The read guard was `if result_id:`, which
  does not exclude an anonymous session, so a logged-out request could be served
  an authenticated user's filtered ids.
- **A wasted write.** The read guard (`if result_id:`) and the write guard (`if
  current_user.is_authenticated:`) were different conditions, so the two RSS
  callers that pass a literal `''` still wrote a 24h key named `''`.

One derived key closes all three, because the read and the write can no longer
disagree. `feed:` collides with no existing namespace: the other
`redis_client` keys in `app/` are `pause_federation`, `cowbell`, raw activity ids
(`app/activitypub/routes.py:677-680`), `ban:<ip>`, `captcha_<uuid>`,
`votes_cast_<date>_<user id>` and `import:<user id>:<gibberish>`. `make_cache_key`
(`app/utils.py:224`) is a Flask-Caching key function over `request.url`, a
different store entirely.

No backward compatibility with pre-fix entries was attempted, deliberately: they
carry a 24h TTL and expire on their own.

### The private-community filter

Found while fixing the cache key, reported there as still open, and now FIXED.
`get_deduped_post_ids`' only `c.private` guard used to sit inside a walrus:

    if private_community_ids := community_membership_private(current_user.id):
        post_id_where.append('(c.private is false OR c.id IN :private_community_ids) ')

A viewer who belongs to no private community makes that guard falsy, so the
clause was never appended and NO private restriction applied to the query at all;
the anonymous branch appended none either. `Community.private` is invite-only real
access control ("only members can view. no federation.", `app/models.py:594`) --
not `Post.private`, the microblog marker. On the "All" feed (`community_ids=[-1]`)
the community filter is just `c.show_all is true` and `Community.show_all`
defaults to True, so every private community's posts were visible to any
anonymous visitor and to any authenticated viewer with no private membership.
The `local` and `popular` views were never affected: they build their own
`(c.private is false OR c.id IN ...)` at the caller
(`app/main/routes.py:124,131`) and pass it in as `community_sql`.

**Why exactly one of the seven walrus-guarded filters was wrong.** Six are
BLOCKLISTS -- blocked domains, blocked instances, blocked communities, blocked
users, communities banned from -- where an empty list genuinely means "block
nothing", so skipping the clause is correct and the walrus is right. This one was
an ALLOWLIST EXCEPTION: the empty list means "this viewer has no private-community
exceptions", and the base restriction must still apply. Same syntax, opposite
semantics. Do not "fix" the other six.

The fix hoists ONE unconditional site above the anonymous/authenticated split
(`app/utils.py:3860-3871`), so the base restriction cannot be lost by adding a
branch, and membership only widens it:

    if current_user.is_authenticated and (private_community_ids := community_membership_private(...)):
        post_id_where.append('(c.private is false OR c.id IN :private_community_ids) ')
        params['private_community_ids'] = tuple(private_community_ids)
    else:
        post_id_where.append('c.private is false ')

`tests/test_feed_private_communities.py` is the regression suite. Four tests, and
the set is what matters: three absence tests (authenticated non-member, anonymous,
member-of-A-not-B) would ALL pass for an over-broad fix that appended
`c.private is false` unconditionally with no widening -- i.e. one that hid a
private community from its own members. The fourth, the presence test, is the only
thing that rejects it.

Two consequences worth knowing. `tests/test_feed_cache.py`'s leak test
discriminates on `hidden_posts` rather than on private-community membership; that
was forced by the defect and is now merely a choice, so leave it as is.
And `TestTheMicroblogGateBindsToTheWholeCommunityDisjunct`
(`tests/test_subscribed_feed_microblogs.py`) had to have its fixture amended: its
viewer was deliberately a member of NEITHER community so the function appended no
private filter, which only produced rows because of this defect. The viewer is now
a member of the private community, which is also what
`app/main/routes.py:126` really implies -- that `community_sql` names the viewer's
OWN private communities, never an arbitrary private id. The paren-collapse
mutation was re-proved to still fail that class' first test after the amendment.

**The Redis-write decision (Task 1).** `get_deduped_post_ids` ends with
`redis_client.set(cache_key, ..., ex=86400)` for every authenticated call with a
non-empty `result_id` --
unbounded growth in the test Redis, which nothing but `--down` clears, and this
campaign forbids `--down` because it forces a replay of ~269 migrations. The
decision was to widen `redis_double` (`tests/conftest.py`) to also patch
`app.redis_client`, rather than add a bespoke fixture or delete keys in teardown.
This works because every `from app import redis_client` site in `app/` (there are
about fourteen) is an INLINE import re-executed on every call, unlike
`get_redis_connection`'s four bindings fixed at module-import time -- so one
`monkeypatch.setattr('app.redis_client', ...)` redirects all of them, with no
second binding problem to solve. Proved with two permanent tests:
`test_redis_double_covers_app_redis_client` (the fake instance actually receives
the write) and `test_authenticated_feed_calls_do_not_grow_the_real_test_redis`
(a second, unpatched Redis connection's `dbsize()` is unchanged across five
authenticated calls). This is the same defect shape sub-project 1a found in
Flask-Limiter's counters, caught before it could repeat.

**The sort-chain divergence (Task 2).** Three functions each dispatch on the same
seven `sort` values, and two of them disagree on `top`: `post_ids_to_models` and
`get_deduped_post_ids`'s raw SQL order `top` by `Post.score`, while
`instance_sticky_posts` orders it by `Post.up_votes - Post.down_votes`. In this
suite's default config the two are numerically identical, because `Post.vote()`'s
`SPICY_UNDER_10/30/60` amplification constants default to `1.0` and are unset in
`.env.test` -- at that value, `score` moves by exactly what `up_votes`/`down_votes`
move by. But the removal path is asymmetric with the addition path regardless of
config (`Post.vote()`, `app/models.py:2601-2716`: undoing a vote always subtracts
exactly `1` from `score`, even if adding it added an amplified `spicy_effect`), so
on any instance where those constants are not `1.0`, the two orderings drift apart
permanently once a vote is added and later removed. Reported as a design tension
between two intentional implementations, not fixed --
`TestTopOrderingDivergesBetweenImplementations` demonstrates the disagreement
directly, without mutating config.

Four things the next reader of this sub-project's tests needs, recorded at length
in `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`: coverage.py's
blindness to SQL-string predicates, the two-direction mutation standard, the
wide/narrow discriminator for chain-of-`continue` functions, and two findings
reported rather than fixed. Read that document before extending this sub-project's
tests or starting the one that covers the next slice of `app/utils.py`.

### The private-community picker and post destination

Two layers of the same defect, both pre-existing, fixed together.

`possible_communities` builds the community picker on the "new post" form. Its
"Others" query filtered only on banned / gone_forever / name, with no
`Community.private` predicate at all, so **every authenticated user was shown
every private community's title and `ap_domain`** -- and could select one as a
post destination. Nothing downstream stopped the post either: `can_create_post`
had no private check and no membership test, and `add_post` resolves the
Community straight from the posted form field.

Both layers had to change, and only one of them is authorisation. The picker is
a disclosure fix; the form field is client-supplied, so a hand-crafted
submission bypasses it entirely. `can_create_post` is what actually holds.

- **`possible_communities`** (`app/utils.py:4385-4386`) gained ONE unconditional
  filter on the Others query, base restriction widened by membership:

      filter(or_(Community.private == False,
                 Community.id.in_(community_membership_private(current_user.get_id()))))

  written as one filter rather than an if/else, for the same reason
  `get_deduped_post_ids`' equivalent is (above): a branch is a place the base
  restriction can later be lost. The Moderating and Joined groups needed no
  change -- both are membership-derived (`moderating_communities`,
  `joined_communities`, `app/utils.py:2565,2626`), and `community_membership_private`
  is a SUPERSET of both, asking for the same CommunityMember rows with only the
  `is_banned is false` condition.

- **`can_create_post`** (`app/utils.py:2362-2363`) gained the canonical check,

      if content.private and content.id not in community_membership_private(user.id):
          return False

  placed AFTER the `content.is_moderator(user) or user.is_admin(): return True`
  early return, alongside the `communities_banned_from` check, so the existing
  moderator/admin model is preserved rather than quietly narrowed.

**A divergence worth knowing, reported and NOT resolved.** That placement makes
`can_create_post` admin-bypassable for private communities, while the READ-side
checks built on the same helper are not: `app/post/routes.py:102` and
`app/api/alpha/views.py:309` refuse an admin who is not a member. The divergence
is pre-existing in the surrounding design -- every other gate in
`can_create_post` sits below the same early return -- and widening or narrowing
admin powers was out of scope for the fix.

`tests/test_possible_communities.py::TestPrivateCommunitiesInOthers` and
`tests/test_utils_can_post.py::TestCanCreatePostPrivateCommunity` are the
regression suites. **An existing test file changed its claims here**: that
module docstring enumerated the Others query's SQL predicates as exactly
banned / gone_forever / name -- rules 8-10, "three predicates" -- which encoded
the absence of the private filter as settled fact. It now enumerates four and
names rule 11, and says outright that rule 11's absence is what the defect was.

One test in that new class looks contrived and is not. `already_added` means a
private community the viewer belongs to is normally claimed by the Moderating or
Joined loop before the Others loop sees it, so the obvious "a member still sees
it" test never reaches the Others query and would pass even for an over-broad
`Community.private == False`. The one reachable path where a private member DOES
fall through to Others is `joined_communities`' extra predicate: it drops
communities whose instance the viewer has an `InstanceBan` against
(`app/utils.py:2636-2637`), while `community_membership_private` has no such
filter. `test_a_member_who_has_blocked_the_communitys_instance_still_sees_it`
drives exactly that, and is the only test that fails when the membership arm of
the `or_` is deleted.

### The private community page

`show_community` (`app/community/routes.py`) never checked `community.private`.
The only `.private` matches in its body were `community.private_mods` -- a
different column -- and a comment about the `Post` microblog marker. The RSS view
(`app/community/routes.py:720`) and the iCal view (`:784`) of the SAME community
have always aborted 403 on it. So a private community's feeds were forbidden
while its HTML page -- posts, sidebar, moderators, description, and the one a
browser actually reaches -- rendered in full for anyone.

The check is the canonical one, immediately after the existing `community.banned`
guard:

    if community.private and community.id not in community_membership_private(current_user.get_id()):
        abort(403)

**403, not 404, and the neighbouring 404 is why that needs saying.**
`show_community` aborts **404** for `community.banned`, so the two statuses now
sit a few lines apart. There is no convention in this codebase of using 404 to
avoid confirming a private community exists: every other private-community
refusal is a 403 or its API equivalent -- `app/community/routes.py:720` and
`:784`, `app/post/routes.py:96` and `:102`, `app/activitypub/routes.py:526`,
`:2124`, `:2153`, `:2756`, `app/shared/tasks/pages.py:153`, with
`app/api/alpha/views.py:309,614,640` raising `Private community - membership
required`. Nothing anywhere aborts 404 on `.private`. Matching the two sibling
views on the same community was the only consistent choice.

`current_user.get_id()` rather than `current_user.id`, because
`show_community` is reachable anonymously when the instance is not private, and
`get_id()` returns None there (the same form `app/feed/routes.py:467` uses).

Moderators are covered and are not locked out: `community_membership_private`
selects CommunityMember rows with `cm.is_banned is false` and NO role predicate,
while `moderating_communities` selects the same table with an extra
`is_moderator OR is_owner`, so the moderator set is a subset of the membership
set. Owners likewise are `is_owner` CommunityMember rows.
`tests/test_private_community_page.py` drives that path rather than asserting it,
and its member and moderator tests are the pair that rejects an over-broad
`if community.private: abort(403)` -- which passes both of the other two tests in
that file.

### Sub-project 1c: `app/utils.py` link parsers

`docs/superpowers/specs/2026-08-27-coverage-utils-links-design.md`. Six tasks
covered `app/utils.py`'s five link-related functions -- `domain_from_url`,
`remove_tracking_from_link`, `fixup_url`, `rewrite_href`, `apply_feed_url_rules`
-- and added a fuzz harness for the first three. Tests:
`tests/test_domain_from_url.py`, `tests/test_remove_tracking_from_link.py`,
`tests/test_fixup_url.py`, `tests/test_rewrite_href.py`,
`tests/test_apply_feed_url_rules.py`, `tests/test_link_parsers_fuzz.py`.

**The `domain_from_url` fix.** Before Task 1's fix (commit `81a3e40e`), line 1443
read `urlparse(url.lower().replace('www.', ''))` -- a blanket string replacement
over the WHOLE url, before parsing, removing every occurrence of `www.` rather
than a leading host label. `https://awww.evil.example/post/1` mangled to
`aevil.example`, colliding with the unrelated host `aevil.example` in the same
`Domain` row -- so banning one banned both. The fix moves the strip to AFTER
parsing and scopes it to a `startswith('www.')` check on the hostname alone.
**This changes future attribution only.** Existing `Domain` rows created under
the old mangling are not migrated or backfilled -- an operator who banned a
domain whose row was created pre-fix keeps that row's behaviour until a new row
is created for the corrected hostname. This is a deliberate choice: backfilling
would mean deciding what happens to `DomainBlock` rows, `post_count` totals and
posts already attributed to the mangled name, a data migration out of
proportion to a fix that just stops the bleeding. Recorded here so a later
reader does not mistake the absence of a migration for an oversight.

**A second fix for the same defect class: `url_needs_archive`.** While Task 1
was authorised and scoped to `domain_from_url`, the campaign found an
identical defect in `app/post/util.py`'s `url_needs_archive` (commits
`e0d08efc`, `3feab57c`; tests in `tests/test_url_needs_archive.py`) and it was
separately owner-authorised and fixed. Before the fix, line 277 read
`urlparse(url.replace('www.', ''))` -- the exact same blanket string
replacement over the whole URL, before parsing, that `domain_from_url` had.
Its severity differs from `domain_from_url`'s: the result feeds a membership
test against a hardcoded `paywalled_sites` list that drives a UI affordance
(whether to offer a `removepaywall.com` archive link), not `Domain` row
attribution or ban enforcement. It still failed in both directions:
`https://awww.nytimes.com/x` mangled to `anytimes.com`, a false negative (no
archive link offered for a genuinely paywalled host), and
`https://nywww.times.com/x` mangled to `ny` + `times.com` = `nytimes.com`, a
false positive (an attacker-registered host treated as paywalled and handed a
`removepaywall.com` link). The fix matches `domain_from_url`'s idiom: parse
first, then strip a `startswith('www.')` prefix from the hostname alone.
`tests/test_url_needs_archive.py` covers it with 11 tests, but the report is
explicit that only 2 of them discriminate the fix itself --
`test_an_interior_www_does_not_produce_a_false_positive` and
`test_www_prefixed_paywalled_host_still_needs_archive`; the false-negative test
does not discriminate on its own, since it returns `False` both before and
after the fix (for the wrong reason pre-fix), and the remaining tests are
regression guards for adjacent behaviour (exemptions, falsy input, the
hostless-URL path). Worth preserving, since it is the kind of
honest-negative claim that is easy to silently drop on the next pass.

That hostless-URL path used to be caught by a bare `except:` -- the third one
on this campaign, after `fixup_url`'s two -- carrying a comment calling it "a
separately reported defect". It was never reported. It is now
`except AttributeError:`, which is the only exception the two lines it wraps
can raise once `urlparse` has returned (`parsed_url.hostname` is None for a
string with no authority, and `None.lower()` is AttributeError; a str hostname
cannot fail `.startswith()` or the slice). The narrowing left behaviour
unchanged, which is what makes
`TestHostlessUrlReachesTheGuards`' two tests a check of it rather than a
restatement: each now fails when its OWN handler is deleted and is not caught
by the other's, where under the bare clause deleting `except ValueError:`
failed nothing.

**Measured and floor.** Re-measured (not carried forward -- see the standing
rule below) at **71.4776%** (`percent_covered`, the blended statement+branch
figure from `coverage.json`, same as every earlier ratchet entry here), up from
1b-ii's 67.6478%. Rounded down, the floor moved from **67 to 71**. Proved to
bite in both directions against that `coverage.json`: set to 72 the ratchet
exits 1 with `app/utils.py: 71.48% is below its floor of 72.00%`; restored to
71 it exits 0 with `All 3 module floors met.` All five functions measured 100%
statement and 100% branch coverage over their own bodies -- `domain_from_url`,
`remove_tracking_from_link`, `fixup_url`, `apply_feed_url_rules` and
`rewrite_href` -- with the caveat below about what 100% branch coverage does
and does not prove. That was measured at this sub-project's own commit; it is a
record of a past reading, not a claim about `app/utils.py` today.

A later re-measurement, taken fresh from the full suite rather than carried
forward, read **73.2449%** and moved the floor from **71 to 73**. That raise
banks the gains from the tasks between (the redirect-target audit and the
`url_needs_archive` narrowing among them) as well as nothing of its own; it is a
ratchet entry, not a sub-project. Proved to bite in both directions against that
`coverage.json`: set to 74 the ratchet exits 1 with `app/utils.py: 73.24% is
below its floor of 74.00%`; restored to 73 it exits 0 with `All 3 module floors
met.`

**No line numbers in this section, deliberately.** It used to give a range for
each of those five functions and four more for individual branches below. Every
one of them rotted, and the docstring that cited the same range for
`rewrite_href` was corrected three times and was stale again each time it was
committed -- see `tests/test_rewrite_href.py`'s docstring, which records the
whole sequence and carries the command that derives positions on demand.
Identify code here by name and behaviour; derive positions when you need them.

Two line numbers below are deliberately kept, and the difference is the rule
worth learning: "Before Task 1's fix (commit `81a3e40e`), line 1443 read
`urlparse(url.lower().replace('www.', ''))`" and its `url_needs_archive`
counterpart are anchored to a NAMED COMMIT and quote the exact source text.
They were never claims about HEAD, so HEAD moving cannot make them wrong, and
the quoted text makes them self-verifying. A bare number describing the current
file has neither property. Keep the first form; do not add the second.

**Fuzzing (`tests/test_link_parsers_fuzz.py`).** Targets `domain_from_url`,
`remove_tracking_from_link`, and fixup_url's YouTube-matrix parsing (never its
peertube branch -- the `/w/`-shaped path against a known peertube instance --
which performs a DB query and a live HTTP GET and is excluded by
construction). Property: no unhandled exception, and no host
confusion -- the output's host must be traceable to the input's host, at most
`www.`-stripped or youtu.be-aliased. Uses `atheris.FuzzedDataProvider` as a
structured random-bytes decoder over a fixed-seed `random` stream, NOT
`atheris.Fuzz()` -- the coverage-guided libFuzzer campaign machinery installs
its own bytecode tracing and must never run under `--cov` (see "Fuzzing"
above), and this file is collected by the very suite run that measures
coverage. 500 iterations per function (1500 total), comfortably under
pytest.ini's 60s per-test timeout (measured at well under a second per
function including `domain_from_url`'s DB round trips) while exercising every
generator branch many times over.

It found a real, unfixed defect, reported per this campaign's rule and NOT
fixed here: none of the three functions catches the `ValueError` Python's own
`urlparse` raises for a malformed netloc -- an unbalanced IPv6 bracket
("Invalid IPv6 URL"), a host that fails urllib's NFKC homograph-confusability
check, or (for `domain_from_url` specifically, one DB round trip further) a
hostname containing a literal NUL byte, which parses fine but is then rejected
by the Postgres driver as a query parameter. A submitted post link containing
any of these raises, uncaught, out of `domain_from_url`,
`remove_tracking_from_link` or `fixup_url` alike. The committed test catches
this specific, documented crash class around each function call so the suite
stays green while the search keeps running on every future run; anything else
escaping is a genuinely new finding.

**A bare `except:` blocks mutation testing (Task 3).** Both clauses in
`fixup_url`'s peertube branch -- the one around the JSON decode and the one
around the whole `get_request` -- USED TO catch everything, including
`KeyboardInterrupt` and `SystemExit`. Both were narrowed in `1d1f25be` (to
`(ValueError, TypeError)` and `httpx.HTTPError`), so the finding below is
history, not a description of the current file; it is kept because the lesson
outlived the defect. Concretely, as it stood then: with the netloc guard
mutated wide (`if True:`), the brief's own
`test_an_unknown_host_makes_no_request` passed unchanged, because with no
`http_mock` route registered `get_request` raised respx's own
`AllMockedAssertionError`, and the outer bare `except:` swallowed that
exception before it ever reached an assertion -- so the test's "makes no
request" claim was not actually being verified by that test alone. A bare
`except:` does not merely hide production failures; it makes the code it
wraps resistant to mutation testing, including mutations that trip
test-infrastructure errors rather than application ones. Reported, not fixed.

**Guard-level vs. dispatch-level mutations (Task 4, `rewrite_href`).** In an
`if`/`elif`/`elif`/`else` chain, a mutation to a GUARD inside an already-selected
arm's body (e.g. the `not community.is_local()` check, or the inner
`if post_reply:`) stays narrow, because every other arm has already been
foreclosed by the dispatch chain before that guard ever runs. A mutation to a
DISPATCH CONDITION itself (the `if`/`elif` tests that choose which arm runs,
e.g. the post rule's URL-shape check) goes wide, because it changes WHICH arm
is selected, stealing inputs that would otherwise have reached a different,
later arm's tests entirely -- the same chain-geometry hazard this campaign's
guidance already names for `continue` chains, applying identically to an
`if`/`elif` dispatch. Confirmed by pairing on the post rule itself: neutralizing
its own return arms while leaving its dispatch condition untouched (narrow)
failed 2 of 11 tests, both inside its own test class; over-broadening that same
dispatch condition to `if True:` (wide) failed 4 of 11, spanning three of the
other rules' classes with zero overlap against the narrow set. Radius alone is
not the signal -- the mutation's class determines the expected radius, and
pairing wide with narrow on the same rule
is what tells chain geometry apart from fixture coupling.

**Assert the message, not just the count (Task 5).** When several rejection
paths in the same function all return `False` and append one error to the same
list, a test asserting only `is False` plus an error count of 1 cannot say
WHICH path rejected -- two different guards can produce the same
return-value-and-count shape for the same input. `apply_feed_url_rules`'s dash
guard and its downstream regex guard both reject `'-'`
the same way for that reason; disabling the dash guard alone left the same
input failing the regex two lines later, so the mutation initially survived
undetected until the test was changed to assert the exact error message
(`'- cannot be in Url. Use _ instead?'`).

**The username-regex probe (Task 5) -- found here, since FIXED.**
`apply_feed_url_rules`'s private-mode branch built
`r'^[a-zA-Z0-9_]+(?:/' + current_user.user_name.lower() + ')?$'` by
interpolating `current_user.user_name` into a regex, unescaped. The
self-registration path was already closed:
`RegistrationForm.validate_user_name` (`app/auth/forms.py`) rejects any
username outside `^[a-zA-Z0-9_]+$` before it can reach a `User` row at all. But
`AddUserForm.validate_user_name` (`app/admin/forms.py`), the
ADMIN-created-user path, checked only for a literal `'@'` -- no charset
restriction -- so an admin-created username such as `a.b` reached
`current_user.user_name` with a live regex metacharacter. `a.b`'s `.` then
matched "any character" instead of a literal dot, so a private feed url
`myfeed/aXb` (not that user's real `<feed>/<username>`) wrongly validated
against user `a.b`'s optional-suffix group -- letting that user claim a
feed url in a namespace that reads as someone else's. `a(b` and `a[b` were
worse still: they made the pattern syntactically invalid, so `re.match` raised
`re.error` out of the form validator and the feed form 500'd.

**Both ends are now fixed, and only one of them is load-bearing.**
`apply_feed_url_rules` wraps the interpolated segment in `re.escape()`, and
`AddUserForm.validate_user_name` now applies the same charset
self-registration does, through the single shared
`app.utils.validate_user_name_charset` / `USER_NAME_CHARSET_RE`. The escape is
the half that holds unconditionally: any database may already contain a
metacharacter username created before the admin form learned to refuse one, and
new validation does not clean old rows. The admin check is defence in depth,
and closes the gap between the two user-creation paths.

**The admin path is deliberately IDENTICAL to self-registration, not more
permissive.** An admin-created user is an ordinary local `User` row downstream
-- same actor url, same webfinger, same feed namespace -- so there is no
consumer that could safely accept a wider charset from one path than the other,
and a "service account" name is expressible in `[a-zA-Z0-9_]` anyway
(`service_account`, `feed_bot_2`). `EditUserForm` has no `user_name` field, so
`AddUserForm` was the only admin route that set one.

`tests/test_username_regex_metacharacters.py` is the regression suite: the
foreign-namespace claim, the crash cases, the admin form, and -- the tests that
matter most -- the over-correction guards that fail for a `re.escape()` done
wrong or a fix that simply locked user `a.b` out of their own namespace.
`TestUsernameRegexMetacharacterProbe` in `tests/test_apply_feed_url_rules.py`
used to pin the defect deliberately (`test_username_regex_metacharacters_are_not_escaped`
asserted `result is True` and said so); it was inverted in place rather than
deleted, and its source-text assertion about `RegistrationForm` was replaced by
a behavioural one driving both validators.

**Coverage cannot see inside strings, again.** The same blind spot 1b-ii found
in a SQL predicate applies here to regex alternations and URL-matching string
literals: a `100%` branch reading on `apply_feed_url_rules` means the `if`
that builds the private-mode regex was taken, not that the regex it built is
correct for every username. The username-regex probe above is exactly that
gap made concrete -- and it is why "100% branch" is read throughout this
sub-project's write-ups as "every Python branch that runs", never as "every
string this code produces behaves correctly".

**`domain_from_url` has a second consumer, which matters for future mutation
runs.** `tests/test_link_parsers_fuzz.py` imports and calls `domain_from_url`
directly (it is one of the three functions the fuzz harness targets), which
makes it a second, independent test file exercising that function alongside
`tests/test_domain_from_url.py`. The whole-branch review's own mutation run
against `domain_from_url` found 3 failures across 11 files, and the fuzz file
was one of them -- proof its property check discriminates rather than merely
restating the implementation, and proof that a future mutation run scoped only
to `tests/test_domain_from_url.py` will under-count `domain_from_url`'s real
kill rate. Include the fuzz file in any future mutation run against this
function, or the run will report a weaker suite than actually exists.

**YouTube's URL formats have no specification.** `fixup_url`'s YouTube-shape
expectations (`/shorts/`, `/watch?v=`, `/playlist`, `/post/`, the five
YouTube hostnames) are derived from the formats the production code already
handles, and pinned as OBSERVED BEHAVIOUR in `tests/test_fixup_url.py` -- not
as a specification YouTube publishes or guarantees. WHATWG's URL Standard and
RFC 3986 govern the parsing underneath; YouTube's own path conventions do not,
and could change without notice.

### Sub-project 2a: `app/activitypub/util.py` actor and object ingestion

`docs/superpowers/specs/2026-08-27-coverage-activitypub-ingest-design.md`. Eight
tasks covered the six functions through which a peer's ActivityPub documents
first become rows in this database: `ensure_domains_match`, `find_community`,
`remote_object_to_json`, `verify_object_from_source`, `find_flair_or_create`,
and `actor_json_to_model` (split three ways, one task per actor branch --
Person/Service, Group, Feed). Tests: `tests/test_ap_ensure_domains_match.py`,
`tests/test_ap_find_community.py`, `tests/test_ap_remote_object_to_json.py`,
`tests/test_ap_verify_object_from_source.py`,
`tests/test_ap_find_flair_or_create.py`, `tests/test_ap_actor_json_person.py`,
`tests/test_ap_actor_json_group.py`, `tests/test_ap_actor_json_feed.py`.

Eight files, 290 tests. Derived, not counted by hand:

    ./run_tests.sh tests/test_ap_ensure_domains_match.py \
        tests/test_ap_find_community.py tests/test_ap_remote_object_to_json.py \
        tests/test_ap_verify_object_from_source.py \
        tests/test_ap_find_flair_or_create.py tests/test_ap_actor_json_person.py \
        tests/test_ap_actor_json_group.py tests/test_ap_actor_json_feed.py \
        --collect-only -q | tail -1

**This module's first floor is 35, and 35 is not a coverage grade.** The module
is very large and this sub-project deliberately covered six functions inside it,
not the module. Every one of those six is fully covered; the floor is low because
everything else in the file is still untested. Read the floor as "the six
ingestion functions are pinned, do not regress them", not as "this module is 35%
good". Per-function coverage, re-derived from the whole suite's `coverage.json`
by intersecting each function's own AST span against the file's
`executed_lines` / `missing_lines` / `executed_branches` / `missing_branches`:

| function | statements | branch arcs |
|---|---|---|
| `ensure_domains_match` | all covered | all covered |
| `find_community` | all covered | all covered |
| `remote_object_to_json` | all covered | all covered |
| `verify_object_from_source` | all covered | all covered |
| `find_flair_or_create` | all covered | all covered |
| `actor_json_to_model` | all covered | one arc missing |

The single missing arc is the false side of the `if` guarding the Feed branch's
post-commit re-fetch of the row it has just committed, looked up by its own
unique `ap_profile_id` in the same session. It is unreachable, and if it ever
were reached the statement after the guarded block dereferences the same `None`
anyway -- so the guard protects nothing. No pragma was added; the arc is left
visible.

**Dead code that no coverage number can surface.** The Feed branch builds
`ap_following_url` from a conditional expression that falls back to `None` when
the document has no `following` key. That else arm can never be taken: the same
key is read *unconditionally*, earlier in the same branch, to fetch the feed's
following collection, so a document without it has already raised `KeyError`
before the constructor runs. Nothing in a coverage report says so. Statements
read 100% because the line executes on every call, and coverage.py emits **no
branch arc at all for a conditional expression**, so the branch number is silent
too. It was found only by enumerating the branch's conditional expressions and
sitting down to write the absent-side test, at which point there was no absent
side to write. This is the sub-project's clearest argument for why the
present-and-absent discipline earns its cost: a `IfExp` is invisible to both
halves of the coverage number, and the only thing that interrogates it is a
human trying to construct both inputs.

The corollary for anyone reading a branch percentage on this file: arcs are
evidence about `if`/`elif` statements and `for` loops. They say nothing about
conditional expressions, of which the three `actor_json_to_model` branches
contain many. What covers those is one present-and-absent test pair per
expression, and the way to check that a pair actually discriminates rather than
merely executing both spellings is to mutate the guard in both directions.

**Patching `time.sleep` is not a mock of the code under test.**
`remote_object_to_json` and `verify_object_from_source` each retry twice, and
each retry path sleeps for three seconds before the second attempt. Tests patch
that out. Two separate bound names have to be patched, and missing the second is
the trap:

- `time.sleep` -- `app/activitypub/util.py` does `import time` and calls
  `time.sleep(...)`, so patching the attribute on the shared `time` module
  object reaches it.
- `app.utils.sleep` -- `app/utils.py` does `from time import sleep`, which is a
  *different* name bound in a different module and is not reached by patching
  `time.sleep`. It matters because `get_request`, which both functions call, has
  its own internal retry that sleeps a random 3-10 seconds. That retry nests
  inside the outer one, so a single "transport error twice" case can chain up to
  four unpatched sleeps.

Both are standard-library patches: they replace the clock, not the function being
tested. No branch is chosen, no return value is supplied, and no assertion is
made against the patch. Every mutation on the retry structure is still caught,
because what the tests assert on is the returned document. With both patched the
network-function files run in about a second instead of tens of seconds.

**Bare `except:` and mutation resistance -- what was actually observed here.**
Sub-project 1c established the general hazard in `fixup_url` (see "A bare
`except:` blocks mutation testing" above): a bare handler swallows respx's own
`AllMockedAssertionError`, so a test whose whole claim is "this makes no request"
passes under a mutation that makes the request. This sub-project looked for live
instances in `remote_object_to_json` and `verify_object_from_source`, which have
two bare `except:` clauses each. The honest result is mixed and worth stating
precisely, because the general principle over-predicts here:

- In both network functions, the bare handlers wrap only the `.json()` parse, not
  the fetch. The fetch is guarded by `except httpx.HTTPError`, and respx's error
  is an `AssertionError` subclass, so it is not caught there either -- nor by any
  of `get_request`'s five handler clauses (`httpx.InvalidURL`, `ValueError`,
  `httpx.ReadError`, `httpx.HTTPError`, `httpx.StreamError`). An unwanted fetch
  therefore still errors the test, which is what makes "register no route and let
  the suite-wide blocker prove no fetch happened" a valid proof in those two
  files. Every mutation aimed at those regions was killed.
- That is a property of how the tests were built, not of the handlers.
  `remote_object_to_json`'s tests deliberately serve the *wrong* branch a body
  that parses as valid JSON carrying a distinguishing marker, so an
  over-broadening mutation is caught by an observably wrong return value rather
  than by an accidental raise the bare handler could have absorbed. Shaped the
  other way -- serving unparseable bodies -- the same mutations would have been
  swallowed. The Feed task hit the mirror image of this and had to fix it: two
  status-code guards mutated to `True` **survived** because the helpers served
  JSON on non-200 responses, so the mutated code's `.json()` succeeded and
  produced the same empty list the unmutated code produces by skipping the block.
  Serving a plain-text body on any non-200 status killed both.
- The live instance of the 1c hazard in this sub-project is elsewhere:
  `make_image_sizes_async` wraps its `get_request` in a bare `except: pass`. Four
  mutations across the Group and Feed tasks -- deleting the icon and image resize
  guards -- could not be killed by any assertion, because the only observable
  effect of those guards is the fetch and the bare handler eats the harness's
  block. They are killed only by `http_mock`'s `assert_all_called=True` turning
  "a registered route was never fetched" into a teardown error. That is a real
  kill (the run is red), but it is the only signal available, and it is why the
  Person task's negative caching test can assert which `File` rows exist but
  cannot assert that no fetch happened.

The rule to carry forward: when a bare `except:` is in scope, work out whether it
wraps the *call* or only the *parse*, and check whether your own fixtures can
raise inside it. Do not assume either way.

**Nineteen defects, all reported and none fixed.** They are catalogued in
`docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` under
"Sub-project 2a", with the call-site analysis that fixed each one's severity.
Three of them share a shape named there as **partially-applied ingest**: a peer
document commits a row and then raises, so the caller sees an exception rather
than a return value while the row survives. If you touch ingestion in this file,
read that group first.

**A peer's document is not a peer's `server`.** Two of the analyses in this
sub-project reversed a predicted severity, and both reversals came from asking
where each compared string comes from rather than from reading the comparison.
A weakness that callers make unreachable is a different thing from one a peer can
drive, and the only way to tell them apart is to enumerate the call sites. Budget
for that: on this file it was the most expensive and the most load-bearing part
of every task.

**The SELinux relabelling trap.** Rewriting a test file by `mv`-ing a rebuilt
copy in from `/tmp` gives it the `user_tmp_t` label, and the test-runner
container then cannot read it -- `PermissionError: [Errno 13]` on a file whose
mode and owner are both correct. Write files in place, or relabel afterwards with
`chcon --reference=<a file that works>`. The failure looks nothing like its cause.

### Sub-project 2c: fixing fifteen of 2a's twenty defects

`docs/superpowers/plans/2026-08-28-fix-activitypub-ingest-defects.md`. Sub-project
2a wrote characterisation tests for twenty defects and fixed none of them, so 2c
began every fix from a test that was already red for the right reason. Fifteen
are now fixed. **Five are still open -- D6, D8, D18, D19 and D20** -- and the
register in `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`
carries the split, a commit per fixed row, and five new defects D25-D29 that the
fixing found and did not fix.

One test file was added, `tests/test_audit_cross_host_actors.py`, for the
read-only audit command described under "Auditing existing rows for a cross-host
`ap_profile_id`" above. Otherwise the eight files 2a wrote are the same eight,
with tests renamed where the behaviour they described stopped existing -- a Group or Feed
document missing a required key is now *refused*, not raised out of, so
`test_missing_unconditional_key_raises_key_error` became
`test_a_missing_constructor_key_is_refused` and asserts both `None` and a row
count of zero. If you are looking for a test by its 2a name and it is not there,
this is why.

**The floor did not move, and the reason is worth knowing before you re-measure
it.** `app/activitypub/util.py` came back at **35.653153153153156** against
35.06818181818182 when 2a set the floor -- 1583 of 4440, the **blended
statement+branch figure**, not a statement percentage. Rounded down that is still
35, so `coverage_floors.ini` was left alone, which is the honest outcome of "set
it to the measured value, rounded down". The floor was still exercised: at 36 the
ratchet exits 1 with `app/activitypub/util.py: 35.65% is below its floor of
36.00%`, and back at 35 it exits 0 with `All 4 module floors met.`

The direction is what a fixing sub-project should check here. Guards add branch
arcs, so a guard whose new arm no test exercises pulls the blended figure
**down**. If yours falls, you have shipped an untested guard -- go and find it.
This one rose.

**Three test gaps found by reviewers and not filled**, recorded so they are not
lost: pinning tests for the breadth of the Feed branch's two `except KeyError`
handlers (fixing D15 removed the only thing policing one of them, and the other
was never policed at all), a test for the `moderators: null` path that D17's
comment claims to cover, and `find_community` given a bad addressing element
followed by a good one -- the difference between "skips the bad entry" and "stops
at the bad entry", which is what the D3 fix actually claims.

### Sub-project 3: the resolve functions

`docs/superpowers/plans/2026-08-28-coverage-resolve-functions.md`. The three
functions sub-project 2b registered as D21-D24 and deliberately left untested:
`resolve_remote_post`, `create_resolved_object` and
`resolve_remote_post_from_search`. 92 tests across three files, and
`app/activitypub/util.py`'s floor rises **35 -> 45**.

Two of the three had never executed a line under test; the third had 25
statements covered incidentally. They are now at 12/12, 55/60 and 73/73
statements. Both remaining gaps are the same unreachable region and are
explained below.

**The fixture shape, and why every test here needs two halves.** These
functions fetch a document and then write rows, so a test needs a mocked HTTP
conversation *and* database state:

- `serve_remote_object(http_mock, uri, document)` registers the one route the
  fetch will hit. Nothing else is registered, so any *other* request the code
  makes is an unmatched request, which `block_outbound_http` raises on. The
  route set is therefore an assertion about how many fetches happen.
- `resolvable_remote_author(instance, name)` stamps `ap_fetched_at`, which is
  what makes `find_actor_or_create` a pure database read instead of an actor
  fetch. Without it, eager Celery runs `refresh_user_profile` inline and the
  actor fetch surfaces as an unmatched request.

**`assert_all_called` is a coverage check on your fixture, and it earns its
keep.** respx fails at teardown if a registered route is never fetched. Four
NodeBB guard tests here stored their row under the request URI, so the
function's *entry* existence check answered and no fetch ever happened -- four
tests that asserted correctly about nothing. Nothing else would have caught
that: not coverage, not mutation, not review. **If your test registers a route,
make sure you know why the code would reach it.**

**The near-duplicate hazard, and what the drift report concluded.**
`create_resolved_object` and `resolve_remote_post_from_search` contain the same
`attributedTo` walk and the same domain gate. Derived, not eyeballed: the two
walks are **byte-identical** once normalised through the AST. A third copy
lives in `verify_object_from_source` and sub-project 2b already fixed that one,
so the family is three functions wide with one member already correct.

That matters when you touch any of them:

- The fixed copy is the **fix template** -- `host_of` instead of raw
  `urlparse(...).netloc`, a dict arm that accepts a bare embedded Person
  object, and an `else` that returns a stated reason.
- The two unfixed copies **disagree with each other** about `inReplyTo`: one
  tests truthiness, the other `is not None`. A present-but-empty-string
  `inReplyTo` becomes a Post through one and a reply attempt through the other.
  Both behaviours are pinned, one per file, and the mutant that makes them agree
  fails a test. **Deduplicate deliberately or not at all.**

**The DEBUG-mode split needs a recorder, and here is why that is not laziness.**
`resolve_remote_post_from_search` calls the NodeBB reply task inline when
`current_app.debug` and through `.delay()` otherwise. Under this suite's eager
Celery, `.delay()` runs the task inline and propagates its exceptions exactly as
the direct call does -- measured, both settings raising the identical
`AllMockedAssertionError` from inside the task. The branch is behaviourally
inert here, so recording *which* call was made is the only honest way to pin it.
That is the single place in these three files where a test asserts on a stand-in
rather than on state.

**Why two coverage gaps are left, and when they close.** Five statements in
`create_resolved_object` and one branch arc in `resolve_remote_post_from_search`
are the reply-branch enrichment and the reply side of `if not in_reply_to`.
Neither can execute: **no remote reply can be created by either resolver.** Both
synthesise their activity without a `'type'` key, `PostReply.new` reads that key
unguarded where `Post.new` guards it, and `create_post_reply` swallows the
resulting `KeyError`. That is D35 in the register. Fix it and these paths become
testable for the first time -- including the surprising contract that a resolved
reply returns its *parent post*.

**Assert on what the path wrote, not on a row's identity.** Three tests in this
sub-project passed under mutants that broke the code they named, all with the
same shape. `Post.new` returns the **existing row** on a duplicate `ap_id`, so a
create is indistinguishable from an update by id and count; and a pre-stored row
lets an early existence check answer before the code under test runs. If you
pre-store a row here, ask which check answers first.

### Sub-project 4: the inbox gate

`docs/superpowers/plans/2026-08-28-coverage-inbox-gate.md` and
`docs/superpowers/specs/2026-08-28-coverage-inbox-gate-design.md`.
`shared_inbox`, its three route aliases (`site_inbox`, `user_inbox`,
`community_inbox`) and `replay_inbox_request` in
`app/activitypub/routes.py` -- the one place in the codebase a remote
instance's POST actually lands, and everything sub-projects 1 through 3
tested sits behind it. 40 test functions (two parametrized) across
`tests/test_inbox_gate_refusals.py`, `tests/test_inbox_gate_dispatch.py` and
`tests/test_inbox_gate_signatures.py`. D41-D46 in the campaign's defect
register.

**Signatures are real, not mocked -- `signed_inbox_post` (`tests/factories.py`)
is why.** Half this gate is signature verification, and
`HttpSignature.verify_request` is never patched anywhere in this suite.
`signed_inbox_post` builds its request with `HttpSignature.signed_request(...,
send_via_async=True)`, which returns `(uri, headers, body_bytes)` instead of
sending -- production's own signing code produces the headers, and the gate
verifies them with its own production code, no mock in between. The same
lever run backwards (a mismatched key, or `body=` tampering after signing)
produces the failure cases. `sender` must be built with `make_user(...,
with_keys=True)`: a keyless user fails at signing rather than at
verification, with an opaque `'NoneType' object has no attribute 'encode'`.

**A 200 alone asserts almost nothing here.** Six of this gate's twenty
outcomes return a bare 200: a missing required field, an Announce with a
malformed Mastodon-shaped object, an Announce of local content, a duplicate
activity id, a `Delete` of an unknown actor, and an actor that cannot be
found or created. Tests here assert the log line, the redis key, the absence
of a dispatch, or the row that was or was not written -- never the status
code alone.

**Redis activity-id uniqueness is a fixture hazard, not just gate
behaviour.** `shared_inbox` writes every activity's `id` into redis for 90
seconds (`routes.py:680`, `ex=90`) to suppress duplicates. `inbox_activity`
(`tests/factories.py`) therefore mints a fresh uuid into `id` on every call;
a test that hardcodes an id collides with any other test reusing it under
the same `redis_double` server and gets refused as a duplicate for a reason
that has nothing to do with what it meant to assert.

**Not covered: `process_inbox_request`'s body.** Same rule as sub-project 3
-- the dispatch call is asserted (which function, with which arguments, on
which `current_app.debug` branch), not what that function then does. That
remains a successor sub-project's scope.

**Test-harness gap found here: `block_outbound_http` did not know about
pyld.** Its docstring (`tests/conftest.py`) already named three network
escapes that bypass httpx entirely -- `urllib.request.urlopen`,
botocore/urllib3, and smtplib. `pyld`'s default JSON-LD document loader is a
fourth: `_default_document_loader = requests_document_loader()`
(`pyld/jsonld.py:6547`) reaches the network through `requests`, which respx
never touches. LD-signature verification calls `jsonld.normalize`, which
resolves `@context` URLs through that loader, so a test exercising the
LD-signature path without addressing this reaches the real internet in
`block_outbound_http`'s presence, silently. This sub-project's answer is the
`no_network_ld_signing` fixture (`tests/test_inbox_gate_signatures.py`),
which installs a static `jsonld.set_document_loader` override serving frozen
local copies of the two `@context` documents this gate ever needs
(activitystreams, security-v1) and asserts `requests.get` is never called
while it is active. `block_outbound_http`'s docstring now lists pyld/requests
as a fourth known escape and points at this fixture as the worked example.

## Every user-influenced redirect target

`is_safe_redirect_target` is the origin check. Three things reach it, and between
them they cover every place a user-supplied value becomes a redirect target:

- `back(default)` -- the `Referer` header. Ten routes. `tests/test_redirect_back.py`.
- `referrer(default)` -- the first usable of `?next=`, the posted `referrer`
  field, and `Referer`. ~29 call sites.
- `safe_redirect_target(candidate, default)` -- ONE candidate with a per-site
  fallback: `?next=` on the auth path, `?redirect=`, `?return_to=`, and the two
  places a posted `referrer` field is redirected to directly. 25 sites.
  `tests/test_redirect_targets.py`.

Sites whose fallback is expensive or has a side effect call
`is_safe_redirect_target` directly instead, so the default stays lazy --
`determine_next_page` commits `finished_onboarding` before choosing, and
`app/shared/auth.py`'s equivalent runs a query.

`tests/test_redirect_targets.py` also carries a SOURCE SCAN that fails if a new
site reads `?next=` / `?redirect=` / `?return_to=` / a posted `referrer` without
the check. It exists because the claim "one origin check for every
user-influenced redirect target" was made once and was false -- three `?next=`
sites and fourteen `?redirect=` sites were still unchecked when it was written.
The scan's `DOCUMENTED_EXEMPTIONS` lists every read that legitimately is not a
redirect target, with the reason, and a second test fails if an exemption names a
line that no longer exists. Read that dict rather than trusting this paragraph.

Deliberately NOT routed through the check, and why:

- `app/main/routes.py` `/anoobis` reads `?next=` but does not call `redirect()`;
  it renders a template whose script sets `location.href`, behind its own
  `furl`-based host test. Its separate defect -- `furl('http://[')` raises, so
  `?next=http://[` is a 500 -- is reported and left for its own change.
- `app/request_hooks.py` stores `Referer` in the session for the registration
  blocklist, and `app/auth/util.py:is_restricted_by_referrer` reads it back. That
  is an inverted substring test, not a redirect, and both were left alone.

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

## `Post.private` is the microblog marker, not a privacy flag

Three different tables have a `private` column and they mean three different
things. Do not reason from one to another.

- `Community.private` -- invite-only. Real access control.
- `PostReply.private` -- followers-only. Real audience restriction
  (`app/models.py:2866`, set from the reply's `to`).
- `Post.private` -- **set by `Post.new()` for any object with no `name`**, i.e.
  every ingested microblog (`app/models.py:1834-1835`, under the comment
  `# Microblog posts`). It is not access control.

`Post.new()` is the ONLY writer of `Post.private`, and `create_post()`
(`app/activitypub/util.py` ~2496) is the only caller of `Post.new()`.
`create_post()` refuses `followers` and `direct` object visibility before calling
it, so **no non-public object is ever stored as a Post at all** -- which is what
makes `Post.private` unusable as a privacy filter: there is nothing for it to
protect. Within microblogs it tracks ACTIVITY-level addressing, a second and less
reliable source of truth than the object-level check: a genuinely unlisted post
comes out `private=False`, while a public post arriving through
`create_resolved_object()`'s synthesised wrapper (no activity-level addressing at
all) comes out `private=True`.

`tests/test_post_private_is_only_the_microblog_marker.py` pins all of that,
including a `Post.new()` call-site scan (parsed with `ast`, because the name also
appears in three docstrings) that fails if a second caller appears without the
visibility refusal.

Where the marker is and is not applied:

- **The community's own listing and RSS feed do NOT filter it.** They used to, and
  the result was that `/c/microblogs@piefed.social` showed nothing while those
  same posts turned up in the subscribed feed. If a post is in a community and you
  are looking at that community, you see it.
  `tests/test_community_shows_microblogs.py`.
- **The aggregate feeds gate the COMMUNITY source only**, never the whole query
  and never the follow/boost disjuncts:

      ((community_disjunct) AND p.private is false) OR followed_author OR followed_booster

  So a microblog reaches an aggregate feed because you follow its author or its
  booster -- never merely because you subscribed to a community that carries it.
  `get_deduped_post_ids` previously dropped the gate for the whole query whenever
  `include_following` was true, which is how Mastodon posts reached the subscribed
  feed of a user following nobody. `tests/test_subscribed_feed_microblogs.py`
  asserts both directions.
- Discovery surfaces (search, tags, domains, user profiles) still filter it, and
  were not touched.

The three SQL fragments are module-level constants in `app/utils.py` --
`FOLLOWED_AUTHOR_SQL`, `FOLLOWED_BOOSTER_SQL`, `MICROBLOG_GATE` -- specifically so
tests import them instead of copying them.
`tests/test_feed_boost_visibility.py` used to hold a hand-transcribed copy of the
boost clause plus a test asserting the copy still appeared in
`inspect.getsource(get_deduped_post_ids)`. That guarded the fragment's own text
but not the structure around it: when the gate moved onto the community disjunct,
that test kept passing while its docstring described a line the function no longer
contained. Import the string; do not transcribe it.

Note also that `find_microblogging_community()` filters `instance_id == 1`, so it
only ever returns the LOCAL `microblogs` community. A remote aggregator community
such as `microblogs@piefed.social` is invisible to it, and to the `local` view's
exclusion built on it.

## Reaching a THEMED template from a test

Themes are not a template search path. `app/utils.py`'s theme-aware
`render_template` swaps only the ONE top-level template it is handed:

    if theme != '' and os.path.exists(f'app/templates/themes/{theme}/{template_name}'):
        content = flask.render_template(f'themes/{theme}/{template_name}', **context)

Jinja's `{% include %}` and `{% from ... import %}` take a literal loader path
with no theme awareness, so a themed page reaches its themed partials only
because it names them explicitly — `themes/dillo/index.html` includes
`themes/dillo/post/_post_teaser.html`, which imports
`themes/dillo/post/post_teaser/_macros.html`.

The consequence, which is easy to get wrong: a theme that overrides a MACRO file
but not the page that imports it is unreachable through that page. `dillo` ships
`index.html` but no `community/community.html`, so `/c/<name>` renders the base
`community/community.html`, which imports the BASE
`post/post_teaser/_macros.html` no matter what theme is selected. A test that
sets a theme and then requests `/c/<name>` silently exercises the main theme —
green, and testing nothing.

So: find which top-level template the theme actually overrides
(`find app/templates/themes/<theme> -name '*.html'`) and drive a route that
renders THAT. For dillo's post teasers the route is
`/home/<sort>/<view_filter>`. `/post/<id>` is unusable in this harness for any
theme: `app/templates/base.html:1` calls `csrf_token()` and TestConfig disables
CSRF.

Select the theme with `user.theme` on the logged-in viewer —
`app.utils.current_theme()` reads it ahead of `Site.default_theme`, so nothing in
the shared `site` fixture has to change. And assert something only that theme
emits (`themes/dillo/styles.css`, from `themes/dillo/base.html`) in a test of its
own, or the rest of the file cannot distinguish "the themed macro is correct"
from "the themed macro never ran".
`tests/test_dillo_video_teaser.py` is the worked example.

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
  test Redis. Add any fifth such import to the fixture's list. Also covered, as
  of the coverage-utils-feed sub-project: `app.redis_client` — a module-level
  global assigned by `create_app()` and read via `from app import redis_client`
  in roughly 14 modules (`grep -rn 'from app import.*redis_client' app/` for the
  current set; not listed here because such a list rots). Every one of those
  sites does the import inside a function body, re-executed on every call, so
  one `monkeypatch.setattr('app.redis_client', ...)` redirects all of them --
  see "The feed cache key" above for the test that proves it
  (`test_redis_double_covers_app_redis_client`,
  `tests/test_factories_feed.py`). Still not covered: the rate limiter and
  Celery app, built from `Config` at import time. **Also not usable as-is for
  a `redis_client.lock(...)` call site**: see "The fakeredis lock limitation"
  below (sub-project 5a) — the fixture's `fakeredis.FakeRedis` instance can
  acquire a lock but raises on release, so a test that reaches a `.lock(...)`
  context manager needs a narrower double, not this fixture directly.
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

- `disable_rate_limiter` — session-scoped and autouse: sets `limiter.enabled =
  False` for the run, restoring it afterwards. Flask-Limiter's storage is the
  test Redis (`CACHE_REDIS_URL`, db 1) with a one-day TTL on each counter, and
  that Redis survives everything except `--down`. So a limited route accumulates
  hits ACROSS RUNS: `/auth/login` is "30 per day", and after enough runs on the
  same day every further run gets `429 - Too Many Requests` from it. This was
  not hypothetical — the eight `TestLoginRouteEndToEnd` tests in
  `tests/test_redirect_targets.py` failed exactly that way at commit `a144f5ed`
  with no change to `app/` or `tests/`, the bucket for `198.51.100.201` holding
  37 with 82,896 seconds still to run. Per-request unique IPs
  (`from_a_fresh_ip`) only spread the accumulation over a few fixed addresses
  and delay it. Nothing in the suite asserts a 429, so nothing loses coverage;
  `tests/test_client_ip.py` exercises the limiter's key function directly, which
  `enabled` does not affect. If you ever DO want to test a limit, re-enable it
  inside that test rather than removing this fixture.

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

## The inbox-dispatch harness (sub-project 5a)

`app/activitypub/routes.py`'s `process_inbox_request` and its dispatch arms
were brought under test in `tests/test_inbox_dispatch_{preamble,announce,
votes,misc}.py`. Five facts this sub-project established, so slices 5b-5d
covering the rest of the module (and anything else that reaches
`redis_client.lock(...)`) do not have to rediscover them:

**1. The entry lever.** `dispatch(activity, store_ap_json=True)`, defined in
`tests/test_inbox_dispatch_preamble.py`, calls `process_inbox_request`
directly. This is not a testing contrivance — it is production's own DEBUG
branch, `app/activitypub/routes.py:758-759`, which calls the same function
directly instead of queuing it through Celery when `current_app.debug` is
true. Later slices should import `dispatch` from that module rather than
reimplement it.

**2. Why seeded rows reach the dispatcher.** `process_inbox_request` does its
work through `get_task_session()`, an independent `Session(bind=db.engine)`,
not through `db.session`. Rows seeded by the factories in a test are still
visible to it because the `db_session` fixture (`tests/conftest.py:137`)
cleans up by deleting rows rather than by rolling back a transaction, and the factories
`commit()`, so by the time the dispatcher's own session queries the database
the rows are durably there for any session bound to the same engine to see —
no transaction-visibility trick is involved.

**3. The request-context asymmetry.** `patch_db_session`
(`app/utils.py:3679`; the number was `:3664` and had drifted) only replaces
`db.session` when `has_request_context()`
is false. A direct call to `dispatch()` has no request context, so patching
occurs and `db.session` becomes a proxy onto the dispatcher's task session; a
real signed HTTP request (Task 8's seam tests) has a request context, so
patching does **not** occur, and the dispatcher's `session` local stays a
genuinely separate object from `db.session`. The two call paths therefore run
the dispatcher under different session arrangements. This is registered as
**D60** in `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`
— cite it rather than re-deriving it; that finding also lists the four
call sites inside `process_inbox_request`/`process_chat` that use the
`session` local directly and were never exercised under a request context by
this sub-project (`session.query(CommunityBan)` at routes.py:950,
`session.query(ChatMessage)` at routes.py:1319, 1739 and 2558).

**4. The fakeredis lock limitation.** `redis_double`'s `fakeredis.FakeRedis`
instance cannot serve a redis-py lock in this environment. fakeredis
(requirements-test.txt, unpinned; observed as 2.37.1 in this environment),
with no `lupa` installed, implements **no Lua scripting** — not `EVAL`, not
`EVALSHA`. Verified directly against the observed version:

```python
>>> fakeredis.FakeRedis(decode_responses=True).eval("return 1", 0)
redis.exceptions.ResponseError: unknown command 'eval'
```

`redis.lock.Lock.acquire()` needs no Lua (plain `SET NX PX`), so it succeeds
against the fixture, but `Lock.release()` calls a Lua script via `EVALSHA` to
atomically check the lock's token before deleting the key, so it raises
`redis.exceptions.ResponseError: unknown command 'evalsha'` on `__exit__`,
every time, for every `with redis_client.lock(...):` block. Task 6 hit this
on `process_question_answer` (routes.py:2475) as five real test failures
before isolating the cause, then confirmed it in isolation with the probe
above. The workaround is a narrow **local** double — not a change to
`redis_double` itself, whose lock behaviour is correct for what it patches
(`app.redis_client` in production code) and simply cannot be backed by this
particular fakeredis version. See `_RedisLockOnlyDouble` and the
`redis_lock_only_double` fixture in `tests/test_inbox_dispatch_votes.py`: it
patches the same single `app.redis_client` attribute `redis_double` teaches,
but with an object whose `.lock(...)` returns `contextlib.nullcontext()` — a
genuine no-op context manager, sufficient because none of these tests depend
on real mutual-exclusion semantics.

Sub-project 12 made `tests/test_ap_moderation.py` the **third** file to carry
one, for `delete_post_or_comment` — which takes **seven** locks, not the four
its plan claimed. That plan told nine tasks to use `redis_double`; the
implementer hit the `evalsha` failure on the first test, found the two
existing doubles, copied the `test_inbox_dispatch_votes.py` shape and reported
the plan defect rather than diverging silently. Registered as **D212**. If a
plan hands you `redis_double` for a function that locks, this is the fact it
was written against.

This will recur. `grep -rn 'redis_client\.lock(' app/` finds 34 call sites in
total, of which 2 are in `app/activitypub/routes.py` (routes.py:1872 and
:2476, the latter covered by Task 6's workaround above) — leaving 32 further
sites spread across `app/activitypub/util.py`, `app/models.py`, `app/cli.py`,
`app/post/routes.py`, `app/shared/user.py`, `app/shared/post.py` and
`app/user/utils.py`. Any coverage work that walks a call path through one of
them will hit this identically — reach for a `_RedisLockOnlyDouble`-shaped
local fixture, not a fix to `redis_double`.

**5. The mutation-evidence rule this sub-project learned.** A mutant killed
by `respx.models.AllMockedAssertionError` is an **infrastructure kill**, not
a behavioural one: it fires inside a blocked network fetch, before any
assertion in the test body ever runs, so it proves only that the mutation
caused an attempted fetch that the mutant-run's mocks did not expect — not
that the mutated guard has any semantic effect. Re-run such a mutant with the
fetch served (`federation_peer`/`http_mock` registering the route) before
claiming a guard is load-bearing. Task 2 recorded a case, earlier in this
campaign, where a guard that appeared to "kill" a mutant this way turned out
to have no semantic effect at all once the fetch was allowed to succeed.

**6. The new fixtures (sub-project 5b).** `make_feed(instance, name='peerfeed',
public=False, local=False, with_keys=False)` in `tests/factories.py` builds a
`Feed` the preamble's feed-only lookup can resolve. `public` defaults to
**False** to match `Feed.public`'s own column default
(`app/models.py:4062`, `db.Column(db.Boolean, default=False, ...)`), so a
test that needs a followable feed must pass `public=True` explicitly. Three
more factories back the membership arms:
`make_community_join_request(user, community, joined_via_feed=False)`,
`make_feed_join_request(user, feed)`, and
`make_user_follow_request(requestor, target)` — the last maps
requestor -> `user_id` and target -> `follow_id`, which is easy to get
backwards.

**7. How the membership arms select their target branch.** In the `Follow`
arm the target comes straight from `core_activity['object']`
(`routes.py:936`; `:938` is the `find_actor_or_create_cached` call two lines
later, not the assignment). In the `Accept` and `Reject` arms there is no such object
lookup — the branch is chosen entirely by what the **outer actor** resolves
to in the preamble, which tries community, then feed, then user in that
order (`routes.py:861-870`). A test therefore picks its Accept/Reject branch
by choosing the activity's `actor`, not its object — the opposite of Follow.
This is not obvious from reading either arm alone and cost time to
establish.

**8. Doubling the outbound sends.** All five `send_post_request` calls
touched by these arms are in the `Follow` arm (`routes.py:962, 980, 999,
1014, 1045`); `Accept` and `Reject` make none. `routes.py:12` imports
`send_post_request` by name, so a double must patch
`app.activitypub.routes.send_post_request` — patching
`app.activitypub.signature` leaves routes' own copy pointing at the
original. `record_sends` in `tests/test_inbox_dispatch_follow.py` is the
working example.

**9. The new fixtures (sub-project 5c).** `make_feed_item(feed, community)`
(`tests/factories.py:190-195`) and `make_feed_member(user, feed,
is_owner=False)` (`tests/factories.py:198-208`). `FeedMember.is_owner` and
`.is_banned` both default to **False** on the model (`app/models.py:4034-
4035`), which is what `Feed.subscribed()` (`app/models.py:4176-4185`) reads
to return `SUBSCRIPTION_MEMBER` rather than `OWNER` or `BANNED`.

**10. `record_moderation(monkeypatch, *names)`**, defined in
`tests/test_inbox_dispatch_lock_delete.py:115-137` and imported by the other
two 5c files. It doubles moderation delegates at their **routes-module
binding site** and returns `{name: [(args, kwargs), ...]}`. It cannot reach
one delegate this way: `do_subscribe` is imported *inline inside the feed-
member loop* in the `Add` arm (`routes.py:1421`, inside the `for fm in
feed_members:` loop starting at `:1415`), so it is not an attribute of the
routes module at all and must be patched at `app.community.routes.do_subscribe`
instead.

**11. How each arm selects its branch.** `Delete` (`routes.py:1264`) and
`Lock` (`routes.py:1360`) resolve their own targets from
`core_activity['object']` — `find_liked_object(ap_id)` at `routes.py:1310`
for Delete, `Post.get_by_ap_id`/`PostReply.get_by_ap_id` against
`core_activity['object']` for Lock. `Add` (`routes.py:1400`), `Remove`
(`routes.py:1473`) and `Block` (`routes.py:1595`) are split across two
paths, and which one runs depends on whether the activity arrived
Announced:

- **Announced path.** When the outer activity is `Announce`/`Accept`/
  `Reject`-typed, the **preamble** (`routes.py:861-870`) resolves the
  activity's *actor* as community, then feed, then user, in that order,
  before the inner `core_activity` is dispatched. `Add`/`Remove`/`Block`
  then read those already-resolved `community`/`feed`/`user` locals, so a
  test picks the branch by choosing the activity's actor.
- **Direct (non-Announced) path.** `Add` and `Remove` each fall back to
  `if not announced and not feed: community = find_community(core_activity)`
  (`routes.py:1403-1404` for Add, `:1476-1477` for Remove) — `find_community`
  (`app/activitypub/util.py:4544`) scans the activity's own
  `audience`/`cc`/`to`/`target`/`inReplyTo` fields, not anything the
  preamble resolved. `Block`'s community-ban branch falls back the same way:
  `community = community if community else find_actor_or_create_cached(
  target, create_if_not_found=False, community_only=True)`
  (`routes.py:1654`), resolved from `core_activity['target']`. So a
  direct-delivery test instead picks its branch by what the activity itself
  carries — `tests/test_inbox_dispatch_add_remove.py`'s
  `test_add_with_neither_community_nor_feed_resolvable_is_refused` and
  `test_remove_with_neither_community_nor_feed_resolvable_is_refused` are
  built this way, with no `Announce` wrapper, and their docstrings say so.

Neither path is obvious from reading a single arm in isolation, and getting
the two conflated cost time to untangle.

**12. Wrap resolvers rather than replacing them.** When a test needs
`find_actor_or_create_cached` to fail for one specific call, wrap the real
function and intercept only that call signature — replacing it outright
makes seeded rows non-load-bearing, because the double answers every lookup
regardless of what is in the database.
`tests/test_inbox_dispatch_add_remove.py:202-211` has the working example; a
5c review caught the wholesale-replacement version and it had to be
narrowed.

**13. If a suite run hangs, do not kill it.** A killed run leaves Postgres
backends idle-in-transaction holding locks, and a later run then blocks in the
`db_session` fixture's teardown — producing hangs and, once connections are
cleared, failures from a half-cleaned database that look exactly like real
regressions. Recovery is `./run_tests.sh --down` plus a rebuild, which
replays ~269 migrations. This cost 5c a long detour.
**THE ADVICE IS UNCHANGED BY THE 2026-09-06 TEARDOWN REWRITE; THE MECHANISM IS
NOT, AND THE DIFFERENCE IS NOT MEASURED.** As written, this fact described the
teardown `TRUNCATE` at `tests/conftest.py:143` blocking on a relation lock.
TRUNCATE takes `ACCESS EXCLUSIVE`, so *any* surviving backend — even one that
only ever ran a `SELECT` — blocked it. The teardown is now `DELETE`
(`tests/conftest.py:119-133`, executed at `:191`), which takes `ROW EXCLUSIVE`
and conflicts only at the row level, so a survivor that holds no uncommitted
writes no longer blocks it at all. **That is reasoned from the lock modes, not
measured** — nobody has reproduced the hang against the new teardown. Expect it
to still happen when the survivor died mid-test with uncommitted writes, which
is the ordinary case, and expect it to be less likely otherwise. Diagnose it
with the `pg_stat_activity` query in fact 18 either way.

**14. `inbox_activity`'s `**fields` is applied last.** `inbox_activity`
(`tests/factories.py:912-929`) builds its default dict — `id`, `type`,
`actor`, `object` (a bare string URI) — and then calls
`activity.update(fields)`, so any keyword a caller passes, `object=` very
much included, overwrites that default rather than being ignored or
colliding with it. A test that needs a dict-shaped or otherwise non-default
`object` passes `object=...` and relies on this ordering (sub-project 5d,
throughout).

**15. `Undo` dispatches on `core_activity['object']['type']`.** Every
`Undo` sub-type (`Follow`, `Delete`, `Like`/`Dislike`, `Announce`,
`ChooseAnswer`, `Lock`, `Block`) is selected by reading
`core_activity['object']['type']` (`app/activitypub/routes.py:1676`
onward), so a **string** inner `object` raises `TypeError` (subscripting a
string by `'type'`) before any sub-type is ever chosen — the arm cannot
reach a sub-type's own body with a string `object` at all. This is why
`Undo`/`ChooseAnswer`'s own `isinstance(core_activity['object'], str)`
branch was unreachable and removed as Fix 4 (sub-project 5d, D109 in
`docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`).

**16. Compare doubled delegates' captured objects by identity, not `.id`.**
When `record_moderation` (or an equivalent double) captures the arguments a
dispatcher-internal call was made with, those arguments are real ORM
objects loaded inside `process_inbox_request`'s own `get_task_session()`
session — closed (`finally: session.close()`) before `dispatch()` returns
to the test. **When an intervening `session.commit()` has expired them**
before the double captured them, a plain `.id` access on one of them
re-triggers a load against the now-closed session and raises
`sqlalchemy.orm.exc.DetachedInstanceError` — this is not automatic on every
captured argument, only ones a commit expired first: several assertions in
sub-project 5d's own tests read `.id` off captured arguments and pass
cleanly, because nothing committed between the object's load and the
double's capture of it (e.g.
`tests/test_inbox_dispatch_undo_content.py`'s
`test_undo_delete_restores_a_deleted_post_and_announces_it`, which reads
`restorer_arg.id` and `to_restore_arg.id` off `restore_post_or_comment`'s
captured arguments). Where a commit does intervene, use
`sqlalchemy.inspect(obj).identity[0]` instead — it reads the primary-key
tuple SQLAlchemy already stored on the instance's state at load time,
which survives both expiration and detachment. Established in sub-project
5c (`tests/test_inbox_dispatch_lock_delete.py:266-274`) and hit again
independently by two different tasks in sub-project 5d.

**17. Scope a `find_actor_or_create_cached` double to one URL.** The
preamble resolves the activity's own signed outer actor through
`find_actor_or_create_cached` (`app/activitypub/routes.py:871`, or `:862`
for `Announce`/`Accept`/`Reject`) before any arm — including `Undo`'s own
sub-type dispatch — ever runs. An unconditional double that makes this
function return `None` (or anything else) for every call therefore breaks
that preamble lookup too, so the activity never reaches the arm under test
at all; it short-circuits earlier with `'Actor was not a user or a
community'`. Capture the real function first and delegate to it for every
URL except the one the test wants unresolvable — sub-project 5d hit this
independently in Tasks 3 and 10 (`tests/test_inbox_dispatch_undo_follow.py`,
`tests/test_inbox_dispatch_undo_moderation.py`).

**18. Never run two pytest sessions against one test stack.** `db_session`
resets state by emptying every table after every test, which assumes
exclusive access to the database. Two concurrent sessions — a full-suite run
and a single-file run, say — corrupt each other in two ways at once: one
session's teardown deletes rows the other just committed, and
their identical seed values collide (`duplicate key value violates unique
constraint "ix_instance_domain"`, `Key (domain)=(peer.example) already
exists`). Worse, they can deadlock outright: one backend sits `idle in
transaction` while the other's teardown blocks, and
neither progresses. Observed twice on 2026-09-01, once costing about fifteen
minutes before it was recognised, and both times the failures looked like
real test regressions rather than contention.
**THE EXCLUSIVE-ACCESS ASSUMPTION SURVIVED THE 2026-09-06 TEARDOWN REWRITE
UNCHANGED, AND ONLY THE LOCK SHAPE MOVED.** This fact was written when the
teardown was `TRUNCATE ... CASCADE`; it is now `DELETE` with
`session_replication_role = replica` (`tests/conftest.py:119-133`). The
**corruption** half is word-for-word as true as before — deleting every row of
every table still deletes the other session's committed rows, and the seed
collisions are untouched. The **blocking** half changes mode: `ACCESS EXCLUSIVE`
became `ROW EXCLUSIVE`, so the block is now row-level rather than relation-level
and the wait shows as `Lock: transactionid` or `Lock: tuple` rather than
`Lock: relation`. Reasoned from the lock modes, **not** re-observed. The rule
does not soften either way: two sessions still destroy each other's data on the
first teardown, whatever they do or do not wait on.

To diagnose it, ask Postgres directly rather than guessing:

    podman-compose -f compose.test.yaml exec test-db psql -U pyfedi -d pyfedi_test \
      -c "select pid, state, wait_event_type, wait_event, now()-state_change as age
          from pg_stat_activity where datname='pyfedi_test' order by age desc;"

`state = 'idle in transaction'` next to another backend waiting on
`Lock`/`relation` is the signature. `podman restart pyfedi_test-db_1` clears
it; `run_tests.sh` then replays the migrations in about eight seconds.

**19. `Poll` is keyed by `post_id`, not a synthetic `id`.** `Poll.post_id`
(`app/models.py:3745`) is declared `primary_key=True` — a `Poll` row's
primary key IS the `post.id` it belongs to, with no separate `id` column at
all. This is why the Create/Update arm's poll-vote block resolves it with
`session.query(Poll).get(post_being_replied_to.id)` (`routes.py:1224`)
rather than a lookup by some other key — `.get()` on a single-column primary
key takes exactly the value that column holds.

**20. `Community.is_local()` returns `True` for every community
`make_community` builds, regardless of `host`.** `Community.is_local()`
(`app/models.py:778-779`) is `self.ap_id is None or
self.profile_id().startswith(SERVER_URL)`. `make_community`
(`tests/factories.py:122-151`) sets `name`, `title`, `instance_id`,
`user_id`, `ap_profile_id`, `ap_public_url`, `ap_followers_url`,
`ap_domain`, `subscriptions_count`, `local_only`, `nsfw` — **never
`ap_id`**. The column has no default, so it stays `None` on every
factory-built community no matter what `host` is passed, the `or`
short-circuits on its first disjunct, and `is_local()` returns `True`
unconditionally — the `host`-sensitive second disjunct is never even
evaluated. A test that needs a genuinely remote community must set
`community.ap_id` explicitly to a value that does not start with
`app.config['SERVER_URL']`, then commit, before dispatching. This cost a
fix round in sub-project 5e (Task 7): the brief's own plan to build a
"remote" community by passing `host='peer.example'` to `make_community`
produced a community indistinguishable from a local one, and a mutation
test on the Group-update permission guard's `community.is_local()` conjunct
would have silently escaped every test in the file had the `ap_id` fix not
been applied.

**21. `make_community` hardcodes `user_id=1` and `instance_id=1` against
real foreign keys.** `make_community` (`tests/factories.py:122-151`) never
takes a `user_id` or `instance_id` argument — it always writes `1` for
both, and `Community.user_id` carries a real `db.ForeignKey('user.id')`
(`app/models.py:543`) enforced by this suite's real Postgres test database.
Since `db_session`'s teardown leaves both tables
empty and both sequences back at 1 at the start of every test, calling `make_community(...)` before any
`User` row exists raises an `IntegrityError` on the factory's own internal
commit — before the code under test ever runs. A test must seed a `User`
(or an `Instance`, for the `instance_id` side) first, so it lands on id 1.
Sub-project 5e's Tasks 8 and 10 each hit this independently, in unrelated
files (`tests/test_inbox_dispatch_create_update.py` and
`tests/test_inbox_dispatch_preamble.py`).

**22. `Conversation.find_existing_conversation` joins `conversation_member`
twice, so it only finds a conversation where BOTH parties are members.**
The method (`app/models.py:254-272`) joins `public.conversation_member`
against itself as `cm1`/`cm2`, binds `cm1.user_id = :user_id_1` and
`cm2.user_id = :user_id_2`, and requires `cm1.user_id <> cm2.user_id`. A
conversation row with only one of the two users attached to it (for
example, one written by hand without appending both members) is invisible
to this lookup — it never matches either join. This is exactly why
`make_conversation` (`tests/factories.py:573-588`) appends both `sender`
and `recipient` to `conversation.members` rather than just the initiator.
The SQL is symmetric in `recipient`/`sender`: for a conversation whose
members are `{A, B}`, the query matches `(user_id_1=A, user_id_2=B)` via
`cm1=A, cm2=B` and equally matches `(user_id_1=B, user_id_2=A)` via
`cm1=B, cm2=A` — so callers do not need to worry about argument order.

**23. `User.accept_private_messages` defaults to `3` ("All instances"),
not to a refusing value.** The column (`app/models.py:1035`) is
`db.Column(db.Integer, default=3)`. A test of `process_chat`'s accepting
path that relies on the column default without setting it explicitly is
resting on a dependency the test itself never states — if the default
silently changed to `2` or `1`, such a test would fail (loudly, since `2`
and `1` both refuse a same-instance/trusted-instance sender that `3`
accepts), and nothing in the test would say why, since no line in it names
the value the assertion actually depends on. Seed `accept_private_messages`
explicitly even when the value you want happens to equal the default (see
`tests/test_inbox_dispatch_chat.py`'s `seed_chat_pair`, which always passes
`accept=` explicitly for this reason).

**24. `make_user` never sets `ap_domain`, unlike `make_community` and
`make_feed`.** `make_community` (`tests/factories.py:144`) and
`make_feed` (`tests/factories.py:178`) both pass `ap_domain=host` when
building the row.
`make_user` (`tests/factories.py:39-65`) sets `ap_id`, `ap_profile_id`,
`ap_public_url` and `ap_inbox_url` from `instance.domain` but never touches
`ap_domain`, which is left `NULL`. Code that branches on a sender's
`ap_domain` (for example `process_chat`'s fediseer.com exemption,
`app/activitypub/routes.py:2550`) will see `None`, not the instance's
domain, unless a test sets `user.ap_domain` itself after calling
`make_user`.

**25. `blocked_phrases()` is `@cache.memoize`'d, but the test config
disables the cache, so writing `Site.blocked_phrases` mid-test is
reliable.** `blocked_phrases()` (`app/utils.py:1735-1748`) is decorated
`@cache.memoize(timeout=86400)`. In production this would mean a change to
`Site.blocked_phrases` made after the function's first call in a process
would not be seen for up to a day. `tests/conftest.py:68` sets
`CACHE_TYPE = 'NullCache'`, so `cache.memoize` never actually caches
anything in this suite — a test that sets `Site.blocked_phrases` and then
dispatches an activity that calls `blocked_phrases()` sees the fresh value
every time, with no need to clear a cache or worry about call order.

**26. `Post.edited_at` and `PostReply.edited_at` have no declared default,
so asserting `is None` on them is not vacuous -- but pins the other side
explicitly.** Both columns are a plain `db.Column(db.DateTime)`
(`app/models.py:1706` for `Post`, `:2886` for `PostReply`), with no
`default=` at all -- unlike `ChatMessage.edited_at` (`:294`) and several
other `edited_at` columns in this file, which default to `utcnow`. A test
asserting `post.edited_at is None` after a fresh `make_post()` (or
`make_post_reply()`) is genuinely testing the column's absence of a value, not a
default the factory happened to reproduce. The other side of that same
lost-race check needs the opposite state seeded explicitly: a test proving
an `Update` does NOT re-apply because it lost a race to a `Create` must set
`post.edited_at` (or `reply.edited_at`) to a real, non-`None` timestamp
itself before dispatching -- nothing in the schema or the factory will do
it for you (`process_new_content`'s own guard reads exactly this:
`activity_json['type'] == 'Update' and post.edited_at is None`,
`app/activitypub/routes.py:2343`, and the `reply` equivalent at `:2391`).

**27. Reaching `process_new_content` on the DIRECT path requires doubling
both `find_community` and `ensure_domains_match`; on the ANNOUNCED path
both are structurally unreachable.** The Create/Update arm gates both
calls behind one condition, `if not announced and not community:`
(`app/activitypub/routes.py:1244-1251`) -- so an announced activity (or one
whose `community` the preamble already resolved) never reaches either
call, no matter how they are doubled. On the direct, un-announced path with
no community yet resolved, both must be doubled for a test to get past this
block cleanly: `find_community(request_json)` (`:1245`) supplies
`community`, and `ensure_domains_match(core_activity['object'])` (`:1249`)
must return truthy or the arm logs `'Domains do not match'` and returns
before `process_new_content` is ever called. On the ANNOUNCED path, the
community instead comes from the outer preamble's own, real
`find_actor_or_create_cached(actor_id, community_only=True,
create_if_not_found=False)` lookup (`app/activitypub/routes.py:862`) --
resolving the community row the test itself seeded -- which is why an
announced-path test must stamp that community's `ap_fetched_at` (`utcnow()`
or later) to suppress a genuine outbound fetch attempt, the same reason
`seed_content_pair`/`seed_chat_pair`-style fixtures stamp it elsewhere in
this suite.

**28. `shorten_string(s, n)` returns `s[:n-3] + '…'`, which is `n - 2`
characters long, not `n`.** The function (`app/utils.py:1595-1602`) takes
97 characters of the input plus one ellipsis character (`'…'` is a single
Unicode code point, so `len()` counts it as 1) when `len(s) > n`. For
`n=100` that is **98** characters, not 100 -- a test asserting a truncated
length against this function must use `n - 2`, not `n`, or it fails against
a passing implementation. Confirmed a second time at a different limit by
sub-project 12: `shorten_string(reason, 255)` in `ban_user` measures **253**.
The other wrong form to avoid is `endswith('...')` -- the ellipsis is the
single code point `U+2026`, not three ASCII dots, so that assertion fails
against a correct implementation too. (The definition has moved since this
fact was written; locate it by name, not by the line numbers above.)

**29. Stopping `run_tests.sh` on the host does not kill pytest inside the
container -- check for and kill survivors before starting another run.**
Cancelling a run from the host leaves the container's pytest process alive,
still holding the test database. Starting a second run then puts two
pytest sessions against one Postgres instance at once (see fact 18 above),
and the second run's `db_session` teardown can block for many
minutes -- nearly ten, once -- behind the survivor's `idle in transaction`
session. (That observation was made against the old `TRUNCATE` teardown; see
fact 13 for what the 2026-09-06 `DELETE` rewrite does and does not change about
it. Killing the survivor is the remedy either way.) The image has no `kill`
binary, so list and kill survivors through
the venv's own Python:

    podman exec pyfedi_test-runner_1 sh -c 'ls -d /proc/[0-9]*| while read d; do tr "\0" " " < "$d/cmdline" | grep -q bin/pytest && echo $d; done'

lists any `/proc/<pid>` directory whose `cmdline` contains `bin/pytest`.

**Correction: this probe false-positives on its own subshell.** The `sh -c
'...'` process running the probe carries the literal argument string
`grep -q bin/pytest` in its own `/proc/<pid>/cmdline`, and that string
contains the substring `bin/pytest` -- so the loop matches and reports the
probe's own PID as a "survivor" every time it runs, even with nothing else
alive. Both "survivors" reported once in this project turned out to be this
false positive; neither vanished because a real process was killed, they
vanished because they had never existed. Fix the probe (exclude its own
`$$`, or grep for the more specific `/venv/bin/pytest`) or, at minimum,
verify each reported PID by reading its `/proc/<pid>/cmdline` before
killing anything -- a real pytest survivor's cmdline names the test files
and options it was invoked with, not a `grep` argument.

For each PID found, kill it with:

    podman exec pyfedi_test-runner_1 /venv/bin/python -c "import os, signal; os.kill(<pid>, signal.SIGKILL)"

Then confirm the database has no lingering `idle in transaction` backends
before starting the next run (see fact 18's `pg_stat_activity` query).

**30. Inverting a defect-pinning test vacates the branch that pin used to
cover -- check what the inversion just lost.** A test that pins a defect by
asserting the buggy branch was taken is also, incidentally, that branch's
only coverage. Flipping its assertions to prove the fix moves the test onto
the *other* branch and silently drops the one it left, and the suite can
stay fully green while a real branch loses its last test. This happened for
real in sub-project 7 (2026-09-02): inverting
`test_an_instance_admin_cannot_edit_a_reply` into
`test_an_instance_admin_can_edit_a_reply` left no test anywhere in
`tests/test_inbox_dispatch_new_content.py` reaching the reply-edit permission
guard's `else:` refusal branch (`app/activitypub/routes.py:2381-2383`,
`'Edit attempt denied'`) any more -- every remaining reply-side test passed
the three-disjunct guard by one disjunct or another. It was caught only by a
reviewer deliberately asking, after every inversion, "which branch has no
test left now?" and repaired with a new test built to isolate that guard.
Ask the same question after every inversion.

**31. `make_feed` (`tests/factories.py:154-188`) sets `ap_id` unconditionally
-- even at `local=True` -- so no Feed it builds is reachable by any lookup
that filters `ap_id=None`.** `make_local_feed` (`tests/factories.py:191-210`)
exists for exactly this: it never sets `ap_id`, so the column stays `None`
and the row is visible to an `ap_id=None` filter. `make_feed`'s `ap_id` is
load-bearing elsewhere -- `find_remote_actor` branches on the `/f/` substring
in `ap_profile_id` -- so this is a sibling factory, not a change to it. Use
`make_feed` when a test needs a Feed resolvable as a REMOTE actor; use
`make_local_feed` when it needs to be resolvable as a LOCAL one (e.g. by
webfinger).

**32. `requestor_domain()` (`app/utils.py:5736-5743`) reads the `User-Agent`
header's `+URL` comment, not a doubled function.** It splits on `'+'`, takes
the last segment, strips a trailing `')'`, and parses the host out with
`furl`. A route guarded by it (webfinger's allowlist/ban checks) is driven in
a test with a `User-Agent` header carrying that shape (e.g.
`'Mastodon/4.2 (+https://evil.example)'`), not with a monkeypatched double --
the header IS the interface.

**33. A filter clause whose value equals what the factory always produces
cannot be killed by any mutation, for any test using that factory
unmodified.** The fix is always the same shape: a test that sets the field
explicitly to something contrary to the factory's default, never one that
trusts the default to already differ. This sub-project's own ledger hit it
twice: the Community lookup's `ap_id=None` clause was unkillable by any
`make_community`-built row, since `make_community` never sets `ap_id`
(closed by `test_a_community_with_a_non_null_ap_id_is_not_served`, which sets
`ap_id` explicitly post-construction); the Feed lookup's identical
`ap_id=None` clause, present at both of its textually-duplicated occurrences,
was unkillable by any `make_local_feed`-built row for the same reason and was
closed the same way. State the rule generally -- it is the most transferable
lesson here, and it recurs across factories, not just this one.

**34. `webfinger` returned HTTP 200 for a not-found actor, a malformed
resource, and a successful lookup alike, before this sub-project's fixes --
a status-only assertion could not tell them apart, and that blindness
disarmed a test.** `test_a_user_is_matched_by_alt_user_name`, written to
isolate the `alt_user_name` disjunct, originally asserted only
`status_code == 200`; dropping the disjunct still returned 200 (the pinned
not-found defect, at the time), so the mutation survived. The fix is the
same for every positive-resolution test: assert on the response body or
`response.json`, never on the status code alone, unless the test's entire
contract is refuse-or-don't (the allowlist/ban guards, correctly, do this).
This is now **partly historical**: the not-found and malformed-resource
fixes (D143, D144 in the findings register) mean a miss is 404 and a
malformed request is 400, so status alone now discriminates those two from a
200. It still does not discriminate WHICH actor a 200 resolved to, so a
positive-resolution test must still assert on the body.

**35. `CACHE_TYPE=NullCache` under test (`tests/conftest.py:68`,
`.env.test:11`) makes every `@cache.memoize`/`@cache.cached` decorator inert
-- which is why no test in this suite clears a cache, and why a
cache-staleness defect can only ever be registered, not demonstrated, from
inside this harness.** `process_webfinger_request` is
`@cache.memoize(timeout=60)` (`app/activitypub/routes.py:74`); in production,
`CACHE_TYPE` defaults to `FileSystemCache` (`config.py:38`) and the memo is
live there, but no test config in this repository can exercise that.

**36. `SERVER_URL` resolves to `https://test.piefed.local` under test.**
`HTTP_PROTOCOL` defaults to `'https'` (`config.py:52`) and is not overridden
by `.env.test` or `TestConfig`, so `create_app` (`app/__init__.py:132-135`)
builds `SERVER_URL` from `f"{HTTP_PROTOCOL}://{SERVER_NAME}"` with
`SERVER_NAME = 'test.piefed.local'` (`tests/conftest.py:69`). Confirm this
rather than re-deriving it. Separately: of webfinger's three actor lookups,
the Community lookup lowercases the actor (`actor.strip().lower()`,
`app/activitypub/routes.py:122`) and so does the User lookup
(`func.lower(...)`, `:118`), but the Feed lookup does not (`actor.strip()`,
`:126`, `:130`) -- a case-sensitivity asymmetry a mixed-case query can
observe directly (`test_a_user_is_matched_case_insensitively`,
`test_a_feed_name_lookup_is_case_sensitive`).

**37. A test in this suite cannot log in after making an earlier request in
the same test.** Flask-Login's `_get_user()` (`flask_login/utils.py:367-374`)
caches the loaded user on `g`: `if "_login_user" not in g:
current_app.login_manager._load_user()`, then `return g._login_user`. `g` is
bound to the Flask **application** context, and `tests/conftest.py`'s `app`
fixture (`:72-73`) is `scope='session'` and pushes exactly one application
context for the whole run with `with application.app_context(): yield
application` (`:112-113`) -- the `db_session` fixture's own docstring notes
this (`:124`, "The `app` fixture pushes one app context for the whole test
session"). `db_session` clears `g.__dict__` before **each test function**
(`:137`), so `g` is fresh at the start of every test, but nothing clears it
**between two requests inside the same test**. So a test that issues an
anonymous first request caches `AnonymousUser` on `g._login_user`, and every
later request in that same test -- session cookie, `session_transaction()`
login and all -- reads the same cached anonymous user back; the login
silently does nothing. This defeated a pin in sub-project 9 that looked
correct, authenticated via `session_transaction()`, and passed for two whole
tasks before commit `d661a12b`'s own investigation (`app/activitypub/routes.py`'s
`user_profile`) discovered the admin branch the pin was written to reach had
never actually run authenticated. A test that must authenticate should log in
**before** its first request, not after.

**38. `Vary` is never absent and never bare on a response this application
sends.** Flask-Compress's `after_request` hook
(`flask_compress/flask_compress.py:222-229`, confirmed empirically in
sub-project 9's Task 3) unconditionally appends `Accept-Encoding` to the
`Vary` header of every response, whether or not the view itself set one.
**"Unconditionally" is literal in two directions that matter, both re-read at
source in sub-project 11's Task 2:** the `Vary` append is the *first* thing
`after_request` does (`:225-229`, the function itself opening at `:222`), before
any mimetype, size or
`COMPRESS_MIN_SIZE` test, so it is not gated on the response being large enough
to compress; and `compress.init_app(app)` (`app/__init__.py:203`) is not gated
on `TESTING`, so this is production behaviour observed in the harness, not a
test-environment artefact. A view that sets
`Vary: Accept` therefore yields `'Accept, Accept-Encoding'`, and a view that
sets no `Vary` at all still yields `'Accept-Encoding'` alone, never a missing
header. Assert the full value (`response.headers.get('Vary') ==
'Accept-Encoding'`, or `'Accept,' in ...`), never `'Vary' not in
response.headers` -- that assertion cannot pass against any real response
from this app and will misread "the view forgot to add Accept" as "the
header is absent".

**39. `icon_id`, `image_id`, `avatar_id`, `cover_id` are real foreign keys to
`file.id`** (`app/models.py:541-542` `Community`, `:989-990` `User`,
`:4057-4058` `Feed`, and similarly on `Post`/`PostReply`/`Feed` elsewhere in
the file) -- a bare `= 1` on one of these columns raises `IntegrityError`
against the test database unless a `File` row with that id actually exists.
Seed a real `File()` row and commit it first. Setting only the `_id` column
is not enough either: the model's own image method (`avatar_image()`,
`icon_image()`, ...) reads the `_id` column to decide whether to return a
real URL or a placeholder, so the accompanying image method must also be
doubled to return the URL the test expects -- the guard and the renderer are
two different things and both must be opened.

**40. An optional block guarded by `if <obj>.<x>_id is not None:` needs an
absence test for THAT outer guard, not only for the URL-shape branch
inside it.** `Community.icon_image()`/`header_image()` (`app/models.py:642`,
`:669`), `User.avatar_image()`/`cover_image()` (`:1188`, `:1203`)
and `Feed.icon_image()`/`header_image()` (`app/models.py:4097`, `:4124`)
each independently guard on the same `_id` column internally and fall
through to a placeholder (`avatar_image()`/`header_image()`-style methods
return `''` or a static placeholder path) rather than raising when the
column is `None`. So if the *outer* `if ..._id is not None:` guard in the
route is deleted, the block runs unconditionally, the image method's
*inner* guard still returns a harmless-looking placeholder, and a bogus
`icon`/`image` key is added to every response with no error and no visibly
wrong value -- undetected by every test that only sets the column present
and checks the URL branch. Test the column's *absence* explicitly and
assert the key is missing from the response body.

**41. Three relationships in this codebase attach in three different
shapes; read the model rather than generalising from a sibling.**
`Community.languages` (`app/models.py:622`) is `lazy='dynamic'` over a
secondary (association) table but takes plain `.append()`.
`User.extra_fields` (`:1072`) is likewise `lazy='dynamic'` (over
`UserExtraField`, `:3556`, with `cascade="all, delete-orphan"`) and also
takes `.append()` directly. `Feed.children` (`:4091`,
`db.relationship('Feed', remote_side=[id], backref=db.backref('children',
lazy='dynamic'))`) is a `lazy='dynamic'` **backref** over a plain
`parent_feed_id` foreign-key column (`:4064`) with no association table --
attach a child by setting `child.parent_feed_id = parent.id` directly and
committing, never `.append()` on `parent.children`. All three report as
`lazy='dynamic'` if inspected loosely, which invites assuming they share one
attachment shape; they do not.

**42. A mutation that strips a clause from two call sites at once proves the
column, not the query.** If the same filter clause appears at two textually
identical call sites (e.g. a local lookup and a fallback lookup both filtering
`deleted=False, banned=False`), deleting the clause from both at once will be
sole-killed by a test even if only one of the two call sites is what the test
actually reaches -- the kill is real but its attribution is ambiguous. Mutate
one call site at a time; a clause that turns out to be unkillable at one site
alone (commonly because a factory-built row can never reach that site's query
in the first place -- see finding 33 above) needs its own dedicated test, not
credit borrowed from its sibling site's kill.

**43. A view that crashes surfaces to a test as a RAISED EXCEPTION, not as a
500 response.** `tests/conftest.py:64` sets `TESTING = True` and never
overrides `PROPAGATE_EXCEPTIONS`, so Flask re-raises rather than converting the
exception to a 500, and the test client's default `raise_server_exceptions=True`
lets it escape the `client.get(...)` call entirely. A test written as
`assert response.status_code == 500` against a crashing view therefore **never
runs its assertion** -- the request line raises first, and the test fails as an
error with the view's own traceback. Pin a crash with `pytest.raises(<ExcType>,
match=...)` wrapped around the request instead, and match on wording specific to
the *mechanism*, because different mechanisms crash differently: a view that
falls off the end and implicitly returns `None` gives Flask's `TypeError: The
view function ... did not return a valid response`, while a view that
dereferences an unresolved row gives `AttributeError: 'NoneType' object has no
attribute '<column>'`. Established empirically in sub-project 10 with a scratch
probe, after a brief that assumed a 500 response would be observable.

**44. A crash-to-404 inversion can only be mutation-killed by a CRASH-kill; an
assertion-kill is structurally impossible for that shape.** When a fix turns a
crashing view into a 404 and its pin is inverted from `pytest.raises(...)` to
`assert response.status_code == 404`, restoring the defect makes the request
raise *before* the assertion is ever evaluated. The kill is real and can be
sole, but it is a crash-kill by construction. Do not read that as a weaker
result and do not go looking for the assertion-kill this campaign usually
prefers -- it cannot exist here. Label which shape the kill is (the campaign's
convention) and say why. Finding 30 covers the separate question of which branch
an inverted pin stops covering.

**45. SQLAlchemy's legacy `Query.all()` deduplicates entities, and can mask a
malformed join completely.** `Query._iter`
(`sqlalchemy/orm/query.py:2859-2879`, installed 2.0.52) calls `result.unique()`
**unconditionally** whenever the query returns mapper entities, and the
per-entity unique filter (`sqlalchemy/orm/loading.py:184-196`) keys on Python's
`id()` of the mapped object -- which the Session's identity map makes the *same*
object for every row sharing a primary key. So a query whose `ON` clause
produces a cartesian product returns the right number of *objects* through
`.all()` and the wrong number through raw SQL or a 2.0-style `select()` without
`.unique()`. A plausible row count is therefore not evidence the query is
correct: compare all four forms -- raw SQL, `select()`, `select().unique()`,
`Query.all()` -- before concluding anything about a join. (This is not the
stricter `multi_row_eager_loaders` path at `loading.py:281-291`, which raises
unless the caller calls `.unique()`; that one needs `joinedload`/
`contains_eager`, not a plain `.join()`.)

**46. A "do not fix X" constraint scopes the CODE, not the prose about it.** A
task told to leave a defect alone still owns every docstring, comment and report
sentence that describes it. A docstring made false by a *sibling* task -- "not
exercised by a test in this file yet", when a later task added exactly that test
-- is a defect the constraint never covered, and leaving it is a bug, not
obedience. Two such claims shipped in sub-project 10 and needed a follow-up
commit. When correcting one, make the replacement **name** the test that now
carries the fact, so the next falsification is greppable rather than a matter of
re-reading the file.

**47. `Feed` and `User` carry AP URL columns that have no defaults, that no
factory sets, and that routes use directly as the response `id`.**
`Feed.ap_followers_url`, `Feed.ap_following_url` and `Feed.ap_outbox_url`
(`app/models.py:4106`, `:4107`, `:4114`) and `Feed.user_id` (`:4079`) all have
no declared default; `make_local_feed` (`tests/factories.py:191-210`) sets none
of them, and `make_feed` -- which sets `ap_id` unconditionally, see finding 31 --
sets none of them either. `User.ap_followers_url` (`:1070`) is the same, and
`make_user` never sets it. This bites twice. `feed_outbox` and `feed_following`
use `feed.ap_outbox_url` / `feed.ap_following_url` **directly** as the response
document's `id`, so it comes back `null` unless the test assigns it. And
`feed_moderators_route` does `db.session.query(User).get(feed.user_id)`, which
with `user_id` unset is `.get(None)` -> `None` -> `AttributeError` on the very
next line's `moderator.ap_profile_id`. `seed_local_community` by contrast gives
every community `user_id=1` for free (finding 21), which invites assuming feeds
behave the same way; they do not.
`tests/test_ap_collections.py:_seed_local_feed` sets all three URL columns as a
single contract for the feed collection tests -- `user_id` is still each test's
own job.

**48. `community_moderators` synthesises an owner, so a factory-built community
can NEVER have an empty moderators collection.** `app/utils.py:2889-2899`
appends an **unpersisted** `CommunityMember(user_id=community.user_id,
is_owner=True, community_id=community.id)` whenever the community's own
`user_id` is absent from its `is_owner OR is_moderator` query results; the
object is constructed and never added to the session, so it has no `id` and no
backing row. `seed_local_community` and `make_community` always set `user_id=1`
(finding 21), and no `CommunityMember` row is ever created for that user, so the
synthesis fires on **every** seeded community. A "community with zero
moderators" test is therefore impossible through these factories, and any
exact-equality assertion on `orderedItems` must account for the always-present
phantom owner -- prefer a `not in` form for exclusion tests, which still kills
the `OR` filter's mutation without going vacuous. `community_members`
(`app/activitypub/util.py:54-58`), which the followers collection uses, has no
such synthesis -- a plain `SELECT COUNT(*)` over persisted rows -- so exact
counts *are* safe there, and a deliberately loose `>= 1` should be tightened to
`== 1`.

**49. When a count in prose changes, grep for every spelling of the OLD value,
not just the new one.** Adding a ninth endpoint to sub-project 10 mid-flight
left two already-merged docstrings saying `community_featured` has "seven
siblings" without a `Cache-Control` header, *after* a cleanup pass had grepped
for and fixed every occurrence of "eight". "Seven siblings" and "eight
endpoints" encode the same fact and neither grep finds the other. Grep the old
numeral, the old word, and the off-by-one on either side of it.

**50. An assertion comparing a response value to the object's OWN column is
vacuous whenever the factory never sets that column.** The no-vacuous-assertion
rule is usually stated about *declared defaults* -- do not assert a value equal
to a column's default without seeding a contrary baseline. This is the same
failure with no default involved at all. `Feed.user_id`'s owner is rendered by
`feed_moderators_route` as `moderator.ap_profile_id`, and
`test_a_feed_moderators_collection_lists_its_owner` asserted
`orderedItems == [owner.ap_profile_id]` -- which reads as a strong assertion and
is one, right up until you notice `make_user(local=True)`
(`tests/factories.py:58-60`) leaves `ap_profile_id`, `ap_public_url` and `ap_id`
all `None`. The assertion compared `[None] == [None]` and would have passed
against a `public_url()` regression, which is precisely the divergence its own
docstring documented (D183). **So: check the FACTORY, not just the model.** A
column with no declared default is not thereby safe; it is the case the usual
phrasing of the rule does not cover. The fix shape is to set the column and its
near-twin to *different* values and assert the literal, plus an explicit `!=`
against the twin -- then a mutation swapping one rendering for the other dies on
the assertion instead of passing silently.

**51. An instance-block guard is UNREACHABLE without a `+`-style `User-Agent`,
and a test that omits one measures the wrong branch while looking correct.**
Fact 32 says how `requestor_domain()` parses the header; this is what happens
when the header is not there. `requestor_domain()` (`app/utils.py:5736-5743`)
returns the empty string unless the `User-Agent` contains a `'+'` (`:5739`);
`find_instance_id('')` returns `None` at its own `if not server:`
(`app/activitypub/util.py:2065-2066`); and `User.has_blocked_instance(None)`
returns `False` at `if instance_id is None:` (`app/models.py:1467-1469`). So
`if author.has_blocked_instance(find_instance_id(requestor_domain())):` --
the shape used by `comment_ap` (`app/activitypub/routes.py:2152`) and `post_ap`
(`:2181`) -- is **always False** for a request that sent no `+URL` suffix, no
matter what blocks are seeded. A test that seeds an `InstanceBlock`, omits the
`User-Agent`, and asserts 200 will pass, and will go on passing if the guard is
deleted entirely. Send `'Test (+https://blocked.example)'` and assert **401** to
measure the guard; send a plain agent only when the *absence* of the block is
the thing under test, and say so in the docstring. Registered as D194.

**52. `find_instance_id()` CREATES AND COMMITS a sparse `Instance` row for a
domain it does not know, so a test that names a domain it did not seed silently
gets a brand-new id.** `app/activitypub/util.py:2064-2086`: on a miss it builds
`Instance(domain=server, software='unknown', inbox=f'https://{server}/inbox',
created_at=utcnow())` (`:2074`), `db.session.add`s and `db.session.commit`s it
(`:2077-2078`), and returns the new id. Nothing about the call site announces
this -- it reads like a lookup. The consequence for a test: send
`'Test (+https://evil.example)'` without a `make_instance(domain='evil.example')`
in the same test and the guard is evaluated against an instance id that exists
but that the author has not blocked, so the test measures the **not-blocked**
branch while its name says "blocked". **Every domain named in a `user_agent=`
argument must have its own `make_instance()` in the same test**, which is the
convention `tests/test_ap_content_objects.py` follows throughout. The write
itself is registered as D193.

**53. `has_blocked_instance(id)` and `has_blocked_instances()` differ by one
letter and drive different branches of the same endpoint.** `User.has_blocked_instance(instance_id)`
(`app/models.py:1467-1471`) asks whether **this** instance is blocked and gates
the 401; `User.has_blocked_instances()` (`:1473-1475`) is a global "does this
user block anyone at all" flag and gates whether the response carries `Vary:
Accept, User-Agent` instead of `Vary: Accept`. `comment_ap` calls both
(`app/activitypub/routes.py:2152` and `:2157`), and so does `post_ap` (`:2181`,
`:2191`). Three consequences for tests: a test for the 401 must seed a block on
the *requesting* domain; a test for the `Vary: Accept, User-Agent` branch needs
only *some* block, on any instance; and a test that seeds one block and asserts
both is not discriminating -- swap either method into the other's branch and it
still passes. Seed a block on a **different** instance from the one requesting
to separate them, as `test_a_comment_is_served_when_the_author_blocked_a_different_instance`
and `test_a_comment_from_a_blocking_author_varies_on_user_agent` do.

**54. A coverage figure identical to the previous sub-project's is a STALE-FILE
symptom, not a result -- check the artefact's mtime, not just its contents.**
Sub-project 11's first coverage run hit the 600s session-timeout wall at 2316 of
~3290 tests, so it never wrote `scratch_full_cov.json`, and yesterday's file was
still sitting there. The figures read out of it were byte-for-byte
sub-project 10's own ending numbers -- which is impossible after 42 new tests
closing 89 previously-uncovered statements, and is therefore the tell. Read
plainly they would have raised the floor against a measurement predating the
entire sub-project, **and the floor check would have passed**, because a stale
number is a self-consistent number. Two mechanical defences, both cheap: `ls -l`
the JSON and confirm its mtime is after the last commit under measurement, and
refuse any figure from a run whose output does not end in a pytest completion
line. When the wall is the cause, it is usually podman stack degradation -- the
same run took 192.56s on a stack freshly torn down with `./run_tests.sh --down`
(fact 29 and the process notes; the teardown replays ~269 migrations, so it is
not a casual step).

**55. A running total in a plan is invalidated by any mid-flight ruling that
adds or removes a test, and the invalidation is SILENT.** Every later task's
"Expected: N passed" stays internally consistent while being wrong by the same
constant, so nothing in the plan contradicts itself and the error surfaces only
when an implementer counts. It happened twice in one sub-project: once
pre-flight (every count one too high, caught by the pre-flight scan) and once
mid-flight, when a fix-round ruling ordered a seventh test to close a
combinatorial gap and every downstream count silently went stale. **Whoever
orders the addition owns correcting the counts downstream of it** -- and an
implementer who finds the stated count off by one should report the discrepancy
rather than invent a test to reach it, which is the failure this fact exists to
prevent. Compare fact 49, which is the same class for counts embedded in prose.

**Corollary, added by sub-project 14, which exercised it six times in nine
tasks: an expected test count is an ESTIMATE, never a reason to ship an unproven
conjunct.** The failure above is a count that is too high; this is the same fact
in the other direction. Sub-project 14's Task 1 found two conjuncts its brief's
six tests could not kill and declined to add tests, because the brief said "6
tests added" and "verbatim". That deference is backwards -- **the count is
DERIVED from the requirement, so letting it override the requirement inverts
them** -- and once the norm was stated, six of the nine remaining test tasks
exceeded their brief's estimate for the same reason, each time closing a guard
the brief's own count would have left unproven (Task 3 by one, Task 4 by five,
Task 6 by six, Task 7 by five, Task 8 by one, Task 9 by twelve). The rule an
implementer needs: **add the tests, and report the discrepancy in the same
breath**; a plan that states deltas rather than running totals lets a task grow
without invalidating anything downstream.

**56. `log_incoming_ap` writes NOTHING unless `LOG_ACTIVITYPUB_TO_DB` is set,
and it is off in every test run.** The function
(`app/activitypub/util.py:4534`) guards its whole body with `if
current_app.config['LOG_ACTIVITYPUB_TO_DB']:` (`:4538`), and `config.py:92`
reads `os.environ.get('LOG_ACTIVITYPUB_TO_DB') or False` with no override in
`tests/conftest.py` and none in `.env.test`. So **any assertion about an
`ActivityPubLog` row is vacuous by default**: it passes against a function
that logged correctly, against one that logged the wrong thing, and against
one whose logging was deleted entirely. Every branch of the inbox delegates
ends in a `log_incoming_ap(...)` call, which makes the log row the most
tempting observable in the file and the one that proves least. Assert the real
effect the branch produced -- the row it deleted, the counter it moved, the
modlog entry it wrote -- and if a branch's *only* observable is the log row,
say so and get a ruling rather than writing the vacuous assertion.

**Corollary, from sub-project 15, which is the ruling for that last case:
turning the flag ON is a legitimate fixture, and it obliges you to assert the
EXACT message.** `create_post_reply` has seven head guards that each do nothing
but log and `return None`, so each is indistinguishable from the others *and
from a deleted guard* until the flag is on. A session-scoped `app` fixture
(`tests/conftest.py:72-73`) means the fixture must set
`app.config['LOG_ACTIVITYPUB_TO_DB'] = True`, yield, and **restore it** -- a
leaked `True` changes behaviour for every later test in the process, surfacing
as an unrelated test writing unexpected `ActivityPubLog` rows. **And then a
substring assertion gives back the exact ambiguity the fixture was bought to
remove.** Two mutants survived a brief's own `'... in log.exception_message'`
assertions and were killed only by tightening to equality, for two different
reasons worth knowing apart: **(a)** two guards' messages shared a substring
and deleting the outer one let the *inner* one fire and write its own row
("Unable to find parent post/comment" and "Could not find parent post" both
contain "parent post"); **(b)** a callee raised an exception whose message the
function's own tail handler logged, near-identical to the head guard's
("Replier blocked" from `PostReply.new` against the guard's "Post author
blocked replier"). In both, a substring match **passed on the wrong row**.
Assert `log.exception_message == '<the exact string>'`, and check the message
is unique in the file before relying on it. (Fact 56's guard has since moved to
`app/activitypub/util.py:4573`, inside `log_incoming_ap` at `:4569-4587`.)

**57. Assigning an undeclared attribute to a SQLAlchemy model instance is
silent in BOTH directions -- it is never persisted and it never raises.** It
sets an ordinary Python attribute on the instance, the surrounding `commit()`
succeeds, and the value is simply gone on the next load. Sub-project 12 found
this in production code that had shipped: `site_ban_remove_data` contained
`blocked.reply_count = 0`, and `User` has no `reply_count` -- it declares
`post_count` and `post_reply_count`, while `reply_count` belongs to `Post`
(registered and fixed as **D201**; a second, never-called instance in
`app/models.py` is **D210**). Two consequences for testing. **Grep the model
for a counter name before trusting it**, in production code and in your own
fixtures alike -- `grep -n 'reply_count' app/models.py` would have settled it
in one command. And note the kill type: a mutation restoring such a line dies
**by assertion, not by crash**, because the model carries no `@validates` hook
and no `__setattr__` override to turn the write into a raise. A defect that
crashes is found on first execution; this class has to be read to be found,
which is why it survives next to a working sibling line.

**58. `expire_on_commit` is default-`True` in this app, so a commit inside the
function under test expires the session's objects and post-call attribute
reads re-fetch.** `app/__init__.py:81` constructs `SQLAlchemy(session_options={"autoflush": False}, ...)`, overriding **only** `autoflush`. So a
`db.session.refresh(obj)` added to a test in order to observe an update made
by raw SQL is usually a no-op: the function's own `commit()` already expired
`obj`. Verified empirically -- the line was removed, the test run twice, and it
passed both times. Keep the refresh if you like (it costs one redundant SELECT
and keeps the assertion correct if `session_options` ever changes), but **do
not write a docstring calling it load-bearing**: that is a claim about the
session configuration, not about the SQL, and it was false here.

**59. A pair of do/undo functions needs a ROUND-TRIP test, in one test
function, with the starting values recorded.** Asserting that the undo leaves
a counter alone proves nothing unless the do moved it, and asserting that the
do moved it proves nothing about whether the undo gives it back. Sub-project
12's most consequential finding (**D200**) is invisible to either
single-direction test and obvious to the round trip: `delete_post_or_comment`
decrements four counters for a reply and `restore_post_or_comment` increments
two, so a moderator who removes a comment and reverses it on appeal leaves two
counters lower than the cycle found them. Write the pair as `do(); undo();
assert <everything back to the seeded value>`, and pair it with a **control**
on a branch you believe is lossless -- here the `Post` branches of the same two
functions -- so a reader can see the finding is specific rather than a general
complaint about counter hygiene.

**State what the round trip measured -- "the undo does not undo what the do
did" -- and stop there.** An earlier version of this fact, of D200 and of the
pin's own docstring all said the two counters were left *permanently* lower.
That is a claim about the whole system, and a two-call test cannot support it:
it was false here, because both counters are rebuilt away from this path
(D200 names the recompute sites and the bound on each drift). Whether the drift
ever recovers has to be read out of production code separately, and belongs in
the register entry, not in the assertion's docstring.

**60. A filter clause is unkillable when the set it excludes is EMPTY under
every fixture in the file, however many tests exercise the function.** This is
distinct from fact 33, which is about a clause whose value matches what the
factory always produces; here the clause is fine and the *rows it would
exclude have never been created*. Two mutants have survived this way in this
campaign -- `post_ap_context`'s `post_id` (**D197**) and
`site_ban_remove_data`'s reply-side `deleted=False` (**D211**) -- and in both
the same clause appeared at two call sites in one function with only one of
them reachable, so a clean sole kill on the first call site says nothing about
the second. (**D186** records four related clauses that no mutation could kill
because no test reached them at all, which is the same symptom from a
different cause.) The remedy is
always a fixture that creates the row the filter exists to exclude, and the
test that does it should seed **only** that row, so the kill is attributable
to one call site rather than shared with its sibling. The prospective version
is cheaper: when a scoping clause names an id, build the fixture with two of
whatever it scopes by. Sub-project 12 did that for
`community_ban_remove_data`'s `community_id` and the mutation killed on the
first attempt.

**61. `make_community` hardcodes `instance_id=1`, so a fixture built to
satisfy one clause of a guard can silently satisfy a second.**
`User.is_instance_admin()` filters `InstanceRole` on the **user's**
`instance_id`; `Community.is_instance_admin(user)` filters it on the
**community's** (`app/models.py`). Those two clauses are only distinguishable
when the two instances differ -- and with `make_community`'s hardcoded 1
(fact 21) against a `seed_*` helper that creates instance 1, they collapse
into one. A fixture that satisfies two disjuncts of an `or` does not fail: it
**stops attributing**, and a mutation dropping either clause then kills two
tests where it should kill one. Move the community off instance 1 first. Two
general points: this is found by RUNNING the mutation and counting the
failures, never by reasoning about the fixture, and **a mutation that kills
more than one test is as much a signal as one that kills none**.

**62. `Community.has_poster(user)` falls back to counting REPLIES when the
user has no posts, so "has posted there" includes "has only replied there".**
The method (`app/models.py:818-825`) runs a `COUNT(*)` against `post` and, only
`if not post_count`, a second against `post_reply`, returning `post_count or
post_reply_count`. It gates the ban and unban notifications in `ban_user` and
`unban_user`, so a fixture built to make `has_poster` false must give the user
neither a post nor a reply in that community -- seeding a reply and no post
produces a true that reads like a false.

**63. A Celery task in this codebase is tested by CALLING IT DIRECTLY, and that
is production's own behaviour rather than a testing contrivance.** Every
`@celery.task` in `app/activitypub/util.py` has a plain-function caller beside
it that branches on `current_app.debug` -- `refresh_user_profile`
(`app/activitypub/util.py:647-651`) calls `refresh_user_profile_task(user_id)`
inline when debug is true and `.apply_async(...)` otherwise, and
`refresh_community_profile` (`:776-780`) and `refresh_feed_profile`
(`:1013-1017`) do the same. On top of that the `app` fixture puts Celery in
eager mode (see "Eager Celery makes outbound federation happen inline"), so `.delay()` and
`.apply_async()` also run inline and propagate. So `refresh_user_profile_task(user.id)`
in a test body is the production path, not a shortcut around one. Two
consequences: **do not build a `.delay()` harness for a task in this file**, and
do not write a test whose only claim is that the DEBUG branch was taken -- under
eager Celery both arms behave identically, and recording *which* call was made
is the only honest way to pin that branch (the sub-project 3 note on
`resolve_remote_post_from_search` says the same thing from the other side).
Generalises item 1 of "The inbox-dispatch harness", which established the same
property for `process_inbox_request`.

**64. `get_task_session()` leaves `autoflush` at SQLAlchemy's default `True`,
where `db.session` in this app is configured `autoflush=False`.** The helper is
two lines -- `return Session(bind=db.engine)` (`app/utils.py:3673-3675`) -- and
passes no `session_options`, while `app/__init__.py:81` constructs
`SQLAlchemy(session_options={"autoflush": False}, ...)`. So the session a task
does its work through flushes pending changes before every query, and the
session the test seeds its fixtures through does not. **The two sessions do not
behave the same way**, and a mental model built on one is wrong about the other:
inside the task, a half-built object can reach the database on the next `SELECT`
and trip a NOT NULL or unique constraint at a line that contains no `commit()`;
in the test, it cannot. When a task raises an `IntegrityError` from a line that
looks like a read, this is why. Note that `patch_db_session` (`app/utils.py:3679`)
makes `db.session` *become* the task session inside the task's `with` block when
there is no request context, so code that reaches for `db.session` while a task
is running gets the autoflush-`True` one -- see fact 3 in the inbox-dispatch
section for the request-context half of that asymmetry.

**65. When a task commits on its OWN `get_task_session()`, a row the test is
holding from `db.session` is stale, and `db.session.refresh(obj)` IS
load-bearing.** This is the complement to fact 58, not a contradiction of it,
and the difference is which session did the committing. Fact 58's case is one
session: the function under test commits on `db.session`, `expire_on_commit` is
`True`, so the test's objects are already expired and an added `refresh()` is a
redundant SELECT. This case is two sessions: the task commits on an independent
`Session(bind=db.engine)`, which expires **its** identity map and knows nothing
about the test's, so the test's object keeps whatever it was loaded with and the
assertion reads a pre-call value. Measured rather than assumed at the start of
sub-project 13 -- without `db.session.refresh(user)`, `user.title` reads back
`None` after a refresh task that demonstrably wrote it. **Ask which session
committed before deciding whether the refresh is a no-op**, and write the
docstring accordingly: calling it load-bearing is a claim about the session
arrangement, and that claim is true here and false in fact 58's case.

**66. `seed_community_owner` is NOT idempotent -- calling it twice for one
domain raises `IntegrityError`.** It unconditionally calls `make_instance(domain)`
(`tests/factories.py:274`) and `Instance.domain` is `unique=True`
(`app/models.py:86`). The trap is indirect, because the second call is usually
hidden inside a helper: a test-local `_remote_user()` that seeds its own peer
will collide with the `_remote_community()` that already seeded it, and the
failure surfaces as a constraint violation in fixture setup rather than as
anything to do with the code under test. **A test needing a SECOND actor on a
peer some other helper already created must use `make_user(<row>.instance, name)`
against the existing `Instance` row**, not reach for the seeding helper again.
The same applies to any helper built on `seed_community_owner`.

**67. An actor lookup inside the code under test can make an outbound fetch, in
two different ways, and both have to be headed off.** (a) **A seeded actor row
with a NULL or stale `ap_fetched_at` triggers a nested refresh.**
`find_actor_or_create` calls `schedule_actor_refresh`
(`app/activitypub/actor.py:134-147`), which fires `refresh_user_profile` /
`refresh_community_profile` / `refresh_feed_profile` when `ap_fetched_at is
None or ap_fetched_at < utcnow() - timedelta(days=1)` -- and under eager Celery
that whole refresh task runs **inline, inside your test, against no mock**.
Stamp `ap_fetched_at = utcnow()` on any actor row the path will look up; that is
what `resolvable_remote_author` exists to do, and a hand-rolled fixture has to
do it by hand. (b) **`create_if_not_found` defaults to `True`**
(`app/activitypub/util.py:280`), so an actor URL that resolves to *no* row at
all reaches `create_actor_from_remote` and fetches. A test that wants the
"unresolvable" arm therefore cannot just pass a URL it never seeded -- pass one
that resolves to a row of the wrong type instead. Sub-project 13's following-loop
false-arm test uses the ActivityStreams Public URI for exactly this reason. Both
arms surface the same way if you miss them: an unmatched request from
`block_outbound_http`, or an `assert_all_called` failure, at a line that mentions
no HTTP.

**68. A fix that ADDS a conjunct must be mutation-proved by deleting THAT
CONJUNCT ALONE. Mutating the whole guard to `if True:` proves the site, not the
clause -- and the gap is self-concealing.** Sub-project 13 added
`following_collection and 'items' in following_collection` around a loop and
recorded a green kill for it; the mutation had been `if True:`, which deletes
both conjuncts at once. Deleting only `following_collection and` survived the
entire file (**57 passed, zero failures**), because every test served a truthy
dict and none served JSON `null`. `if True:` answers "does anything depend on
this *line*?" when the fix raises "does anything depend on this *clause*?" It is
worse than a plain missing test because **the site-level mutant does die**, so
the table shows a kill and nothing in the artefacts contradicts itself -- there
is no trace of the missing proof to find later. The rule: **one mutation per
clause the fix introduces**, named in the table as the clause and not as the
line.

A corollary from the same fix, worth knowing before you try to pin a truthiness
conjunct: **adding a membership check can shrink the set of inputs that kill its
neighbour.** Before the fix, deleting a guard's leading `data and` and serving
`{}` raised `KeyError` on `{}['type']` and would have killed. After it,
`'type' in {}` is merely `False` and the guard skips, so **`null` is the only
discriminator left**. Nothing regressed -- those conjuncts were already unpinned
-- but the cheap input no longer works, and someone who tries `{}`, sees it
survive and concludes the clause is dead would be wrong.

Related, and the reason this file has no separate fact for it: **a leading
truthiness conjunct of a guard like these is sole-killable only by CRASH, and
that is a legitimate kill rather than a gap to paper over.** No JSON value is
both falsy and carrying the keys the rest of the guard reads, because a falsy
dict is necessarily empty -- so there is no input that satisfies the remaining
conjuncts while falsifying the first, and no assertion-kill can exist. That is
**fact 44's rule in a different syntactic position** (44 states it for a
crash-to-404 inversion): label which shape the kill is and prove the
impossibility from the value domain rather than reporting a weak result. The
proof is the part that must be written down -- "it only crashes" is a finding
only once you have shown no non-crashing input exists.

**69. When two guards sit in sequence, the pin for the OUTER one must serve a
payload the INNER one accepts.** Otherwise both mutations die by the same
exception and the two guards become indistinguishable: you have one test that
kills two mutants for one reason, which attributes nothing. Sub-project 13 hit
this on a status check followed by a JSON-decode guard. The non-200 pin's body
is **deliberately valid JSON naming a resolvable community**; garbage at 502
would have made dropping *either* guard die by `JSONDecodeError`. As written,
dropping the status check creates a row and flips a count while dropping the
decode guard raises -- two guards, two different kills, each leaving the other
test passing. The general form: **the outer pin's payload must be innocuous to
everything downstream of the guard it targets**, so the only thing the mutation
changes is whether the guard fired.

**70. Grep the mechanism's IDENTIFIER, not the words used to describe it.** A
docstring-and-comment audit at the end of sub-project 13 grepped the crash
vocabulary its own briefs had used ("crash", "ungated", "workaround") and found
three sites; a fourth existed and was missed. Grepping the **column name** the
workaround actually sets -- `ap_following_url` -- found it immediately: a test
that set the column with no explanation at all, which is precisely the site an
audit exists to catch, and precisely the one that matches no descriptive word.
The same applies to stale citations, renamed pins and inverted tests: search for
the symbol, the column, the URL, the old test name -- something the code must
contain -- rather than for how you would describe it in prose.

**71. `in` against a string is a SUBSTRING test, not a type error -- so a
"wrong type" fixture chosen as a string sails through a downstream membership
conjunct that would have raised on any other type.** Sub-project 14 hit this
twice, in opposite directions. Proving `isinstance(source, dict)` in
`'source' in obj and isinstance(obj['source'], dict) and 'mediaType' in
obj['source'] and ...`, the obvious fixture is `source='not a dict'` -- but with
the `isinstance` conjunct deleted, the next conjunct evaluates
`'mediaType' in 'not a dict'`, which is `False`, not an exception. The guard
short-circuits identically with and without the clause and the mutant survives.
`'mediaType' in None` raises `TypeError`; `'mediaType' in ['a']` returns `False`
but the following subscript then raises. **The fixture for a type guard must be
a value on which the NEXT operation actually diverges** -- `None` if the
membership test itself must raise, a list if you want it to succeed and the
subscript to fail -- and a string is the one wrong-type value that quietly does
neither. The other direction is worse because it appears later: **adding** a
membership check in front of a subscript can turn a previously-crashing string
input into a silent skip, which is a real behaviour widening to record (here it
made a tag loop *uniform*, since a sibling arm already behaved that way) and
which is also fact 74's mechanism. Verify the claim in a bare interpreter --
`'x' in 'abc'` versus `'x' in None` -- rather than reasoning about it; two
agents in sub-project 14 did, and it is two lines.

**Sub-project 17 supplied the worked instance this fact had been predicting,
and the value of it is the SHAPE TABLE rather than the case.** The fix at
`30a9dcec` added `'url' in request_json['object']['image']` in front of a
nested subscript (`app/activitypub/util.py:3391`), copying the guarded twin
ninety-five lines below `:3392` (`:3487`). Measured shape by shape in a bare
interpreter, not
reasoned: a dict without `url` raises `KeyError` before and is handled after
(**the repair**); `"image": ".../pic.png"` -- a string lacking the substring
`url` -- crashed with `TypeError` before and is now **silently dropped**
(**a widening**); `"image": [...]` the same (**a widening**); `"image":
".../url.png"` -- a string that happens to *contain* `url` -- passes the guard
and still raises `TypeError: string indices must be integers` (**unchanged**);
`"image": null` or a number makes **the guard itself** raise `TypeError`
(**unchanged**). **Two lessons. (a) A membership guard added in front of a
subscript changes the outcome for exactly two of four non-dict shapes, and
which two depends on whether the key name is a substring of the value** -- so
"does adding this guard change behaviour?" has no answer short of enumerating
the shapes. **(b) Both sites are still open for the non-dict case** -- the
guard is only correct for dicts at `:3391` *and* at `:3487` -- so a
membership-only repair closes the missing-key hole and leaves the type hole at
every site it was copied to (D285). Write the widening down; do not deny it.

**72. A test that reaches a guard's False side NATURALLY cannot kill a mutant
that FORCES that guard False. The two are indistinguishable by construction.**
Sub-project 14's `if attachment_list:` regeneration gate survived six tests
written for it: the four with non-empty lists asserted only on `reply.body`,
which is identical whether or not `body_html` is regenerated, the no-url test
appended nothing either way, and the empty-list test exercised the natural False
path -- so nothing observed the difference. **Proving such a guard needs a test
on the TRUE side that asserts what the guard's BODY does**, which here meant
asserting `body_html` after a non-empty attachment list, something no existing
test did. The general rule: a forced-False mutation is killed only by evidence
that the body ran, so the pin belongs on the True side even though the guard
"looks" like it is about the False side. The corollary for reviewing a mutation
table: an empty-input test listed as covering a truthiness guard is covering the
*line*, not the *clause*, which is fact 68's distinction in a different shape.

**73. Three ways a LATER step in the same run masks what your mutation changed
-- and one remedy for all three: deny the mutant every downstream route to the
observable.** Sub-project 14 hit all three in different tasks, and they read as
separate puzzles until you see the shape.

- **A later lookup independently produces the same negative.** A Mention guard
  `profile_id.startswith('https://' + SERVER_NAME)` was to be pinned by a
  remote-user test -- but a Mention naming a user on another host finds no
  matching local recipient **either way**, so deleting the guard changes nothing
  observable. The killer is a **case-mismatched HOST on the same server**: it
  lowers to an exact match, so bypassing the guard produces a real notification.
  **The same shape reaches one step further out: a CALLEE that re-checks the
  guard's own condition.** `create_post_reply`'s parent-comment block guards
  are followed by `PostReply.new`, which runs
  `notification_target.author.has_blocked_user(...)` again -- so if the fixture
  lets `notification_target` resolve to the same author the guard names, the
  callee produces the identical refusal and the mutant lives. Sub-project 15
  killed it with a **distinct third author**: `notification_target` is the
  `post` whenever the parent comment is itself top-level, and giving the post
  and the parent comment different, unblocked-vs-blocked authors leaves the
  callee nothing to re-catch. The rule generalises: **when a mutation deletes a
  guard, read what the guard's callees check, not only what the rest of the
  function does** -- a duplicated check downstream is invisible in the diff and
  fatal to the kill.
- **A later normalisation converges the value.** An assertion on a value that a
  later idempotent step would produce anyway cannot kill the arm that produced
  it early. A `text/html` content arm and the `else` arm both end up wrapping
  bare content in `<p>`, and `html_to_text` renders single- and double-wrapped
  input identically -- so a test whose fixture is *already wrapped*, or which
  asserts only on `body`, passes under "delete the `text/html` arm". The killer
  needs **unwrapped** content and an assertion on the value **before**
  convergence.
- **A later region of the same function overwrites the write.** A scoped
  region's `post.url` write is invisible under `type='Note'`, because the
  function's later Links section sets `new_url = None` when there is no
  `attachment` key and overwrites unconditionally. Asserting it requires
  steering the function down a branch that skips the later write -- here
  `type='Video'`, which returns before it.

The remedy in each case is the same and it is worth stating as one rule:
**before believing a surviving mutant is unkillable, enumerate every other way
the run reaches the value you asserted on**, and either remove those ways from
the fixture or assert on something upstream of them. This is also why a
deliberate convention break in one test (a lone `type='Video'` in a file that
otherwise uses `'Note'`) can be correct and must be explained in its docstring
rather than normalised away.

**74. Adding a conjunct can UNKILL an existing test, so re-run the guard's
EXISTING mutations after changing it, not only the new one.** This is the
stronger form of fact 68's corollary, and sub-project 14 observed it as an
actual regression in proof rather than as a theoretical shrinkage: a string
`source` fixture had been the sole killer of an `isinstance(..., dict)` mutant;
inserting `'mediaType' in ...` after it made `'mediaType' in 'not a dict'` a
substring test returning `False` (fact 71), so the guard short-circuited with or
without `isinstance` and **the existing test passed under the existing
mutation**. Nothing in the diff hints at it -- the fix is one line, the test is
untouched, and the suite is green. The operational rule: **a fix that changes a
guard invalidates every mutation result previously recorded for that guard**;
re-run all of them and restore the kills the fix vacated, in the same commit
that vacated them. Sub-project 14 did, adding the `source=None` companion the
sibling suite already carried.

**Two corollaries from sub-project 17, both found by review rather than by the
implementer.** **(a) A regression spot-check must run THROUGH the test you
changed.** Strengthening an assertion in one test and then re-running a mutant
whose sole killer is a *different, untouched* test demonstrates nothing about
the change: it re-proves a kill the change could not have affected. Pick a
mutant whose sole kill runs through the modified test -- Task 1 re-ran the
wrong one, was told, and re-ran the right one, which still died. **(b) The
"same commit" clause is the half that slips.** Sub-project 17 landed a
one-line guard fix and re-ran the mutations previously recorded against that
guard **two commits later**, after a reviewer asked. The re-runs were clean and
no kill had been vacated, so nothing was lost -- but the invariant this fact
states was permanently unmet for that commit, and the only thing that caught it
was a reviewer reading the fact. **If you change a guard, the list of
mutations to re-run is derivable from the diff; derive it before you write the
commit message, not after someone asks.**

**75. The five catalogued causes of an unkillable CLAUSE, TWO of an unkillable
STATEMENT, and one of an unkillable ARM OF A CONDITIONAL EXPRESSION. Name which
one you have and prove it; never invent a test to fake a kill.** Causes 4 and 5
were added by sub-project 14 and are the two that most often get mis-filed as
ordinary fixture gaps. **The list is organised by SYNTACTIC UNIT, and reading
the unit first is what keeps a survivor from being mis-filed: causes 1-5 are
causes of an unkillable *clause*; causes 6 and 8, added by sub-projects 16 and
19, are the statement-level cases; cause 7, added by sub-project 17, is the
expression-arm case. Each was numbered separately rather than folded into 4(a)
for the same reason -- force-fitting one unit into another unit's taxonomy is
what produces a mis-filed survivor.** Read the scope of the item before you
claim it: if the mutant you are explaining deleted or narrowed a whole
statement rather than dropping a conjunct, 1-5 do not apply to it; if it
collapsed one arm of an `a if c else b`, neither 1-5, 6 nor 8 do.

1. **The factory always produces the matching value** (fact 33) -- the clause is
   fine, the fixture cannot vary what it tests. Fixable.
2. **The excluded set is empty under every fixture in the file** (fact 60) --
   the rows the filter exists to exclude have never been created. Fixable.
3. **Subsumption** -- a later conjunct implies this one. Prove it
   *algebraically*, not by observing zero failures: given
   `isinstance(profile_id, str)` and `profile_id.startswith('https://...')`,
   `profile_id` cannot be falsy, because the only falsy string is `''` and
   `''.startswith(<non-empty>)` is always `False`. Not fixable.
4. **Tautology** -- a guard that cannot discriminate, in either of two shapes.
   **(a) The body writes what the guard's own False condition asserts is
   already there.** `if new_language.id != old_language_id: post.language_id =
   new_language.id` cannot be killed by forcing it to fire, because firing it
   writes back the value already present. **(b) The condition is falsified by
   an invariant established BEFORE the guard runs -- by a caller, or by an
   enclosing guard -- so its True branch is dead code.** `if post_id is None:`
   inside `if post_id or parent_comment_id or root_id:` in
   `create_post_reply`: `find_reply_parent` never sets `parent_comment_id` or
   `root_id` without setting `post_id` in the same statement group, so the
   enclosing gate admits nothing the inner guard can catch (D265). Shape (b) is
   proved against **every branch of whatever establishes the invariant**, not
   against a sample -- and note it is not cause 5: the value is not one the
   column cannot hold, it is one this call site cannot deliver. Neither shape
   is subsumption and neither is a fixture gap: **no test can ever kill it and
   none should be written.**
5. **Unreachable data** -- a value the column **cannot hold**. Distinct from
   causes 1 and 2 because no fixture could close it. `element == 0` in a
   `reply.path` skip became unkillable once the surrounding empty-`IN` crash was
   guarded: the leading `0` is a sentinel *precisely because* `post_reply.id`
   starts at 1, so inserting a `PostReply` with id 0 to force the kill
   fabricates a state production cannot reach -- a fake kill, and the reviewer
   said so before the implementer was tempted.
6. **Redundant statement** -- **the only cause on this list that is not about a
   clause.** The mutated statement's only observable effect is performed
   unconditionally by code that runs after it on every path, typically a
   `finally`. Prove it by **naming the code that repeats the effect and showing
   that no path between the two observes the difference** -- not by counting
   failures. `session.rollback()` in `notify_about_post_task`'s tail handler
   (`app/activitypub/util.py:2933`) can be deleted and nothing fails, because
   `finally: session.close()` (`:2935-2936`) ends the transaction on the
   exception path whether or not the rollback ran, so no persisted state ever
   differs (D281; D231 records the identical shape at three sites in the three
   actor-refresh tasks, registered by sub-project 13, and that pair of
   independent instances is why this is a cause rather than an anecdote). The same cause covers a
   narrowed handler: `except Exception:` -> `except ValueError:` on the same
   block also survives, because the exception propagates either way and
   `finally` still ends the transaction -- **the breadth of a handler whose body
   is observably a no-op is unpinnable for exactly the reason its body is.**
   Corroboration can be indirect and still count: deleting `session.close()` as
   well does not expose the rollback -- the run **hangs** on the locks the
   uncommitted INSERT holds -- and that is the same proof, because the rows are
   still never committed by anyone. Not fixable, and no test should be written
   for it.
7. **Guarded callee** -- **the only cause on this list that is about an ARM OF A
   CONDITIONAL EXPRESSION.** One arm of an `a if c else b` cannot be
   distinguished from its sibling because the **callee inside the other arm
   already performs the same guard** and returns the same value with no side
   effect, so both arms compute one value for every input. `old_domain =
   domain_from_url(old_url) if old_url else None`
   (`app/activitypub/util.py:3509`) survives collapsing the `else` arm --
   i.e. becoming a bare `domain_from_url(old_url)` -- because
   `domain_from_url` opens `if not url: return None` (`app/utils.py:1562-1568`)
   and nothing precedes that return. **Prove it by reading the callee's first
   statements and showing the early return has no side effect**, which makes
   this equivalent for *all inputs* rather than for all fixtures -- that
   distinction is the whole reason it is not cause 1 or 2. Discriminate it from
   the three it is nearest: it is **not 4(a)**, because there is no body and
   nothing is written back; **not 4(b)**, because `old_url` genuinely can be
   falsy and the arm is genuinely reached, so the ternary discriminates
   *control* while failing to discriminate *value*; and **not 6**, because the
   redundant work runs *instead of* the mutated arm rather than *after* it on
   every path. **Not fixable, and no test should be written for it** -- but the
   arm is still worth *covering*, because a later change to the callee's guard
   would make the two arms diverge and only a test that exercises the falsy
   input would notice. **Standing on one instance where cause 6 stood on two,
   and recorded that way deliberately.** It is listed anyway because the
   alternative on meeting this shape is force-fitting it into 4(a) or writing a
   fake kill, which is exactly what this fact exists to prevent; a second
   independent instance would settle it, and a demonstration that some
   *existing* member of 1-6 already covers it would retire it. **Note where this
   cause lives: fact 87 says coverage emits no arc for a ternary, so an unkilled
   expression arm is invisible to both figures and is only ever found by an AST
   enumeration followed by a mutation** -- which is how this one was found (D294).
8. **Unreachable handler** -- **the second statement-scoped cause, and the one
   for a `try`/`except` whose body can never run.** A bare `except: pass`, or
   any handler, is unkillable when the **callee inside the `try` has no raising
   path for the argument shape this call site can produce**. `search_for_user`
   (`app/user/utils.py:85-158`) is called twice in `send_post`, at
   `app/shared/tasks/pages.py:106` and `:112`, each inside a bare `except: pass`
   -- and only one of the two handlers is dead. On the **local** arm the address
   has no host, so `:88`'s `if '@' in address` is false, `:91-92` set
   `server = ''`, `:94`'s `if server:` is then false and the function's sole
   `raise` (`:98`, the blocked-instance check) is skipped; the hit path returns
   a `User` at `:104` and the miss path ends at `:108-109` returning `None`. **No
   INPUT to that call reaches a `raise`, so `:107-108` cannot be reached by
   choosing a mention** -- which is the property this cause is about, and it is
   what was proved. It is **not** the stronger "the handler can never run": the
   clause is **bare** (`except:`), and `:101` is a
   `db.session.query(User).filter_by(...).first()`, so a `SQLAlchemyError` from
   the DB layer still lands in it. **Say "unreachable for every input", not
   "dead"** -- the weaker claim is the one the argument supports, and it is
   already enough to explain why no test chases the lines. On the **remote** arm the address
   always contains `@`, so `:94` opens and `:98` can fire, and that handler is
   ordinary reachable code. **The two handlers are the same three tokens and
   only one of them is dead**, which is the whole reason this needs proving
   rather than eyeballing. **Prove it the way cause 7 is proved -- by reading
   the callee's statements in order and showing no `raise` is reachable for this
   call site's argument -- and confirm the `raise` set by an AST walk for
   `ast.Raise` inside the callee's `FunctionDef` rather than by `grep`.**
   Discriminate it from the two it is nearest: it is **not 6**, because cause 6
   requires the mutated statement's effect to be performed by other code that
   runs after it, and an unreachable handler body performs **no effect at all** --
   it is unreachable, not redundant; and it is **not 1-5 or 7**, which are scoped
   to a clause and an expression arm respectively. Not fixable, and no test
   should be written for it. **Standing on one instance**, like cause 7 did when
   it was added; it is listed anyway because the alternative on meeting this
   shape is force-fitting it into 6 or manufacturing a `raise` the call site
   cannot produce -- sub-project 19 met it, argued it in a committed comment
   block in `tests/test_shared_tasks_send_post.py`, and wrote no test for it.

**NAME THE ESTABLISHER, NOT ONLY THE CAUSE.** Every entry above answers *what
kind* of survivor you have; a checkable entry also answers **what makes it
true**, and the campaign has now met **four distinct establishers** for cause
4(b) and cause 8 alone: an **earlier return** in the same function (`send_post`'s
`:153-154` returning on `community.local_only`, which is why the false arms of
`:270` and `:333` are dead); an **unconditional assignment** (`:196` setting
`page['name']` inside the dict literal, which is why `:312`'s false arm is
dead); a **callee with no raising path** on the argument shape this site
produces (cause 8's own establisher); and **the SQL query that produced the loop
variable** (`Community.following_instances` and `User.following_instances`
filtering `dormant`, `gone_forever` and `id != 1` in SQL, which is why three
conjuncts in `send_post`'s two delivery loops are unreachable-False -- D302).
Note that 4(b) as written names a **caller** or an **enclosing guard** and none
of these four is either. **Recording which establisher applies is what makes the
entry checkable later**, because that is the thing a future change breaks: a
guard whose establisher is an SQL filter comes back to life the moment somebody
passes `include_dormant=True`, and nobody re-reads an entry that only says
"tautology".

**Crash-only killability is another thing that looks like this list but is not
on it**: fact 68's third note describes a clause no *assertion* can kill because
no input satisfies the remaining conjuncts while falsifying it. That is a shape
of kill, not a cause of survival -- the mutant does die, by exception. Label
which you have. And in every case the part that must be written down is the
**proof**, not the observation: "0 failures" is a measurement, "no such input
exists" is a finding.

**76. A "missing header" defect cannot be pinned by asserting the header is
ABSENT -- the HTTP client supplies its own default -- and the pin must read the
request the mock RECORDED, not the outcome.** Sub-project 13 registered a
`get_request` call that passed no `headers=`, describing the peer as seeing "a
request that expresses no preference". Measured while writing the pin, that is
false twice over. `httpx.Client`'s own defaults fill in `Accept: */*` when the
caller sets none, and per-request headers **merge onto** client defaults rather
than replacing them -- so the peer saw a **positive invitation to
content-negotiate**, and `*/*` is the worst possible value here because it
explicitly welcomes the HTML representation. Two consequences: **a presence
check would have passed UNFIXED**, so the pin has to be an *equality* against
the intended value (`assert ... == 'application/activity+json'`); and the
assertion must read `route.calls.last.request`, because respx serves its canned
response whatever the request asked for, so an **outcome-only** test passes
either way. The general form: when the defect is "we did not say X", find out
what the library said on your behalf before deciding what "fixed" looks like.

**77. A quoted CODE BLOCK is the one kind of docstring claim that can be audited
mechanically; prose claims about production structure cannot.** Sub-project 14
shipped **five** false prose docstrings across six tasks -- "the block breaks out
per tag", "matches on url alone", a wrong function attribution, an overstated
crash scope, and a false adjacency claim -- and every one had to be found by a
human re-reading source. Exactly **one** quoted block drifted, and a script
found it. The method is the reusable part: `ast`-parse every docstring in the
file, dedent each against **its own body indent** rather than against the whole
string (the summary line sits at column 0 and otherwise defeats
`textwrap.dedent`), and treat runs indented **further** than the body as
quotations -- which separates real quoted blocks from ordinary paragraphs in a
way a plain indent grep cannot. Then diff each block against source. The
practical consequence for writing tests: **prefer a quoted block to a sentence
whenever you are describing production structure**, because the block is
checkable forever and the sentence is checkable only by whoever happens to
re-read it. This is fact 70's mirror image and the more expensive half.

**78. A stale stack produces SHIFTING false failures -- a different test fails
on each run -- so any surprising failure gets `./run_tests.sh --down` and a
re-run before it is believed.** Distinct from fact 18, which is two concurrent
pytest sessions; this is a **single** session against a stack that has been up
too long. Two agents hit it independently on 2026-09-04. One saw 4 failed / 102
passed, then an immediate re-run **with nothing changed** giving 1 failed / 105
passed **with a different test failing**; the other saw 4 failed / 54 passed on
tests neither of its fixes touched, each passing in isolation, then 58 passed on
a bare re-run. Every failure was
`sqlalchemy.exc.IntegrityError: duplicate key value violates unique constraint
"ix_instance_domain"` during seeding. `--down` then gave clean runs twice
consecutively in both cases. **Because the failing test changes between runs, a
single bad run reads as a real regression in whatever test happened to lose the
race** -- and in a fix wave, that is a regression you will believe, because you
just changed production code. The cost of `--down` is real (fact 29: it replays
~269 migrations), which is exactly why the rule is "before it is believed"
rather than "before every run".

**79. A test file NEED NOT self-contain its kills -- a kill verified in a
sibling file is real coverage -- but the reliance must be DISCLOSED at the point
of reliance.** Sub-project 14 found a mutant it could not kill from its own
file: forcing `link != ''` False deletes a `post.url` write that nothing in
`tests/test_ap_update_pair.py` observes, while
`tests/test_unparseable_url_ingress.py::test_an_ordinary_microblog_link_is_still_stored`
does kill it (that file's `federated_post.url` starts `None`, so the missing
write fails its assertion). The implementer declined to duplicate the other
file's ground and documented the split; the reviewer read the other test,
confirmed the kill, and accepted it. **Ruled at that sub-project's final review:
no, and duplicating would be the worse option** -- this campaign has repeatedly
forbidden reproducing another file's ground precisely because duplicate tests
**rot independently**, and two copies of one pin diverge silently the first time
either subject changes. What the reliance costs, and what must therefore be paid
explicitly: the guard's own test docstring has to **name the file and the test
that kills it**, so a reader auditing the guard is not misled into thinking it
is unproven. A mutation-table entry reading "survives in this file" with no
pointer is the failure; "survives here, killed by `<file>::<test>`, not
duplicated per the brief" is the disclosure. The residual risk is real and
accepted: the kill lives one file away from the guard it proves, findable only
by following the pointer.

**80. A mutation round can damage the tree in two ways, and each has a cheap
check. Run both.** Neither is hypothetical -- both happened in sub-project 14.

- **The window between apply and restore leaves production code BROKEN, so an
  agent killed inside it leaves a silently mutated tree.** A Task 1 fix round
  was terminated at a session boundary between applying a mutant and reverting
  it, leaving `'mediaType' in request_json['object']['source'] and` replaced by
  `True and` in `app/activitypub/util.py`. Nothing in the process detected it;
  the controller found it only by running `git status` during recovery, and the
  next agent would otherwise have built on a mutated file. **Every resume from a
  stopped or interrupted implementer begins with `git diff -- app/` before
  anything else**, and every task that runs mutations asserts that diff is empty
  at commit time as well as after each cycle.
- **A string-replace mutation on a NON-UNIQUE string patches the wrong site, or
  several.** `if 'type' in json_tag and json_tag['type'] == 'Mention':` occurs
  **three** times in `app/activitypub/util.py`, two of them outside the function
  under test, so a naive replace would have mutated all three and attributed the
  result to one. **Assert the occurrence count before replacing**; when it is
  not 1, switch to line-addressed patching. Sub-project 14's tooling aborted on
  its own count assertion *before* the write, which is the whole point of
  checking first rather than reading the diff afterwards.
- **NEVER BATCH MUTATIONS. Apply one, run, restore, verify -- then the next.**
  Sub-project 17 added this after the same task stalled **twice** with a live
  mutation in the tree: the first stall left `for vote in votes:` rewritten to
  `for vote in []:` in `app/activitypub/util.py`, alongside ~374 uncommitted
  test lines, and the standing `git diff -- app/` check on resume is the only
  thing that found either. The reason batching is the cause and not merely the
  occasion: **a batch that dies partway leaves no record of which mutant is
  applied**, so the recovering agent cannot tell an applied mutant from a
  restored one without reading the diff and guessing, and cannot say which rows
  of its own table were actually measured. One-at-a-time makes the tree's state
  derivable at every instant.
- **Once a fix lands mid-slice, "the tree is unmodified" stops meaning "the
  diff against the slice's BASE is empty".** Sub-project 17 committed a
  one-line production fix at its fifth task, after which
  `git diff <BASE>.. -- app/` was permanently non-empty and useless as a
  restore check. The check that survives a mid-slice fix is against **HEAD** --
  `git diff -- app/`, or an `md5sum` of the file compared to
  `git show HEAD:<path>`, which is what the remaining tasks used. State which
  baseline your check uses; "app/ is clean" is ambiguous the moment a slice
  lands code.

**81. When a function contains an UNFIXED CRASH inside a broad handler, that
crash is a downstream route to every "nothing happened" assertion in the
function -- and the routing around it must be PER TEST, never blanket.** This
is fact 73's family arriving from the opposite direction: 73 is about a later
step *producing* the observable you asserted on; this is about an exception
handler *converting* an unrelated failure into the same observable. In
sub-project 15, `create_post_reply` carried D243's `ProgrammingError` inside a
tail `except Exception as ex: log_incoming_ap(...); return None`, so any
negative test whose fixture happened to reach that query passed with **zero
notifications** for a reason that had nothing to do with the guard it was
written against. The remedy is a helper that steers the fixture off the crash
path -- here `_use_a_non_microblog_instance`, which skips the gate the query
sits behind. **Applying it to every negative test would have destroyed the
information.** Instead each test was classified: a test whose guard, if
deleted, would leave the recipient list **empty** never reaches the crash and
must NOT be routed; a test whose guard, if deleted, would **populate** the list
and reach the notify loop must be. Four needed it, five did not, and the
reviewer checked each individually. **Two consequences to plan for.** First, a
routing helper is a claim -- "this test needs it, and for this reason" -- and
the claim goes stale the moment the crash is fixed: after the fix, the helper
was **measured** redundant in nine of its ten callers, and the measurement went
into its docstring so a later slice can retire the rest rather than re-deriving
it. Second, when the crash *is* fixed, every mutant that was dying **through**
it comes back to life (fact 74) -- so fix the crash and re-run the guard's
mutations in the same commit.

**82. A CORRECTION DOES NOT CORRECT ITS COPIES. After correcting a claim, grep
for the specific ENTITIES the correction names and read what they say.** Two
sub-projects running have shipped a corrected claim with a live copy of the
falsehood left behind, and the shape was identical both times: the author did
the measurement, wrote the correct reason into the *helper's* docstring 250
lines from where the wrong one lived, and did not propagate it back to the test
the measurement was **about**. Its own diagnosis is the general one: *"the place
I learned the truth became the place I wrote it, and writing it there felt like
discharging the finding."* Fact 70 says grep the identifier rather than the
description; this is the step after that, and it is mechanical: **a correction
that names a test, a helper, a defect number or a column is pointing at the
places most likely to still hold the old claim** -- so grep for those names, not
for the wording of the claim you just fixed. The corrected helper docstring in
sub-project 15 even *named* the contradicting test, and the contradiction
survived anyway. Related failure caught in the same round: a docstring
generalised a measurement run of "1 failed, 67 passed" into "every test in the
module green", **past the 1**, while writing the correction to a different
measurement error.

**83. "Grep the file for the twin" is NECESSARY BUT NOT SUFFICIENT -- the twin
must also WORK. Check the register for whether the sibling you are about to
copy is itself a known defect.** Sub-project 14's rule was: before registering
a defect for want of a correct spelling, grep the file for the twin, because a
mirrored pair is not the only place a sibling can live. Sub-project 15 found
the limit. Its Mention loop had **no** de-duplication check where the twin had
one, so the grep succeeded and the fix looked mechanical -- but the twin's
check was **already registered as a defect** (it is defeated by
`autoflush=False`, so it dedupes across calls and not within one document,
which is exactly the case the missing check was about). Copying it would have
imported a known-broken guard **and let the slice claim a fix that fixes
nothing**, which is worse than the honest registration, because a closed entry
stops anyone looking. The check costs one grep of the findings register for the
sibling's file and line. **A twin that is already a D-entry is not a model.**

**84. A SOURCE READ OF A GUARD IS NOT COMPLETE WITHOUT THE SESSION SETTINGS IT
RUNS UNDER.** `app/__init__.py:81` constructs
`SQLAlchemy(session_options={"autoflush": False}, ...)`, and that single word
decides whether a whole class of guard works. A reviewer in sub-project 15 read
a de-duplication check -- `Notification.query.filter(user_id == ..., url ==
...).first()` followed by `if not existing_notification:` -- concluded it was
sound, and wrote that "default SQLAlchemy autoflush would make it catch
same-call duplicates too". The premise is false in this app: the first
iteration's pending `db.session.add` is never flushed, so the second
iteration's query cannot see it and the check does not dedupe within one
document at all. **The reviewer was right about what the code says and wrong
about what the app configures.** This is the same root cause as facts 58 and
64, arriving for the first time in a **review** rather than in production code,
which is why it earns its own line: any ORM read-after-write inside one request
-- a uniqueness check, an existence check, a count -- must be evaluated
against `autoflush=False` before it is called correct, and any claim that such
a guard works is a claim about the session configuration and not only about the
SQL.

**85. A docstring explaining why a fixture is SAFE is itself a claim about
production -- write it from tracing what a MISS would do, not from the shape of
the code.** Sub-project 15 shipped one that had **both halves** inverted. It
said a Postgres `Integer` cast meant "a mismatched type here would silently
miss and the test would pass for the wrong reason". In fact `->>` returns text
for a JSON number *and* for a JSON string, so `{'post_id': '5'}` casts to `5`
and matches -- there is no silent miss; and had it missed, the suppression rule
would not have fired, the reply would have reached delivery, and the test's own
`assert len(rows) == 1` would have seen **two** rows and **failed loudly**. The
author's diagnosis is the reusable part: it was written *"from the shape of the
code -- a cast implies a type matters -- rather than from tracing what a miss
would do."* **"Would pass for the wrong reason" is the most load-bearing
sentence a test docstring can contain**, because it is the sentence that tells
the next reader not to strengthen the test. Never write it without following
the miss to an observable. Fact 77's remedy applies here too: a quoted code
block can be audited mechanically and a sentence like this one cannot.

**86. For a TWO-CONJUNCT filter, write one negative PER CONJUNCT, each holding
the other conjunct TRUE. A positive test alone cannot kill either.** Concretely,
for `.where(A).where(B)`: test 1 is the positive; test 2 holds **A true and B
false**; test 3 holds **B true and A false**. Each of 2 and 3 fails for a
distinct reason, so dropping either conjunct is killed by exactly one named
test. A single positive cannot distinguish a dropped-conjunct mutant from
correct code, and a single negative that falsifies **both** conjunct kills
neither attributably -- it is fact 69's problem in a filter chain rather than in
a guard sequence. Sub-project 15 applied this to a mark-read `UPDATE` whose
`.where()` chain matched a user **and** a target comment id: the "different
comment" and "different user" tests are the orthogonal pair. This is the third
distinct form the campaign has recorded of *a positive test alone cannot prove
a guard* -- the others being fact 72 (a naturally-False path cannot kill a
forced-False mutant) and fact 68 (`if True:` proves the site, not the clause).
The recipe generalises to N conjuncts as N negatives, and the count is a
**floor**, not an estimate to be trimmed (fact 55's corollary).

**87. COVERAGE.PY EMITS NO ARC FOR A CONDITIONAL EXPRESSION, so neither the
statement figure nor the branch figure can see an unexercised ternary arm.
Enumerate ternaries by READING the region -- the report will never list one.**
`a if c else b` is one statement on one line: both arms execute that line, so
the line is covered whichever arm ran, and no branch arc is recorded for the
choice. A region can sit at 100% statements and 100% branches with an
unexercised arm behind every ternary in it, and the "no guard survives a
dropped conjunct" criterion is therefore **not measurable by coverage for
ternaries at all**. Sub-project 15's whole-branch review found **six** such
arms in a region whose measured figures had already been signed off, all six
reachable with one fixture line each. Two practical notes. **(a) The
interesting arm is often the one the FACTORIES never produce**, not the one the
production data never produces: five of the six were `else` arms, but
`community.ap_id if community.ap_id else community.name` was untested on its
**`if`** side, because `make_community` (tests/factories.py) never sets `ap_id`
-- fact 33 wearing a different hat. **(b) Sort the ternaries into guards and
cosmetics before writing anything.** `language_id = language.id if language
else None` is a real guard: `find_language` returns `None` on a miss, so
dropping `if language` raises `AttributeError` out of the function. Three
sibling `author.ap_id if author.ap_id else author.user_name` expressions are
display-name fallbacks whose mutants merely store a null. Both kinds want a
test; only the first is a defect risk, and saying which is which is what keeps
the six from reading as busywork. Mutation-prove each arm **individually** --
identical expressions at several sites need one test apiece, and the proof is
that reverting one site kills exactly one test. **(c) An AST WALK beats a grep
for the enumeration itself**, added by sub-project 16 after a reviewer used one
to re-derive an implementer's list independently: `ast.parse` the module, take
every `IfExp` inside the `FunctionDef` nodes you care about, and read each
function's extent off `end_lineno` rather than off "the next `def`". A textual
`' if .* else '` grep misses a ternary inside an f-string, a dict literal, a
comprehension or an argument default, and a seventh untested ternary is exactly
the failure this fact exists to prevent -- one that neither coverage nor a
mutation table would show. Both methods agreed there; agreement between two
methods is the result worth recording, because a single method's silence is not
evidence.

**88. A REVIEWER'S citations fail as often as an implementer's, and a
correction gets LESS scrutiny precisely because it carries more authority.**
Three times in one sub-project a review's own findings carried wrong line
numbers: **three of the four in a single round** -- a docstring sentence cited
one line past where it began, a fixture cited at a blank line some seventy
lines above the seeding, and a bold sentence cited at the docstring's first
line instead of its own (test-file addresses, deliberately not reproduced here,
because an intra-file line number does not survive the next append) -- and one
in a later round that placed a **production** line at
`app/activitypub/util.py:2935` when it was, and still is, at `:2931`. **The
implementer caught every one**, and sub-project 15
saw the same shape. The asymmetry is the point: a review finding arrives framed
as "you got this wrong", which invites agreement rather than verification, so
the citation inside it is the least-checked claim in the exchange. **Hold a
reviewer's citations to the standard you hold an implementer's: read the cited
line back from source before you edit anything, and if it is wrong say so in the
fix report rather than silently editing the right line.** The corollary for a
reviewer: a finding whose line number is wrong is still usually a real finding,
so state the *content* you read as well as the address you read it at -- the
content survives an off-by-one and the address does not.

**89. THE PER-TEST SEQUENCE RESET (once `TRUNCATE ... RESTART IDENTITY`, now a
`setval` sweep) makes two entities share a primary key, so
an id-valued assertion can be SILENTLY VACUOUS with the whole file green.**
`db_session`'s teardown resets every sequence in the `public` schema to 1
(`tests/conftest.py:131-132`, inside the statement `_teardown_sql` builds at
`:119-133`), so every sequence
restarts at 1 in every test. **The 2026-09-06 rewrite changed the spelling and
nothing else about this fact**: `RESTART IDENTITY` went away with the `TRUNCATE`
it was attached to, and `SELECT setval(c.oid, 1, false)` over
`pg_class WHERE relkind = 'S'` was written to preserve exactly this behaviour
because fixtures depend on it. Everything below still holds, and so do facts 21
and 61, which rest on the same reset. A helper that seeds one `Community` and then one
`Post` gives **both** primary key 1, and an assertion like `targets ==
{..., 'post_id': post.id, 'community_id': community.id}` cannot tell the two
apart. Measured, not reasoned: mutating production's `'community_id':
post.community_id` to `post.id` left **fifteen of fifteen tests passing**.
**Coverage cannot see this, a mutation score computed only over guards cannot
see it, and a green suite is what it looks like.** Two remedies, and use both:
seed the entities so the ids are **pairwise distinct** (seed a second row of one
type, or assign an explicit primary key -- an explicit id fabricates no state
production cannot reach), and carry an explicit `assert len({a.id, b.id, ...})
== n` in the test so the arrangement fails loudly if a factory's ordering ever
changes. **Prove the guard is load-bearing before trusting it**: re-run the
substitution mutation with the ids collided and confirm it survives, and with
them distinct and confirm it dies. Note that assigning an explicit primary key
does **not** advance the sequence, so a later unseeded row of the same type
takes id 1 and reintroduces the collision -- reserve ids in a helper's docstring
and say which are taken.
**A SHARPER EDGE OF THE SAME MECHANISM, added by sub-project 18 and folded in
here rather than given its own number, because a reader who looks up `RESTART
IDENTITY` should find both consequences without knowing to look twice: seeding
ORDER is load-bearing, not just distinctness, and what it corrupts is a FOREIGN
KEY rather than an assertion.** (Facts 21 and 61 own the other two halves of
this same collision -- 21 that `make_community` hard-codes the FKs at all, 61
that the hard-coding makes two guard clauses collapse into one. This is the
third: which ROW those hard-coded ids point at.) `make_community`
(`tests/factories.py:122-151`) hard-codes `instance_id=1` (`:139`), and
the per-test sequence reset means whichever `make_instance` runs **first** gets id 1. So
a test that builds a peer `Instance` before seeding its own community leaves
that community's `instance_id` pointing at the **peer's** row. Nothing is
vacuous and no assertion is weakened -- the fixture is simply wrong about which
instance the community belongs to, and every later query that joins through it
inherits that. The remedy is ordering, not an extra assertion: seed local first,
or pass the instance explicitly. **And the failure this DOES NOT cause is worth
recording, because it is the one people reach for first:** it does **not** flip
`Community.is_local()`, which tests `ap_id` (`app/models.py:3201-3202`) and
never looks at `instance_id`, and `make_community` sets no `ap_id` at all. A
test that "proves" its community went remote by checking `is_local()` is
measuring nothing. Sub-project 17's closure of a parked residual is the same
mechanism seen from the other side -- `post.instance_id == community.id` **by
construction**, so a distinctness guard could not be made true there -- which is
why this is one fact and not two.

**90. A task that commits INSIDE its recipient loop leaves durable partial
state that no rollback removes -- and that state is assertable, so assert it.**
`notify_about_post_task` (`app/activitypub/util.py:2804`) calls
`session.commit()` per recipient at `:2842`, `:2865`, `:2896` and `:2930`,
inside each of its four arms' loops, while its single tail handler
(`:2932-2934`) rolls back and re-raises. So a mid-fan-out exception leaves every
earlier recipient's row **committed** and only the failing iteration discarded.
For a test author this cuts both ways. **It is a hazard**: a "nothing happened"
assertion is wrong for such a function, because something did happen and it is
still there after the raise. **And it is an opportunity**: the split is exactly
what pins the handler, which is otherwise very hard to reach. Assert it in
three parts -- the surviving recipient's row **and** its counter, the failing
recipient's absence, **and a whole-table count with a contrary baseline taken
before the call**, since "one row for this user" and "one row in the table" are
different claims and only the second excludes a mutant that skipped the failing
recipient entirely. Arm ORDER is what makes the split deterministic: two
recipients inside one arm are not ordered, because `notification_subscribers`
(`app/utils.py:2936-2939`) is a raw `SELECT` with no `ORDER BY`. Interlock the
zero-rows half with `pytest.raises`, so a run that never reached the failure
cannot pass by leaving the same zero rows.

**91. FOUR near-identical guard chains in sequence make fact 72's hazard the
DOMINANT one, not an edge case -- and a stored discriminator is what makes a
row attributable to the arm that wrote it.** `notify_about_post_task` has four
arms whose guards share text almost exactly: `if notify_id != post.user_id and
notify_id not in notifications_sent_to and \` occurs **twice** in the file,
`post.community_id not in blocked_comms` **three** times, `post.instance_id not
in blocked_ints` **four** times. Three consequences, all learned the hard way
here. **(a) Mutate by LINE, never by string** -- a `sed 's/old/new/'` over such
a file silently mutates a sibling arm, and the kill you record is then
attributed to the wrong conjunct. **(b) Mutate each conjunct by forcing it
TRUE, not False.** Forcing True tests the conjunct's exclusion power and is
killed by the one test that seeds the state it exists to exclude; forcing False
runs into fact 72, and with four near-identical chains that trap is the normal
case rather than the exception. **(c) Record which named test kills each
conjunct and whether the kill is SOLE or multi.** With four arms able to produce
a `Notification` for the same recipient, "one row exists" proves nothing about
which arm made it -- what proves it is a **stored discriminator**: this
function's `targets` dict differs per arm (`community_id` only in the community
arm, `topic_name` only in the topic arm, `feed_id` only in the feed arm)
alongside `notif_type` and `subtype`. Assert the discriminator, not just the
count, and a cross-arm de-duplication test then says *which* arm won rather than
merely that one did. A sole-kill column in the mutation table is what lets a
reviewer check all of this without re-running anything.

**92. Flask's `json` SHADOWS the standard library's inside
`app/activitypub/util.py`, so that module's `json.dumps` sorts keys.**
`app/activitypub/util.py:18` is `from flask import current_app, request, g,
url_for, json`, and the binding is module-wide. The consequence a test author
meets: `log_incoming_ap` writes `ActivityPubLog.activity_json` with the Flask
`json.dumps`, so an assertion comparing that column against a `json.dumps(...)`
built with the **stdlib** `json` in a test module fails on key ordering alone,
with a diff that reads like a content mismatch. It produced sub-project 16's
only genuine RED. **Assert `json.loads(column) == document`**, which pins what
the column is for and leaves ordering -- which no test here is about --
unpinned. Generalise the habit, not the instance: before asserting on a string
some other module produced, read that module's imports for what its serialiser
actually is.

**93. A provably BEHAVIOUR-PRESERVING fix cannot be proved by a mutation that
fails a named test, and the instrument that replaces that gate is the
accumulated mutation table re-run against BOTH versions.** The campaign's
standing rule is that every production fix is proved by a mutation failing a
named test. That rule exists to stop unfounded behaviour changes, and it is
unsatisfiable by construction when the change is behaviour-preserving: if no
observable differs, no test can fail. **Do not manufacture a test that appears
to fail for the stated reason while actually failing for another, and do not
conclude the fix must therefore be wrong** -- an equivalence argument derived
from source is a *stronger* claim than a failing test, not a weaker one. What
takes the gate's place: (1) state the equivalence argument from source, naming
the load-bearing fact; (2) apply the fix; (3) re-run **every** mutation from
every accumulated table against the pre-fix and the post-fix file with the
**same** test file, and compare the full tuple `(passed, failed, sorted failing
test names, exception kinds)` rather than "did something fail"; (4) confirm that
the mutation which deletes the moved statement still dies, so the fix did not
turn a live statement into dead code. Sub-project 16 ran 70 x 2 with zero
divergence. Two practical notes. **A behaviour-preserving edit has a non-empty
expected diff, so "is `git diff -- app/` empty?" is not the hygiene check any
more** -- save the intended diff before the first mutation and compare against
it. And **match the INVARIANT, not the literal text of the sibling you are
copying**: three sibling arms put the statement at indent 20, and copying 20
into the fourth arm would have reproduced the bug, because the invariant was
"the last statement of the `if` body" and that arm sits one level deeper.

**94. To enumerate a SET, apply its membership test to every candidate. Walking
the cross-references its known members make to each other returns a SUBSET, and
it looks like an answer.** Any enumeration you are about to call complete -- the
members of a defect family, the conditional expressions in a region, the call
sites of a helper, the tests that pin a guard -- has two available methods, and
only one of them can be complete. **Sub-project 16 got this wrong and right in
the same slice, which is why it is a fact and not an anecdote.**

- **Wrong.** The first draft of the unguarded-peer-input family index was built
  by following the references the entries make to one another -- D255's naming
  sentence, D259's "of the D236 family", D264's "extends the family". That finds
  every member that *cites* the family and misses every member that predates the
  name or never mentions it, which is how it missed **D237** and **D261**. **The
  sharp part: D258 was already in the index and its own cell names D237 as its
  precedent** -- so the omission was derivable from a row the draft had already
  listed, and the citation-walk missed it anyway. A method that fails on data it
  already contains is not a method that got unlucky.
- **Right.** Task 6's reviewer re-derived the conditional-expression enumeration
  with an AST walk over every `IfExp` node rather than a textual
  `' if .* else '` grep (fact 87(c)), for the same reason stated the other way
  round: **a grep finds what it is shaped to find**, and a ternary in an f-string
  or an argument default is not shaped like the grep.

The operational rule: **state the membership test first, then apply it to a
candidate set you enumerated by some property the candidates cannot opt out of**
-- every `IfExp` node, every register row whose function is in scope, every call
site of the symbol -- and record the candidates you *rejected* with the reason,
so the next sweep does not re-open them. This is not fact 70 (grep the
identifier, not the description): you can grep for the perfect identifier and
still be walking a subset, because a member that never mentions the family
contains no identifier to find. Nor is it fact 82 (a correction does not correct
its copies), which is about propagating a truth you already have. **This one is
about how you decided what to look at at all**, and the tell that you are in it
is a completeness claim -- "every", "the family spans N", "all the call sites".
If a count is going into prose, say which property bounded the candidate set,
and bound the claim to that scope explicitly rather than letting "every" read as
"every, anywhere".

**95. A CORRECTION TRAVELLING UPWARD -- into a committed file, or into a record
nobody downstream will re-open -- is the least-checked claim in the exchange,
because its reader is checking the correction's CLAIM and not its CITATION.**
Fact 88 names this authority asymmetry from the receiving side and tells you to
hold a reviewer's citations to an implementer's standard. This is the same
asymmetry travelling the other way, and **direction of travel is what decides
whether it gets caught**. Sub-project 16 produced **five** citation errors.
**Four were caught, and every one because the agent receiving it had the source
open anyway**: three of the four line numbers in one task review's findings; a
review placing a production line at `app/activitypub/util.py:2935` when it was,
and still is, at `:2931` (those two are the ones fact 88 records); an off-by-two
repeated out of a controller's brief into a reviewer's; and seven of the eight
citations in one finding of the final whole-sub-project review. **The fifth
reversed direction and reached a committed docstring.** A fix wave reported that
a published harness fact cited `tests/conftest.py:143` where the TRUNCATE was at
`:142`, and wrote `:142` into a test file. `:143` was right and the fact was
right. **The evidence that should have stopped it was already on the page:
three independent artefacts -- fact 89, another citation in this same file, and
a register cell -- all said `:143` and agreed with each other.** A correction
that disagrees with N agreeing prior citations needs N-fold verification, not
less. And **nobody downstream had a reason to re-open `tests/conftest.py`**,
which is precisely why this was the one that landed.
(Both of those line numbers are now history and are left standing as history:
the `TRUNCATE` teardown was replaced on 2026-09-06 and `tests/conftest.py:143`
is inside `db_session`'s docstring today. The episode is what this paragraph
records, not the location, and rewriting the numbers would falsify it.)

**The proximate cause is mechanical, and it is worth more than the principle.**
`sed -n 'A,Bp'` prints content with **no line numbers**, so mapping the first
printed line to A is off by one the moment the range opens on a blank line --
which is what happened. `grep -n ''`, `awk '{printf "%d\t%s\n", NR, $0}'` and
`cat -n` print the number beside the line and cannot fail this way. The same
session had used `grep -n` correctly on `app/utils.py` minutes earlier, so
**this is a tooling hazard rather than a competence one, and the remedy is a
rule about the tool, not about care: never attribute a line number to output
that did not print one.**

The operational rule, in three steps. **(1)** Before writing a correction to an
existing citation, re-read the target with a tool that prints line numbers.
**(2)** Grep for every other citation of the same fact and state in the
correction whether they agree -- **agreement among prior citations is evidence
against you, not background noise.** **(3)** If the correction is going into a
committed file rather than into a report, treat it as the highest-scrutiny claim
in the change rather than the lowest, because it is the one with no downstream
reader. Distinct from fact 82 (a correction does not correct its copies), which
is about propagating a truth you already hold; here the correction was false and
the copies were the check that was skipped.

**96. A CORRECTION THAT ONLY *DELETES* A FALSE CLAIM WILL BE RE-DERIVED. Land
the REFUTATION in the artefact, not just the correction -- and land it in the
artefact being corrected, not only in the report that found it.** Sub-project
17 produced this twice in one task, which is why it is a fact rather than an
anecdote. Task 1's self-review caught a docstring saying "the app factory's
`autoflush=False`" -- `db = SQLAlchemy(session_options={"autoflush": False},
...)` is at `app/__init__.py:81`, at **module scope**, and `create_app` does not
start until `:129` -- and corrected it by deleting the phrase. The correction
went into the task **report**. One fix round later, a fresh comment was written
for the same mechanism, without the report or the module docstring in the
writer's working set, and it **regenerated the identical wrong shorthand**.
Nothing had copied it; nothing needed to. The natural phrasing for that
mechanism *is* the wrong one, and a deletion leaves nothing on the page saying
so.

**This is the sibling of fact 82, not a restatement of it, and the difference
is operational.** Fact 82 is about a claim with EXISTING copies: correct it,
then grep for the copies. This is the case with **no copy at all** -- the
correction had nowhere to propagate to, and the failure was that the next
writer had no contradiction available. Fact 82's remedy (grep for copies) finds
nothing here and reports success. **The remedy is different: write the
correction as a refutation that names the wrong claim and says why it is
wrong** -- "at module scope, *not* in the factory, which is why `create_app`
cannot be where you look for it" -- so the sentence survives being read by
someone who has never seen the error. A second occurrence in the same
sub-project came at it from the adjacent angle: prose rewritten to repair a
false test-attribution introduced a *new* false attribution of the same class,
because the replacement was reasoned from a class's **name** rather than read
off the call sites. The structural repair there was the same in kind -- the
replacement enumeration now **quotes each call's argument list**, written while
reading that call, so the evidence and the claim occupy the same lines and a
future writer cannot regenerate the claim without also regenerating the quote.
**The test for whether your correction is durable: could a writer who never saw
the original error reproduce it from what is now on the page? If yes, you
deleted a claim instead of refuting one.**

**97. TO PIN A `commit()`, EXPIRE THE OBJECT FIRST.** `db.session.expire(obj)`
before the assertions is the campaign's standard instrument for a test whose
subject is *that a function persisted something*, and without it a deleted-commit
mutant survives. **This is a third case alongside facts 58 and 65, not a
restatement of either, and the discriminator is what the mutant did.** Fact 58:
one session, the function commits on `db.session`, `expire_on_commit` is `True`,
so the objects are already expired and an added `refresh()` is a redundant
SELECT. Fact 65: two sessions, so the refresh IS load-bearing. **This case: one
session, and the mutation DELETED the commit** -- so fact 58's premise is gone.
The session was constructed with `autoflush=False` (`app/__init__.py:81`, module
scope), so the test's read returns the writing session's **unflushed in-memory**
attribute state and cannot distinguish committed from pending; the assertion
passes and the mutant lives. `db.session.expire(obj)` discards that in-memory
state and forces a re-SELECT, and the mutant then fails on the seeded value.
**Measured, and it overturned a proposed taxonomy entry**: sub-project 17 first
recorded such a survivor as a new class of "harness-invisible effect" and
proposed it as a seventh cause for fact 75. A reviewer challenged the premise,
the experiment was run, `expire()` killed it cleanly at `assert 7 == 6`, and the
proposed cause was **withdrawn**. It was an ordinary fixture gap wearing a
taxonomy's clothes. **The transferable half is the challenge, not the
technique: before filing a survivor as unkillable, ask what state the assertion
is actually reading.** Two notes. **(a)** It generalises to every assertion in
this campaign that reads columns written by a function that commits on
`db.session`, which is an unswept candidate across the earlier sub-projects'
files. **(b)** It costs one line and no correctness when it was not needed, so
add it whenever persistence is the subject -- but do not write a docstring
calling it load-bearing without saying which mutant it kills, for fact 58's
reason.

**98. A STALE COUNT IN ONE ROW OF A MUTATION TABLE MEANS THE TABLE PREDATES
SOMETHING. RE-SWEEP IT; DO NOT PATCH THE ROW.** A mutation table records
`(mutant, kill kind, killers)` against **the file as it stood when the row was
measured**. Adding a test mid-task invalidates every row measured before it,
not only the rows someone noticed. Sub-project 17's Task 6 review named **two**
rows with stale kill counts; the implementer re-swept all **35** against the
final file rather than patching the two, and found **twelve** stale plus one
status change -- a mutant recorded as a SOLE kill that a later test also kills,
so it is MULTI. Totals were unaffected, which is the point: **the summary
numbers can be right while most of the detail is stale, so "the totals still
add up" is not evidence the rows do.** Patching the named rows would have left
ten wrong ones behind and made the table look freshly checked. The
status change matters more than the counts, because "sole kill" is a claim
other prose leans on -- minimality arguments, "this test alone suffices",
redundancy counts -- so when a re-sweep moves a row from sole to multi,
**grep the report for every downstream claim resting on its soleness** before
recording it. Cheapest prevention: measure the table **after** the last test is
written, or state at the top of the table which test count it was measured
against.

**99. INSIDE A FILE YOU ARE EDITING, LOCATE THINGS BY ORDINAL POSITION, NOT BY
ABSOLUTE LINE NUMBER.** "The third of the six classes below, between `X` and
`Y`, and not the last" cannot go stale; ":3397" goes stale the moment anything
above it grows, and a test file grows by construction -- every task in a
coverage slice appends to it. Sub-project 17 adopted this in Task 7 after one
locator in a committed banner went stale **twice** in the same slice, and the
same slice parked a report-only locator table that had drifted **17-18 lines**
because nobody re-derived it. **The two rules that follow from it.** **(a)
Prefer a locator that names what a reader can count** -- ordinal position among
named siblings, the enclosing class, the symbol name -- over one that names a
coordinate. Names and ordinals survive insertion; coordinates do not. **(b)
RE-DERIVE EVERY LINE NUMBER YOU COPY OUT OF A REPORT OR A REVIEW, without
exception**, because a report is written against one commit and read against
another, and a drifted pointer often lands on a line that still *reads* as if it
belonged to the claim -- which is how it survives a check. Absolute line numbers
into **production** source are still the right currency for a register entry,
where the whole cell is dated and re-verified at a known commit; this fact is
about pointers that live inside the moving file. And when you do read one,
read it with a tool that prints the number -- `grep -n`, `awk` on `NR`,
`cat -n` -- never by counting out of a bare `sed -n 'A,Bp'` (fact 95). **Fact
100 is this fact's outward-facing half**: 99 keeps a pointer valid inside the
file you are editing; 100 verifies the pointers a record aims at other files.

**100. A CITATION SWEEP NEEDS TWO PASSES, AND THE ONE PEOPLE SKIP IS THE CHEAP
ONE.** (Carried here from fact 99, where it had been drafted as a third
sub-rule; sub-project 17's final review split it out because verifying a
record's outward pointers is a different activity from locating things inside a
file you are editing, and a reader searching for "citation sweeps need two
passes" would never look under a heading about ordinal locators.) Verifying
citations by extracting them with a `file:line` pattern and
checking each against source misses **every reference written without a line
number** -- and a bare filename is the reference *most* likely to be invented,
precisely because nothing forces the writer to open the file. Sub-project 17's
register round did exactly this, and **its own attempts to SIZE the two
sets then failed four times over, which is worth more than the sizes** -- see
below. So: **pass one
resolves every `file:line` against its content; pass two resolves every bare
path against `git ls-files`.** Expect the second pass to flag deliberate
negatives ("there is no such file") and read them rather than deleting them.
This is fact 94's point wearing a tool: the sweep was applied to a candidate set
the candidates could opt out of by omitting a colon.

**A THIRD CLASS BOTH PASSES RESOLVE AND THE SWEEP STILL MISSES: a citation whose
TARGET is a file the same change is editing.** It is correct when the sweep runs
and stale when the commit lands, so scoping the sweep by path -- sub-project 17's
Task 9 scoped its verification to `app/*.py` citations -- is what lets it
through: that task's own README insertions moved fact 87 down **91** lines,
from `:3098` at commit `b881cad5` to `:3189`, and left a `tests/README.md:3098`
pointer in a committed test file landing mid-sentence in fact 81's tail instead.
(**91** is re-derived here from both commits, not copied: the review that
reported this said "+211 lines, all above it", where the file actually grew
**260** lines of which **91** were above fact 87 -- fact 99(b) catching the very
report that raised fact 99's own violation.) **Sweep every file the change touches, and rewrite a pointer INTO one of
them as a name or an ordinal (fact 99) rather than re-verifying a coordinate you
are about to invalidate again.**

**What this cost, stated without a tally -- and the tally is deliberately
absent.** In sub-project 17's register round the extraction pattern required a
digit after the filename, so it never examined a single reference written
**without** one; and one of the paths it never examined was
`tests/test_ap_create_post.py`, **a file that does not exist**, cited in a
register cell that shipped. That is the entire argument, and **no count is
needed to carry it**: the skipped class was non-empty and contained a
fabrication.

**A count WAS attempted, four times, and the disagreement is now this fact's
most useful content.** Four agents ran patterns over the same bytes to size the
two sets and produced **four different answers** -- "17 of 25", "16 of 24",
"23 / 15 / 8" and "24 / 15 / 9". Each is internally defensible; none is endorsed
here. They diverge on three axes, and **not one of the four reports stated any
of them**:

- **The pattern.** Whether the leading backtick anchor is present changes the
  answer by one. `[\w./]+` cannot cross a hyphen, so a real path like
  `docs/superpowers/plans/2026-09-04-coverage-update-tails-17.md:97-101`
  truncates, and the tail `17.md:97-101` becomes matchable -- but only once the
  anchor is gone, since no backtick precedes it. **One character of pattern, one
  unit of answer.**
- **The counting unit.** Raw matches, `(file, line)` tuples, or distinct
  filenames are three different quantities. Only the distinct-filename grouping
  reproduces the anchored/unanchored pair above.
- **The revision.** The section grew between the first count and the last, so a
  numerator from one commit with a denominator from another is not a ratio at
  all.

**KNOWN LIMITATION, recorded rather than iterated on. The absolute size of that
section's reference set was never pinned, and this fact does not claim it.**
Four careful attempts is sufficient evidence that the quantity was
**underspecified** rather than that four people miscounted. **A worked example
nobody can reproduce teaches the opposite of its lesson**, so the worked example
here is the disagreement itself.

**The operational rule, which none of the arithmetic touches.** When a count
goes into a record, state **the pattern, the revision, and the counting unit**,
and take every figure in a ratio from **one run**. And when the argument does
not need the count -- this one did not -- **make the claim without it**: "the
skipped class was non-empty and contained a fabricated path" is checkable
forever, where "8 of 23" was not checkable for a week.

**Two artifacts to expect, and they are different in kind.** `util.py:3517` is a
**human shorthand**, an abbreviation a writer chose; no pattern change removes
it. `17.md` is the **regex failing on a legitimate full path it could not
traverse** -- a character class narrower than the paths in your repo
manufactures those silently, and on the page they look exactly like shorthands.

**A CORRECTION SWEEP HUNTS THE OLD VALUE, NOT THE NEW TEXT, AND THIS IS THE
SUB-RULE THAT ACTUALLY GETS SKIPPED.** The two passes above tell you how to
verify citations you are *reading*. When you are **changing** one -- correcting
a line number, retiring a range, renaming a symbol -- there is a third move, and
it is a different grep: **search the whole repository for the value you are
retiring.** Sub-project 19 corrected `send_post`'s extent from `:88-371` to
`:88-352` and swept by grepping `88-352`, the text it had just written. Every
occurrence it found was one it had already fixed, the sweep came back clean, and
**a stale `:88-371` survived the whole fix round** in a file the sweep never had
a reason to open. A bare `grep -n '371'` would have found it in one command.
**The rule, and it is one line: after you change a number, grep for the number
you changed it FROM.** The same applies to a retired symbol name and to a
renamed fixture. **It generalises past citations**, which is why it lives here
rather than in a task brief: a search built from the corrected state can only
ever confirm the correction, and confirming your own edit is not a sweep.

**When that grep was finally run, it did not find one stale value -- it found
THREE VALUES FOR ONE FACT, and that is the yield worth expecting.** The same
function's extent was written `88-371` in the sub-project's plan (seven lines),
`88-368` in its design (five lines) and `88-352` in its tests and its register
entry; none was a typo, each was a different rule for where a function ends, and
no reader comparing any two documents would have flagged it because each
document was internally consistent. **A retired-value grep is therefore not only
a cleanup pass -- it is the cheapest available test of whether the people
writing about one thing were counting it the same way.** Run it before you
believe a correction is finished, and when it returns hits, state **the pattern,
the revision and the counting unit** with the enumeration.

**101. A HELPER THAT LOOKS LIKE STRING WORK ISSUES AN HTTP REQUEST, AND ITS
`except` IS TOO NARROW TO HIDE RESPX FROM YOU.** `is_image_url`
(`app/utils.py:247`) reads like extension sniffing and mostly is -- but before
it looks at the path it calls `mime_type_using_head` (`:270`), which issues a
real `httpx_client.head(url, timeout=5)` (`:336`). That function catches
`httpx.HTTPError` and `httpx.InvalidURL` (`:345`) and returns `''`, which is
exactly the shape that lulls you: a malformed url degrades silently, so nothing
in the helper's behaviour suggests a route is needed. **respx's
unmatched-request error is neither of those two types**, so under `http_mock` it
escapes `mime_type_using_head` entirely and surfaces inside your test at a line
that mentions no HTTP at all. **A url-bearing test must register the HEAD
route.** And count the calls rather than assuming one: `edit_post` can call
`is_image_url` **twice** -- `app/shared/post.py:410` on `post.url` and `:601` on
the new `url` -- so a test that seeds a post with a url and then edits it to a
different url pays for two HEADs to two different addresses. **This is fact 67's
shape reached by a different mechanism** -- there, an actor lookup fetches; here,
a string helper does -- and it fails identically, as an unmatched request at a
line that mentions no HTTP. The rule both share: **before mocking, read what a
helper does on the way to its answer, not just what it returns.**

**102. AN `assert_all_called` TEARDOWN FAILURE IS USUALLY A CONTROL-FLOW
MISREADING, NOT A ROUTE TYPO -- AND IT COMES IN TWO SHAPES.** `http_mock` sets
`assert_all_called=True` -- the *Fixtures for external services* section above
explains why it is a coverage check on your fixture and worth keeping -- so a
route you register and the code never requests fails the test at
teardown, in a traceback that names respx and not your misreading. Both shapes
below cost real time in sub-project 18 and are diagnosed the same way: **go read
the control flow that reaches the request, do not adjust the route.**
**(a) NESTING IS READ OFF INDENTATION COLUMNS, NEVER OFF PROXIMITY.** In
`edit_post`, `if url and (from_scratch or url_changed):` (`app/shared/post.py:565`)
sits at 4 spaces while `:566`, `:600` and `:601` all sit at 8. So `:600-601` are
**siblings of `:566` inside `:565`'s body**, not nested inside `if domain:`
(`:567`) forty lines closer to them -- and `:660`'s `elif` at 4 spaces pairs
with `:565`'s `if`, which is the confirming reading. A test whose url is
**unchanged** never reaches `:601` at all, so a GET registered for it is dead
and the run fails on a passing assertion. **(b) A GUARD THAT RAISES MAKES EVERY
ROUTE DEAD, INCLUDING THE FIXTURE ITSELF.** `app/shared/post.py:568-569` raises
inside `if domain:` and **above** `:600-601`, so a test of the banned-domain or
`.pages.dev` path must register **no routes at all** and must not request
`http_mock` -- an unused router is an `assert_all_called` failure whether or not
any route is defined. This was established by an actual teardown failure, not by
inspection. Two notes on asserting that raise: it is a **bare `Exception`**, so
`pytest.raises(Exception)` is the only available assertion and it will happily
swallow an `AttributeError` or a `TypeError` your fixture caused -- **match on
the message**, which is `f'{domain.name} is blocked by admin'`, or the test
passes for the wrong reason.

**103. `Site.admins()` NEEDS THE ROLE ROW'S ID TO *BE* `ROLE_ADMIN`, SO
`grant_permission` IS NOT ENOUGH.** `Site.admins()` (`app/models.py:3995-4000`)
takes its JOIN arm whenever `g.admin_ids` is unset, which `tests/conftest.py`
guarantees by clearing `flask.g` before every test. That arm filters on
`user_role.c.role_id == ROLE_ADMIN` (4, `app/constants.py:81`) and joins
`user_role` with an **INNER** join, so a roleless `User.id == 1` is eliminated
before the `or_` is evaluated and the `User.id == 1` disjunct cannot rescue it
-- that is **D295**. `grant_permission` (`tests/factories.py:363`) creates its
`Role` with an **auto** id (`:371`), which satisfies the join but not the
filter, so it makes a user with a role and not an admin. **The shape that works
is a get-or-create keyed on `ROLE_ADMIN` itself** -- `db.session.get(Role,
ROLE_ADMIN)`, and only insert `Role(id=ROLE_ADMIN, ...)` if it is absent -- and
the get-or-create half is load-bearing rather than defensive: `Role` rows are
**shared across calls**, so a test that makes two admins in one run (there is
one:  `test_a_moderator_and_a_separate_admin_are_both_notified` in
`tests/test_shared_post_edit.py`) hits the same primary key twice and a bare
insert raises. Nothing else about the helper needs to change for a second
admin; only that branch.

**104. WRITING AN AWARE `datetime` INTO A NAIVE `db.DateTime` COLUMN CONVERTS TO
UTC AND *THEN* STRIPS -- IT DOES NOT KEEP THE WALL-CLOCK DIGITS.** Measured
against this stack: `'2030-06-01T12:00:00+05:00'` parsed with a bare
`datetime.fromisoformat` and written to `Poll.end_poll` (`app/models.py:3782`,
a plain `db.DateTime`, i.e. TIMESTAMP WITHOUT TIME ZONE) reads back as
`datetime(2030, 6, 1, 7, 0)` with `tzinfo is None`. **Both halves matter for an
assertion.** Asserting the hour alone cannot distinguish this from the other
lossy answer -- `.replace(tzinfo=None)`, which keeps hour 12 and discards the
instant -- so **assert the hour AND `tzinfo is None`**, and choose an offset
that makes the two answers differ (a `+00:00` fixture proves nothing). Do not
reason from the value you passed in; round-trip it. This is the mechanism behind
**D297**, where three sites in one function disagree about which of the two
answers they want.

**105. A TEST DOUBLE WHOSE FIELDS ARE ALL OBJECTS CANNOT EXPRESS A *FALSY
FIELD*, AND YOU NEED BOTH SPELLINGS IN THE SAME DOUBLE.** A WTForms-shaped
double is usually built by wrapping each value in something with a `.data`
attribute. That works until the code under test distinguishes **the field object
being falsy** from **the field's `.data` being `None`** -- and `edit_post`'s
`SRC_WEB` arm does both, a few lines apart: `if input.flair:`
(`app/shared/post.py:333`), `if input.finish_in:` (`:354`) and
`hasattr(input, 'image_alt_text') and input.image_alt_text` (`:340`) test the
**object**, while `:331`, `:337` and `:338` read `.data` unconditionally.
Wrapping `None` in a field object makes it **truthy**, so the first group's
false arms become unreachable; setting the attribute to a bare `None` makes
`.data` raise `AttributeError`, so the second group breaks. **Neither spelling
works for the whole form** -- choose per field, and say in the fixture's
docstring which fields are bare and why, because the next person to add a field
will copy whichever spelling is nearest.

**106. BOTH SOURCE BRANCHES OF `edit_post` OPEN WITH `if not user:`, SO PASSING
`user=` SKIPS AUTHENTICATION ENTIRELY.** `app/shared/post.py:252` (`SRC_API`,
guarding `authorise_api_user` at `:253`) and `:316` (`SRC_WEB`, guarding
`user = current_user` at `:317`) are the same escape written twice. Supplying
`user=` therefore needs **no request context, no login and no auth token**,
which is why most tests of this function are plain unit tests -- and it is worth
knowing before you build a request context you do not need. **Both other arms
are reachable and both are now demonstrated**, so neither has to be re-derived:
for `:253`, a real `User.encode_jwt_token()` passed as `auth=f'Bearer {token}'`,
with the user's `password_updated_at` pinned well in the past because `iat` is
truncated to whole seconds (`app/models.py:1618`) and a token minted in the same
second as the password stamp loses that race -- the `api_baseline` fixture
(`tests/conftest.py:550`, `:622`) pins `2000-01-01` for exactly this reason. For
`:317`, `app.test_request_context('/')` with `login_user`, the same shape
`tests/factories.py:117-118` already uses. **The general point is that a
convenience parameter which bypasses a guard makes that guard's own arms look
unreachable in a suite that always uses the convenience.**

**107. `send_post` HAS NO USABLE DEFAULT FOR `session`, AND THE FAILURE IS ON
THE FIRST LINE.** `send_post(post_id, edit=False, session=None)`
(`app/shared/tasks/pages.py:88`) dereferences it immediately: `:89` is
`session.query(Post).get(post_id)`. **The default has NO user in the tree.** An
earlier draft of this fact said it "exists for the two Celery wrappers above it";
it does not. `make_post` (`:63-72`) and `edit_post` (`:76-85`) each build a task
session with `get_task_session()` and pass it in **explicitly** -- `:67` is
`send_post(post_id, session=session)` and `:80` is
`send_post(post_id, edit=True, session=session)`, both under `patch_db_session`
-- so neither wrapper ever takes the default. **A default parameter with no
caller that uses it is not a convenience, it is an unexploded trap**, and this
one detonates on the first statement of the function. **A direct unit test must pass `db.session` itself**; there
is no arrangement of fixtures under which omitting it works, and the
`AttributeError: 'NoneType' object has no attribute 'query'` you get instead
looks like a fixture problem rather than a signature one.

**108. FOUR EARLY RETURNS STAND BETWEEN `send_post`'s ENTRY AND ITS BUILDER, AND
THE ONE EVERYBODY REACHES FOR AS A WORKAROUND DOES NOT DO WHAT THIS
SUB-PROJECT'S OWN PLAN AND DESIGN SAID IT DID.** (That headline named "its
users" until the users were checked; see the correction below -- the ten tests
usually blamed for this had it right in writing.) To reach the Page builder at
all, a test must clear every one of
`:149-150` (`if not community.instance.online(): return`), `:153-154`
(`if community.local_only or community.private: return`), `:156-158` (a
`CommunityBan` row for this user and community) and `:159-161` (a remote
community whose instance the user has blocked, or that is instance-banned).
`Instance.online()` is `not (self.dormant or self.gone_forever)`
(`app/models.py:118-119`) and both columns default `False`, so a factory
instance is online without help. **The trap is `:153`.** Setting
`community.local_only = True` **returns at `:154`, before the builder runs at
all** -- it does not merely skip delivery at `:270`. **WHO ACTUALLY BELIEVED
OTHERWISE IS NARROWER THAN THIS FACT FIRST CLAIMED, AND THE CORRECTION MATTERS
BECAUSE THE FACT NAMED TEN INNOCENT TESTS.** The first draft said this "is what
ten tests in `tests/test_shared_post_edit.py` and one implementation plan all
assumed", and that "the workaround still worked, for a different reason than the
one written in its docstrings". **The implementation-plan half is true**
(`docs/superpowers/plans/2026-09-06-coverage-send-post-19.md:535`, and the design
at `docs/superpowers/specs/2026-09-06-coverage-send-post-19-design.md:177`, both
say `:267`). **The ten-tests half is FALSE.** All ten sites --
`tests/test_shared_post_edit.py:534`, `:562`, `:580`, `:630`, `:665`, `:680`,
`:866`, `:901`, `:1075`, `:1456` -- state the **correct** mechanism in their
docstrings: `app/shared/post.py:736` sets `federate = False`, so `:743` never
dispatches. **Those tests never enter `send_post` at all**, so neither `:270`
nor `:153-154` is their mechanism, and their docstrings say so. The `:153`
observation stands and is worth keeping, but it is a fact about **direct
`send_post` tests** -- the ones in `tests/test_shared_tasks_send_post.py` -- and
not about anything in `tests/test_shared_post_edit.py`. **A fact that blames the
wrong artefact sends the next reader to rewrite ten correct docstrings.** It is also why
the false arms of `:270` and `:333` (both `if not community.local_only:`) are
unreachable: `community` is bound once at `:91` and never rebound in `:88-352`,
so by `:270` the flag is necessarily falsy. **When a fixture line is a
workaround, write down the mechanism AND check it, because the next reader will
inherit the mechanism and not the outcome.**

**109. `send_post`'s NATURAL STOPPING POINT IS `:339-341`, AND A BUILDER TEST
THEREFORE NEEDS NO `http_mock` AT ALL.** `followers = session.query(UserFollower)
.filter_by(local_user_id=post.user_id, is_inward=True).all()` at `:339` followed
by `if not followers: return` at `:341` ends the function before the follower
fan-out. Combine that with a **local** community that has no
`following_instances()` and the entire builder -- mentions, Page, Create,
Announce construction, the Note amendment -- runs with **zero outbound
requests**. Assert on the built structures and stop there. Reach for `respx`
only when the test is about delivery, and when it is, remember fact 102: an
`assert_all_called` failure at that boundary is usually one of these returns,
not a route typo.

**110. A SENDER WITHOUT `with_keys=True` DIES AT SIGNING, BEFORE ANY REQUEST IS
ATTEMPTED, AND THE TRACEBACK BLAMES THE WRONG THING.** `make_user`
(`tests/factories.py:39`) generates a real RSA keypair only when asked, because
generation is slow, and leaves `private_key`/`public_key` as `None` otherwise
(`:49`). Signing calls `.encode()` on the private key, so a delivery test whose
sending actor was built with the default dies inside the signature machinery
with an `AttributeError` on `None` -- **and it dies before the HTTP layer is
reached**, so `respx` sees **no request at all**. That rules out the diagnosis
you would reach for: there is no unmatched-route error, because an unmatched
route needs a request to not match. What you get is an unmet `assert_all_called`
in teardown, which is fact 102's shape -- a control-flow failure wearing a
routing failure's clothes. The factory's own docstring says it (`:47`):
**any test asserting on delivery must build its sending actor with
`with_keys=True`.** Note the actor that matters is whichever one signs: on
`send_post`'s Announce path that is the **community** for both Announces
(`:298`, `:302`) and the **user** for both direct Creates (`:300`, `:306`).
**The signer grouping is right; an earlier draft's LABELS for it were swapped,
and they are corrected here rather than quietly reworded.** That draft called
`:298` and `:302` together "the group Announce". They are two different
activities: **`:298` sends `microblog_announce`** (built at `:285-293`, whose
`object` is the bare `post.ap_id`), to an instance in `MICROBLOG_APPS` when
`activity == 'create'`; **`:302` sends `group_announce`** (built at `:276-284`,
whose `object` is the whole `create`), to everything else. Both are signed with
`community.private_key`, which is why the grouping held while the names did not.
Symmetrically, `:300` is the **microblog update** -- the same microblog instance
when `activity != 'create'`, sent as a bare `create` from the user -- and `:306`
is the Create posted to a **remote community's** `ap_inbox_url`, also from the
user. **Two sends that share a signer are not the same activity, and a fixture
fact that names them by signer will mislabel them the moment someone asserts on
the body.**

**111. TWO BARE `except: pass` CLAUSES THAT LOOK IDENTICAL ARE NOT, AND A TEST
ASSERTING AN ABSENCE THROUGH ONE OF THEM PROVES NOTHING ABOUT WHY.**
`send_post`'s mention scanner calls `search_for_user` twice --
`app/shared/tasks/pages.py:106` under `except: pass` at `:107-108`, and `:112`
under `except: pass` at `:113-114` -- and the two are the same three tokens with
opposite reachability. The **remote** handler at `:113-114` is live:
`search_for_user` (`app/user/utils.py:85-158`) raises at `:98` for a
blocked-instance host, so a mention of a user on a banned instance is silently
dropped and a test can pin that. The **local** handler at `:107-108` is
**unreachable for every input** (it was written "dead" here, which claims more
than was proved -- the clause is bare and a DB-layer error out of
`app/user/utils.py:101` would still land in it): a bare local name never
satisfies `:88`'s `if '@' in address`, so `:94`'s `if server:` is false and
`:98` is unreachable. **The consequence for a
test is that "the mention produced no recipient" has at least three causes here
and the assertion cannot distinguish them** -- the user did not exist, the host
was banned, or the name matched the author so `app/shared/tasks/pages.py:104`
(`if user_name != user.user_name:`) never called at all. Assert on the
*reason* (seed the banned instance, or the missing user, and say which in the
docstring), not merely on the empty `recipients` list; and see fact 75's cause 8
before trying to kill a mutation of the dead one.

**`app/shared/tasks/notes.py:100-101`/`:106-107` is the identical shape a
second time, and a closing attempt against it was written and withdrawn.**
`send_reply`'s LOCAL arm (`try: recipient = search_for_user(user_name) /
except: pass` at `:98-101`) cannot be reached BY THE RAISE, for the same
reason as `pages.py`'s LOCAL arm above -- `user_name` carries no `@`, so
`search_for_user`'s `server` is always `''` and its one `raise`
(`app/user/utils.py:98`) never runs. **That is narrower than calling the
clause unreachable outright**: `if server:`'s `else` branch still runs a
database call, `already_exists = db.session.query(User).filter_by(
user_name=name, ap_id=None).first()` (`app/user/utils.py:101`), inside the
same bare `try`, and a DB-layer exception there would still land in this
`except: pass` -- unreachable-by-the-raise, not unreachable. The REMOTE arm
(`:102-107`) passes an `@`-qualified address and is live. A sub-project 28
task wrote a mock-forced test against the LOCAL arm anyway, raised the
module's floor 99 -> 100 on it, and had the change rejected and reverted
once `tests/test_shared_tasks_send_answer.py:977-989`'s already-registered
proof of raise-unreachability was found -- see the campaign register's
sub-project 28 section, item 4, for the full withdrawal and the process rule
it establishes.

**112. `Community.is_local()` IS A DISJUNCTION, SO CLEARING OR SETTING `ap_id`
ALONE DOES NOT MAKE A FACTORY COMMUNITY REMOTE.** `app/models.py:795-796` is
`return self.ap_id is None or self.profile_id().startswith(f"{SERVER_URL}")`.
**Both** disjuncts must be false, and `profile_id()` (`app/models.py:787-789`)
reads `ap_profile_id`, falling back to a `SERVER_URL`-based value only when that
column is unset. `make_community` (`tests/factories.py:122`) takes `host` as a
parameter but **defaults it to `'test.piefed.local'`**, which is exactly
`SERVER_NAME` in the test config (`tests/conftest.py:69`), and writes
`ap_profile_id=f'https://{host}/c/{name}'` at `:141`. So a community given an
`ap_id` still answers `is_local() == True` through the second disjunct, and a test that expected to
exercise `send_post`'s remote branch (`:305-307`) silently takes the local
Announce branch instead -- with no error, because both branches send. **A
sub-project 18 correction said "`is_local()` tests `ap_id`" and was itself
incomplete in exactly the direction that matters**, which is the general
warning: a correction to a one-line predicate is worth reading the predicate
for, because "it tests X" and "it tests X **or** Y" fail differently and only
the second one fails silently. Set `ap_profile_id` to a foreign host as well.
**EXTENDED BY SUB-PROJECT 20 RATHER THAN GIVEN ITS OWN NUMBER, BECAUSE
`User.is_local()` IS THE SAME DISJUNCTION AND FAILS BY THE OPPOSITE SYMPTOM --
a reader who learns only one of the two will mis-predict the other.**
`User.is_local()` (`app/models.py:1251-1252`) is
`return self.ap_id is None or self.ap_profile_id.startswith(SERVER_URL)`. It
reads `ap_profile_id` **directly**, with no `profile_id()` fallback in front of
it, so setting `ap_id` alone on a factory user does not make the user remote and
does not silently answer local either -- it **crashes**, with
`AttributeError: 'NoneType' object has no attribute 'startswith'` raised from
inside `is_local()` at whatever production line happened to call it. The
`Community` half above falls back to a computed default and therefore decides
LOCAL in silence. Same shape, opposite failure: one class blows up in your face
and the other lies to you. Sub-project 19 hit the `Community` half (a guard
that silently never opened); sub-project 20's Task 3 hit the `User` half while
writing the true-arm test for `send_reply`'s `:129` ternary. **The remedy is the
same for both: set the second disjunct's column too, never the first alone.**

**113. THE DELIVERY PATH RUNS `is_invalid_get_request_uri` ON POSTs TOO, AND IT
FALLS THROUGH TO A REAL DNS LOOKUP.** The name says GET; `signed_request`
(`app/activitypub/signature.py:442`) calls it for every request it signs,
`if is_invalid_get_request_uri(uri): raise ValueError("URI is invalid")`. The
validator (`app/utils.py:5494-5536` by `ast`) short-circuits under
`current_app.debug` (`:5495-5496`) and otherwise resolves the host with
`socket.getaddrinfo` (`:5520`) to reject private and loopback addresses,
**failing open** on `gaierror`/`timeout` (`:5521-5522`). **The extent read
`5494-5533` until the final review of sub-project 19, and the three lines it
dropped QUALIFY THIS FACT'S OWN HEADLINE.** `:5535-5536` is a trailing
`except Exception: return True` wrapping the whole body -- it **fails CLOSED**,
rejecting the URI, for anything the resolver or the parse raises that is not
`gaierror`/`timeout`. So "it falls through to a real DNS lookup" and "fails
open" are both true of `:5521-5522` specifically and **not** of the function as
a whole: the same validator fails open on one exception class and closed on
every other. **Two committed values disagreed about this range**, and the test
file had the right one: `tests/test_shared_tasks_send_post.py:166` has said
`5494-5536` since it was written. **When two of your own documents cite
different extents for one function, the one that is WRONG is not reliably the
older one** -- check both against `ast` rather than assuming the newer citation
was derived. So a suite that mocks HTTP but not DNS
still touches the resolver on every outbound federation call, and its speed
depends on how fast the network says no. **The fix is a delegating stub, not a
blanket one**: intercept `getaddrinfo` for `.example` hosts and return a canned
**global** address, delegating everything else. That keeps the suite off the
resolver while leaving the validator itself genuinely running -- a stub that
made the function return `False` outright would have deleted the check the tests
are supposed to be exercising.

**114. `send_reply`'s `parent_id` IS A BRANCH, NOT A LOOKUP DETAIL, AND THE TWO
ARMS BIND `parent` TO DIFFERENT CLASSES.** `app/shared/tasks/notes.py:83-86` is
`if parent_id:` -> `parent = session.query(PostReply)...one()`, `else:` ->
`parent = reply.post`. So `parent` is a `PostReply` for a nested reply and a
`Post` for a top-level one, and everything downstream that touches `parent`
inherits that: `:90` seeds `recipients` with `parent.author`, `:120` excludes
`parent.author.id` from notification, and `:175` writes
`'inReplyTo': parent.public_url()`. **`inReplyTo` is the observable**, and it is
the only one that discriminates cleanly -- `PostReply.public_url()` and
`Post.public_url()` produce different URL shapes, so a test that asserts it
cannot pass under the other arm. A test that instead asserts
`Notification.query.count() == 0` proves nothing here, because both arms can
reach zero notifications for unrelated reasons. Pass `parent_id` explicitly in
every helper that calls `send_reply`; a helper that defaults it silently tests
one arm twice.

**115. `recipients` IS SEEDED WITH THE PARENT'S AUTHOR BEFORE THE MENTION SCAN
RUNS, WHICH MAKES TWO THINGS UNOBSERVABLE UNDER THE DEFAULT SEED.**
`app/shared/tasks/notes.py:90` is `recipients = [parent.author]`, not `[]`.
Two consequences, and both bit sub-project 20's Task 1:
(a) the dedup loop at `:110-114` runs against a **non-empty** list from its very
first iteration, so there is no "first mention is never compared" arm to cover,
and a test that mentions the parent's author exercises the `add_recipient =
False` path at `:113` rather than the append at `:116` (which `:115`'s
`if add_recipient:` guards);
(b) the parent's author is a **delivery target that is excluded from
notification** -- `:120` is `if recipient.is_local() and recipient.id !=
parent.author.id:`, while `:154-158` still puts them in `tag` and `cc` -- `cc`
initialised at `:154`, the loop `:156-158`, with `tag.append` at `:157` and
`cc.append` at `:158` -- and `:227` still delivers to them. Those two roles are easy to conflate and a test that
conflates them cannot fail.
**The trap is that the default seed makes the reply's author and the parent's
author the same person**, which collapses `:97`'s
`if user_name != user.user_name:` and `:120`'s second conjunct onto the same
state, so neither arm is observable. The fix is a helper that reauthors the
parent to a third user (`_reauthor_the_parent` in
`tests/test_shared_tasks_send_reply.py`) -- and note it depends on session
expire/reload mechanics, so reauthor **before** the call under test, not after.

**116. A NULLABLE COLUMN IS NOT EVIDENCE THAT A MISSING GUARD IS A DEFECT.
SETTLE REACHABILITY OVER THE DISPATCHERS, NOT OVER THE COLUMN.** Sub-project 20
opened on a real asymmetry: `app/shared/tasks/pages.py:97` guards its mention
scan with `if post.body:` and its twin `app/shared/tasks/notes.py:92` does not,
and `PostReply.body` is `db.Column(db.Text)` (`app/models.py:2901`), nullable.
The column's nullability makes the crash **storable**; it says nothing about
whether anything can dispatch that row into the function. Enumerating the
dispatchers settled it in the other direction: `send_reply` is called only from
`notes.py:59` and `:72`, `task_selector('make_reply'|'edit_reply')` appears at
exactly three sites (`app/shared/reply.py:194`, `:232`, `app/post/routes.py:928`),
all three write `body` through `piefed_markdown_to_lemmy_markdown`
(`app/utils.py:1233-1237`), and that function **raises the identical `TypeError`
on `None` before any commit** -- so the bad state dies in the writer. The
`None`-capable writers that do exist (`app/activitypub/util.py:3020`, `:2639`,
`app/community/util.py:272`) are all inbound and dispatch no task.
**Two method points, both of which cost time when skipped.** Enumerate
**call sites of the task**, not call sites of the function -- a `grep` for the
function name misses the Celery indirection in both directions. And **execute
the suspected raiser** rather than reading it: one line in a REPL settled what
two documents had been arguing about. The finding is registered as latent
(D310), the probe test is kept as characterization, and no guard was written --
a guard on a state no writer can produce has mutants no production-shaped test
can kill.

**117. A WRONG `--cov` TARGET COLLECTS NOTHING, WRITES NO JSON, AND EXITS 0.**
`pytest-cov`'s `--cov` takes a **module** path, not a file path.
`--cov=app/shared/tasks/notes.py` produces `CoverageWarning: Module
app/shared/tasks/notes.py was never imported (module-not-imported)` followed by
`CoverageWarning: No data was collected (no-data-collected)`, writes **no**
`--cov-report=json:` file at all, and **pytest still exits 0**. A full 257.23s
suite run was spent this way before the warnings were read. The dotted form
`--cov=app.shared.tasks.notes` works, and `--cov=app` is the campaign's standard
because one run serves every module. **Re-measured 2026-09-06 rather than
carried forward**: `pytest tests/test_shared_tasks_send_reply.py -q
-k targets_data_uses --cov=app/shared/tasks/notes.py --cov-branch
--cov-report=json:/tmp/badcov.json` printed both CoverageWarnings, `1 passed`,
exit **0**, and `/tmp/badcov.json` did not exist afterwards.
**THIS FACT'S HEADLINE IS TRUE AND ITS ORIGINAL COMPARISON WAS NOT.** It used to
say this is "the same failure mode as the `session_timeout` truncation -- a
green exit code over a run that produced nothing". A session timeout exits
**non-zero** (fact 118), so the two are not the same failure mode. What they
share is narrower and still worth knowing: **a run can look finished and have
produced nothing.** This one really does exit 0, because every test really did
pass -- only the *coverage* side was a no-op. So here the exit code cannot help
you and **the mtime of the report file is the only check**; for a timeout the
exit code is the fastest check you have. Two different traps, two different
detectors, and conflating them was what put a false claim in this document.

**118. A SESSION TIMEOUT EXITS 1. IF YOU PIPE PYTEST, YOU THROW ITS EXIT CODE
AWAY -- THAT, NOT PYTEST, IS WHERE THE "EXIT 0" CAME FROM.** For several
sub-projects this document and the campaign's Global Constraints blocks
asserted that "pytest still exits 0 on a session timeout, so a truncated run
reads as green -- never trust the exit code". **That is false**, and the wrong
justification is worse than no justification, because an agent told never to
trust the exit code will ignore a genuine failure. Measured 2026-09-06, four
commands, all reproducible from this page:

    # 1. pytest directly, inside the container
    podman-compose -f compose.test.yaml exec -T test-runner \
      pytest tests/test_shared_tasks_send_reply.py -q -o session_timeout=1
    # -> "!!! session-timeout: 1.0 sec exceeded !!!", "1 passed", exit 1

    # 2. the same thing through the wrapper -- it propagates
    ./run_tests.sh tests/test_shared_tasks_send_reply.py -q -o session_timeout=1
    # -> exit 1

    # 3. control: the wrapper propagates other codes too
    ./run_tests.sh tests/test_shared_tasks_send_reply.py -q -k no_such_test_name_exists
    # -> exit 5 (pytest's "no tests collected")

    # 4. the actual culprit
    ./run_tests.sh tests/test_shared_tasks_send_reply.py -q -o session_timeout=1 2>&1 | tail -1
    # -> $? is 0, but ${PIPESTATUS[0]} is 1

`pytest.ini:26-27` ("Checked BETWEEN tests, so the run stops at the first test
to start after the budget is spent, **and exits non-zero**") was right the whole
time, and the false claim sat three lines from a citation of it. **The rule to
carry is about the shell, not about pytest**: `$?` after a pipeline is the
status of the LAST command in the pipe, so `pytest ... | grep`, `| tail` or
`| tee` reports grep's, tail's or tee's success. Either do not pipe, or read
`${PIPESTATUS[0]}`. **And keep checking the test count and the report mtime
anyway** -- they catch the case fact 117 records, where the exit code is
genuinely 0 and genuinely uninformative. Two detectors, both cheap, neither
sufficient alone.

**How the wrong version survived so long is the transferable part.** It was
never measured; it was inferred from one observation made through a pipe, then
copied verbatim into two designs, two plans and this file, where each copy
corroborated the others. **Four agreeing citations of an unmeasured claim are
one claim, not four** -- fact 88's rule about N agreeing prior citations cuts
both ways, and the tie-breaker is a five-second experiment, not a vote.

**119. TWO ROW LOOKUPS FORTY LINES APART IN ONE MODULE RAISE DIFFERENT
EXCEPTIONS FOR A MISSING ID, SO "PASS AN ABSENT ID" IS NOT ONE TECHNIQUE BUT
TWO.** `app/shared/tasks/notes.py:81` is
`session.query(PostReply).filter_by(id=reply_id).one()`, which raises
`sqlalchemy.exc.NoResultFound` **at that line** when the row is absent.
`app/shared/tasks/notes.py:246` is `session.query(PostReply).get(post_reply_id)`,
which returns **`None`** and raises nothing; the failure surfaces later and
elsewhere, as `AttributeError` at `:248` when the guard dereferences
`post_reply.community`. Same file, same model, same intent, two error contracts.
**This cost a fix round in sub-project 21**: the plan reached the error arm by
"pass a missing id" and generalised `send_reply`'s style to `send_answer`'s, so
the `pytest.raises` named the wrong exception and the test failed for a reason
that had nothing to do with the arm it was written for. The tests that stand
record both contracts explicitly --
`tests/test_shared_tasks_send_reply.py:1703` and `:1731` assert `NoResultFound`
and say in their docstrings that it is **not** the `.get()`-returns-`None`
shape, and `tests/test_shared_tasks_send_answer.py:605` asserts `AttributeError`
and names the two-step path that produces it. **The rule: read the lookup you
are about to defeat before writing the `raises`.** A sibling function is not
evidence about this one, and the two styles are close enough in a diff to look
interchangeable. The same split exists between `.one()`, `.first()`,
`.one_or_none()` and `.get()` generally; only `.one()` raises.

**120. A MUTATION THAT APPLIES CLEANLY AND LEAVES EVERY TEST GREEN IS NOT
EVIDENCE ABOUT THE TESTS UNTIL YOU CONFIRM THE SUBSTITUTION CHANGED THE
PROGRAM.** Three outcomes look identical in the pytest output and mean opposite
things:

- a **survivor** -- a real semantic change that no test caught. A gap in the
  tests, and the only one of the three that is a finding about coverage.
- an **equivalent mutant** -- a real semantic change that provably cannot be
  observed. A dead end; fact 75 catalogues the causes.
- a **no-op substitution** -- nothing was mutated. The `sed` applied, the file
  changed, the program did not. It says **nothing whatsoever** about the tests.

Sub-project 21's M4 was the third kind: a `sed` prepending a dead `if False`
expression to a `del`, which produced a line whose trailing statement was the
original statement character-for-character, still executing unconditionally.
It applied perfectly, ran green, and was first written up as a survivor and then
as an equivalent mutant before being measured -- the mutated line was executed
against a dict and removed the key exactly as the original did. **The committed
mutation record at the end of `tests/test_shared_tasks_send_answer.py` works
this through in full, with the defective `sed`, the line it produced, and the
fallback form that is a real double assertion-kill; read it there rather than
re-deriving it.** The general failure mode the plan anticipated was a *syntax*
error ("if it does not apply cleanly, use the fallback"); the one that occurred
was silent. **Before filing a green mutation run as any kind of result, show
that the mutant computes something different** -- run the changed line, or diff
the behaviour, not just the file.

**121. A TASK FUNCTION THAT OPENS `get_task_session()` WITHOUT
`patch_db_session` SPLITS ITS READS ACROSS TWO SESSIONS, AND IN A TEST THE
SECOND ONE IS YOUR OWN.** `make_reply` (`app/shared/tasks/notes.py:56`, `:58`)
and `edit_reply` (`:69`, `:71`) open a task session **and** enter
`with patch_db_session(session):`, so `db.session` *becomes* that task session
for the call. `send_answer` (`:243`) opens one and uses it directly, with no
patch -- so its own two lookups (`:245`, `:246`) run on the task session while
`Community.following_instances()` (`app/models.py:843`) and
`User.has_blocked_instance()` (`app/models.py:1470`) run on `db.session`, which
in a test is the session the fixtures seeded through. **Measured, not read**: an
`InstanceBlock` flushed-but-not-committed into `db.session` makes `send_answer`
skip a follower it would otherwise deliver to, and a `CommunityMember` flushed
the same way makes it deliver to a follower the task session cannot see -- while
inside `patch_db_session` the identical row is invisible. **Both directions of
the hazard come from the SAME uncommitted state**, and they differ only in
whether the extra visibility makes the function do more or do less. A test can
accidentally *pass* because a row it flushed but never committed was visible
through the unpatched half and made the function **deliver** -- the
`CommunityMember` case above; and a test can accidentally *fail* because a row
it flushed but never committed was visible through the unpatched half and made
the function **skip** -- the `InstanceBlock` case, where a "delivery happened"
assertion dies on state the test would have reasoned was invisible.
**A COMMITTED row is NOT this mechanism and cannot be**: both sessions bind to
the same engine, the fixture commits rather than holding an outer transaction,
and isolation is READ COMMITTED, so a committed row is visible to **both**
halves and nothing diverges. D312 says the same thing from the other side --
"both sessions are bound to the same engine and read the same committed rows".
So if one of the other unpatched task functions fails on an ordinary committed
fixture row, **the cause is somewhere else**; do not add `patch_db_session`
scaffolding to chase a mechanism that is not there. Fact 64 is
the other end of this: `get_task_session()` leaves `autoflush=True` where
`db.session` in this app is `autoflush=False` (`app/__init__.py:81`), so the two
sessions do not even agree about when a pending row becomes visible. **Ask which
session each read goes through before deciding what a seeded row proves** -- and
the cheap way to find out is a `do_orm_execute` listener keyed on session
identity, which answers it in one run instead of by tracing callees. The
asymmetry itself is registered as D312.

**122. AN ACTIVITY BUILDER THAT `del`s KEYS BEFORE DELIVERY CANNOT BE ASSERTED
THROUGH A RECORDER HOLDING THE DICT -- ASSERT ON THE SERIALIZED BYTES.**
`send_answer` builds `lock`, `undo` and `announce` as locals that are never
persisted, and mutates them **in place**: `app/shared/tasks/notes.py:266` strips
`lock['@context']` when `lock` is about to be nested inside `undo` at `:272`,
and `:281` and `:284` strip `undo`'s and `lock`'s when either is about to be
nested inside `announce` at `:294`. The outermost object keeps the `@context` it
was built with. **A recorder that captures the dict object captures a
reference**, so by the time the assertion runs it reads the dict in its
post-`del` state and cannot tell "this key was never there" from "this key was
deleted after you saw it". The tests therefore assert on the JSON body respx
captured at `app/activitypub/signature.py:494` -- the bytes that actually left
-- via `_sent_activity()` in `tests/test_shared_tasks_send_answer.py`. **The
generalisation is not about `@context`**: any builder that mutates a payload
between construction and send has this property, and a recorder is only safe
when it snapshots (`copy.deepcopy`, or a serialization) at capture time. There
is a second reason to prefer the wire here, recorded because it has already gone
wrong once: sub-project 19 got a `@context` claim wrong by **reasoning** about
which deletes run on which path, and declared an arm unreachable that the
ordinary path reached. Assert, do not argue.

**123. WHEN A TEST'S PROOF DEPENDS ON WHICH ROW AN UNORDERED QUERY RETURNS
FIRST, ASSERT THE ORDER -- FACT 90 SAYS AVOID THE DEPENDENCE, THIS SAYS WHAT TO
DO WHEN THE DISCRIMINATING CASE REQUIRES IT.** `Community.following_instances()`
(`app/models.py:842-851`) ends in an unordered `.distinct().all()` with no
`ORDER BY`, so which follower comes back first is a property of the current
query plan. `tests/test_shared_tasks_send_answer.py:484`'s skip-then-deliver
test is the case where the dependence cannot be designed away: a **single**
follower cannot show that a guard skips an instance *without also stopping the
loop*, because "skipped" and "loop ended" look identical with one row; two are
needed, and the proof only holds if the **skipped** one comes first. So the test
asserts `[i.domain for i in s.community.following_instances()] ==
['mute.example', 'fan.example']` before acting, with a failure message saying
why. **The failure mode this prevents is silent, not loud**: if the planner ever
returns them the other way, a mutant that turned "skip and continue" into "skip
and break" would still leave one delivery and one `ActivityPubLog` row, and the
test would keep passing while testing less than its name claims. An explicit
order assertion converts that into a failure with an explanation. **Where fact
90 applies -- two rows inside one arm, where the order is incidental -- keep
following it and remove the dependence instead.** The two facts are the same
observation about unordered SQL with opposite remedies, chosen by whether the
order is load-bearing for the proof. **AMENDED BY FACT 131, READ IT BEFORE
APPLYING THIS ONE.** Sub-project 23 carried this remedy into its own
skip-then-deliver tests and then measured them failing on a first run and passing
on an immediate re-run **on a clean, unmutated tree**: an order assertion makes an
incidental dependence fail loudly, which is not the same thing as making the test
reliable. Fact 131 narrows this fact to the case where the ordering is the test's
**subject**, and prescribes controlling the sequence -- patching the method to
return a known one -- where it is not. **TWO tests still carry the superseded assertion form, and BOTH are named here
because the first draft of this sentence named only one.**
`tests/test_shared_tasks_send_answer.py:484`
(`test_one_following_instance_is_skipped_while_another_receives`, assertion at
`:511-514`) is the example the paragraph above uses, and
`tests/test_shared_tasks_send_post.py:2646`
(`test_one_following_instance_is_skipped_while_another_receives_the_move`,
assertion at `:2669-2672`) carries the byte-for-byte identical idiom, down to the
failure message. **`grep -rn 'ordered ==' tests/` returns exactly these two and
nothing else** -- run it rather than trusting this count, because it is the
enumeration and not the prose that keeps this honest. Neither has been audited
for the flake fact 131 measured; that is a statement about what has been
measured, not an argument that either is safe, and **fixing only the named one
would leave the class open with nothing pointing at the other** -- which is this
campaign's own copy-hunt rule, and the reason the enumeration is written out
instead of "the example".

**124. ONE FUNCTION, THREE `.get()` LOOKUPS, TWO OPPOSITE FAILURE MODES --
BECAUSE ONLY THE FIRST ONE IS GUARDED. FACT 119 IS ABOUT LOOKUP STYLE; THIS IS
ABOUT GUARD PLACEMENT, AND IT BITES WHEN THE STYLE IS UNIFORM.**
`move_post` (`app/shared/tasks/pages.py:373-387`) is fifteen lines and makes
three `session.query(...).get(...)` calls, all of which return `None` rather
than raising. `:378` looks up the `Post` and `:379` guards it
(`if post and not post.deleted:`), so a **missing post is silently swallowed**
and the function returns having done nothing. `:380` and `:381` look up the two
`Community` rows and **nothing guards either**; both are handed straight to
`move_object` at `:382`, whose `:393` is
`if isinstance(origin, Community) and isinstance(target, Community):` and whose
`:396` raises. So a **missing community raises `TaskError`** out of the same
call. Same module, same function, same lookup style, opposite contracts,
decided entirely by which result has an `if` in front of it. The committed
tests pin both directions and name them:
`test_move_post_does_nothing_when_the_post_is_missing`
(`tests/test_shared_tasks_send_post.py:2696`) asserts the swallow, and
`test_move_post_rolls_back_and_re_raises_on_a_bad_community` (`:2733`) passes a
real `Community` as `origin` and a missing (`None`) `target` and asserts the
raise, the `rollback` and the re-raise. **The practical rule is fact 119's with
the other half filled in**: read the lookup you are about to defeat, AND read
what stands between it and its first dereference. A uniform lookup style is
what makes this one invisible -- there is no `.one()`/`.get()` tell to notice,
only an absent `if`. **The asymmetry also shows up in mutation**: sub-project
22's M1, an `and`->`or` at `:393`, killed `move_post`'s rollback test as a
third, unpredicted victim precisely because that test reaches `:393` through
the unguarded pair.

**125. WHERE A NEW SYMBOL GOES CAN LEGITIMATELY BE DECIDED BY CITATION
ARITHMETIC -- BUT STATE THE REASON AS AN EXTREMUM, NOT AS A COUNT, BECAUSE
COUNTS IN THIS CAMPAIGN DO NOT REPRODUCE AND EXTREMA DO.** Sub-project 22
needed a `TaskError` class and put it at the END of `app/utils.py`, not beside
`get_task_session` (`:3673-3675`) and `patch_db_session` (`:3679`) where it
belongs thematically, because inserting there would shift every line below it
and this campaign's chronic defect is stale citations. **The decision was
right. The justification was a number, and the number did not survive
re-derivation.** The design and the class's own docstring said "358
`utils.py:NNN` citations in tracked files, 135 of them at or after `:3673`". **As
of `2a63f063^`, the commit the class was appended in**, the 135 reproduces
exactly; the 358 does not -- the same sweep gives **322** occurrences (324 if
only the `*.po` glob is excluded, **844** if the whole `.po` family is counted)
and **191** distinct strings (the 324-sweep has 192 -- the distinct counts
differ by the same file the occurrence counts do, and attaching 192 to the
322-sweep was this fact's own first draft getting it wrong), and a reviewer's
third sweep gave 239 unique / ~330-346 occurrences / 105 at-or-after. **Three
things make those sweeps disagree, and all three are worth knowing before
running one.** (a) The `.po` files carry auto-generated `#: app/utils.py:NNN`
gettext source references that a human "citation" count means to exclude --
**and the obvious glob `app/translations/**/*.po` is itself incomplete**,
missing `app/translations/lt/LC_MESSAGES/messages.po.original`, which carries
the same references and is exactly the 322-versus-324 gap. (b) A naive `:NNN`
regex stops at the START of a range citation, so it reports the maximum of
`app/utils.py:5736-5743` as 5736. (c) **And "count the endpoint" is not a rule
you can then apply uniformly** -- one citation, `app/utils.py:3663-3673` in
D60's cell, straddles the `:3673` boundary, so endpoint-counting the
at-or-after test gives 136 where start-counting gives 135. Endpoints everywhere
yields 136 and `:5743`; starts everywhere yields 135 and `:5736`; **only
endpoint-for-the-maximum with start-for-the-threshold reproduces both published
figures**, and it is the reading that matches what a range means -- it BEGINS
at a line and REACHES another. The same happened to the companion figure:
the design's "**96** `pages.py:NNN` citations" reproduces as 82, 113, 35 or 41
at its own commit depending on the path prefix and the `.po` question, and as
none of them is it 96.

**The fix is not a better grep. It is to state the constraint as an extremum,
AND TO ANCHOR IT TO A COMMIT** -- because the un-anchored form of this very
claim was falsified inside the round that wrote it. The argument is: as of
`2a63f063^` the highest `app/utils.py` line cited anywhere was `:5743` and the
file's last line was `:5798`, so appending below `:5798` could not move a cited
line **however many there were**, and citations written afterwards point into
the appended region by construction. One number, stable under the `.po`
question that makes every count diverge, and checkable in one command.
**Then the register commit two commits later cited `app/utils.py:5801` -- the
new class's own line -- and at that commit the sweep gives 323 occurrences, 136
at or after `:3673`, and a maximum of `:5801`.** For a few hours the findings
file contradicted itself: one paragraph said the maximum was `:5743`, another
cited `:5801`. **A claim about citation counts is falsifiable by the commit
that makes it**, and a register entry citing a symbol is the most likely
falsifier of any claim about where that symbol's file is cited. Prefer
`max(cited line) < insertion point, as of <commit>` to `N citations would
break`, and write the as-of clause even when the claim is true as you type it.
Fact 98 is the same lesson for mutation tables: a count in committed prose is a
claim with a short shelf life, and one that two derivations disagree about
should be replaced, not arbitrated.

**126. A MUTATION COMMAND WRITTEN IN A PLAN IS UNTESTED CODE. DRY-RUN EVERY
SUBSTITUTION WITHOUT `-i`, READ THE LINE IT PRODUCES, AND RECORD THE COMMAND
YOU ACTUALLY RAN RATHER THAN THE ONE THE PLAN PROPOSED.** Three consecutive
rounds have now been bitten by a different failure of the same kind, and none
of the three was a failure of the *tests*:

- **It does not parse.** Sub-project 22's M5 was written
  `sed -i '417s\|^\|#\|'`, which exits with
  ``sed: -e expression #1, char 12: unknown option to `s'``. Nothing was
  mutated and nothing ran. The working form is `sed -i '417s|^|#|'`.
- **It parses, applies, and mutates nothing.** Sub-project 21's M4 produced a
  line whose trailing statement was the original character-for-character. That
  is fact 120's *no-op substitution*, and it was written up twice, wrongly,
  before being measured.
- **It parses, applies, mutates -- and the LABEL is wrong.** Sub-project 21's
  M1 was described as negating a whole guard; `sed` replaces only the first
  match on a line, so it negated one conjunct and left two others unmutated
  while the record implied they were proved. Fact 68's defect class inverted.

**The three failures need three different checks and only the second is fact
120's.** Run the `sed` without `-i` first and read BOTH its stdout and its
stderr -- the first failure is invisible on stdout alone; then diff the file;
then read the produced line back into the record verbatim. The committed
mutation records at the ends of `tests/test_shared_tasks_send_post.py` and
`tests/test_shared_tasks_send_answer.py` both now carry the produced line under
every entry, which is the form that makes all three failures visible at review
time instead of at re-derivation time.

**THE VOCABULARY IS LOAD-BEARING AND THIS FACT'S OWN SOURCE RECORD GOT IT
WRONG.** The `send_post` record's summary line called M5 "1 no-op substitution",
which is the *second* class, not the first -- and a later round grepping for
prior art would then have applied the wrong check, diffing a file against a
`sed` that never executed. It is corrected in place with the old wording named.
**"Nothing happened" is not one outcome but two**: the command did not run, or
the command ran and the program did not change. Say which.

**127. `@context` IS RE-ADDED AT THE TOP LEVEL ONLY -- BY TWO INDEPENDENT
SITES -- WHICH IS EXACTLY WHY A NESTED-OBJECT `@context` ABSENCE ASSERTION IS
MEANINGFUL AND A TOP-LEVEL PRESENCE ASSERTION IS NOT.** `post_request`
(`app/activitypub/signature.py:100-101`) and `HttpSignature.signed_request`
(`:454-455`) each run the identical `if '@context' not in body:
body['@context'] = default_context()` on the outermost `body` before it is
serialized. Neither walks into `body['object']`. Two consequences, opposite in
sign, and this file already records only one of them:

- **A top-level `@context` cannot be pinned.** A mutant deleting the two-line block that
  re-adds it (`app/shared/tasks/pages.py:310-311`,
  `app/shared/tasks/notes.py:225-226`) is an EQUIVALENT MUTANT: the key
  comes back one frame later, with the same value, in the same trailing
  position, and no assertion reachable from the wire can tell the two apart.
  Established by sub-project 19 and repeated at
  `tests/test_shared_tasks_send_reply.py:1498-1513`.
- **A NESTED `@context` absence CAN be pinned, and that is the half worth
  writing down**, because it is what makes three sub-projects' assertions real
  rather than accidentally true. `del`s of a nested object's `@context`
  (`app/shared/tasks/pages.py:417`, `app/shared/tasks/notes.py:266`, `:281`,
  `:284`) are NOT undone, because the
  re-adders only ever see the envelope. `assert '@context' not in
  sent['object']` therefore fails when the `del` is removed -- measured, not
  argued: sub-project 22's M5 commenting out `pages.py:417` is a SOLE
  assertion-kill of
  `test_a_local_community_announces_the_move_and_strips_its_inner_context`
  (`tests/test_shared_tasks_send_post.py:2574`), and
  `tests/test_shared_tasks_send_answer.py:434-436` asserts absence two levels
  deep.

**The generalisation: before asserting that a key is absent from a delivered
payload, find every writer between construction and the wire and check which
level it operates on.** Here the answer is "top level only, twice"; a re-adder
that recursed would have made every one of those assertions vacuous while
leaving them green. Fact 122 says assert on the serialized bytes rather than on
a dict; this says what the bytes can and cannot prove once you have them.

**128. BEFORE REGISTERING SOMETHING AS "ANOTHER INSTANCE OF D_n", READ D_n. A
DESIGN'S SUMMARY OF A REGISTER ENTRY IS NOT THE ENTRY, AND THE SUMMARY IS WHAT
THE NEXT TASK WILL COPY.** Sub-project 22's design and every brief derived from
it said "D312 names nine other unpatched task functions sharing `send_answer`'s
shape", and instructed the register round to establish whether `move_post` was
a tenth. **D312 names no such nine.** Its cell is about `send_answer` alone and
contrasts it with `make_reply` and `edit_reply`, which do patch. And the
premise inverted on measurement: `move_post` (`app/shared/tasks/pages.py:377`)
**does** enter `with patch_db_session(session):`, so it was never a candidate.
**The measurement that should have been made instead is cheap and settles the
question for good** -- an `ast` walk over `app/shared/tasks/` for every
`FunctionDef` whose body mentions `get_task_session`, partitioned on whether it
also mentions `patch_db_session`: **61 open a task session and 21 never patch**,
of which 15 are in `maintenance.py` and 6 elsewhere (`follows.py:214`, `:242`,
`likes.py:55`, `:176`, `notes.py:242`, `users.py:14`). Neither "nine" nor "ten"
is any of those numbers. **The failure mode is specific and recurring**:
sub-project 18's documents called D286 "D292" from the design onward, and this
round's called a nonexistent list "D312". A planning document's paraphrase of a
register cell is a citation like any other and gets checked like one (fact 99);
what makes this class worse than a stale line number is that the paraphrase
reads as authoritative and no grep contradicts it. **Open the cell, and prefer
re-measuring the population to inheriting its size.**


**129. A FACTORY THAT LEAVES BOTH COLUMNS A TERNARY SELECTS BETWEEN AT `None`
MAKES THE TERNARY'S TWO ARMS INDISTINGUISHABLE -- AND EVERY ASSERTION STILL
PASSES, EVERY COVERAGE FIGURE STILL READS 100%. FACT 87 IS THE COVERAGE HALF;
THIS IS THE ASSERTION HALF, AND IT IS THE ONE NO MUTATION OF THE PRODUCTION FILE
WOULD CATCH.** `app/shared/tasks/adds.py:74` and `app/shared/tasks/removes.py:74`
are `'target': community.ap_moderators_url if community_id else
community.ap_featured_url`. `make_community` (`tests/factories.py:122`) sets
**neither** column, so both default to `None`. With both `None` the two arms
return the same value: a test that passes `community_id` and a test that does not
both see `target is None`, `assert sent['target'] == <whatever>` passes under
either arm, and the pair of tests that exists specifically to discriminate the
arms discriminates nothing. **Fact 87 says coverage cannot see an unexercised
ternary arm. This says that even when BOTH arms are exercised and BOTH are
asserted on, the assertions can be vacuous** -- and the failure is invisible from
every direction the campaign normally looks: the tests are green, the arms both
run, the branch figure is clean, and swapping the ternary's arms in the source
kills nothing, so even mutation testing reports the code as adequately covered.
The defect is in the FIXTURE, and the fixture is not what gets mutated.
**The remedy is two lines and belongs in the shared seed helper**: set the two
columns to distinct non-`None` values and assert they differ
(`assert FEATURED_URL != MODERATORS_URL`), so a later edit that drops one
assignment fails loudly instead of silently voiding every test that reads the
ternary. `_seed` in `tests/test_shared_tasks_add_remove.py` does exactly that, and
its docstring says the assignments must stay. **It was proved empirically rather
than argued, twice**: temporarily removing the two assignments makes both ternary
smoke tests fail while the structural-equivalence test keeps passing, run once by
the implementer and reproduced independently by the reviewer. **The general rule:
when a ternary selects between two FACTORY-DEFAULTED fields, the fixture is part
of the test's discrimination, not part of its setup.** Enumerate the ternaries
with the `ast` walk fact 87(c) prescribes, then for each one ask what the factory
leaves the two arms holding.

**130. WHEN A MODULE HAS A TWIN, RUN EVERY MUTATION AGAINST BOTH -- AND TREAT A
KILL/SURVIVE ASYMMETRY AS A DEFECT IN THE TESTS, NOT IN THE CODE.**
`app/shared/tasks/adds.py` and `app/shared/tasks/removes.py` are the same file
under two names: `diff` reports eight hunks, all of them name substitutions, and
normalising all eight of them makes the files byte-identical. **The
normalisation has to be hunk-directed, not a blind `remove`->`add` rewrite**:
`unsticky_post` -> `sticky_post` is a PREFIX deletion rather than a token
substitution, and `:22`'s `For Announce, remove @context from inner object`
contains the English word `remove` identically in BOTH files, so a blind rewrite
manufactures two spurious differences on a pair that has not diverged. The
verified recipe is in D318's cell and at the end of
`tests/test_shared_tasks_add_remove.py`. **The recipe is the convenience; the
committed check is `test_the_twins_are_structurally_identical`, which applies the
same four rules in-process and asserts BYTE IDENTITY** -- if the two ever
disagree the test is authoritative. It was shape-only (line count plus `ast`
extents) until the final review wave, and shape-only could not see a conjunct
dropped from one twin, a swapped ternary or a renamed local: **an equivalence
invariant that compares SHAPE cannot see the divergence shape a one-line
production fix produces, which is the shape it exists to catch.**
Sub-project 23 applied ten single-line mutations to each file separately -- **20
runs** -- and every pair came back with the same kill count, the same kill type
(sole/multi, assertion/crash) and mirrored test names. **The value of the second
run of each pair is not confirmation; it is that a DISAGREEMENT would have been
a finding about the tests.** Identical files cannot legitimately answer the same
perturbation differently, so an asymmetry means one twin's test is observing
something its counterpart's is not -- a copied test that patches the wrong
module, an assertion that got weakened on one side during a rename, a fixture
that only one of the pair reaches. Those are exactly the bugs a one-file-per-twin
test suite invites, and they are invisible to a run that mutates only one twin.
**The same logic sets the bar for a FIX**: sub-project 23's flake fix had to land
in both twins identically, because a fix applied to one only would itself be the
first divergence. **And it sets the bar for a PRODUCTION change**: guarding one
twin and not the other is not a smaller change than guarding both, it is a
structural divergence, so "one production change per round" counts changes and
not files. Where the twin-specificity of a test matters -- as it did for the
`private` guard -- revert each twin's line separately and require exactly one
failure, that twin's own; if reverting either fails both, the tests are not
twin-specific and the whole layout is unsound.

**131. A TEST THAT ASSERTS AN INCIDENTAL DATABASE ORDERING FAILS LOUDLY AT
RANDOM. CONTROL THE SEQUENCE INSTEAD. THIS AMENDS FACT 123, WHICH IS RIGHT ONLY
WHEN THE ORDER IS LOAD-BEARING FOR THE PROOF.** Fact 123 prescribed asserting the
order returned by `Community.following_instances()` (`app/models.py:842-851`,
an unordered `.distinct().all()`) in the skip-then-deliver tests, so that a
reversal would fail loudly rather than silently weaken the test. Sub-project 23
carried that forward, and the result was measured: both two-instance tests failed
on a first run and passed on an immediate re-run **on a clean, unmutated tree**.
The assertion worked exactly as designed; the DESIGN was hopeful. **The
discriminator is whether the ordering is the test's subject or incidental to it.**
Here the subject is the loop's continue-past-a-skip behaviour -- with one follower
"skipped" and "loop ended" are indistinguishable, so two are needed and the
skipped one must come first -- and the *query's* ordering is merely how the two
rows happened to arrive. So the fix patches `following_instances` for the duration
of the call to hand back a known sequence. **That is strictly MORE discriminating
than the assertion it replaces**, because it no longer depends on the planner
cooperating: the loop is tested against a known input every run instead of a
hoped-for one. **What fact 123 got right and this does not overturn: do not add
`ORDER BY` to production.** The delivery loop never relied on any order, only on
visiting every follower, so an `ORDER BY` would be an unmotivated production
change hung off a test artefact. The defect was an assumption the TEST made that
production never promised, and the test is where it belongs fixed. **Asserting an
incidental order buys a loud failure, not a reliable test, and a test that fails
loudly at random is still a test that fails at random.** **THE SUPERSEDED FORM
SURVIVES AT EXACTLY TWO SITES, BOTH NAMED**:
`tests/test_shared_tasks_send_answer.py:484` (assertion `:511-514`) and
`tests/test_shared_tasks_send_post.py:2646` (assertion `:2669-2672`), found by
`grep -rn 'ordered ==' tests/`. Sub-project 23 fixed its own two and deliberately
left these -- another sub-project's files, with the flake unmeasured there -- so a
later round applying this fact should expect **two** conversions, not one.

**132. A `monkeypatch` THAT SILENTLY FAILS TO INTERCEPT LEAVES THE TESTS GREEN
FOR THE WRONG REASON, SO A DEFLAKE IS NOT FINISHED UNTIL A PROBE HAS SHOWN THE
STUB IS LOAD-BEARING.** `monkeypatch.setattr(obj, 'method', stub)` patches the
object you hand it. If the code under test reaches the method through a
*different* object -- a second identity-mapped instance, a re-query, a copy -- the
patch applies, the test runs, nothing raises, and the test passes on the real
call it was meant to replace. **Every symptom of a working patch is present
except the interception**, and for a deflake that is the worst possible outcome:
the flake is hidden rather than fixed, and it returns later looking new. **The
probe is three steps and costs one run**: make the stub RAISE instead of
returning its value, run the tests, and confirm they now fail with the injected
error -- then revert and byte-diff the file back before running for real.
Sub-project 23 ran this probe on BOTH carriers and got OPPOSITE answers, which is
the whole reason the fact exists. In `tests/test_shared_tasks_send_post.py` the
instance-level `monkeypatch.setattr(s.community, 'following_instances', ...)`
intercepted correctly: `_move` (:2413) calls `move_object(db.session, ...,
origin=s.community, ...)` directly and `app/shared/tasks/pages.py:394` is
`community = origin`, a plain reference assignment, so the object the test patched
IS the object the loop touches. In `tests/test_shared_tasks_send_answer.py` the
SAME patch shape intercepted NOTHING -- the raising stub never fired and the test
reported `1 passed` -- because `send_answer` opens its own session at
`app/shared/tasks/notes.py:243` and re-loads the reply at `:246`, so
`post_reply.community` (:248, :299) is a different Python object.
`get_task_session()` returns `Session(bind=db.engine)` (`app/utils.py:3673-3675`),
and two Sessions never share identity-mapped objects for one row. That file needs
`monkeypatch.setattr(type(s.community), 'following_instances', probe)`, whose stub
takes `self` because class-level patching binds through the descriptor protocol.
**ONE question decides which form a call site needs: does the production path
re-load the object, or is the test's object passed straight through?** Answer it
by reading the path, then confirm the answer with the raising probe -- never
copy the form from a neighbouring file, because these two neighbours disagree. **Then run the deflaked test
SEVERAL times, not once.** One green run is what let the original flake through;
that fix was followed by five consecutive clean runs and a sixth after the commit.
The same probe applies to any patch whose success is indistinguishable from its
failure -- `setattr` on an instance, a patched module attribute the caller
imported by value, a fixture that replaces a symbol the code re-imports.

**133. `.env.test` SETS `CACHE_TYPE=NullCache` (`.env.test:11`), SO EVERY
`@cache.memoize` IS INERT UNDER TEST -- DO NOT DESIGN A DEFENCE AGAINST A STALE
MEMOIZED VALUE; THERE IS NO CACHE TO GO STALE.** `get_setting`
(`app/utils.py:202-211`) is decorated `@cache.memoize(timeout=500)` and is the
case that prompts this fact: reading the decorator alone, a test author's first
instinct is to worry about a cached value outliving the row it was read from,
and to reach for `cache.clear()` or a timeout workaround. Under `NullCache`
every call recomputes from the database, so that worry is unfounded in this
suite specifically -- worth recording because the decorator is visible in the
source and the test config is not, so the defence looks obviously necessary
right up until it is measured against `.env.test`.

**134. `respx` CANNOT OBSERVE `Response.close()`. PROVING A CLOSE REQUIRES A
RECORDING DOUBLE, NOT AN `is_closed` ASSERTION.** The `httpx.Response` a
production function calls `.close()` on never leaves that function, and a
respx-mocked response may already report `is_closed == True` before `close()`
is ever called -- so an assertion on `is_closed` passes identically whether or
not the code under test calls `close()` at all. The carrier is D320
(`app/shared/tasks/users.py`'s email leg, fixed by adding `email_response.close()`
at `:74`): `test_both_responses_are_closed`
(`tests/test_shared_tasks_users.py:628`) replaces the module's `httpx_client`
with a recording double (`_Response`/`_recording_client`, `:99-158`) whose
`close()` appends the response object to a real list, and asserts
`len(client.closed) == 2`. **The pre-fix number must be checked, not just the
post-fix one**: the list holds one entry before the fix (only the IP leg's
`close()` runs) and two after: a fix that produced `0 == 2` instead of `1 == 2`
would mean the double was not observing the other leg either, and the fix
would be passing the test for the wrong reason.

**135. A NAME IMPORTED BY VALUE MUST BE PATCHED IN THE IMPORTING MODULE'S OWN
NAMESPACE, NOT THE ORIGIN MODULE'S.** `app/shared/tasks/users.py:1` is
`from time import sleep`, which binds `sleep` inside the `users` module's own
namespace at import time; `check_user_application` calls
`sleep(random.randint(1, 30))` at `:50`, once per configured ban-check domain.
Patching `time.sleep` leaves that binding untouched and the call still resolves
to the real function, so a test that patches the origin module absorbs the
full random wait per domain instead of skipping it. The fix is
`monkeypatch.setattr('app.shared.tasks.users.sleep', ...)`. Same family as fact
132's class-versus-instance question -- a patch that looks like it should work
and does not -- and settled by the same probe: make the stub raise and confirm
the test fails before trusting that it passes for the right reason.

**136. `app/activitypub/signature.py:100-101`'s `@context` REINJECTION IS
TOP-LEVEL ONLY, AND WHETHER THAT MAKES AN ASSERTION MEANINGFUL OR VACUOUS
DEPENDS ENTIRELY ON WHETHER THE ACTIVITY IS ANNOUNCE-WRAPPED.** `post_request`
does `if '@context' not in body: body['@context'] = default_context()`
immediately before signing and sending -- on `body` as posted, not on anything
nested inside it. Sub-projects 20-23's senders wrap their objects in an
Announce, so this reinjection lands on the Announce's own top level and never
reaches the nested inner object; "no nested `@context`" is therefore a real,
discriminating assertion there. `app/shared/tasks/flags.py`'s `report_object`
posts a bare Flag with no wrapper (`tests/test_shared_tasks_flags.py:561`), so
the SAME reinjection mechanism lands on the Flag itself, with the identical
value `default_context()` produces in both places -- `@context` is present and
correct on the wire whether or not the builder set it, so no assertion about
its presence or its value can distinguish the two cases. One mechanism,
opposite consequences, decided entirely by whether a wrapper exists: check for
one before writing a `@context` assertion, rather than copying the nested-
absence pattern by convention.

**137. THE COVERAGE JSON WRITTEN BY `--cov-report=json:<path>` LANDS INSIDE THE
`pyfedi_test-runner` CONTAINER, NOT ON THE HOST BIND MOUNT.** Retrieve it with
`podman cp pyfedi_test-runner_1:<path> <same path>`. Do not conclude a coverage
run failed because the file is not where the host `--cov` target implied it
would be, and do not conclude it succeeded without reading the file back,
since a wrong `--cov` target fails silently and green.

**138. A SURVIVING MUTATION IS INFORMATION ABOUT THE TEST, NOT PROOF THE MUTANT
IS DEFECTIVE.** `app/shared/tasks/users.py:18`'s outer guard,
`if not application or not application.user:`, was mutated to drop its second
disjunct and the mutation SURVIVED against
`test_an_application_without_a_user_returns_without_requests`
(`tests/test_shared_tasks_users.py:177`) as that test was originally written:
with no `ban_check_servers` configured, `get_setting` returns `''`, the loop
never touches `application.user` either way, and `client.posts == []` holds
identically with or without the disjunct. **That is a real gap in the test, not
a defective mutation** -- dropping the disjunct genuinely changes behaviour,
the original assertion just could not see it. The fix strengthens the test
rather than declaring the mutation invalid: configure a real domain so the loop
body is entered, and assert on a captured `current_app.logger.error` call
instead of on `client.posts` -- under the mutant the function proceeds into the
loop, dereferences `application.user.ip_address` against `None`, raises, and
that raise is swallowed and logged by the per-domain `except` before any HTTP
call is made, so the logger call is what actually discriminates "returned
before the loop" from "entered the loop and failed inside it." **Guard against
the opposite error too**: a test rewritten until a mutation dies can end up
asserting the implementation rather than the behaviour a caller could observe
-- ask whether the new assertion describes something external, the way "the
logger fired" does here, before trusting that a kill is real progress.

**139. FOUR DOCUMENTATION-ROT MECHANISMS SHIPPED IN THIS SUB-PROJECT ALONE,
NONE CATCHABLE BY A SWEEP THAT ONLY CHECKS A CITED LINE RESOLVES.** Listed
together because the lesson is the same for all four: "re-derive the line
numbers" is necessary and nowhere near sufficient.
  1. **Stale ordinal.** `flags.py:57` gained a `private` disjunct in the middle
     of an existing guard (commit `038f2180`); a neighbouring test's docstring
     went on calling `community.instance.online()` the guard's "second
     disjunct" when it had become the third the moment the guard grew a middle
     term. Fixed by commit `72926db6`.
  2. **Reused figure.** A docstring said the `object.community` read arrives
     "fifteen lines later" than the `.get(post_id)` lookup it follows, but
     fifteen was the distance for a different pair of lines (the module
     docstring's `:45` minus `:30`); the real distance from `:45` to `:56` is
     eleven. Only doing the subtraction for the SPECIFIC pair in the sentence
     catches this. Fixed by commit `db5e340d`.
  3. **Self-invalidated citation.** `tests/test_shared_tasks_users.py` cited
     `make_user` at `tests/factories.py:39`, correct when drafted -- and the
     SAME commit that drafted it edited `factories.py`'s `app.models` import
     list, pushing the target to `:40`. True when copied, false when committed.
     **This defeats the obvious defence**: verifying a citation before editing
     cannot catch a citation the edit itself breaks. The rule is that citations
     into a file your own diff touches must be re-derived AFTER the diff is
     final, not before. Fixed by commit `976bf673`.
  4. **Prose attached to a deleted line, surviving the deletion.** Three
     `xfail(strict=True, ...)` decorators were removed from
     `tests/test_shared_tasks_users.py` in the same commit that fixed the
     `D319` defect they existed to pin -- and the docstrings attached to them
     still read "EXPECTED TO FAIL" and "do not fix `app/` here", arguing
     against the very fix that commit made. A decorator and its docstring are
     one unit: when a line is changed, read the prose ATTACHED to it, not only
     prose elsewhere that cites it. Fixed by commit `462ff8dc`.

**140. A DOCSTRING SHOULD STATE WHAT ITS TEST CANNOT PROVE.** Two cases from
this round earned their place.
  - `test_the_loop_continues_past_an_instance_without_an_inbox`
    (`tests/test_shared_tasks_flags.py:484`) depends on Postgres returning an
    unordered, two-row `Instance.id.in_(...)` scan in ascending-id order, which
    creation order makes likely but does not guarantee. Its docstring says so,
    and says which DIRECTION it fails in if the assumption breaks: **lax**
    (passes under a broken, aborting loop too), never **flaky** (fails under a
    correct one) -- because the campaign's ban on ordered assertions exists to
    prevent random red, and a lax degradation is a weaker proof but not a
    source of one.
  - `test_a_private_community_sends_no_flag`
    (`tests/test_shared_tasks_flags.py:251`) states outright that the order of
    `flags.py:57`'s disjuncts is load-bearing in a way the test cannot fail on:
    `community.private` sits before `not community.instance.online()`, so a
    private community with no instance row (`Community.instance_id` is a
    nullable FK, `app/models.py:575`) short-circuits at the `private` check
    instead of raising `AttributeError` on `None.online()`. Reordering the two
    disjuncts would reopen that crash without failing this test, because this
    test's community always has an instance. Both docstrings are more useful
    than one that claims more than its test can actually deliver.

**141. `Community.following_instances()` (`app/models.py:842-851`) JOINS
`CommunityMember`, SO A RECIPIENT FIXTURE NEEDS A MEMBER, NOT JUST AN
INSTANCE.** The method's query joins `Instance` to `User` to `CommunityMember`
and filters `CommunityMember.community_id == self.id`; it also filters
`Instance.id != 1`, excluding the local instance. A fixture that creates only
an `Instance` row and sets its `inbox` produces a query that returns zero
rows, so the delivery loop never runs -- and every delivery assertion in that
test passes vacuously, against zero deliveries, not against the delivery it
was written to prove happened. The recipient needs a **user on that
instance who is a member of the community**, and the instance must not be
`id == 1` (the local one). Both of this sub-project's test files build this
in their own `_follower` helper (`tests/test_shared_tasks_locks.py:146-168`,
`tests/test_shared_tasks_likes.py:134-157`) and each says why in its
docstring.

**142. A REDUNDANT CONJUNCT CAN BE INVISIBLE TO BRANCH COVERAGE AND STILL
VISIBLE TO MUTATION TESTING.** `coverage.py` does not decompose a boolean
conjunction into its conjuncts -- `if a and b and c:` is recorded as one arc
pair, taken or not -- so any input that fails at least one conjunct covers the
False arc and the module can still reach 100%. A mutation that deletes a
genuinely redundant conjunct SURVIVES that same suite, because no input the
suite can construct is able to distinguish the guard with the conjunct from
the guard without it. **When a mutation survives on a conjunct like this,
check whether the conjunct is provably redundant (an earlier query already
filters the same column) before treating the survival as a test gap** --
fact 138's rule ("a surviving mutation is information about the test") has
this as its stated exception. Both survivals recorded in this sub-project
(the `instance.online()` re-check inside `following_instances()`'s loop, in
both `locks.py` and `likes.py`) were confirmed as REAL mutations first -- the
mutated line was read back and diffed against the original before the suite
was run -- which is what separates a surviving mutation from a defective one
that never actually changed the code.

**143. `likes.py` RESOLVES `redis_client` BY IMPORTING IT INSIDE THE
FUNCTION** (`from app import redis_client`, `likes.py:155`), so patching
`app.redis_client` before the call intercepts the name the function will
bind. Assert on **what was published** -- the channel name, the collected
urls/headers, and the decoded JSON payload's activity type -- not merely that
`publish` was called, which is satisfiable by a call that published nothing
useful.

**144. AN ORDERING ARRANGEMENT IN A SKIP TEST WAS MEASURED IN THIS ROUND, NOT
MERELY ASSUMED -- AND THE MEASUREMENT IS BOUNDED, NOT A GENERAL PROOF.** Tests
proving that a delivery loop CONTINUES past a skipped instance create the
skipped instance first, so it is more likely to take the lower id.
`following_instances()` ends in `.distinct().all()` with no `ORDER BY`, and
Postgres commonly implements `SELECT DISTINCT` via a `HashAggregate`, whose
output order follows hash-bucket layout rather than insertion order -- so the
ordering arrangement might buy the test nothing at all. This round tested
that empirically rather than leaving it as an assumption: commit `ddc16e6c`
records **M17**, a `continue` -> `break` mutation applied to `send_vote`'s
delivery loop and run four times, "killed every time, by all three skip
tests" -- the same three test names failing each time. **State exactly what that licenses and no more**: four runs
against one Postgres instance and one dataset is an environment-scoped
observation, not a proof of order-independence in general -- a different
Postgres version, a different query planner choice, or a differently-sized
table could still return rows in an order that makes the "create the skip
first" arrangement load-bearing after all. The skip tests' own docstrings
already say the discrimination "degrades to LAX, never FLAKY" if the
assumption breaks; this measurement supports that existing language rather
than replacing it or strengthening it into a guarantee.

**145. A MUTATION LOOP MUST RESTORE PRODUCTION CODE BEFORE ANY POINT WHERE IT
MIGHT STOP AND REPORT, NOT ONLY AT THE END OF A BATCH.** A run in this
sub-project applied a `continue` -> `break` mutation to production code,
executed four full test runs against it (the four runs fact 144 records), and
reached its own reporting boundary before it had run the trailing `git
checkout -- app/` that restores the file -- leaving a mutated production file
on disk with no agent watching it. The restore ran and nothing was lost, but
had the loop failed or been interrupted between the last run and the restore,
the working tree would have been left dirty with a live behavioural change in
`app/`. The rule this licenses: restore after EVERY mutation before doing
anything else, including reporting results, rather than batching the restore
to the end of a set of runs.

**146. `task_selector`'s TWO DISPATCH ARMS ARE INDISTINGUISHABLE UNDER
`task_always_eager` EXCEPT BY RETURN VALUE.** `app/shared/tasks/__init__.py:66`
calls `.delay()` and falls off the end returning `None`; `:68` calls the task
directly and returns its value. A test asserting only that the task executed
passes under either arm. The technique that works: patch the task's own
module attribute with a stub returning a sentinel -- `task_selector` imports
inside its own body (`:6-18`), so the names resolve at call time and the
stub reaches the freshly-built `tasks` dict. The stub cannot exercise `:66`,
which needs a real Celery task for `.delay()` to be a valid attribute at all.

**147. A SITE-WIDE FAN-OUT AND A COMMUNITY FAN-OUT NEED DIFFERENT FIXTURES.**
`app/shared/tasks/blocks.py:159` queries `Instance` directly with only a
`software` filter, so a site-ban recipient needs no `CommunityMember` row --
while every `Community.following_instances()` path does (fact 141). A helper
built for one will silently produce zero recipients for the other.

**148. A `respx` UNMATCHED REQUEST CANNOT FAIL A FEDERATION TEST IN THIS
SUITE -- IT BECOMES AN `ActivityPubLog` ROW INSTEAD.** `post_request`'s
`except Exception as e:` (`app/activitypub/signature.py:143`) catches
respx's own unmatched-request assertion exactly as it would a real transport
error, and records an `ActivityPubLog` failure row rather than propagating
(`tests/conftest.py:314-316` already states this design choice in prose).
**Consequence: a spurious or misrouted send is invisible to
`_delivered_inboxes(...)` and to `len(route.calls)`, both of which read only
the routes a test itself registers -- it is visible ONLY to an
`ActivityPubLog` row count.** Proven by mutation twice in
`tests/test_shared_tasks_blocks.py` (dropping `:159`'s
`Instance.software != 'mastodon'` filter and, separately, `:161`'s
`instance.id != 1` conjunct: both SURVIVED against docstrings that claimed
an unmatched request would fail the test instead), and found independently
written into two other sub-projects' files besides, one of them sitting
right next to a sibling test that already used the correct idiom. **Route
and delivered-inbox assertions prove what WAS sent; only a row count can
catch what should NOT have been.**

**149. A GUARD SPLIT ACROSS SEVERAL STATEMENTS IS INVISIBLE TO A
CITATION-LEVEL CHECK.** `app/shared/tasks/deletes.py:127-134` reads as three
separate `if` blocks: two carry `local_only` (`:127`, `:130`) and the third
carries `online()` (`:133`). A register entry quoting `:127` and `:130`
correctly still concluded the wrong thing about the function as a whole,
because the conjunct it was looking for (`private`) turned out to belong on
`:133`, a line the entry never opened. Both cited lines were quoted
accurately; the claim about the guard they belong to was false anyway. When
a claim is about a GUARD, the unit to open is the block the guard spans, not
merely the line or lines the claim happens to cite.

**150. THE FOLLOWER FAN-OUT AND THE COMMUNITY FAN-OUT NEED DIFFERENT
FIXTURES, AND NEITHER IS INTERCHANGEABLE.** `app/shared/tasks/deletes.py:212`
joins `Instance -> User -> UserFollower`, filtered on `local_user_id`;
`Community.following_instances()` (fact 141) joins `CommunityMember`
instead. A recipient row built for one produces zero deliveries on the
other, under assertions that still pass -- the same shape fact 147 records
for `blocks.py:159`, now confirmed a third time in a third package.

**151. A `session=None` DEFAULT ON AN INTERNAL HELPER TURNS A MISSING
KEYWORD INTO AN `AttributeError` AT THE FIRST QUERY RATHER THAN A
`TypeError` AT THE CALL.** `delete_object`'s `session` parameter defaults to
`None` (`app/shared/tasks/deletes.py:118`) and `:119`'s
`session.query(User).get(user_id)` dereferences it immediately. The single
call site that forgot `session=` (`delete_posts_with_blocked_images`'s call
into `delete_object`, `:250`) had been raising on every invocation since it
was written, because no test reached the function at all -- a keyword
default that looks defensive instead converts a call-site typo a linter or
a `TypeError` would catch immediately into a runtime crash three lines into
the callee, after any of the callee's own preceding side effects have
already committed.

**152. STATING THE CITE-THE-STATEMENT-NOT-THE-GUARD RULE DOES NOT PREVENT
BREAKING IT.** The plan that produced this file's current shape stated the
rule in its Global Constraints and repeated it in every task dispatch, and
five separate docstrings written for that same plan still cited a guard
where they meant its statement: `:120` for `:123`, `:210` for `:208`, `:214`
for `:215`, `:292` for `:293`, and `signature.py:109` for `:111`. Every one
was caught by a reader opening the line, never by the rule's presence. The
working countermeasure is a reader re-deriving citations against the tree;
the rule's value is in telling that reader what to look for, not in
preventing the error before it is written.

**153. A TASK WRITES THROUGH ITS OWN `Session`, SO A TEST ASSERTING ON AN
ORM OBJECT'S ATTRIBUTES AFTER THE TASK RUNS READS STALE IN-MEMORY STATE.**
`get_task_session()` returns `Session(bind=db.engine)`
(`app/utils.py:3673-3675`) with its own identity map, separate from the
test's `db.session`. `patch_db_session` only swaps the global `db.session`
pointer for the duration of the call and restores it in a `finally`; it
never touches attribute state on objects the test already holds. **This is
the inverse of fact 58, and the two must be read together, because a
reader who has internalised 58 is exactly the reader who will get this
wrong.** Fact 58 says `app/__init__.py:81` never overrides
`expire_on_commit`, so it stays default-`True` -- and concludes that when
the function under test commits ON THE TEST'S OWN `db.session`, that
commit already expires the objects the test holds, making an added
`db.session.refresh(obj)` usually redundant. Here the commit happens on a
DIFFERENT session (the task's own), so `expire_on_commit`'s default `True`
applies to that session's objects, not the test's -- nothing expires the
test's copy, and the exact refresh fact 58 calls usually-redundant becomes
load-bearing. Assertions that issue a fresh query (`db.session.query(X).count()`)
are immune, because they never consult the stale in-memory attribute at
all; attribute reads on an object the test built earlier (`obj.attr`) are
not, and `db.session.expire_all()` before the assertion is the fix. Not
hypothetical: Task 4 of sub-project 27 shipped
`test_the_blocked_image_batch_deletes_every_post` asserting
`s.post.deleted is True` immediately after calling
`delete_posts_with_blocked_images`, and it failed with `assert False is
True` for exactly this reason before `db.session.expire_all()`
(`tests/test_shared_tasks_deletes.py:614`) was added.

**154. A TEST THAT NEEDS A REAL FILE ON DISK MUST CREATE IT INSIDE THE TEST
PROCESS.** `compose.test.yaml`'s `test-runner` service declares exactly one
volume, `- ./:/app:z` (`compose.test.yaml:67`), and neither it, `.env.test`
nor the `Dockerfile` sets a `TMPDIR` override. A file written by a host-side
command outside that one bind mount is invisible to the container process
that actually runs the code under test; `tempfile.mkstemp()` called FROM
INSIDE the test process lands in the container's own `/tmp`, which is
outside the bind mount in the other direction -- invisible to the host and
to `git status` -- but is the one location both the test and the code
under test can see. Worked case:
`test_the_blocked_image_batch_recalculates_cross_posts_and_removes_the_file`
(`tests/test_shared_tasks_deletes.py:640`) needs `File.delete_from_disk()`
to do observable work, and `make_file()` called with no arguments leaves
`file_path`, `thumbnail_path` and `source_url` all `None`
(`tests/factories.py:1137`), under which `delete_from_disk()`'s three
`if self.<x>_path:` guards (`app/models.py:424`, `:436`, `:449`) make it a
genuine no-op regardless of whether the caller under test even reached it
-- confirmed by two mutations that survived against an earlier, path-less
version of this test before the real file was added. The test calls
`tempfile.mkstemp(suffix='.png')` directly and passes the resulting path to
`make_file(file_path=...)`. **The test originally carried no `try`/`finally`
around the temp file**, so a failure before the code under test unlinks it
(the assertion this test makes at its own end) leaked the file inside the
container's own `/tmp` -- contained, and cleared when the container is torn
down, but real. Closed by sub-project 27's final whole-branch review: the
body from the file's creation onward is now wrapped in a `try`/`finally`
whose `finally` removes the file if it still exists, so an early assertion
failure no longer leaves it behind. The gap this fact records is historical
rather than live in this file now, and is kept here because the pattern --
a real file created for a test, with no cleanup on the failure path -- is
worth a future test author checking for on sight, not only in this one
case.

**155. A DELETED-THEN-COMMITTED SQLALCHEMY INSTANCE IS EXPUNGED, NOT
EXPIRED -- A THIRD CASE, DISTINCT FROM FACTS 58 AND 153, AND THE TWO MUST
NOT BE READ AS COVERING IT.** Fact 58 says a commit inside the function
under test expires the session's objects, so a later attribute read on the
TEST's own copy re-fetches. Fact 153 says a commit on the TASK's own,
separate session leaves the test's in-memory copy stale, because that
commit's `expire_on_commit` applies to the task session's objects, not the
test's. Neither is what happens when the object itself is the one
`session.delete()`d: a container probe against the live app/db stack
(`get_task_session()`, no pytest) showed that after
`session.delete(join_request); session.commit()`, `inspect(join_request)`
reports `persistent=False, deleted=False, detached=True, expired=False`.
`Session.commit()`'s default `expire_on_commit` expires attributes only on
instances that remain PERSISTENT after the flush; an instance that was
deleted and then committed is EXPUNGED instead -- it becomes detached, and a
detached object's attribute read never triggers a reload (there is no
session left to reload it with), so it returns whatever value was already
sitting in its Python `__dict__` from the query that originally loaded it.
`app/shared/tasks/follows.py`'s `leave_community` and `unfollow_user` both
read `join_request.uuid` after their own `session.delete()`+`session.commit()`
without crashing for exactly this reason
(`docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`,
sub-project 28, item 1/D337) -- true regardless of `expire_on_commit`'s
setting, and true independent of facts 58 and 153, which both describe a
surviving, still-persistent object rather than one the code itself deleted.

**156. `flash()` NEEDS A REQUEST CONTEXT, AND THE `app` FIXTURE DOES NOT
PUSH ONE.** `tests/conftest.py:112` is `with application.app_context():` --
an app context only. A test reaching a `SRC_WEB` arm that calls `flash()`
must push its own request context, `with current_app.test_request_context('/'):`,
around the call and read `get_flashed_messages()` before that context is
popped (flashed messages live in the request/session, not after it ends).
Worked case: `tests/test_shared_tasks_follows.py:296`.

**157. PUSHING A REQUEST CONTEXT DISABLES `patch_db_session`, SO FACT 156's
FIX AND `patch_db_session` COMPOSE BADLY.** `app/utils.py:3685` is `if
has_request_context():`, and `:3688` is the `return` inside it -- `yield`
then `return` with no patching applied. Fact 3 already states this guard's
effect for a different module's request/no-request asymmetry; the
composition specific to fact 156 is that a test which pushes
`current_app.test_request_context('/')` to make `flash()` work thereby ALSO
stops `db.session` from being the task's own session for the duration of
that context. Such a test cannot assert on attributes of objects the task
touched through `db.session` -- it must assert through a fresh query
instead, exactly as fact 153 already prescribes for the task's-own-session
case, but for a different underlying reason.

**158. A MODULE'S COVERAGE CANNOT BE MEASURED FROM ONE TEST FILE WHEN ITS
FUNCTIONS ARE SPLIT ACROSS TWO.** `app/shared/tasks/notes.py` holds both
`send_reply`/`make_reply`/`edit_reply` (tested in
`tests/test_shared_tasks_send_reply.py`) and
`send_answer`/`choose_answer`/`unchoose_answer` (tested in
`tests/test_shared_tasks_send_answer.py`); measuring
`--cov=app.shared.tasks.notes` against the first file alone reports 79% and
cannot support a 100% claim, only a run including both files can.
`app/shared/tasks/pages.py` does not have this problem: every function it
defines (`make_post`, `edit_post`, `send_post`, `move_post`, `move_object`)
is exercised inside the single `tests/test_shared_tasks_send_post.py`.
**The mechanical way to tell which case a module is in is NOT to grep for the
module's own function names -- a name collision with something else in
`tests/` silently misclassifies the "not split" case as split.**
`pages.py`'s own `make_post` is also a factory, `tests/factories.py:294`,
so `grep -rl make_post tests/` returns 57 files (every test that builds a
post) and the not-split module fails the recipe's own test. **Grep for how
the module is IMPORTED instead**: `grep -rl` for
`from app\.shared\.tasks\.<module> import` and
`import app\.shared\.tasks\.<module>` (e.g.
`grep -rlE 'app\.shared\.tasks\.pages\b' tests/*.py`); if every match is the
same one file, a single-file coverage run is sufficient, and if the file set
has more than one member, the module's true percentage is only the union
run's. Verified against both modules named above: the import grep resolves
`pages.py` to exactly `tests/test_shared_tasks_send_post.py` (which even
aliases its own import, `make_post as make_post_task`, to dodge the same
collision) and resolves `notes.py` to exactly the two files already named,
`tests/test_shared_tasks_send_answer.py` and
`tests/test_shared_tasks_send_reply.py`.

**159. BEFORE CLOSING A RESIDUAL, CHECK WHETHER AN EARLIER ROUND ALREADY
PROVED IT UNREACHABLE -- THE PROOF MAY LIVE IN ANOTHER SUB-PROJECT'S TEST
FILE RATHER THAN IN THE REGISTER.** A sub-project 28 task wrote a
mock-forced test against `app/shared/tasks/notes.py:100-101` (a bare
`except: pass`) and raised that module's floor 99 -> 100 on it, without
first checking `tests/test_shared_tasks_send_answer.py:977-989`, which
already recorded a sub-project 20 finding that the explicit `raise` inside
that clause cannot be reached from this arm -- narrower than saying the two
lines are UNREACHABLE outright, since a DB-layer exception out of
`app/user/utils.py:101` still lands in the same bare `except:`. The commit
was rejected and reverted regardless: forcing the unreachable `raise` with a
mock proves the mock can raise, not that production's `raise` can, and a
passing test built on it still buys a floor number that contradicts an
already-registered fact. See fact 111's extension for the mechanism, and the
campaign register's sub-project 28 section, item 4, for the full withdrawal.

**160. A `# pragma: no branch` ON A PROVEN-UNREACHABLE ARM EARNS THE IDIOM
ONLY ONCE A REVIEWER HAS TRIED TO DEFEAT THE PROOF AND FAILED -- NOT MERELY
ONCE THE AUTHOR BELIEVES IT.** Applied at four sites in one round:
`app/shared/tasks/follows.py:188` and `app/shared/tasks/pages.py:270`,
`:312` and `:333`. Each proof was independently attacked by searching for a
rebinding of the tested variable, an intervening commit or refresh, an
alternate entry path into the guarded block, and (for `:312`) a deletion of
the key the guard tests -- and each survived. The idiom predates this
campaign (`app/request_hooks.py:137`); this is the first round the
campaign's own register records using it, and the standard it records is
the reviewer's attempt, not the author's confidence.

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

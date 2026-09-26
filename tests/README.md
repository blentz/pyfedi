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

**75. The ~~five~~ SIX catalogued causes of an unkillable CLAUSE, TWO of an
unkillable STATEMENT, and one of an unkillable ARM OF A CONDITIONAL EXPRESSION.
Name which one you have and prove it; never invent a test to fake a kill.**
Causes 4 and 5 were added by sub-project 14 and are the two that most often get
mis-filed as ordinary fixture gaps. **The list is organised by SYNTACTIC UNIT,
and reading the unit first is what keeps a survivor from being mis-filed -- but
THE NUMBER DOES NOT ENCODE THE UNIT, so read the unit off this sentence rather
than off the ordinal:**

- **clause-scoped: 1, 2, 3, 4(a), 4(b), 5 and 9;**
- **statement-scoped: 6 and 8;**
- **arm of a conditional expression: 7.**

**Numbers were assigned in the order the shapes were met, not in unit order.**
~~causes 1-5 are causes of an unkillable *clause*; causes 6 and 8, added by
sub-projects 16 and 19, are the statement-level cases; cause 7, added by
sub-project 17, is the expression-arm case.~~ **That grouping held only while
the highest number was 8, and it was rewritten when cause 9 -- clause-scoped,
added by sub-project 49 -- landed after two statement-scoped causes and one
expression-arm one. A contiguous "1-5 are clauses" reading is now WRONG, and
nobody should read "9 > 8" as "a new syntactic unit".** 6 and 8 came from
sub-projects 16 and 19, 7 from sub-project 17, 9 from sub-project 49. Each was
numbered separately rather than folded into 4(a) for the same reason --
force-fitting one unit into another unit's taxonomy is what produces a mis-filed
survivor. Read the scope of the item before you claim it: if the mutant you are
explaining deleted or narrowed a whole statement rather than dropping a
conjunct, the clause-scoped causes do not apply to it; if it collapsed one arm
of an `a if c else b`, neither the clause-scoped causes nor 6 nor 8 do.

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
9. **Tautology stranding the FALSE arm** -- **the mirror image of 4(b), and a
   CLAUSE-scoped cause despite following two statement-scoped ones.** 4(b)'s
   invariant *falsifies* the condition, so its **True** branch is dead. Here the
   invariant *satisfies* it: the condition is **always true when reached**, and
   the dead code is the **False** arm -- the `elif` chain's fall-through, the
   absent `else`, the path not taken. The proof obligation is 4(b)'s exactly:
   **name the construct that establishes the invariant and prove it against
   EVERY branch of that construct, not against a sample.** The verdict is 4(b)'s
   too: not fixable, and no test should be written for the dead arm.
   **Four instances, three of them registered as D578 and unenacted for three
   sub-projects.** `app/shared/auth.py:54`, `:74` and `:111`: `:21-39` is an
   exhaustive `if src == SRC_WEB / elif src == SRC_API / else: return None` and
   `src` is a parameter **never reassigned anywhere in the body**, so every line
   after `:39` runs with `src` provably in `{SRC_WEB, SRC_API}`; each later
   `elif src == SRC_API:` is reached only when its paired `if src == SRC_WEB:`
   was false, is therefore always true when reached, and its fall-through
   (`[[54,57]]`, `[[74,77]]`, `[[111,-18]]`) is dead. Note the establisher there
   is **an exhaustive earlier dispatch whose `else` returns** -- neither a caller
   nor an enclosing guard, which is why 4(b)'s own establisher clause does not
   describe it either. The fourth instance, and **the first outside
   `auth.py`**, is `app/shared/feed.py:487`: `proceed = True` at `:459` and
   **nothing between `:459` and `:487` reassigns it** -- `:461-485` only builds
   and sends an Undo -- so `if proceed:` is a tautology and its False arm, which
   would skip the `CommunityMember` delete, the `CommunityJoinRequest` delete,
   the `subscriptions_count` decrement and the commit, is unreachable. That
   establisher is **an unconditional assignment in the same block**, the same
   category as the second one the note below enumerates.
   Discriminate it from its neighbours by each one's own text: **not 4(b)**, on
   polarity -- and note that the first draft of D578's ruling filed it there by
   quoting 4(b) with a bracketed "[non-matching]" substituted for the source's
   literal "True", a bent quotation that concealed exactly this mismatch (fact
   252); **not 3**, because subsumption is a later conjunct implying an earlier
   one inside the same clause and here the establisher sits outside the
   condition entirely; **not 5**, because the value is not one the column cannot
   hold; **not 6, 7 or 8**, which are scoped to a statement, an expression arm
   and a `try`/`except` respectively.
   **WHY THIS IS 9 AND NOT 4(c), WHICH IS WHERE IT TAXONOMICALLY BELONGS.** It
   is a second mirror-shape under cause 4 and the obvious label for it is 4(c).
   That label was rejected deliberately, and the reason is recorded here so it is
   not re-litigated. Fact 251 is *titled* **"FACT 75 HAS NO CAUSE 4(c), AND THE
   LABEL THAT DOES NOT EXIST WAS CITED…"**, and the nonexistence is asserted in
   passing at further sites throughout this file and the findings register.
   **Enacting 4(c) would not amend fact 251; it would falsify it** -- and it
   would do something worse than a stale fact. **A stale `4(c)` citation must
   keep looking wrong rather than start looking valid.** Today a reader who meets
   `4(c)` in an old artifact knows on sight that it is an error and goes looking
   for what was meant. Had 4(c) been created, that citation would look plausible
   while pointing at **this** shape -- when every recorded `4(c)` citation
   actually meant **cause 8**, the unreachable handler. **All of those assertions
   stand and remain true: there is still no cause 4(c), and none of them is
   amended by this item.**
   **What this enactment settles elsewhere, and what it does not.** Fact 252
   registered this shape while it was unenacted; its "not yet folded into fact
   75" clause is struck there and points here. Facts 266 and 269 each list
   fact 252/D578 among "registered-but-unenacted taxonomy extensions" --
   **that one element of each list is now spent; the rest of each list is not.**
   D589 (configuration-scoped unkillability), fact 266/D616 (cause 3's
   disjunctive dual) and fact 269/D628 (a callee invoked at the guard site) all
   still stand uncatalogued, and fact 269's "none of the **eight** catalogued
   causes fits" should now be read as nine with its argument unchanged: **cause 9
   does not fit D628 either**, whose unsatisfiable guard strands its *True* arm,
   not its False one. And **D589 was proposed as "a possible ninth cause": the
   number 9 is now taken by this shape.** That proposal is untouched on its
   merits and needs a different number if it is ever enacted.

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
and `PostReply.body` is `db.Column(db.Text)` (`app/models.py:2906`), nullable.
The column's nullability makes the crash **storable**; it says nothing about
whether anything can dispatch that row into the function. Enumerating the
dispatchers settled it in the other direction: `send_reply` is called only from
`notes.py:59` and `:72`, `task_selector('make_reply'|'edit_reply')` appears at
exactly three sites (`app/shared/reply.py:208`, `:246`, `app/post/routes.py:928`),
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
**CORRECTED 2026-09-13 (sub-project 41, register D532): the two
`app/shared/reply.py` citations above read `:194` and `:232` when this fact was
written and are now `:208` and `:246`.** Commit `903dab20` inserted fourteen
lines into `vote_for_reply`, moving every line from old `app/shared/reply.py:26`
onward by +14. The marker is here rather than only in fact 232, because a reader
arriving at THIS fact has no way to learn from 2,700 lines away that it was
touched. `app/post/routes.py:928` and `app/utils.py:1233-1237` were re-derived
at the same time and are unchanged.
**CORRECTED AGAIN 2026-09-13 (sub-project 41, FINAL FIX WAVE): the
`app/models.py` citation for `PostReply.body` read `:2901` and is now `:2906`
(+5), verified by content — `git show 903dab20^:app/models.py | sed -n '2901p'`
and `sed -n '2906p' app/models.py` both return `    body = db.Column(db.Text)`.
**IT WAS STALE FOR THE SAME REASON AND BY THE SAME COMMIT AS THE TWO CORRECTED
BY THE BLOCK IMMEDIATELY ABOVE, AND THAT BLOCK WALKED PAST IT.** Counted at
`fa4978c3`: the `models.py` citation was this fact's line 5, the two
`app/shared/reply.py` citations the block corrected were five lines further
down, and the block itself nineteen lines below the `models.py` one — all three
inside one fact, all three invalidated by one commit. The sweep that wrote the
block was scoped to `app/shared/reply.py` citations, so the `app/models.py`
citation a few lines away survived it, which is D532's own "scope by the COMMIT,
not by the cited filename" rule failing inside a correction written to record
that rule. **When you correct one citation in a paragraph, re-derive EVERY
citation in that paragraph**, whatever file it names.

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

**161. `CREATE TEMPORARY TABLE ... ON COMMIT DROP` NEEDS NO SPECIAL FIXTURE
UNDER THIS HARNESS.** A probe run as a temporary pytest test called
`calculate_community_activity_stats()` (`app/shared/tasks/maintenance.py:749`,
which creates `temp_community_activity` `ON COMMIT DROP` at `:765-770`) under
the ordinary `db_session` fixture and it ran to completion with no error;
`db.session.execute(db.text("SELECT to_regclass('temp_community_activity')")).scalar()`
returned `None` afterward, confirming no catalog object was left behind.
Caveat carried forward honestly: that check ran from `db.session`, a
different connection from the task's own `get_task_session()` connection
(`app/utils.py:3673-3675`), and temp tables are invisible outside their
creating session regardless of whether `ON COMMIT DROP` fired -- so the
probe proves "ran cleanly end to end, no leftover global catalog object,"
not a stronger "confirmed DROP fired before commit." The task closes and
disposes its own session in a `finally` block, so the distinction does not
change what a test needs to do: use the ordinary `db_session` fixture like
any other task in this file.

**162. `tests/conftest.py`'s `db_session` FIXTURE DOES NOT ROLL BACK -- A
`commit()` INSIDE A TEST IS REAL.** `tests/conftest.py:137-202`: the fixture
(`:137`) yields `db.session` (`:158`), then DELETEs every row via
`_teardown_sql` (`:191`) and commits that teardown itself (`:192`) before
closing the session (`:202`). Nothing in this fixture opens a nested
transaction or issues a `SAVEPOINT` for the test to roll back to -- a test
that calls `db.session.commit()` (or drives a task that commits through its
own separate connection) has genuinely committed, and only the fixture's own
end-of-test DELETE sweep removes the rows, not an automatic rollback. This
round's own spec document carried the opposite belief in an early draft and
was corrected before Task 1 shipped; a task report independently repeated
the corrected-away version afterward, in prose only (gitignored, no effect
on the shipped diff) -- evidence the belief is easy to hold even after
seeing the fixture's own docstring say otherwise.

**163. `monkeypatch.setattr('app.shared.tasks.maintenance.utcnow', _boom)`
REACHES A TASK'S `except` ARM WITHOUT TOUCHING `tests/factories.py`, FOR
NINE OF TEN GROUP A TASKS.** `utcnow` is bound into
`app/shared/tasks/maintenance.py`'s own namespace by its import at `:16`
(`from app.models import (..., utcnow, ...)`), so patching the name on the
`maintenance` module -- not on `app.models`, which is what
`tests/factories.py` reaches `utcnow` through -- makes the very next call to
`utcnow()` inside a task's `try` raise, exercising the shared
`except Exception: session.rollback(); raise` arm every one of these tasks
carries. The one task this idiom does NOT work for is
`update_hashtag_counts`: it calls no clock function anywhere in its body, so
there is no `utcnow()` call to intercept. Its error-path test instead
monkeypatches `app.shared.tasks.maintenance.text` to raise, reaching the
same `except` arm through the function's own `text()` calls instead.

**164. AN N+1 REGRESSION'S SELECT COUNT DECOMPOSES AS ONE ELIGIBILITY QUERY
PLUS ONE PER-PRIMARY-KEY RE-SELECT PER SURVIVING ROW, AND THE OBSERVED COUNT
CONFIRMS IT EXACTLY.** Counting `SELECT ... FROM community` statements
(word-boundary regex, not a substring match -- `' FROM community' in
s.lower()` also matches `FROM community_member`, which
`app/shared/tasks/maintenance.py:295-303`'s
`select(func.count()).select_from(CommunityMember)` emits) across four
communities driving `update_community_stats`'s pre-fix per-iteration commit
(`:318`, inside the loop opened at `:293`) read `assert 4 == 1`: one
statement for the initial `communities = session.query(Community).filter(...).all()`
at `:288-291`, plus three more, one per community at the top of iterations
2 through 4, because `expire_on_commit` (default True, fact 58) expires
every loaded `Community` after each in-loop commit and the next iteration's
first attribute access re-SELECTs it by primary key. After the fix moved
`:317`'s commit to run once after the loop, the same oracle read exactly 1 --
the mechanism predicts N pre-fix and exactly 1 post-fix, not merely
"smaller," and both numbers were observed rather than assumed.

**165. A CITATION CHECKED BY COUNTING LINES IN AN UNNUMBERED `sed -n
'X,Yp'` RANGE HAS NOT BEEN CHECKED -- AND NEITHER HAS ONE READ FROM `grep
-n` OVER ALREADY-EXTRACTED OR PIPED OUTPUT.** Both failure modes produced a
wrong citation in this round, from the same underlying mistake: treating a
line number from a DERIVED view as though it were the file's own line
number. First: a `sed -n '3683,3690p' app/utils.py` range prints no line
numbers of its own; counting output lines to assign numbers came out short
because two lines in the window are blank, producing a confident but WRONG
claim that `app/utils.py:3685`/`:3688` were off by one and a "correction" of
the plan and spec to `:3684`/`:3687` -- reverted once numbered output
(`awk 'NR==3685||NR==3688 {printf "%d\t[%s]\n",NR,$0}' app/utils.py`)
showed `:3685` is `if has_request_context():` and `:3688` is the `return`
inside it, exactly as originally cited. Second: running `grep -n` over the
output of a command that had ALREADY extracted a class body (rather than
over the file itself) and reading those numbers as file line numbers
produced a citation of `Community.profile_id` at `:234` (blank in the real
file) instead of its true location, `app/models.py:787`. The check that
works, used to close both: numbered output over the real file --
`awk 'NR==X {...}'` or `grep -n` run directly against the file, never
against another command's already-extracted or piped text.

**166. AN EDIT CAN FALSIFY PROSE THE SAME TASK WROTE MINUTES EARLIER, AND
THE STANDING INSTRUCTION TO RE-DERIVE AFTER THE DIFF IS FINAL WAS NOT
SUFFICIENT ON ITS OWN, THREE TIMES IN ONE ROUND.** A moved commit: dedenting
`update_community_stats`'s `session.commit()` from inside its loop to after
it made `TestUpdateCommunityStatsIsAtomic`'s own class docstring ("the
rollback READS as though it protects the task's whole effect. It does not")
and a sibling comment false the instant the fix landed -- caught by a
reviewer, not the implementer. Fixed by commit `a949d824`. A two-line
deletion: removing `recalculate_user_attitudes`'s dead `processed = 0`/
`processed += 1` shifted every citation below it by one or two lines, and
the task's own report had already derived and written down the exact shift
table before committing -- but did not apply it to the docstrings the same
task had written two steps earlier, leaving five stale citations in its own
new test class plus three more in two earlier tasks' docstrings that
happened to cite lines below the deletion; a reviewer opening the cited
lines caught all of it. Fixed by commit `f58d5d3a`. An INNER-to-LEFT join:
rewriting `calculate_community_activity_stats`'s SELECT to drive from
`community` with a LEFT JOIN, instead of from the temp table with an INNER
JOIN, made a present-tense docstring description of the INNER JOIN false --
this time the IMPLEMENTER caught it before committing, by re-reading its own
docstring for tense and rewriting the bug in past tense and the fix in
present tense. **No fix commit exists for this third instance because it
never shipped stale** -- the correction landed inside the same commit
(`1e5916f5`) that made the join change, before review ever saw it. Three
occurrences, two caught by review and one caught by self-review; the
instruction to re-derive after the diff is final was present in every
dispatch this round and was not, by itself, enough to prevent any of the
three.

**167. A COVERAGE RUN LEAVES ITS JSON IN THE HOST REPO ROOT, NOT ONLY IN
THE CONTAINER.** `compose.test.yaml`'s `test-runner` service bind-mounts the
whole repository (`./:/app:z`), so a coverage invocation's
`--cov-report=json:/app/x.json` writes to a path that is simultaneously
inside the container's filesystem and, via the bind mount, the host
working tree -- the file appears in `git status` on the host, not only
inside the container where `podman cp` would otherwise be needed to reach
it. This refines the campaign's standing rule ("the coverage JSON lands
inside the container, retrieve it with `podman cp`") rather than replacing
it: retrieval still works that way, but a coverage run also leaves a stray
artifact directly in the working tree that must be cleaned up (`rm` on the
host, not only inside the container) before the tree is clean again.

**168. `check_coverage_floors.py` COMPARES THE BLENDED `percent_covered`,
NOT THE HIGHER, STATEMENTS-ONLY `percent_statements_covered`.** Both fields
come from the same `--cov-branch` JSON summary, but they read differently:
for `app/shared/tasks/maintenance.py`'s Group A closure, `percent_covered`
(statements and branches combined) measured 24.641148325358852%, while
`percent_statements_covered` (statements alone) measured 30.28% for the
identical run. `check_coverage_floors.py` reads `entry['summary']['percent_covered']`,
so a floor set from the statements-only field would be inflated and the
ratchet could not hold it -- confirmed directly against the tree before the
floor of 24 was written, and independently re-derived by the task review
from its own freshly retrieved JSON with an exact match.

**169. COVERAGE CANNOT DISTINGUISH AN INSERT THAT RAN FROM ONE A TEST WOULD
MISS IF IT WERE DELETED.** `calculate_community_activity_stats` populates
`temp_community_activity` with four separate `INSERT` statements (posts,
post replies, post votes, post reply votes; `app/shared/tasks/maintenance.py:773-815`),
and the downstream aggregate counts `COUNT(DISTINCT ... user_id)` per
community. A test seeding one actor per source drove all four `INSERT`s to
execute and add a row apiece -- 100% coverage on all four -- but because the
post-vote and post-reply-vote sources both attributed to the SAME seeded
user, the DISTINCT-user aggregate collapsed both contributions into one
already-counted id. A reviewer's eight-cell matrix (per source: does
deleting the INSERT fail a test; does removing its bot-exclusion filter fail
a test) found two of the four cells genuinely empty in each direction --
the post-vote and post-reply-vote INSERTs were each deletable outright, and
each source's bot filter was each removable, with every test in the file
still green. Statement execution proved the code ran; it did not prove
anything would notice if it were gone. Closed by giving the vote and
reply-vote sources their own distinct actor, which is what let the
aggregate see all four independently.

**170. A PROVEN EQUIVALENT MUTANT: DELETING `unban_expired_users`'S `AND
banned_until is not null` CONJUNCT.** `app/shared/tasks/maintenance.py:398`'s
UPDATE predicate is `banned is true AND banned_until < :cutoff AND
banned_until is not null`. Deleting the trailing conjunct survived all 53
tests, 0 failures, and this is a genuine equivalent mutant rather than an
untested hole: SQL's comparison operators follow three-valued logic, so
`banned_until < :cutoff` for a NULL `banned_until` evaluates to `UNKNOWN`,
which a `WHERE`/`UPDATE ... WHERE` clause treats identically to `false` --
excluding the row. There is therefore no row and no `:cutoff` value for
which the three-conjunct predicate (with `is not null` removed) and the
four-conjunct original diverge: when `banned_until IS NULL` the `<`
comparison already excludes the row on its own; when it is not NULL the
extra conjunct is unconditionally true and changes nothing. Run and
demonstrated in the mutation transcript with the full three-valued-logic
argument written out, not merely asserted from background knowledge of SQL
NULL semantics.

**171. WHEN A MUTATION SURVIVES, CHECK THE SITE AGAINST THE TEST THE PLAN
NAMED AS ITS KILLER BEFORE RECORDING AN EQUIVALENT MUTANT -- A SURVIVOR AT
THE WRONG SITE LOOKS EXACTLY LIKE A SURVIVOR AT THE RIGHT ONE.** Mutation 14
was specified against "PC2," and the implementer first applied it to
`calculate_community_activity_stats` (re-indenting its post-loop commit at
what was then line 859 back inside the loop) because that function also
loops and commits once after the loop, the same shape `update_community_stats`
has. That mutation ran clean, 53 passed, 0 failed -- a result indistinguishable
from a genuine equivalent mutant. Instead of banking it as one, the
implementer traced the brief's own named killer test,
`test_a_failure_partway_through_leaves_no_partial_writes`, found it lives in
`TestUpdateCommunityStatsIsAtomic` and patches `text()` to fail mid-loop
specifically against `update_community_stats`'s `:309`/`:313` calls -- a
function the first mutation never touched -- concluded the site was wrong,
restored, and re-ran the same mutation against `update_community_stats`'s
own commit at `:317`, where it killed cleanly. Had this check not been made,
the round would have recorded a false equivalence: not a hole in the tests,
but a hole in the mutation record itself, indistinguishable from a real one
without re-deriving the site against the test the plan said would catch it.

**172. A CONFLICTING UNCOMMITTED WRITE DEADLOCKS POSTGRES, AND
`pytest-timeout` CANNOT INTERRUPT IT -- TWO INDEPENDENT REASONS, BOTH
LOAD-BEARING.** A probe holding an uncommitted write on a row another
session also touches (one candidate approach to discriminating a
read/write session split) deadlocked live Postgres rather than returning a
pass or fail. The configured 60s signal-based `pytest-timeout` ceiling did
not fire; `pg_stat_activity` showed the blocked backend's `dur` climbing
past several minutes. **Halfway to Postgres:** libpq's blocking-mode
readiness wait, inside `pqWait`/`pqSocketPoll`, has a `poll()`/`select()`
loop that `continue`s UNCONDITIONALLY on `EINTR` -- it is not a `read(2)`
that "commonly retries"; the wait itself never treats an arriving signal as
a reason to return control. **Halfway to Python:** psycopg2 releases the
GIL around that libpq call, so the process's C-level `SIGALRM` handler (set
by `pytest-timeout`) fires while the interpreter holds no GIL and is not
between bytecode instructions -- it does the minimum a signal handler must
(sets a flag, writes the interpreter's wakeup file descriptor), and the
Python-level handler that actually raises `pytest-timeout`'s `Failed`
cannot run until the eval loop next checks for pending signals, which
cannot happen until the blocked C call returns. Both halves have to hold
for the timeout to fail to fire; either alone would not explain it.
**Recovery**: `podman exec <db-container> psql ... -c "SELECT pid, state,
wait_event_type, wait_event, query, now()-query_start AS dur FROM
pg_stat_activity WHERE datname='<db>';"` to find the blocked/blocking
backends, `SELECT pg_terminate_backend(<pid>), ...` for every backend
involved (blocker and blockee both -- terminating only the blocker can
still leave the blockee's client-side state, and the test's own
connection, inconsistent), and `./run_tests.sh --down` before trusting
anything the recycled stack reports next, since a forcibly-terminated
backend can leave cascading errors (an `IntegrityError` from a skipped
per-test teardown, observed here) in whatever test runs immediately after.
The run does not recover on its own; it stays wedged until acted on.

**173. COVERAGE.PY RECORDS ONE ARC PAIR PER `if`, SO IT CANNOT SEE THAT A
COMPOUND'S SUB-CONDITION WAS NEVER INDEPENDENTLY EXERCISED ONCE ANY PATH
TAKES THE DECISION'S FALSE ARC BY ANOTHER ROUTE -- ONLY MUTATION CAN.**
`delete_old_soft_deleted_content:255`'s `if post and (post.image_id is
None or post.image_id not in images_used_by_many_posts):` reads as FULLY
covered in `missing_branches` -- no `(255, ...)` entry appears anywhere in
the whole-module JSON -- because an existing test takes the compound's
false arc through the reachable route (`post` truthy, but the `image_id`
sub-clause excludes it), not through the `post is None` sub-condition a
structural proof shows is unreachable single-threaded. Decision coverage is
satisfied the moment ANY path takes the false arc; it has no mechanism to
attribute that arc to a particular sub-term, so a fully-covered decision
can still contain an entirely untested conjunct and the coverage report
gives no signal that this happened. A scripted mutation dropping the
`post and ` conjunct (row 15b) is what surfaces it: the mutation survived
all 56 tests, proving the sub-condition's absence is invisible to the
whole suite, not merely to the coverage tool. No statement number or
branch number, read on its own, can ever reveal this class of gap; only
running the mutation can.

**174. THE ERROR-PATH IDIOM (`monkeypatch` A CLOCK OR HELPER TO RAISE, TO
REACH A TASK'S `except` ARM) FAILS ON TWO INDEPENDENT AXES, AND BOTH
APPEARED IN THIS MODULE.** A patched symbol must be reachable on two
counts: it must sit INSIDE the `try`, and it must be in a statement that
ACTUALLY EXECUTES given the test's own seeded data. `pwn_bots` failed the
first axis -- its cutoff (`cut_off = utcnow() - timedelta(days=1)`) is
computed one line ABOVE the `try:`, so patching `utcnow` raised before the
handler was ever entered; the test passed and would have kept passing had
the whole `except` clause been deleted. `remove_old_community_content`
failed the second axis -- its `utcnow()` call is inside a `for community in
communities:` loop over communities with `content_retention > 0`, and the
first attempt at this test seeded no such community, so the loop body
never ran and the patched clock never fired: the test failed LOUDLY
(`DID NOT RAISE`) rather than passing for the wrong reason. Both failure
modes produce a test that looks like it exercises an error path and does
not; the first passes silently, the second fails loudly, and only the
second is self-revealing.

**175. `ObjectDeletedError` DEPENDS ON WHICH SESSION DELETED THE ROW --
READ THIS BESIDE FACT 155, NOT IN PLACE OF IT.** Fact 155 established that
a deleted-then-committed SQLAlchemy instance is EXPUNGED, not expired, so a
post-delete attribute read on that SAME instance returns the pre-delete
value rather than raising. This is the opposite case, observed in the same
sub-project: when a DIFFERENT session deletes and commits a row while the
test's own session still holds an instance for that row as PERSISTENT
(never itself deleted or expunged), the test session's next refresh of
that instance finds nothing to reload and raises `ObjectDeletedError`.
Same exception class, opposite session relationship to the deleting
transaction -- one raises because the instance was refreshed after being
orphaned by someone else's commit, the other returns cleanly because the
instance was the very thing deleted and is already gone from its own
session's bookkeeping. Confirmed directly: a test whose OWN session called
`archive_user` (matching fact 155's shape) saw a clean `None` on
`db.session.get()`; a test where deletion happened through the task's
`get_task_session()` session while the test asserted through `db.session`
raised, and was fixed by capturing the id into a local variable before the
delete rather than re-fetching the ORM instance afterward.

**176. A `@celery.task`-DECORATED FUNCTION CALLED AS A BARE `foo()` RUNS
SYNCHRONOUSLY IN-PROCESS REGARDLESS OF EAGER MODE, BY ORDINARY CELERY
`Task.__call__` SEMANTICS.** This is not a property of this harness's test
configuration (`CELERY_ALWAYS_EAGER` or similar) -- it holds in production
too. Adding `@celery.task` to a function two of whose only call sites both
invoke it as a plain function call (never `.delay()` or `.apply_async()`)
therefore changes nothing observable at either call site: the decorator
makes the function DISPATCHABLE (callable via `.delay()` from elsewhere),
it does not make an existing bare call route through the broker. Anywhere
in this codebase a decorated task is called bare, that call was already
running synchronously before the decorator existed and continues to after.

**177. `File.delete_from_disk` (`app/models.py:421-434`) TOLERATES A
NONEXISTENT PATH, AND A `/static/...`-ROOTED PATH CANNOT TAKE THE S3
BRANCH ABOVE IT.** `:429`'s `elif os.path.isfile(self.file_path):` gates
the `os.unlink()` call -- when the path does not exist on disk, the
condition is simply False and the unlink is skipped with no exception, no
monkeypatch needed to test it. Confirmed directly by reading `:421-434`,
not inferred. The branch above it, `:425`, needs
`self.file_path.startswith(f'https://{current_app.config["S3_PUBLIC_URL"]}')`
AND `_store_files_in_s3()` to be true; a `file_path` beginning with
`/static/...` (the shape every locally-stored file uses) can never satisfy
the `startswith` half regardless of the S3 config flags or setting, so a
test constructing such a path exercises `:429`'s branch for the right
reason rather than merely by the S3 arm failing to trigger for an
unrelated reason.

**178. A SWEEP CLAIMED COMPLETE CAN STILL BE WRONG, AND CAN STILL BE
WRONG A SECOND TIME.** Two consecutive rounds in the same sub-project each
swept a file for stale citations after a line-shifting edit, reported the
sweep complete, and were wrong: the first round's "every remaining
citation already matched" claim left two more stale citations undetected,
caught only when a reviewer ran its OWN independent sweep rather than
trusting the claim; the second round's fix, corrected those two, again
claimed completeness, and a further reviewer's independent full sweep
found none remaining -- the third attempt was the one done to the
standard the constraint asks for, and it was independently re-verified
rather than merely re-asserted. **A sweep's completeness claim is worth
exactly as much as the check that verified it was complete, and stating
"every citation was checked" is a claim to verify, not a fact simply
because it was written down.**

**179. THE `_Recorder` IDIOM FOR A CALLABLE BELONGING TO ANOTHER MODULE
REPLACES IT IN THE CALLING MODULE'S OWN NAMESPACE, NOT THE CALLEE'S, SO
THE TEST ASSERTS ON THE HANDOVER RATHER THAN ON THE CALLEE'S OWN
BEHAVIOUR.** `delete_post` belongs to `app/shared/post.py`, a separately
floored module with its own federation behaviour this round does not test.
Its call sites in `app/shared/tasks/maintenance.py` are patched via
`monkeypatch.setattr('app.shared.tasks.maintenance.delete_post', recorder)`
-- the NAME bound inside `maintenance`'s own module namespace by its
`from app.shared.post import delete_post` import -- rather than
`app.shared.post.delete_post`, which would also work but tests a
different claim. `_Recorder.calls` then holds one positional-argument
tuple per invocation, letting a test assert on WHICH ids and WHAT
arguments reached the boundary between the two modules, without asserting
anything about what `delete_post` itself does with them once called.

**180. A `before_cursor_execute` LISTENER ON `db.engine`, RECORDING
`id(conn.connection)` PER STATEMENT, DISCRIMINATES WHICH DBAPI CONNECTION
CHECKOUT ISSUED EACH STATEMENT.** This is the technique that produced a
usable, deterministic result when other candidate approaches to testing a
read/write session split either could not run at all (a conflicting write
held across sessions deadlocked Postgres -- fact 172) or were never
attempted (whether wrapping the body changed any EXISTING test's
behaviour; whether data written on one session was visible through the
other, the harness's own premise rather than something it had proven).
Partitioning recorded statements by connection-checkout identity shows
directly whether a read and the writes that follow it share one
transaction or two, deterministically in both directions rather than by
pool-order luck: when a `db.session` read opens a transaction that stays
open across subsequent writes on a separate session object, the two
checkouts cannot coincide by accident, so a genuine split reads as two
distinct ids and a wrapped, single-session body reads as one.

**181. CONSTRUCTING A `boto3` CLIENT MAKES NO NETWORK CALL, SO AN
S3-CONFIGURED ARM CAN BE TAKEN IN A TEST WITHOUT CONTACTING AN ENDPOINT.**
`boto3.session.Session().client(service_name='s3', ...)` builds a client
object and validates its arguments locally; it does not open a connection
or perform any handshake against `endpoint_url` until a request method is
actually called on the client. A test setting all three of
`S3_ACCESS_KEY`, `S3_ACCESS_SECRET` and `S3_ENDPOINT` non-empty (so
`store_files_in_s3()` reads True) and then observing that the client
object is passed to the function under test can therefore run to
completion with no mocked transport and no real network access, confirmed
directly: such a test passed in this harness with no `http_mock` or
respx involvement.

**182. `PostReply.has_replies` (`app/models.py:3287-3292`) READS THROUGH
`db.session`, SO IT ONLY SEES A TASK'S OWN ROWS BECAUSE THE CALLER WRAPPED
THE BODY IN `patch_db_session` -- A HELPER WHOSE CORRECTNESS DEPENDS
ENTIRELY ON ITS CALLER'S WRAPPER.** Both of `has_replies`'s query branches
(`include_deleted` True or False) query through the bare `db.session`
name, not through any session passed as an argument. Inside
`delete_old_soft_deleted_content`, `:272`'s
`post_reply.has_replies(include_deleted=True)` call sees the task's own
uncommitted work ONLY because `:221`'s `with patch_db_session(session):`
has already replaced `db.session` for the duration of the `with` block --
without that wrapper (outside a Flask request context, per fact 157's
`has_request_context()` short-circuit), `db.session` would resolve to a
separate, unrelated session that has not seen any of the task's own writes.
The helper itself is correct and needs no change; its correctness for this
caller is entirely borrowed from the caller's own `patch_db_session`
wrapper, not intrinsic to the helper.

**183. `respx`'s UNMATCHED-REQUEST FAILURE IS CAUGHT BY A BARE `except
Exception`, AND TWO FUNCTIONS IN ONE MODULE CATCH IT DIFFERENTLY.**
`respx.models.AllMockedAssertionError` descends from `AssertionError`, not
`httpx.HTTPError`, confirmed by a probe run before any test in this round's
file was written: driving an unmatched request through
`refresh_instance_chooser` printed `REFRESH RETURNED NORMALLY` and the test
PASSED -- `:1010`'s bare `except Exception as e:` swallowed the
`AllMockedAssertionError` into its "Failed to connect to {domain}" branch
exactly as an ordinary connection failure would be handled. Driving the
identical unmatched request through `add_remote_communities` FAILED the
test instead: the exception propagated straight through `get_request`
(`app/utils.py:145`) and out of `add_remote_communities:1072`'s call,
because `:1077`'s narrower `except httpx.HTTPError:` does not match an
`AssertionError` subclass. **Two functions in the same module, given the
identical unmatched-route failure, take opposite paths -- one swallows it
into a documented trap, the other lets it kill the test -- and a test in
the first that simply forgets to register a route silently exercises the
failure path rather than failing loudly.** This is fact 148's shape (a
test's own oracle silently exercising the wrong path) recurring through a
different mechanism: there, an assertion too weak to notice; here, an
exception-hierarchy mismatch between what `respx` raises and what one of
two structurally similar functions happens to catch.

**184. `get_request` SLEEPS 3-10 SECONDS ON RETRY, AT TWO SEPARATE
HANDLERS -- AND A `httpx.HTTPError` FROM A MOCKED TRANSPORT IS NOT A WAY TO
AVOID THAT.** `app/utils.py:158-172` (the `httpx.ReadError` handler) and
`:173-180` (the `httpx.HTTPError`/timeout handler) both call
`sleep(random.randint(3, 10))` before retrying. **A mocked transport that
raises `httpx.HTTPError` -- or any of its subclasses, including
`httpx.ConnectError`, `httpx.ReadError` and `httpx.TransportError` --
enters exactly `:173`'s `except httpx.HTTPError as read_timeout:` and pays
the full sleep**; an earlier version of this fact said the opposite and was
wrong. The exceptions that reach `get_request`'s caller WITHOUT sleeping are
the ones `get_request` normalises immediately, with no retry: the
`is_invalid_get_request_uri` check's direct `raise httpx.HTTPError(...)`
(`:132-134`), `httpx.InvalidURL` (`:146`), a plain `ValueError` (`:155-157`),
and `httpx.StreamError` (`:181`) -- none of the last three is itself an
`httpx.HTTPError`, so none can be reached by raising `httpx.HTTPError` from
a mock. **The actually-cheap way to reach a caller's own
`except httpx.HTTPError` (or broader `except Exception`) arm is not to make
the transport raise at all: it is to replace `get_request` itself**, via
`monkeypatch.setattr('<caller's module>.get_request', ...)`, when
`get_request` is imported at the caller's module scope and called there as
a bare name (confirmed for `app/shared/tasks/maintenance.py:19`'s import,
reached by both `:1009` and `:1072`). That patch never touches
`get_request`'s own body, so none of its retry handlers, or its
sleep-free normalisations, ever run -- the caller's handler is exercised
directly, at zero cost, regardless of which real `get_request` exception
it is meant to stand in for. Only when a test needs `get_request`'s OWN
internal behaviour on a genuine transport failure -- not just a caller's
reaction to `get_request` having failed -- does this shortcut not apply,
and closing that gap really does cost the 3-10 second sleep.

**185. A HELPER IMPORTED *INSIDE* A FUNCTION CANNOT BE PATCHED IN THE
CALLING MODULE'S OWN NAMESPACE.** `search_for_community` is imported at
`app/shared/tasks/maintenance.py:1111`, inside
`add_remote_community_from_post`'s own body, not at module scope -- so
`app.shared.tasks.maintenance.search_for_community` never exists as an
attribute of `maintenance`'s namespace at any point before the function
actually runs, and `monkeypatch.setattr('app.shared.tasks.maintenance.search_for_community', ...)`
raises `AttributeError` rather than patching anything. The patch has to
target the name where it actually lives, `app.community.util.search_for_community`
(confirmed module-level at `app/community/util.py:34`), re-resolved from
that module's own namespace every time `:1111`'s `from ... import` line
executes. Contrast `find_language_or_create`, imported at module scope in
`maintenance.py:13` -- because that import runs once at module load and
binds a name permanently inside `maintenance`'s own namespace, the usual
`monkeypatch.setattr('app.shared.tasks.maintenance.find_language_or_create', ...)`
idiom does reach it, and `refresh_instance_chooser`'s own tests use exactly
that form.

**186. `tempfile.mkdtemp()` IS THE REMEDY ONLY WHEN THE CODE UNDER TEST IS
TOLD WHERE TO LOOK.** Fact 154 established that a file a test needs to see
must be created inside the test process, typically via `tempfile.mkstemp()`
or `tempfile.mkdtemp()`, because only that process's own filesystem view is
guaranteed visible to both the test and the code under test. That advice
does not by itself make a directory-sweeping task testable:
`clean_up_tmp` hardcoded a relative path (`directory = 'app/static/tmp'`)
with no parameter accepting a caller-supplied directory, so a test's own
`tempfile.mkdtemp()` directory -- however correctly created -- had no way
to reach the function under test at all.
`test_a_stale_image_is_removed`'s first attempt to call
`clean_up_tmp(directory)` against the unmodified function failed with
`TypeError: clean_up_tmp() takes 0 positional arguments but 1 was given`.
Fact 154's advice only became applicable once a production change added a
`directory=None` parameter for the test to pass a `tempfile.mkdtemp()` path
into. The distinction: `tempfile.mkdtemp()` solves WHERE a test-visible
directory lives; it does nothing about WHETHER the code under test accepts
being told where to look.

**187. A MODULE MEASURING ZERO MISSING STATEMENTS AND ZERO MISSING ARCS CAN
STILL HIDE UNTESTED BEHAVIOUR -- THE MUTATION PASS IS NOT REDUNDANT WITH THE
COVERAGE GATE.** After `app/shared/tasks/maintenance.py`'s Group C first
half closed to `coverage.py`'s own zero-missing measurement, a one-at-a-time
mutation pass over the same closed ranges still found six real holes, and
**three of the six were found by mutation alone -- nothing else in the round
had flagged them**: the `flipboard.com` domain literal in
`check_instance_health`'s skip guard (`:453`) had never been exercised by any
seeded test domain; two of the three accepted nodeinfo schema URLs in
`check_instance_health`'s discovery match (`:484`, the `https` 2.0 variant,
and `:485`, the 2.1 variant) had never been matched, every discovery test
using only the first (`:483`); and `monitor_healthy_instances`' `elif
nodeinfo.status_code >= 300:` arm (`:582`) had never been driven by a genuine
`[300, 400)` status, every discovery test using only 200 or 404. All six
mutants SURVIVED the full suite before a new test closed each in turn.
Decision-level branch coverage records that a line ran and that its
alternative ran somewhere; it does not record which literal, which of several
equally-typed alternatives, or which numeric range was the one actually
exercised.

**188. COVERAGE.PY RECORDS THAT BOTH ARCS OF A BRANCH RAN, NEVER WHICH
THRESHOLD VALUE DISCRIMINATED IT.** `monitor_healthy_instances`' no-href
`else` arm checks `instance.failures > 5` (`:629`, setting `dormant`) and
`instance.failures > 12` (`:632`, setting `gone_forever`). Both arcs of both
`if`s had been exercised by an existing test before any mutation ran --
`coverage.py` reported the lines fully covered -- but the only test on this
arm seeded `failures` from 12 to 14, a jump that lands past BOTH boundaries
at once. That single test cannot tell `> 5` from `>= 5`, nor `> 12` from
`>= 12`: a mutant substituting either comparison operator, on either line,
survives unchanged, because every seed the suite used was already decisively
on one side of both boundaries. Closed with boundary-exact pairs seeded at
3/4 and 10/11. Coverage was satisfied by this arm long before discrimination
was; the two states are not the same claim, and only mutation (or a
boundary-exact test written on purpose) tells them apart.

**189. AN ORACLE OVER A FIELD THAT SEVERAL ARMS OF THE SAME FUNCTION WRITE
PINS NONE OF THEM IN PARTICULAR.** `monitor_healthy_instances`'
fetch-and-escalate block has four reachable arms once `nodeinfo_href` is
set -- the 200 arm (`:597-604`), the `elif >= 300` arm (`:605-611`), the
`except` arm (`:612-620`), and (for a DIFFERENT instance, taking the
`if instance.nodeinfo_href:` false branch) the no-href `else` arm
(`:626-634`) -- and `instance.failures` is written in every one of them:
reset to `0` at `:602`, incremented at `:607`, `:614` and `:627`. An
assertion that only checks `reloaded.failures == <some value>` after running
the function is bound to whichever arm actually ran, not to the arm a
docstring names, because three of the four writers produce numerically
plausible results for a variety of unrelated scenarios. This round fixed six
tests or docstrings across four different tasks that claimed to prove one
arm while actually being satisfied by another -- the recurring shape being
"some field moved" or "some field did not move" read as proof of a specific
LINE, when several lines in the same function move that field for different
reasons.

**190. A MOCK THAT RETURNS WHERE PRODUCTION WOULD RAISE REMOVES THE EXACT
SIGNAL AN ASSERTION DEPENDS ON TO FAIL.** `test_a_live_instance_is_untouched_by_the_sweep`
asserts `reloaded.gone_forever is False` and `reloaded.failures == 0`; it
never asserted `dormant is True`. The second property could not fail no
matter how badly `check_instance_health`'s second-loop dormant filter
(`:447` -- NOT `:437`, which belongs to the first loop and is caught by this
same test's first assertion instead) was broken, because the patched
`get_request_instance` was originally configured to RETURN
`httpx.Response(status_code=500)` rather than raise, and `:496`'s
`instance.failures += 1` sits inside the recheck loop's `except` arm, which a
mocked non-raising 500 never enters. Weakening `:447`'s filter by hand and
re-running showed exactly this: `failures == 0` did NOT fail, while the
strengthened version of the same test (the mock changed to RAISE) failed
cleanly with `assert 1 == 0` (`1 = <Instance live.example>.failures`). The
tautological assertion and the binding one, side by side, against the
identical mutation, is the clearest demonstration this round produced of why
a test's oracle is only as strong as the path its own mocks force the code
down.

**191. THE FIRST `Instance` A TEST SEEDS BECOMES id 1, BECAUSE OF THE
HARNESS'S OWN SEQUENCE RESET -- AND THE FIX IS A ROW EXCLUDED BY STATE, NOT
BY id.** `tests/conftest.py:131`'s `db_session` teardown runs `SELECT
setval(c.oid, 1, false) FROM pg_class c` across every sequence after each
test, so the very first `Instance` row the NEXT test inserts always claims
id 1 -- and `check_instance_health:449` and `monitor_healthy_instances:543`
both filter `Instance.id != 1`. A test that seeds one instance and asserts a
health task changed it would have PASSED even if the task processed nothing
at all, because its subject was silently excluded from the task's own
selection query before the loop body ever ran -- confirmed directly: an
instrumented probe seeding exactly one instance showed `instance.id=1` and
`get_request_instance` never called, while the identical probe seeding a
decoy first (landing the subject on id 2) showed the call fire and
`failures` move `0 -> 1`. The remedy is a `_seed_instance(domain,
software='mastodon')` helper that seeds a RESERVED row first if none exists
yet (`dormant=True`, `gone_forever=True`, `start_trying_again` a year in the
future) before returning `make_instance(domain, software=software)` for the
caller's actual subject. The reserved row is excluded BY STATE, not by id,
which is deliberately more robust than seeding an inert decoy to consume id
1: a decoy excluded only by id is still selected if the sequence has already
advanced past 1 for an unrelated reason, where a row excluded by its own
`dormant`/`gone_forever`/`start_trying_again` columns is invisible to both
tasks' selection queries regardless of which id it actually receives.

**192. A BARE `except:` IN A SHARED HELPER DEFEATS AN AUTOUSE OUTBOUND-HTTP
GUARD -- THE SAME SHAPE AS FACT 183, REACHED BY A DIFFERENT ROUTE.**
`block_outbound_http` (`tests/conftest.py:262-263`, session-scoped, autouse)
makes an httpx request that no `http_mock` route matches RAISE rather than
reach a real transport. `get_request_instance`'s bare `except:`
(`app/utils.py:192`, catching everything, not merely `Exception`) swallows
that raise exactly as it would swallow a genuine connection failure,
increments `instance.failures`, and returns a synthetic
`httpx.Response(status_code=500)` -- so a test that forgets to patch
`get_request_instance` does not fail loudly with a blocked-request error; it
silently exercises the function's own failure-handling branch and can still
pass. Confirmed directly: an unpatched call in this round's probe raised
inside `get_request` (blocked by the fixture), was caught, moved `failures`
`0 -> 1`, and the enclosing test still `PASSED`. Fact 183 established this
same shape for `respx`'s `AllMockedAssertionError` being swallowed by a bare
`except Exception` in `refresh_instance_chooser` -- also
`app/shared/tasks/maintenance.py`, the sibling module this round extends;
this is the identical "a guard fixture's own failure signal gets caught by
the code under test" hazard, reached through a different fixture
(`block_outbound_http` rather than `http_mock`) and a different bare
`except:` (a literal bare clause rather than `except Exception`, which is if
anything a wider net). Fact 148 is the shape's earlier and more distant
sighting: `post_request`'s `except Exception as e:`
(`app/activitypub/signature.py:143`) swallows an unmatched respx route too,
but into a different side channel entirely -- an `ActivityPubLog` failure
row, checked by a row count rather than by a recorder or a status field --
in a different module (`app/activitypub/signature.py`, not
`maintenance.py`) with a different consequence for a test (a spurious send
becomes invisible to `_delivered_inboxes`/`route.calls`, not a `failures`
counter moving). Fact 183 already names this lineage explicitly ("this is
fact 148's shape... recurring through a different mechanism"); this entry
is the third sighting, closest in shape to 183 (same module family, a bare
except over an httpx-adjacent guard) and traceable through it back to 148
(the same failure-swallowed-into-a-side-channel mechanism, one module
further out).

**193. REPLACING A REQUEST HELPER BY NAME, AT ITS CALLER'S MODULE SCOPE,
BEATS MAKING THE TRANSPORT FAIL.** Every helper `check_instance_health` and
`monitor_healthy_instances` call -- `get_request_instance`, `get_request`,
`instance_banned`, `download_defeds` -- is imported at
`app/shared/tasks/maintenance.py`'s module scope, all four on the single
`from app.utils import ...` line at `:19`. (`:13`'s import from
`app.activitypub.util` is a different line entirely -- `find_actor_or_create`,
`find_language_or_create`, `find_instance_id` -- none of the four.) So
`monkeypatch.setattr('app.shared.tasks.maintenance.<name>', ...)` reaches
every call site directly. This round's entire test file used this idiom
exclusively and never needed `respx` or a real (or even a fake) `httpx`
transport: a test that needs a helper to fail patches it to raise; a test
that needs a non-200 response constructs a real `httpx.Response(status_code=
...)` and returns it. Two costs this avoids, both established by earlier
rounds: `respx`'s unmatched-request failure being a distinct exception type
a bare `except Exception` can swallow in ways specific to which function
catches it (fact 183), and `get_request`'s own internal retry logic sleeping
3-10 real seconds on certain exception types (fact 184) -- neither applies
when `get_request` itself never runs. Contrast a helper imported INSIDE a
function's own body rather than at its caller's module scope (fact 185):
that idiom cannot reach it, because the name is never bound in the calling
module's namespace before the function executes.

**194. TWO SIBLING TASKS IN ONE MODULE CAN DIFFER IN WHETHER THEY WRAP IN
`patch_db_session`, AND WHETHER THE DIFFERENCE IS OBSERVABLE DEPENDS ON WHERE
THE WRAPPED HELPER'S WRITES ACTUALLY LAND.** `check_instance_health` wraps
its entire body in `with patch_db_session(session):`
(`app/shared/tasks/maintenance.py:431`); `monitor_healthy_instances`
(`:533-742`) does not, at any line. Investigating whether that asymmetry is
a real defect measured, rather than assumed, that it is not: after forcing
`get_request_instance`'s exception path, `failures`/`dormant`/`gone_forever`
came out byte-identical with and without a hypothetical wrapper, three runs
of three. The reason generalises past this one pair of functions:
`patch_db_session` (`app/utils.py:3679-3711`) installs a `SessionWrapper`
(`:3707`) whose `__getattr__` (`:3700-3705`) delegates every attribute read
to the task's OWN session object, so under the wrapper `db.session.commit()`
is textually the task session's own commit -- there is nothing for a
wrapped and an unwrapped call to disagree about UNLESS the object being
committed lives somewhere the unwrapped `db.session` can also see it (the
request-scoped session's own identity map, or a row already persisted to the
database). Here it does not: `instance` lives only in the task session's own
identity map, so no session other than the task's own has anything of it to
flush. The asymmetry is real as a matter of code shape; whether it is a
defect depends entirely on whether some OTHER caller's write can reach the
same identity map from outside the task session, which is a case-by-case
question this measurement answers only for these two functions and this one
field.

**195. A PROCESS THAT DIES MID-MUTATION LEAVES NO OPPORTUNITY TO RESTORE
ITSELF -- THE CHECK BELONGS ON THE CONTROLLER'S SIDE AT RESUME, NOT ONLY THE
IMPLEMENTER'S.** The mutation discipline this campaign uses says to restore
production code before any point where the work might stop, so that a
mid-mutation interruption never leaves a probe applied. That rule assumes
the interruption is something the implementer's own process can observe and
act on. It cannot cover the one failure mode where the implementer's process
itself dies: this round had an implementer's session terminate mid-task with
a probe still applied (`isinstance(links, dict)` removed from a discovery
guard, mid-verification of a mutant), and no restore ever ran because there
was no surviving process left to run it. The controller found the stray
mutation on resume by checking `git diff --stat -- app/` before resuming the
task, rather than trusting that the discipline had held, and restored it
with `git checkout -- <file>`. The general point: a self-discipline rule
stated entirely in terms of what the acting process does before it stops has
a blind spot at the one place a process cannot help itself -- its own
death -- and the check for probe residue needs a second, independent home on
whichever side resumes the work.

**196. THE "TAUTOLOGICAL ASSERTION VERSUS BINDING ONE" DEMONSTRATION IN FACT
190 CAME FROM A DIFFERENT TEST THAN THE ONE FACT 190 NAMES.** The `dormant is
True` / "did NOT fail" pairing belongs to
`test_a_document_without_software_leaves_the_instance_dormant`, not
`test_a_live_instance_is_untouched_by_the_sweep`. With `check_instance_health`'s
`'software' in node_json` guard (`:463`) negated by hand, one run produced:
`assert reloaded.dormant is True` -- did NOT fail, while `assert
reloaded.failures == 0` DID fail (`assert 1 == 0`), because negating `:463`
raises a `KeyError` that lands in `:494`'s `except`, incrementing `failures`
without touching `dormant`. Two incidents -- this one and fact 190's -- had
been spliced into one fact in an earlier draft of this register; they are
separated here because they pin different lines (`:463` here, `:447` in fact
190) against different tests.

**197. A NOISY BINDING IS STILL A BINDING, BUT THE NOISE IS WORTH RECORDING
SO A LATER FIX DOESN'T LOOK LIKE A REGRESSION -- AND SUB-PROJECT 33 IS THE
FIX THIS FACT PREDICTED.** `test_a_banned_domain_is_skipped_before_any_request`
(`monitor_healthy_instances`' `:547` guard), AT THE TIME THIS FACT WAS
WRITTEN, failed when the guard was removed via the pre-existing unbound-
`response` `UnboundLocalError` at (then) `:689` -- `banned.example` got
processed, the recorder's 200-with-`software` body set `instance.software`
and cleared `dormant`, `:637`'s admin-role block was entered, the unpatched
`get_request` raised, and the `finally`'s `if response:` threw before
`assert recorder.calls == []` was ever evaluated. The failure was real and
reliable, so the test bound -- but it bound by crash, not by its own
assertion. Sub-project 33's D383 fix is that future round: `response = None`
is now seeded before both identity `try` blocks and both `finally` guards
read `is not None`, so the site this fact named is now `:694`, not `:689`,
and the raise is caught by the block's own `except` instead of escaping
unbound. This test's failure mode has changed shape from a crash to a clean
assertion failure exactly as predicted -- see
`tests/test_shared_tasks_maintenance_health.py:786-793`'s rewritten
docstring for the post-fix account, and facts 198-205 for what sub-project
33 recorded about the fix itself.

**198. AN UNPATCHED `get_request` KILLS THE TASK RATHER THAN BEING SWALLOWED
-- THE OPPOSITE OF WHAT SUB-PROJECTS 31 AND 32 FOUND FOR
`refresh_instance_chooser` AND `get_request_instance`, AND A FIX THEN
CHANGED WHICH STATE IS TRUE.** Before sub-project 33's DC1 fix, Task 1
probed what an unpatched `get_request` does inside `monitor_healthy_
instances`' identity blocks, against completely unmodified production code,
and recorded the result verbatim: `PROBE: task raised UnboundLocalError:
cannot access local variable 'response' where it is not associated with a
value` and `PROBE: failures=2 dormant=False`. `get_request` (`app/
utils.py:131-185`) always raises on failure -- unlike `get_request_
instance` (`:189-196`), whose bare `except:` swallows everything and
returns a synthetic 500 -- so a forgotten patch here does not quietly
redirect a test down the wrong branch, it crashes the whole task loudly and
immediately (the raise is `respx`'s `AllMockedAssertionError`, which skips
every one of `get_request`'s retry-sleep branches). Sub-project 33's DC1
fix (`response = None` seeded before each identity block's `try`) then
changed that state: the same unpatched call is now caught by the block's
own `except Exception:` and becomes an ordinary failure increment instead of
a crash. Both states are true of this file, at different points in its
history, and this fact records both rather than only the current one.

**199. AN INSTANCE'S `software` VALUE DECIDES WHICH BLOCKS A TEST ENTERS,
AND A TEST THAT SETS IT FOR ONE REASON ENTERS THEM FOR ALL REASONS.**
`monitor_healthy_instances`' Lemmy/PieFed admin-role block (`app/shared/
tasks/maintenance.py:637`) is gated on `instance.software` being one of
`'lemmy'`, `'piefed'` or `'pylova'`; the MBIN block (`:703`) is gated on
`'mbin'`. `instance.online()` (`not (dormant or gone_forever)`) needs no
arranging, since `_seed_instance` leaves both `False`. So a fixture that
sets `software` to satisfy some UNRELATED check enters whichever
`software`-gated block that value happens to match, whether the test
intended to exercise it or not. Sub-project 32 hit this by accident: one of
its version-comparison tests set `software='lemmy'` for reasons having
nothing to do with the admin-role block, entered it anyway, and crashed on
what later became D375. Sub-project 33's own `_quiet_http_half` (fact 200)
exists specifically to make entering these blocks harmless rather than to
prevent entry, because preventing entry is not an option once `software` is
set for any reason at all.

**200. `_quiet_http_half`'S SHAPE: WHY AN IDENTITY-PHASE TEST MUST
NEUTRALISE THE BLOCKS ABOVE IT, AND WHAT A 404 THERE CONTRIBUTES TO
`failures`.** `tests/test_shared_tasks_maintenance_identity.py:131-141`
patches `get_request_instance` to always return a 404, so every fixture in
the file goes through the fetch and discovery blocks above the identity
phases before reaching them (fact 199 is why a software-gated identity
block cannot simply be avoided instead). Walking a 404 through those blocks
for an instance with no `nodeinfo_href` set: the discovery block's `elif
status_code >= 300:` arm increments `failures` once, and the still-unset
`nodeinfo_href` then falls to the no-href `else` arm below the fetch block,
incrementing `failures` a second time -- **exactly two**, independently
derived from the code and confirmed against Task 1's probe output
(`failures=2`), and well under the `> 5` dormancy threshold so `dormant`
stays `False`. Every identity-phase assertion on `failures` in this file is
written against a baseline of 2 for exactly this reason -- `before + 2`
after the identity block runs cleanly, `before + 3` if it catches a
swallowed crash instead. That pattern is reused six times, at every guard
whose negation is a crash swallowed into a skip rather than a clean
early-out: the Lemmy unresolvable-actor guard (`:650`'s `user and`,
identity `:336`), the Lemmy duplicate-admin guard (`:650`'s second
conjunct, identity `:361`), the Lemmy non-200 site-response guard (`:641`,
identity `:386`), the MBIN missing-username guard (`:712`, identity `:677`),
the MBIN unknown-username guard (`:715`, identity `:718`), and the MBIN
non-200 admins-response guard (`:707`, identity `:784`). No removal-arm
test carries a `failures` assertion -- `test_an_admin_no_longer_listed_
loses_the_role` and `test_a_still_listed_admin_keeps_the_role` (both the
Lemmy and MBIN versions) assert on role sets only, since a broken removal
predicate does not raise, it just deletes or keeps the wrong rows.

**201. PATCHING `cache.delete_memoized` MUTATES A SHARED OBJECT, NOT A
MODULE-LOCAL NAME, AND UNDER `NullCache` AN INVALIDATION IS UNOBSERVABLE --
SO THE ORACLE MUST RECORD THE CALL.** `app/shared/tasks/maintenance.py:12`
imports `cache` from `app` (`from app import celery, cache, httpx_client`),
the SAME `Cache` instance every other module that imports `cache` shares --
not a name local to `maintenance.py`. `monkeypatch.setattr('app.shared.
tasks.maintenance.cache.delete_memoized', recorder)` therefore patches the
live shared object's bound method for the duration of the test, restored at
teardown; this is safe under this project's single-process, non-`xdist`
test runner, where nothing else can observe the patched method mid-test.
`tests/conftest.py:68` sets `CACHE_TYPE = 'NullCache'` on the test config,
under which `cache.delete_memoized` has no backing store to invalidate --
so an oracle that tried to observe cache STATE after the call would pass
whether or not the call fired at all. The only oracle available is
recording the call itself via the monkeypatched recorder, which is what
made DC2 (a non-200 response still invalidating the cache) observable at
all.

**202. A MODULE MEASURING ZERO MISSING STATEMENTS AND ZERO MISSING ARCS CAN
STILL HIDE FOUR REAL BEHAVIOURAL HOLES, EVERY ONE INSIDE A CONSTRUCT
COVERAGE.PY RECORDS AS A SINGLE ARC PAIR -- AND THIS TIME IT HAPPENED AT
100%, NOT PARTIAL COVERAGE.** `app/shared/tasks/maintenance.py` measured
`missing_lines: []` and `missing_branches: []` three separate times this
round (Task 9's implementer, Task 9's review, and Task 10 after its own
mutation pass) -- genuinely zero on both instruments. Task 10's mutation
pass against that fully-covered module still found four survivors that were
real holes, not equivalents: dropping `'pylova'` from `:637`'s three-way
`or`, dropping `'piefed'` from the same disjunction, dropping the
`http://` disjunct from `:647`'s scheme check, and dropping the `username
and` conjunct from `:713`'s compound guard. Every one of these lives inside
a construct `coverage.py` records as ONE arc pair regardless of which
specific disjunct or conjunct made it true, so a test suite that entered
the branch via any one path registered full coverage of it while never
supplying the specific input the missing disjunct/conjunct existed to
handle. Fact 187 (sub-project 32) showed this same shape at partial
coverage; this round reproduces it at FULL coverage, which is the stronger
version of the lesson -- there is no coverage number, including 100, that
makes a mutation pass redundant.

**203. `get_task_session()` CARRIES `autoflush=True`; `db.session` IS
PINNED TO `autoflush=False` -- REASONING ABOUT PENDING-WRITE VISIBILITY
INSIDE A CELERY TASK DIFFERS FROM THE SAME REASONING IN A REQUEST.**
`get_task_session()` (`app/utils.py:3673-3675`) returns a bare
`Session(bind=db.engine)` with no `session_options` override, so it keeps
SQLAlchemy's library default, `autoflush=True`. `db.session` (`app/
__init__.py:81`) is explicitly constructed with `session_options={"autoflush":
False}`. Under `autoflush=True`, any pending write on that session is
flushed to the database automatically the moment the NEXT query on the same
session executes, regardless of what shape consumes that query's result
(directly by a `for` loop, or drained into a list first) -- both see the
identical, already-flushed state at the identical program point. Every
Celery task in this module runs against the `autoflush=True` session; every
test fixture in this file seeds and asserts through the `autoflush=False`
`db.session`. A fixture built to catch a pending-write-visibility bug in a
Celery task must account for this asymmetry or it will not be testing what
it thinks it is testing -- see fact 204, which is exactly what happened
here.

**204. A GATE THAT CANNOT FAIL IS AS UNINFORMATIVE AS A TEST THAT CANNOT
FAIL.** Sub-project 33's DC3 fix was authorised by a gate comparing the set
of `InstanceRole` rows a removal query would act on, before and after
draining that query into a list before deleting from it, under a fixture
seeded with both a surviving and a departing admin. The row sets matched
(`{(2, 2)}` both times), and the change was made on that basis. A later
review found the comparison could not have failed regardless of the
fixture: the surviving admin's row was already committed before the task
ran, so no write was ever pending when the removal query executed, and --
independent of that gap -- fact 203's `autoflush=True` asymmetry means no
fixture on this codebase COULD make the two orderings disagree, because a
pending write is always flushed before the removal query next runs either
way. The change itself was still correct, on grounds independent of the
vacuous gate (mutating a result set mid-iteration and desynchronizing the
session's identity map are defects on their own terms) -- but the gate that
was supposed to have tested the specific ordering risk never could have,
which the record now states plainly rather than leaving the original,
overstated justification standing. This campaign has spent six rounds
learning this lesson about TESTS; this is the first time it has bitten a
GATE designed to authorise a production change before the change was made.

**205. A WORKING-TREE SCANNER AND A MUTATION-TESTING TASK COLLIDE ON EVERY
ROUND THAT MUTATES PRODUCTION, AND THE COLLISIONS CLUSTER ON EXACTLY THE
GUARDS WORTH MUTATING.** Sub-project 33 had three such collisions, one per
task that mutated `app/shared/tasks/maintenance.py` in place to prove a
test's kill (the Lemmy admin-role removal's `not in` negation, the MBIN
block's `(isAdmin or isGlobalModerator)` compound, and a response-status
guard) -- each time, a background security scanner flagged the LIVE,
in-progress mutation as a HIGH-severity authorization or logic-inversion
defect. Every flag was a false positive in the sense that mattered:
`git show HEAD:` showed the correct code, and `git diff -- app/` was empty
once the implementer's own restore had run; the scanner was reading the
WORKING TREE during the exact window a probe was intentionally applied to
it. None of the three cost more than a single verification command once the
pattern was recognised. The collisions are not coincidental: a scanner
looks for authorization conditions, membership tests and status checks, and
those are also precisely the guards a coverage campaign most needs to
mutate to discriminate. The mitigation is not to stop mutating -- it is
that during a probe window, HEAD is the artifact under test, not the
working tree; check `git show HEAD:` before believing OR dismissing such a
finding, and expect it to be naming a real invariant, since the mutation
was chosen precisely because that invariant matters.

**206. THE `SRC_API` ARM OF AN `app/shared/` FUNCTION NEEDS NO REQUEST
CONTEXT; THE `SRC_WEB` ARM DOES; AND WHAT BLOCKS AN API-ARM TEST IS THAT
`make_user` DEFAULTS TO A NON-LOCAL USER.** Sub-project 34's design spec
asserted, as "the fact most likely to be missed", that both source arms of
`vote_for_post` and `vote_for_poll` need a request context, because both
reach `if user.banned or user_ip_banned():` and `user_ip_banned`
(`app/utils.py:2311-2314`) resolves through `app/utils.py:2308` to
`app/__init__.py`'s `get_ip_address`, which reads `request`. The chain is
right and the conclusion is wrong: `get_ip_address` (`app/__init__.py:68-77`)
wraps its whole body in `try: ... except RuntimeError: ip = ''`, catching
exactly the "working outside of request context" error, so `user_ip_banned`
sees a falsy address, falls off the end, returns `None`, and the guard
passes. **Call the `SRC_API` arm directly, with no context.** The `SRC_WEB`
arm genuinely does need one, for `flash` and `request.args`. What actually
stops an API-arm test is unrelated: `authorise_api_user`
(`app/utils.py:3628-3629`) raises `Exception('incorrect_login')` for any user
with `ap_id is not None`, and `tests/factories.py`'s `make_user` defaults to
non-local — **seed with `local=True` or every bearer-token test fails before
reaching the function under test.** Carrying a request context an API test
does not need is not harmless: the next round copies the harness and inherits
the wrong model of it.

**207. `community.private` IS THE FEDERATION LEVER; `community.local_only` IS
A TRAP, BECAUSE `can_downvote` READS IT.** To stop a shared-layer function's
eager Celery task from attempting outbound federation, set
`community.private = True`: `app/shared/tasks/likes.py`'s body then stops
after its row lookups and before any request, and `block_outbound_http` never
fires. `local_only` looks like the same switch and is not — `can_downvote`
reads it at `app/utils.py:2448` (`if community.local_only and not
user.is_local():`), so setting it silently changes which permission arm a
vote test takes and a test written to isolate federation quietly becomes a
test of the permission gate instead. The general shape: **before using a
model flag as a test lever, grep for every read of it, not just the one you
are aiming at.**

**208. `mark_post_read` AND `hide_post` EACH NAME TWO DIFFERENT FUNCTIONS —
ONE IN `tests/factories.py`, ONE IN `app/shared/post.py` — WITH DIFFERENT
SIGNATURES.** `tests/factories.py:665` is `mark_post_read(user: User, post:
Post) -> None`, a seeder that inserts one `read_posts` row.
`app/shared/post.py:1115` is `mark_post_read(post_ids: List[int], read: bool,
user_id: int)`, the production function under test. Likewise
`tests/factories.py:679` `hide_post(user, post)` against
`app/shared/post.py:1020` `hide_post(post_id, hidden, src, auth=None)`. A
file that imports both gets whichever was imported last, with **no error** —
the arguments are positional and the types are close enough that the failure
surfaces somewhere else entirely. Import one of them qualified
(`from app.shared import post as post_module`, then `post_module.hide_post`),
or alias the factory. Group B's round tests `hide_post` directly and will hit
this.

**209. RAISE A USER'S DAILY VOTE COUNT BY WRITING THE REDIS KEY, NOT BY
SEEDING VOTES — AND DELETE IT AFTERWARDS OR YOU RAISE A LATER TEST'S QUOTA.**
`votes_cast_today` (`app/models.py:47-52`) reads
`votes_cast_{date.today()}_{user_id}` from Redis and returns `0` when the key
is absent. So `redis_client.set(f'votes_cast_{date.today()}_{user.id}',
str(app.config['VOTE_QUOTA']))` puts a user exactly at the quota boundary
with no database rows at all, which is what makes both directions of
`app/shared/post.py:53`'s `>` cheap to pin. **The hazard is the teardown.**
These tests run against the compose stack's real, shared Redis (see fact 210
for why the `redis_double` fixture is not an option here), and Redis is not
in the database teardown: `tests/conftest.py:126-132` deletes every table and
resets every sequence, so the NEXT test gets a user with the same id and
inherits the leftover key — its quota is silently raised and, if it then
fails, the failure looks like a production defect in the quota check. Every
test that writes such a key must clear it in a `finally`; sub-project 34 does
this through a `_clear_votes_cast(user_id)` helper. The same reasoning
applies to any Redis key keyed on a database id.

**210. `redis_double` REACHES A FUNCTION-BODY `from app import redis_client`
AND THEN BREAKS IT ON `EVALSHA`.** `tests/conftest.py:459-465` warns that the
fixture may not reach `from app import redis_client` sites written inside a
function body. For `app/shared/post.py:1126` the warning's implication is
wrong in both directions: the fixture **does** reach the import (the import
re-executes on every call, so patching the single attribute `app.redis_client`
redirects it), but `:1127`'s `redis_client.lock(...)` releases its lock via
`EVALSHA`, which fakeredis does not implement, so every path through
`mark_post_read` dies on `redis.exceptions.ResponseError: unknown command
'evalsha'`. **Do not request `redis_double` in a test whose code path takes a
Redis lock**; bind to the compose stack's real Redis instead, and pay fact
209's teardown cost. `app/shared/post.py:765` (`delete_post`) and
`app/models.py:2730,2754,2818` have the same lock-inside-a-function shape.

**211. WHEN A DOCUMENT CARRIES BOTH THE EVIDENCE AND THE CLAIM, CHECK THE
CLAIM AGAINST THE NUMBER, NOT AGAINST ITS OWN REASONING.** Sub-project 34's
mutation report recorded, in a results table, `C1 SURVIVED (58 passed)`. Four
sections later the same report, and a test docstring it had just written,
claimed that dropping that same cast alone made a downstream filter discard
every choice — behaviour which, had it been true, would have made that very
test kill C1. **The evidence and the claim sat in one artifact, disagreeing,
and neither the implementer nor the first reviewer caught it, because both
checked the claim's reasoning rather than the claim against the number.** It
took a second review pass. This is the same failure as fact 204's vacuous
gate, one level out: an argument can be internally coherent and still
contradict a measurement printed on the same page. **When a report contains a
run's output, read the claims against the output first and against their own
logic second.** It costs seconds and it is the only check that is independent
of the reasoning being checked. The correction matters more in a test
docstring than in a report, because the report is a workspace artifact that
is deleted when the round closes and the docstring is inherited by every
later round that reads the file.

**212. API AND WEB CALLERS OF THE SAME SHARED FUNCTION ROUTINELY DISAGREE
ABOUT TYPE, AND A SUITE THAT SEEDS THROUGH THE ORM WILL TEST ONLY ONE OF
THEM.** The API deserializes through a marshmallow schema; the web route
reads `request.form` raw. For `vote_for_poll` the difference is inside a
single ternary: `app/post/routes.py:642` hands single mode an `int` (it calls
`int(...)`) and multiple mode a **`list[str]`** (`request.form.getlist`),
while `app/api/alpha/schema.py:1745` declares
`fields.List(fields.Integer())`, so `"5"` arrives as `5` and `"abc"` is
rejected before the handler runs. Every one of sub-project 34's 18 poll call
sites passed ints, because ORM-seeded ids are ints — so the whole file
exercised the API's type on both arms and a genuine defect in a string-only
path (`app/shared/post.py:1165`) was invisible to coverage and found only by
mutation. Worse, a docstring in the file had already noticed the gap and
concluded "the cast is exercised either way", which is **true of execution
and false of observation**: the line was covered and nothing downstream could
tell whether it had run. **The check is cheap and belongs in every round: for
each entry point, read the route that BUILDS the argument, and confirm at
least one test supplies that type.**

**213. NOT EVERY `app/shared/` FUNCTION RENDERS A TEMPLATE, AND THE
`status_code` RULE IS NOT A PROPERTY OF THE SRC_WEB ARM.** Sub-project 34's
rule — recorded as **D393**(c) in
`docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` and in
`tests/test_shared_post_interactions.py`'s module docstring — is that a WEB
arm returns a Flask `Response` (because `app/shared/post.py:23`'s
`render_template` is `app.utils.render_template`, which wraps in
`make_response` at `app/utils.py:86`) and must be asserted with
`result.status_code == 200`, never `isinstance(result, str)`. That holds for
functions that return a rendered
template, which in `app/shared/post.py` means `vote_for_post` and
`subscribe_post`. **None of the six moderator verbs does.** `lock_post:927`,
`move_post:963`, `sticky_post:994`, `hide_post:1026`, `mod_remove_post:1045`
and `mod_restore_post:1088` return `user.id, post` on SRC_API, and `None` on
the SRC_WEB arms of all but `sticky_post` and `hide_post`. A test written to
the `status_code` rule against one of these **cannot fail for the right
reason** — it fails on an `AttributeError` against `None` or a tuple, which
looks like a harness problem and is not. The general check before writing the
first assertion in a new group: **read what the function actually returns on
each arm, rather than inheriting the previous group's oracle.** Where the
return shape is the same on the permitted and the refused path — which is true
of all six here — **the return value distinguishes nothing and the oracle must
be a side effect**: the post's field, the `ModLog` row count, and where
federation is in question, whether `task_selector` fired.

**214. `add_to_modlog` COMMITS, AND RAISES ON AN ACTION OUTSIDE ITS MAP.**
`app/utils.py:3564-3582`. Two consequences for any test over a function that
calls it. (a) **It ends with `db.session.commit()` at `:3582`**, so a test
asserting that *nothing* was written must query the database rather than
inspect the session — and a test asserting something *was* written sees it
committed even if the calling function later rolls back. (b) **`:3568-3569`
raises `Exception('Invalid action: ' + action)` for any action not in
`ModLog.action_map`.** That matters most during a mutation pass: swapping an
action string to something unmapped produces a crash on the `add_to_modlog`
call itself, not an observation by a test, so the row is a **crash kill** and
must be adjudicated as one (fact 215's question applies). Verify the swapped
string is in `ModLog.action_map` before scoring the mutation. `add_to_modlog`
also decides `action_type` by reading `actor.is_instance_admin() or
actor.is_admin() or actor.is_staff()` at `:3570`, so fact 216's id-1 trap
changes the `type` column of every ModLog row an id-1 actor writes.

**215. A CRASH KILL IS NOT A KILL. THE QUESTION IS WHETHER A VIABLE
NON-CRASHING VARIANT OF THE SAME FAULT SURVIVES.** When a mutant dies on a
`TypeError`, `AttributeError`, `KeyError` or import error rather than an
assertion, the row is evidence only if the crash is the test observing the
thing the mutation changed. Sub-project 35 found one row out of fifty-nine
where it was not: a `task_selector` key swap at `app/shared/post.py:985` died
on `TypeError: delete_post() got an unexpected keyword argument
'old_community_id'` — a Celery signature mismatch, unrelated to any test.
**A false kill is worse than a survivor**, because a survivor is a finding
someone acts on while a false kill enters the table as evidence the site is
covered. Re-mutated with a signature-compatible substitute, the same site
survived all 64 tests and was hiding a real defect. **The operator itself can
be structurally void:** `task_selector` dispatches through a hard
`tasks[task_key]` subscript (`app/shared/tasks/__init__.py:66,68`) and no task
in the map accepts `**kwargs`, so at a call site whose kwargs only one task
accepts, *every* key-only swap crashes and the site is unmeasurable by that
operator. **Two rules follow.** (i) After any crash kill, ask the question in
this fact's title; if the answer is no, record VOID rather than K or S. (ii)
**`-x` reports only the FIRST failing arm**, so a crash kill can mask an
assertion kill on the other arm of the same fork — re-run the second arm's
tests explicitly before concluding anything about it. The summary table cannot
show the difference.

**216. THE FIRST `User` A TEST SEEDS BECOMES id 1, AND `User.is_admin()`
RETURNS TRUE FOR id 1 UNCONDITIONALLY.** `app/models.py:1259-1261` opens
`if self.id == 1: return True`, before any role lookup. `tests/conftest.py`
resets sequences after every test and `seed_post_context`
(`tests/factories.py:1187`) creates `author` before `voter`, so **`s.author`
is a site admin in every seeded context and `s.voter` (id 2) is not.** This is
fact 191's `Instance`-id-1 trap with teeth: a permission test that picks
`s.author` as its "unprivileged actor" passes the gate through the
`is_admin_or_staff()` disjunct, witnesses nothing, and looks exactly like a
test that works. **Use `s.voter` for every unprivileged actor.** Verify by
fixture, not by docstring — a reviewer checking this claim must read which
rows the helper actually writes, because the seeding order is the only thing
establishing the id. Two places the shortcut leaks beyond permission gates:
`add_to_modlog:3570` types an id-1 actor's rows as `admin`, and
`User.is_admin_or_staff()` (`app/models.py:1274`) inherits it.

**AMENDMENT (sub-project 36).** This fact is correct about `User.is_admin()`
and ONLY about `User.is_admin()` — it does not generalise to "id 1 is an
admin" as a fact about this codebase, because two other predicates answer the
same question differently. A prior round's spec, plan and this file all
carried the unscoped claim forward from here, and it survived until a
reviewer traced it back to this exact paragraph (`task-1-review.md`). Do not
add a fourth, contradicting fact for the correction — it belongs here, next
to the claim it qualifies. All three predicates, for a User id 1 carrying no
`user_role` row (every seeded user in this suite, via `make_user`,
`tests/factories.py:41-67`, which never inserts one):

| Predicate | Mechanism | Answer for role-less id 1 |
|---|---|---|
| `User.is_admin()` (`app/models.py:1259-1261`) | `if self.id == 1: return True`, checked before `self.roles` is ever read | admin |
| `g.admin_ids` (`app/request_hooks.py:97-107`, the query at `:100-106`) | a SQL `UNION`: `SELECT u.id FROM "user" u WHERE u.id = 1` unioned with a second `SELECT` joined to `user_role` for `ROLE_ADMIN` — the id-1 branch is its own `SELECT` with no join | admin |
| `Site.admins()` (`app/models.py:3995-4000`, the join+filter at `:3999-4000`) | `.filter_by(deleted=False, banned=False).join(user_role).filter(or_(user_role.c.role_id == ROLE_ADMIN, User.id == 1))` — an INNER join, so a user with zero `user_role` rows is dropped before the `User.id == 1` disjunct is ever reached | NOT admin |

**BUT THAT TABLE'S THIRD ROW IS THE HARNESS'S ANSWER, NOT PRODUCTION'S.**
`Site.admins()` has TWO branches. `app/models.py:3996-3997` returns
`User.id.in_(tuple(g.admin_ids))` whenever `g.admin_ids` is set, and only
falls through to the INNER-join query when it is not.
`app/request_hooks.py:79` is an `@app.before_request` (registered at
`app/__init__.py:366`) that sets `g.admin_ids` on every request except
`/inbox` and paths under `/static/` (`:94`). So on a normal request path all
three predicates AGREE and a role-less id 1 IS an admin.

The join branch is reached only where `g.admin_ids` is unset: outside any
request context — Celery, CLI, **and this harness**, because `web_ctx` uses
`test_request_context`, which pushes a request context but never dispatches,
so `before_request` never fires — or during `/inbox` handling, which the hook
skips. That is why `Site.admins()` returns `[]` here and would not on a live
site.

**What this means for writing tests, which is unchanged:** a test asserting
an admin notification through `report_post`'s `Site.admins()` path (`:893`)
must still seed a real `user_role`/`Role('Admin')` row, because the harness
always takes the join branch. What changed is the reason — it is an artifact
of running without a dispatched request, not a defect the test is pinning.
Do not cite this as a production bug; the campaign register's D442 carries the
corrected version and names the one unverified case (callers reached from
`/inbox`, the path the hook skips). **A claim about "admin" that does not name
its predicate is not a claim** — and, it turns out, naming the predicate is
not enough either when the predicate itself branches on context.

**217. `grant_permission` CANNOT MAKE A SITE ADMIN — `is_admin()` AND
`is_staff()` CHECK ROLE *NAMES*, NOT PERMISSIONS.** `User.is_admin()`
(`app/models.py:1259-1265`) and `User.is_staff()` (`:1268-1272`) iterate
`self.roles` and compare `role.name` to the literals `'Admin'` and `'Staff'`.
**A role carrying every permission in the system but named anything else is
not an admin to any gate in `app/shared/`.** So
`grant_permission(user, 'change instance settings')`
(`tests/factories.py:365`) is the wrong tool for any test that needs to
witness an admin disjunct — and the test **passes anyway**, exercising the
unprivileged path while claiming to prove the opposite, because these gates
refuse silently (see `app/shared/post.py:941`, `:971`, `:1003`). The working
form is a `Role` named literally `'Admin'` plus a `user_role` row;
`tests/test_shared_post_moderation.py:93`'s `make_site_admin(user)` is the
reference implementation. Two properties make the raw insert safe: `User.roles`
is `lazy='dynamic'`, so the row is visible immediately without a refresh, and
the test config's NullCache neutralises `is_admin()`'s
`@cache.memoize(timeout=30)`. Note the three privilege helpers are genuinely
disjoint and that is what makes them usable as single-disjunct witnesses:
`seed_moderator` writes only `CommunityMember`, `make_instance_admin` only
`InstanceRole`, `make_site_admin` only `Role`/`user_role`. Watch which
`is_instance_admin` a gate calls — `Community.is_instance_admin(user)`
(`app/models.py:769-776`) keys on the **COMMUNITY's** `instance_id`, while
`User.is_instance_admin()` (`:1277-1282`) keys on the **USER's**, and only the
first appears in `app/shared/post.py`'s gates.

**218. TWO IDENTICALLY-NAMED TESTS IN ONE FILE: PYTHON REBINDS, THE EARLIER
ONE STOPS RUNNING, AND NOTHING REPORTS IT.** A second `def test_foo` at module
scope silently replaces the first. **No error, no warning, no collection
failure** — the earlier test simply ceases to exist. Every other check this
repo runs is blind to it: coverage does not notice (the survivor covers
similar lines), a citation sweep does not notice (nothing moved), and the
collection count drops by exactly one, which is **indistinguishable from
having miscounted** — and predicted counts in a plan are wrong often enough
that the drop reads as the plan being wrong rather than a test disappearing.
The risk is highest where it looks most like good practice: a naming
convention that makes tests read as pairs across sibling functions makes
`test_the_web_arm_returns_none` the natural name for the same assertion about
four different functions. **The check is one line and belongs in every round's
final gate, over every test file the round touched:**

    grep -oE "^def (test_[a-z_]+)" tests/test_your_file.py | sort | uniq -d

Empty output is the pass. Cross-check it against pytest's own collection
count and the file's `grep -c "^def test_"`; the three agreeing is the
positive signal.

**219. `Community.moderators()` EXCLUDES BANNED MEMBERS, AND EVERY GATE IN
`app/shared/post.py` READS THROUGH IT.** `app/models.py:716-722` filters
`CommunityMember` on `is_owner OR is_moderator` **and then**
`.filter(CommunityMember.is_banned == False)`. `Community.is_moderator()`
(`:736-740`) and `Community.is_owner()` (`:742-747`) both iterate
`self.moderators()`, so **a banned moderator is not a moderator anywhere in
this module** — a test seeding `CommunityMember(is_moderator=True,
is_banned=True)` is seeding an unprivileged actor, which is a useful witness
for the false arm of a gate and a silent failure if you meant otherwise. Two
practical notes. `moderators()` carries `@cache.memoize(timeout=300)`, which
the test config's NullCache neutralises, so a membership row written
mid-test is visible to the next gate call. And when citing this: the adjacent
range `:736-740` is `is_moderator()`, not `moderators()` — a plan in
sub-project 35 cited it wrongly and the error was caught only because the
implementer re-derived the range instead of copying it.

**220. `delete_post` IS CALLED FROM CELERY TASKS WITH NO REQUEST CONTEXT, AND
THE `user_id = 1` FALLBACK IS LIVE THERE.** Two `@celery.task`s call
`delete_post(..., SRC_WEB, None)` from inside `with patch_db_session(session):`,
with no Flask request context pushed: `remove_old_community_content`
(`app/shared/tasks/maintenance.py:134-150`, the call at `:150`) and
`remove_old_bot_content` (`:161-185`, the call at `:185`). `delete_post:760`'s
`if current_user:` is False in that situation — `current_user` (a
`flask_login` `LocalProxy`) resolves falsy with no request context — so
`:763`'s `user_id = 1  # for remove_old_community_content()` fallback fires,
confirmed directly by calling `delete_post(post_id, False, SRC_WEB, None)`
with no context pushed and reading back `deleted_by == 1`. Not dead code, not
a defensive-only path: every automated content-retention deletion in this
codebase attributes itself to user id 1.

**221. `report_post`'s SRC_API ARM NEEDS NO REQUEST CONTEXT, INCLUDING
`force_locale`.** `:877`'s `with force_locale(get_recipient_language(moderator.id)):`
and `:878`'s `gettext(...)` both run cleanly with no request context pushed —
confirmed by calling `report_post` directly (SRC_API, with a `bearer()` auth
object) outside any `app.test_request_context()`/`web_ctx`. `get_ip_address`
(inside `authorise_api_user`'s call chain) swallows the `RuntimeError` a
missing request context would otherwise raise, and nothing downstream of it
in this arm needs one either. Delete/restore's SRC_API arms need no context
for the same reason — `authorise_api_user` is the only thing in either arm
that could need one, and it doesn't.

**222. THE WEB ARM'S FORM STAND-IN SHAPE — BUILD IT AS A NESTED
`SimpleNamespace`, NOT A WTForms INSTANCE.** `report_post`'s WEB arm takes a
WTForms-like object, not a dict: `:836-839` reads
`input.reasons_to_string(input.reasons.data)`, `input.description.data`,
`input.report_remote.data`. A minimal stand-in needs no WTForms import —
`SimpleNamespace(reasons=SimpleNamespace(data=['5']),
description=SimpleNamespace(data='x'), report_remote=SimpleNamespace(data=False),
reasons_to_string=lambda data: 'Minor abuse or sexualization')` satisfies every
attribute access the function makes, including a *method* (`reasons_to_string`)
as a plain lambda attribute — Python does not distinguish a bound method from
a callable attribute at the call site. Groups D and E's WEB arms (`make_post`,
`edit_post`) take the same kind of WTForms object and can build stand-ins the
same way; read the specific field names each function actually accesses
before assuming this exact shape transfers unchanged.

**223. `Community.is_local()` IS TRUE UNDER `seed_post_context` BECAUSE
`make_community` NEVER SETS `ap_id`.** `Community.is_local()`
(`app/models.py:796`) is `self.ap_id is None or
self.profile_id().startswith(SERVER_URL)` — an OR, so either disjunct alone
keeps a community local. `make_community` (`tests/factories.py:124-144`) sets
`ap_profile_id`/`ap_public_url` but never `ap_id`, so the first disjunct is
always true for a `seed_post_context` community regardless of host. This is
the fact that decides whether `report_post:841`'s `post.community.is_local()`
conjunct and `:904`'s `if not post.community.is_local():` guard are reachable
in a test at all, and THE TWO WANT OPPOSITE THINGS, so read this slowly.
`:841` is `if post.community.is_local() and post.community.un_moderated:` —
its TRUE arm needs the community to STAY LOCAL, which is the default, so
override nothing. `:904` is `if not post.community.is_local():` — its TRUE arm
needs the community to be NON-local, which requires overriding BOTH `ap_id`
AND `ap_profile_id` to a remote domain. The second disjunct alone
(`ap_profile_id` pointed at a real remote host) is not enough, because
`ap_id is None` already satisfies the first. An earlier version of this fact
said the True arm of `:841` needed both overridden; that is its FALSE arm, and
following it would produce a test that takes the opposite arm and passes. Assert
`community.is_local() is False` before calling the function under test so a
fixture that silently fails to clear both fields announces itself instead of
exercising the wrong arm.

**224. `make_post` NEEDS `with_keys=True` ON ITS LOCAL AUTHOR, AND WITHOUT IT
THE FAILURE MESSAGE POINTS AWAY FROM THE CAUSE.** `can_create_post`
(`app/utils.py:2504-2506`) opens `if user.is_local(): if user.verified is
False or user.private_key is None: return False` -- a LOCAL user is refused
if `private_key` is `None`, which is exactly what `make_user(..., local=True)`
leaves it unless `with_keys=True` is passed (`tests/factories.py:41-51`).
Every `make_post` test needs a local author that PASSES `can_create_post`, so
this is load-bearing, not incidental. Cost: keypair generation via
`RsaKeys.generate_keypair()` takes roughly a second
(`tests/factories.py:44-45`'s own docstring), which is why
`tests/test_shared_post_make.py`'s `seed_make_context` mints exactly ONE keyed
author and every test in the file reuses it rather than seeding a fresh one
per test. The error it prevents is actively misleading: without
`with_keys=True`, every call dies at `make_post:187` with `Exception('You are
not permitted to make posts in this community')` -- a message naming a
PERMISSION problem, with nothing in it hinting that the real cause is a
missing RSA key rather than an actual authorisation gap. (`with_keys=True`
also covers fact 224's OWN prerequisite for a *sending* actor per
`make_user`'s docstring -- `HttpSignature.signed_request` calls `.encode()` on
the private key, so a keyless sender separately dies at signing with
`'NoneType' object has no attribute 'encode'`; `make_post`'s failure mode is
earlier and different, at the permission gate rather than at signing.)

**225. `g.site` MUST BE SUPPLIED EXPLICITLY FOR `make_post`, AND
`can_create_post`'S OWN `g.site` FALLBACK DOES NOT HELP A LOCAL AUTHOR.**
`make_post:219` is `community.last_active = g.site.last_active = utcnow()`,
executed unconditionally partway through the function -- with no `g.site` set,
this raises `AttributeError` on a bare `g` the first time any harness call
reaches it. `can_create_post` (app/utils.py) has its own `g.site` fallback at
`:2508-2509` (`if not hasattr(g, 'site'): g.site = db.session.query(Site).get(1)`),
but it sits on the REMOTE-user `else` branch (`:2507`) -- a LOCAL author (fact
224's keyed user) takes the `if user.is_local():` branch two lines above and
never reaches it. In production `g.site` is populated by the `before_request`
hook (`app/request_hooks.py:79`, registered at `app/__init__.py:366`, doing
the actual write at `:94-95` for every request except `/inbox` and
`/static/`); this harness dispatches no real request for either arm --
`web_ctx` only pushes a Flask REQUEST context on top of the `app` fixture's
already-open APP context, and the SRC_API arm has no request context at all --
so `before_request` never runs and `g.site` must be set by the test itself.
`tests/test_shared_post_make.py`'s `seed_make_context` does this directly
(`g.site = site`), and because `g` is bound to the app context rather than the
request context, setting it there ONCE is visible both to later `web_ctx`
blocks (which push/pop only a request context on top of the same app context)
and to the SRC_API arm (which runs inside the same per-test app context with
no request context needed at all) -- no per-arm mechanism required.

**226. `notify_about_post` DISPATCHES A CELERY TASK THAT RUNS INLINE UNDER
THIS HARNESS'S EAGER CONFIG, AND IT WRITES REAL `Notification` ROWS THROUGH A
SEPARATE DB SESSION.** `notify_about_post` (`app/activitypub/util.py:2796`)
calls `notify_about_post_task.delay(post.id)` (or invokes it directly under
`current_app.debug`); `tests/conftest.py:106` sets `task_always_eager=True`,
so `.delay(...)` runs the task's body inline and synchronously rather than
enqueueing it for a worker that nothing in this harness drains. The task body
opens its OWN session (`get_task_session()`/`patch_db_session`), re-queries
the `Post`, its author and its community by id, and writes real `Notification`
rows for every matching subscriber it finds -- it is not a no-op and not
mocked by default. `make_post:238` guards the call on `post.status ==
POST_STATUS_PUBLISHED`, the column default, so the ordinary path through
`make_post` reaches it every time. Two consequences for a test in this file:
(a) a test that does not care what happens downstream should monkeypatch
`post_module.notify_about_post` itself (the `stub_notify` fixture in
`tests/test_shared_post_make.py` does exactly this, PER TEST rather than as an
autouse fixture or a module-level patch, because a global suppression would
hide the call from a test that specifically needs to observe it); (b) a test
that DOES need to observe it should replace `post_module.notify_about_post`
with a recorder rather than let the real Celery body run, since the real
body's own side effects (subscriber notifications) are out of scope for
`make_post`'s own tests.

**227. CROSS-MODULE TEST HELPER IMPORTS ARE ESTABLISHED PRECEDENT HERE, AND
CARRY A RULE OF THREE.** `tests/test_shared_post_make.py` imports `_api_input`,
`_web_form`, `_Field` and `_OMIT` directly from `tests.test_shared_post_edit`
rather than duplicating them, because `make_post:231` passes its own `input`
straight through to `edit_post`, so the two functions' tests need the SAME
input shapes (`_web_form` already carries the `link_url`/`video_url` fields
Group D's url-arm tests need, `tests/test_shared_post_edit.py:152`). This is
not a new pattern: `tests/test_ap_collections.py:6` already imports
`seed_actors` from `tests/test_actor_profiles.py`, and `tests/__init__.py`
makes `tests` a package so such imports resolve at all. The imported names are
underscore-prefixed, which ordinarily signals module-private -- importing them
anyway is a deliberate choice to avoid a second, drifting copy of the same
fixture shape, not an oversight. **Rule of three**: a THIRD consumer of the
same helper justifies promoting it out of whichever test file first defined it
and into `tests/factories.py`; two consumers importing from one original
module does not yet meet that bar.

**228. `edit_post` DUPLICATES FOUR OF `make_post`'S BLOCKS, SO A TEST
ASSERTING ON THE FINISHED POST MAY BE WITNESSING `edit_post`, NOT THE FUNCTION
IT NAMES.** `make_post:231` always calls `edit_post(..., from_scratch=True)`,
and `edit_post` independently re-derives and re-checks four of the same
things `make_post` itself just computed: the post's title (`make_post:168`/
`:173` vs `edit_post:254`/`:318`, with `edit_post:395` unconditionally
overwriting `post.title` afterward regardless of which value `make_post`
produced), its url (`make_post:169`/`:174-179` vs `edit_post:256`/`:321-327`
-- `make_post`'s own `url` local never reaches the `Post` row at all, since
`make_post:206-207`'s constructor call takes only `title` and `language_id`),
the domain-ban check (`make_post:190-195` vs `edit_post:565-569`, the guarded
raise itself byte-identical) and the upload-extension check
(`make_post:197-204` vs `edit_post:461-468`, byte-identical including the
comment). Practically: a test that asserts `post.url == '...'` or reads
`post.title` after a `make_post` call is reading a value `edit_post` produced,
not one `make_post`'s own arms are responsible for -- a regression in
`make_post`'s own url/title derivation can pass such a test undetected, because
`edit_post`'s copy silently reproduces the same-looking result from the same
underlying form/API input. Isolate what `make_post`'s OWN arms did by either
recording whether `edit_post` was entered at all (a `post_module.edit_post`
recorder that delegates to the original) or by stubbing `edit_post` to
identity and reading the Post fields `make_post` itself sets directly
(`up_votes`, `score`, community/user counters, `PostVote`) -- never by reading
`post.title`/`post.url` as if `make_post` were the only writer.

**229. `edit_post`'S FILE-UPLOAD BLOCK (`:461-563`) NEEDED THREE HARNESS
FACTS NOTHING EARLIER IN THE CAMPAIGN HAD NEEDED, PLUS AN `http_mock`
REQUIREMENT WITH NO GET ROUTE, AN AVIF TRAP, AND A SHARED RAISE MESSAGE.**
This is the first block driving a real file through a real image pipeline
rather than pure control flow over database rows, and `tests/test_shared_
post_upload.py` is a new file built around five things worth carrying
forward:

1. **`chdir_upload`.** `:475` builds its directory as `'app/static/media/
   posts/' + ...` -- RELATIVE to the process's working directory, which in
   the container is the bind-mounted repo root. Left unredirected, every
   test would write a real file into the source tree under a random
   `gibberish(15)` name (`:470`); `.gitignore:162-163` hides it from `git
   status`, but nothing removes it, so the files accumulate silently.
   `chdir_upload` (`monkeypatch.chdir(tmp_path)`) redirects every relative
   write into pytest's own per-test `tmp_path`, cleaned up automatically.
   Verified end to end (a file found under
   `tmp_path/app/static/media/posts/XX/YY/<gibberish>.png`) and verified
   negatively (the repository's own `app/static/media/` file and directory
   counts identical before and after every task's run in this round).

2. **`make_upload`'s genuine-versus-renamed split.** The helper produces
   GENUINE content for `fmt` in `PNG`, `GIF`, `JPEG` (real
   `Image.new(...).save(buf, format=fmt)` bytes) and for `fmt='SVG'` (real
   SVG/XML text, since Pillow has no SVG writer and SVG is not a raster
   format). It produces RENAMED RASTER BYTES ONLY for `HEIC`, `AVIF`, or any
   video-like filename (e.g. `.mp4`): real PNG/GIF/JPEG bytes wearing a
   different extension, sufficient for branches gated on the FILENAME string
   alone (`:491`'s `.heic` check, `:493`'s `.avif` check, `:513`'s
   `is_video_url(final_place)`) but insufficient for a real HEIC/AVIF decode,
   a genuine format-mismatch at `:515` for those formats, or any real
   video-content behaviour. A test needing that needs a different helper.

3. **The `http_mock` `url__regex` requirement -- and the GET route rule,
   WHICH HAS TWO HALVES. CORRECTED 2026-09-12 (sub-project 39): the
   original text below stated only the first half as an absolute, and a
   round that reads only that form cannot reach `:619`, `:630` or `:641`
   at all.** Calling `edit_post` with `uploaded_file=` set reaches `:601`'s
   `is_image_url(url)` on the NEWLY BUILT url (`:535`, which embeds
   `gibberish(15)`) regardless of how narrow a test's intent is -- this is
   downstream of the upload block entirely, in the function's shared tail.
   A plain `http_mock.head('https://...')` (exact URL) cannot be used there,
   since the filename is random; every full `edit_post()` call in
   `tests/test_shared_post_upload.py` needs `http_mock.head(url__regex=r'.*')`
   instead. **What decides the GET route is which of `:601`/`:619`/`:630`/
   `:641` the HEAD's content type selects.** Those four are one mutually
   exclusive `if`/`elif`/`elif`/`else` chain, and only `:601` skips
   `opengraph_parse` -- which is the call that issues the GET.

   - **HEAD says an IMAGE type -> NO GET route.** `Content-Type: image/png`
     makes `is_image_url` return `True` (`app/utils.py:271-273` computes
     `'.png' in common_image_extensions`), `:601`'s arm is taken, nothing
     calls `opengraph_parse`, and a registered-but-unreached GET route fails
     `http_mock`'s `assert_all_called=True` at teardown. This serves the
     **upload arm** and is the convention throughout
     `tests/test_shared_post_upload.py`.
   - **HEAD says a NON-image type -> A GET ROUTE IS REQUIRED.** The inverse,
     and it is what the `:619` (pixelfed), `:630` (loops.video) and `:641`
     (generic opengraph) arms need: `Content-Type: text/html` makes
     `is_image_url` return `False` (`'.html' in common_image_extensions` is
     False -- it does NOT fall through to extension sniffing), `:601` is
     skipped, the chain reaches the url arms, `opengraph_parse` runs and
     issues a GET, and omitting the route leaves respx with an unmatched
     request. `video/mp4` behaves the same way. This serves the **url arms**
     and is the convention throughout `tests/test_shared_post_url.py`.
     Both directions were measured under the real `http_mock` router rather
     than reasoned about: `text/html -> False`, `image/png -> True`.

   The two conventions coexist in the same suite and are chosen PER TEST by
   what the test needs to reach. Neither is "the" rule.

4. **The AVIF trap.** This container's Pillow (12.3.0) registers AVIF
   natively -- `features.check('avif')` is `True`, and an AVIF save
   SUCCEEDS even with `pillow_avif` absent from `sys.modules` -- so a
   successful-save assertion proves nothing about whether `:494`'s
   (filename-gated) or `:511`'s (config-gated, `MEDIA_IMAGE_FORMAT ==
   'AVIF'`) `import pillow_avif` actually ran. Both had to be witnessed
   through `sys.modules` MEMBERSHIP instead (popped before, asserted
   present/absent after, restored in `finally`). A witness for one of the
   two imports must also PIN the other import's gate to its non-triggering
   value (e.g. `assert app.config['MEDIA_IMAGE_FORMAT'] != 'AVIF'` for a
   `:494` test) -- otherwise the same `sys.modules` key being written by
   either import makes the assertion ambiguous between them.

5. **`:468` and `:533` raise byte-identical messages.** Both are
   `raise Exception('filetype not allowed')` -- `:468` fires BEFORE `:487`'s
   save (no file exists on disk afterward), `:533` fires AFTER it, following
   `:515`'s failed post-decode format check (`:487`'s original save survives
   untouched -- `:531`, the only thing that would overwrite it, is the
   mutually exclusive `if` arm of the same check, so it never ran before
   `:533`'s `else` fired). A test of either raise must assert whether the
   uploaded file EXISTS afterward, not just match the exception message --
   otherwise a mutant that made the wrong one of the two fire would still
   pass.

**230. `edit_post`'S URL HALF (`:565-703`) AND ITS PERMISSION/TEARDOWN
HEAD (`:387-460`): EIGHT FACTS THAT DECIDE WHETHER A TEST THERE WITNESSES
ANYTHING.** These closed the module (sub-project 39,
`tests/test_shared_post_url.py`); every one was measured, not reasoned about.

1. **The HEAD content type selects the arm, and therefore whether a GET
   route is required.** This is the INVERSE of the convention
   `tests/test_shared_post_upload.py` uses, and it is a fact in its own
   right: to reach `:619`/`:630`/`:641` at all, `is_image_url(url)` must be
   FALSE, which means the HEAD must report a NON-image content type
   (`text/html`, `video/mp4`), and `opengraph_parse` then issues a GET that
   the router must have a route for. An `image/png` HEAD takes `:601` and
   forbids a GET route. Both directions measured: `text/html -> False`,
   `image/png -> True`. See fact 229 point 3 for the full statement of both
   halves and which test file follows which.

2. **`fixup_url` returns `(url, url)` for an ordinary url, so `post.url`
   CANNOT distinguish `:640` from `:652`.** `app/utils.py:3311-3312` is
   `thumbnail_url = embed_url = url`, and the two rewrite paths below it fire
   only for peertube `/w/` urls and for `youtube_domains` hosts; everything
   else falls through with `embed_url is url`. `:640` (`post.url = url`, the
   loops.video arm) and `:652` (`post.url = embed_url`, the generic
   opengraph arm) therefore write the SAME STRING for any ordinary test url.
   A test asserting `post.url == '...'` witnesses neither line. What
   discriminates the two arms is `post.type`, the `File` rows created, and
   the `og:` fetches issued -- not the url.

3. **`url_to_thumbnail_file` writes to a WORKING-DIRECTORY-RELATIVE path, so
   `chdir_upload` is needed by tests that upload nothing.** `app/utils.py:3065`
   builds `directory = 'app/static/media/posts/' + ...` and `:3069` opens it
   for writing -- relative to the process's cwd, the bind-mounted repo root
   in the container. This is reached from `edit_post:646`, inside the GENERIC
   OPENGRAPH arm, on a path where no `uploaded_file` was ever passed. The
   `chdir_upload` fixture (fact 229 point 1) was written for the upload block
   and is required here too; without it a thumbnail-download test leaves a
   real `gibberish(15)` file in the source tree that `.gitignore` hides.

4. **`S3_PUBLIC_URL` defaults to `''`, which makes `:446`'s third conjunct
   trivially true.** `config.py:108` is
   `S3_PUBLIC_URL = os.environ.get('S3_PUBLIC_URL') or ''`, so
   `post.url.startswith(f'https://{...S3_PUBLIC_URL...}')` at `:446` reduces
   to `post.url.startswith('https://')` under the default -- true of every
   https url a test seeds. The conjunct IS falsifiable (an `http://` url
   falsifies it), but a test that only sets `store_files_in_s3()` and a
   `POST_TYPE_VIDEO` post is not witnessing it. Pin `S3_PUBLIC_URL` to a real
   host if the third conjunct is the thing under test.

5. **`scheduled_for` is UNREACHABLE from the API branch.** `:281` is a hard
   `scheduled_for = None` in the `SRC_API` arm; only the form arm (`:337`,
   `input.scheduled_for.data`) can supply a value. So `:399`'s write and
   `:413`'s true arm can be witnessed only through `SRC_WEB`. An API test
   that "sets a scheduled_for" is setting a key nothing reads.

6. **`mime_type_using_head`'s `@cache.memoize` is inert ONLY because the test
   config disables caching.** `app/utils.py:332` decorates it with
   `@cache.memoize(timeout=10)`; `tests/conftest.py:68` sets
   `CACHE_TYPE = 'NullCache'` where `config.py:38` would otherwise give
   `FileSystemCache`. Two tests in the same process may therefore feed the
   same url different HEAD responses and each get its own. That is a property
   of the harness, not of the function -- do not carry the assumption into
   any runner that does not set `NullCache`. The same applies to
   `User.is_admin()`'s `@cache.memoize(timeout=30)` (fact 216) and to
   `Community.moderators()`'s `@cache.memoize(timeout=300)`
   (`app/models.py:715`).

7. **`:387`'s SECOND DISJUNCT CANNOT BE WITNESSED ALONE -- it is subsumed by
   the first.** `:387` is
   `if post.community.is_moderator(user) or post.community.is_owner(user) or user.is_admin():`.
   `Community.moderators()` (`app/models.py:716-722`) admits a row on
   `is_owner OR is_moderator`; `Community.is_moderator()` (`:736-740`) tests
   only `moderator.user_id == user.id` over that same list, while
   `is_owner()` (`:742-747` -- the whole method; `:743-745` is the
   `current_user` arm and `:747` is the `user=` arm this call takes) tests
   `user_id` AND `is_owner`. So any row that
   makes `is_owner(user)` true makes `is_moderator(user)` true first, and the
   first disjunct short-circuits. No fixture can make disjunct 2 decide the
   compound. A test claiming to witness "the owner arm" of `:387` is
   witnessing the moderator arm. Registered as D479.

8. **`app/shared/post.py`'s FLOOR OF 100 IS PARTLY HELD BY A
   `# pragma: no branch`, AND DELETING IT LOOKS LIKE A TEST REGRESSION.**
   `app/shared/post.py:673` (`if 'choices' in poll_data:`) carries
   `# pragma: no branch -- see proof in test_shared_post_url.py`. Its false
   arm is UNREACHABLE -- `poll_data` is assigned at exactly `:300`, `:314`,
   `:349` and `:357`, `:306` and `:352` always set a `'choices'` key, `:315`
   is a bare `else:` so those arms are exhaustive, and `:669`'s `and
   poll_data` stops every falsy value -- so no test can close the arc and the
   pragma is the only way to reach 100. **If someone removes that comment,
   `coverage_floors.ini`'s entry of 100 fails and it will present as a test
   regression.** It is not one: it is a proof expiring. The proof is an
   ENUMERATION, so a FIFTH assignment to `poll_data`, or a `'choices'` key
   dropped from either dict literal, genuinely invalidates it and the pragma
   must then go. Registered as D480; the enumeration is repeated beside
   `TestPollAndEventTail` in `tests/test_shared_post_url.py`. Note that
   `.coveragerc` sets no `partial_branches`, so this pragma is the module's
   only such exclusion.

**231. `app/shared/reply.py`'S HARNESS: THREE SUB-PROJECT 34 FACTS TRANSFER
UNCHANGED, AND FOUR THINGS DO NOT.** Sub-project 40 took Groups A and C
(`vote_for_reply`, `bookmark_reply`, `remove_bookmark_reply`,
`subscribe_reply`, `extra_rate_limit_check`, `delete_reply`, `restore_reply`)
in `tests/test_shared_reply_interactions.py`. `reply.py` is `post.py`'s twin,
so most of the harness is inherited; what follows is the difference, measured
rather than reasoned about.

1. **What transfers from `tests/test_shared_post_interactions.py` unchanged.**
   (a) **No `user=` escape hatch.** `edit_reply` takes one; no Group A
   function does — `vote_for_reply:21`/`:28`, `bookmark_reply:58`,
   `remove_bookmark_reply:76` and `subscribe_reply:95` read `current_user` or
   call `authorise_api_user` with no way around it, so every test supplies a
   real user. (b) **The `SRC_API` arm needs no request context.**
   `get_ip_address` (`app/__init__.py:68-78`) wraps its `request` read in
   `try/except RuntimeError` — its own comment at `:76` names the case, "no
   application or request context (e.g. a CLI command)" — and returns `''`, so
   `user_ip_banned()` sees a falsy IP and `vote_for_reply:30`'s guard lets a
   context-free call through. `web_ctx` is for the `SRC_WEB` arms, which need
   `flash` and rendering. (c) **What blocks an API test is an `ap_id`, not
   context.** `authorise_api_user` requires `ap_id is None`, `verified` true,
   `banned` false and `deleted` false — one compound condition at
   `app/utils.py:3628` — and `tests/factories.py:41`'s `make_user` defaults
   `local=False`, which mints a non-None `ap_id`. Pass `local=True`.

2. **`make_post_reply` DOES NOT SET `path`, so `delete_reply:256`'s
   `if reply.path:` and `restore_reply:282`'s are FALSE by default — and a
   ONE-element path does not merely take a different branch, it RAISES.**
   `tests/factories.py:463-471` sets exactly `user_id`, `post_id`,
   `community_id`, `instance_id`, `body`, `posted_at` and `deleted`; `path` is
   a nullable ARRAY column with no default, so a factory reply has
   `path is None` and the two raw-SQL arcs (`delete_reply:257-258`,
   `restore_reply:283-284`) are unreachable until a test seeds one. Seed the
   three-element shape `app/models.py:3058-3065` builds
   (`[0, parent_id, reply_id]`), not a one-element one: `reply.path[:-1]` is
   then `()`, psycopg2 renders `where id in ()` and raises
   `ProgrammingError: (psycopg2.errors.SyntaxError) syntax error at or near
   ")"`. That was measured in the container against the live test database
   with `delete_reply:257`'s own statement text — `(1, 2)` and `(1,)` run and
   return no rows, `()` raises. It is **not** a test-only curiosity: see
   register entry D497 for the production path that produces a one-element
   `path`. `child_count` (`app/models.py:2904`) is likewise unset by the
   factory but has a column default of 0 rather than None. Both column facts
   were re-derived by runtime introspection of the mapped table as well as by
   reading the `db.Column(...)` line, because reading the source line cannot
   see a later override of the mapped attribute.

3. **`subscribe_reply:94` JOINS `Post` and filters `deleted=False` on BOTH the
   reply and its parent post; `subscribe_post` had no join.** A seeded reply
   needs a live parent post, and the lookup ends in `.one()`, which raises
   `NoResultFound` rather than returning None or 404ing. A test that seeds a
   reply without a post, or that soft-deletes either row, gets an exception
   from the first line of the function rather than the behaviour it meant to
   drive.

4. **A `Site` row with id 1 is needed for TWO unrelated reasons, and they are
   not always separate.** The first is rendering: `subscribe_reply:131` and
   `vote_for_reply:51` are the module's only two `render_template` calls, and
   without the row `current_theme()` (`app/utils.py:3228-3240`) falls to
   `Site.query.get(1)` at `:3233` and dereferences None at `:3238` —
   `AttributeError: 'NoneType' object has no attribute 'default_theme'`, not a
   skipped lookup. The second is `can_downvote`, which reads
   `Site.query.get(1)` at `app/utils.py:2443` and dereferences it at `:2445`;
   that binds `SRC_API` tests that never render. They combine because a
   TEMPLATE can be what reaches `can_downvote`:
   `post/_comment_voting_buttons.html` line 10 calls it as a Jinja global
   (registered at `app/request_hooks.py:54`) and `vote_for_reply:51` passes no
   `can_downvote_here` to short-circuit it. `make_site()`'s own docstring
   (`tests/factories.py:353-358`) names only the `blocked_phrases` reason and
   understates this — register entry D504. Note also that
   `Site.default_theme` defaults to `''`, so `make_site()` alone gives a
   theme-less render; a themed render needs `default_theme` set explicitly.
   `delete_reply` and `restore_reply` need no `Site` row at all: neither
   renders and neither reaches `can_downvote`, executed for both halves of the
   pair rather than merely read.

5. **Fact 209's Redis vote-key rule applies to `reply.vote()`, and the key is
   NOT namespaced by entity type.** `PostReply.vote` (`app/models.py:3316-3414`)
   writes `votes_cast_{date.today()}_{user_id}` at `:3396` (`set`) and `:3398`
   (`incr`) — byte-identical to what `Post.vote` writes at `:2832`/`:2834`. So
   a leaked key from a post-voting test raises the quota for a reply-voting
   test whose user lands on the same id, across test files, and vice versa.
   Every test completing a real vote must clear the key in a `finally`;
   `tests/test_shared_reply_interactions.py` uses a local
   `_clear_votes_cast(user_id)` helper for exactly this, and its module
   docstring keeps a live count of which tests do and do not, because nothing
   else enforces the rule. Fact 210's prohibition transfers too, and it
   transfers on FOUR locks, not two: `awk 'NR>=3316 && NR<=3414 &&
   /redis_client.lock/' app/models.py` returns `:3319`, `:3345`, `:3387` and
   `:3408`, so `redis_double` cannot be used against any path that completes a
   vote. **The load-bearing one is `:3319`, and an earlier wording of this fact
   omitted it.** `:3319` is the OUTERMOST lock, `lock:post_reply:{self.id}`,
   taken on entry before any branch is chosen; the other three are per-user
   locks (`lock:user:{...}`) inside branches. Cite only the inner three and the
   prohibition reads as path-dependent — avoidable by driving a path that
   misses them. `:3319` is what makes it UNCONDITIONAL: every call to
   `PostReply.vote` takes a real redis lock, so there is no vote-completing
   path a fake redis can be substituted under.
   **For the quota boundary itself there is a cheaper method than fact 209's,
   and this round used it: move the BOUNDARY, not the COUNT.**
   `monkeypatch.setitem(app.config, 'VOTE_QUOTA', 0)` against a
   `votes_cast_today` of 0 puts a user exactly at the boundary with no Redis
   write and therefore no Redis teardown, and `-1` puts them over it. It is
   safe on the session-scoped `app` fixture (`tests/conftest.py:72-113`)
   because monkeypatch restores the key at teardown. Fact 209's key-writing
   method is still what you need when the COUNT is the thing under test.

**CORRECTED 2026-09-13 (sub-project 41, FINAL FIX WAVE; register D532). Every
`app/models.py` line number in items 2 and 5 above was stale by `903dab20` and
is re-derived here by content, not by adding an offset.** Item 2: path
construction `:3054-3059` -> **`:3058-3065`** (and the range was one line short
at both ends before the shift — it began on a `session.commit()` and stopped
before the `else:` that produces the `[0, reply.id]` shape it is cited FOR, so
this is Ruling 7's correction and the +5 in one edit); `child_count`
`:2899` -> **`:2904`**. Item 5: `PostReply.vote` `:3311-3402` -> **`:3316-3414`**
(note the two ends move by DIFFERENT amounts, +5 and +12, because the range
straddles D517's second hunk — no single offset maps it); the quota writes
`:3384`/`:3386` -> **`:3396`/`:3398`**; `Post.vote`'s twin writes
`:2827`/`:2829` -> **`:2832`/`:2834`**; the four locks `:3314`, `:3333`,
`:3375`, `:3396` -> **`:3319`, `:3345`, `:3387`, `:3408`**.
**THE LOCK LIST IS THE WARNING THIS WHOLE CLASS DESERVES:** old `:3396` was a
`redis_client.lock`, and HEAD `:3396` is the `redis_client.set` of the quota
key. A stale citation does not merely point at nothing — **it can point at real,
plausible, WRONG code in the same function**, and a reader checking it finds a
line that looks close enough to believe. That is why the remedy is content
comparison (`git show 903dab20^:app/models.py | sed -n 'Np'` against
`sed -n 'Mp' app/models.py` returning byte-identical lines) and never arithmetic.

**232. GROUP A OF `app/shared/reply.py` IS NOT AT ZERO OVER
`tests/test_shared_*.py` ALONE — SEVEN STATEMENTS AND FIVE ARCS COME FROM
`tests/test_api_reply_bookmarks.py`.** Sub-project 40's plan measured the
module over `tests/test_shared_reply*.py tests/test_shared_post*.py`, and that
file list is incomplete: it reports `bookmark_reply:76`, `:77`, `:86` and
`remove_bookmark_reply:94`, `:95`, `:99`, `:104` missing, plus the five arcs
`(75,76)`, `(85,86)`, `(93,94)`, `(98,99)`, `(103,104)`. Every `bookmark_reply`
test in `tests/test_shared_reply_interactions.py` seeds a bookmark first, so
`:75`'s true arm never runs; no `remove_bookmark_reply` test there seeds one,
so `:93`'s true arm never runs either — the two dead arms are opposite ends of
the same single-row fixture habit. The closer is the one-test file
`tests/test_api_reply_bookmarks.py`, which drives both functions from the
opposite fixture state — `tests/test_shared_reply_interactions.py`'s module
docstring says so under `WHAT TASK 7'S MUTATION PASS LEFT OPEN`, and all seven
differing statements are in `bookmark_reply`/`remove_bookmark_reply`. Three
measurements, same commit,
`--cov=app.shared.reply --cov-branch`:

| scope | tests | missing stmts | missing arcs | `percent_covered` |
|---|---|---|---|---|
| the plan's 8 `test_shared_*` files | 398 | 238 | 131 | 32.909 |
| those 8 plus the 2 `test_api_reply_*` files plus `test_ap_moderation.py`, `test_shared_tasks_send_reply.py` | 482 | 231 | 126 | 35.091 |
| `test_shared_reply_interactions.py` + the 2 `test_api_reply_*` files ONLY | 36 | 231 | 126 | 35.091 |

The third row is the useful one twice over. It proves the seven statements and
five arcs are the whole difference, and it proves the seven
`tests/test_shared_post_*.py` files contribute **nothing** to
`app/shared/reply.py` — 36 tests reach exactly what 482 do. **Measure this
module over the three reply files; adding the post files costs 45 seconds and
buys zero coverage.** The general rule: a module-scoped floor taken over a
glob chosen for its NAME can under-report, and the under-report is invisible
because the run still exits 0.

**THE SEVEN LINE NUMBERS IN THIS FACT WERE CORRECTED ABOVE — +14, and they went
wrong on the day they were written, not slowly.** They were written at
`61f32494` (2026-09-13), where `bookmark_reply` began at `:57`. Commit
`903dab20`, **the same day and on the same branch**, added a fourteen-line
`'reversal'` arm to
`vote_for_reply` — one hunk, `@@ -23,6 +23,20 @@` — and every line below it in
`app/shared/reply.py` moved by exactly +14 **at that commit** (the composite
offset to HEAD is larger in two places — see below). Verified not by arithmetic
but by
content: `git show 903dab20^:app/shared/reply.py | sed -n '62p;63p;72p;80p;81p;85p;90p'`
and `sed -n '76p;77p;86p;94p;95p;99p;104p' app/shared/reply.py` return
byte-identical lines. **The same commit is already registered (D517) as having
invalidated the campaign's `app/models.py` citations, and that sweep was scoped
by the cited FILENAME, so this class — its own module's citations, in this
file — went unswept. So did two more; see the next paragraph.** Scope a
citation sweep by the COMMIT, not by the file the stale citations happen to
name. Fact 116 above carried the same +14
and is corrected too, with a marker at its own site. **The interval between
writing a citation and its going stale can be hours**, so "I wrote it recently"
is not evidence that a line number is still right.

**AND THIS FILE'S `app/shared/post.py` CITATIONS ARE STALE TOO — THE SAME
COMMIT, +12, AND THEY ARE NOT CORRECTED.** `903dab20` shifted four production
files, not one: `app/models.py` (+5 from old `:2743`, +12 from old `:3322`),
`app/shared/reply.py` (+14 from old `:26`), `app/shared/post.py` (+12 from old
`:39`) and `app/cli.py` (+10 from old `:673`). Under `tests/`, **56
`app/shared/post.py:NNN` citations across 16 files predate it and sit in the
shifted region**, fifteen of them in this file. One is two words from a
`reply.py` citation this round DID flag: `tests/test_shared_reply_interactions.py:2135`
and `:2147` cite `app/shared/post.py:53` as the `VOTE_QUOTA` refusal, which is
now `:65` (`:53` is `user = current_user`). (**Those two locations read
`:2087` -- a single number for two sites -- until sub-project 42 task 9's fix
round re-derived them by content; `:2087` was already wrong when written and
sub-project 42's own docstring edit then added 23 more. Registered as D545.**) **Treat every `app/shared/post.py:NNN` in
this file as unverified until checked**, and check by content —
`git show 903dab20^:app/shared/post.py | sed -n 'Np'` against
`sed -n '(N+12)p' app/shared/post.py` — rather than by adding 12, because a
citation already stale from an earlier generation will not move by 12. The
sweep is registered as D532 and owed to sub-project 42.

**AND SO ARE THIS FILE'S `app/models.py` CITATIONS — THE SAME COMMIT, +5 OR
+12, AND THIS PARAGRAPH IS THE ONLY WARNING THEY HAVE.** Until sub-project 41's
final fix wave this fact warned about `app/shared/post.py` and said nothing
about `app/models.py`, which is the largest class of the four and the one D532
has been chasing since it was opened. Measured at `fa4978c3`, i.e. before this
wave touched anything: `git show fa4978c3:tests/README.md | /usr/bin/grep -oE
'models\.py:[0-9]+'` returns **59** citations, **18** of which name a line at or
after old `:2743` and are therefore inside the shifted region. Exactly one of
the 18 — fact 234's `app/models.py:3058-3065`, written after the shift — was
correct, leaving **17 stale candidates**; **four are corrected in this wave**
(fact 116's `:2901`, and fact 231's `:3054-3059`, `:2899` and `:3311-3402`) and
**about thirteen remain**. The offsets are `app/models.py`'s, from the table
above: **+5** from old `:2743`, **+12** from old `:3322`, nothing below old
`:2743`. **TREAT EVERY `app/models.py:N` IN THIS FILE WITH `N >= 2743` AS
UNVERIFIED UNTIL CHECKED BY CONTENT.** Re-running that grep against the working
tree now over-counts, because the correction markers quote the old numbers
alongside the new ones; scope any recount to a revision.

Two worked examples, both corrected in the fix wave, both chosen because they
show the class surviving its own remedy:

- **`PostReply.body` in fact 116** read `app/models.py:2901` and is now
  `:2906`. It sat **nineteen lines above fact 116's own D532 correction block**
  — a block added in this very round, by a sweep scoped to `app/shared/reply.py`
  citations, which corrected the two `reply.py` numbers five lines below it and
  walked past the `models.py` number. The named defect class, occurring inside a
  correction written for the named defect class.
- **The path construction in fact 231** read `app/models.py:3054-3059` and is
  now `:3058-3065`. That is the SAME construct, and the same correction, that
  Ruling 7 applied to three sites in `tests/test_shared_reply_moderation.py`
  and two in `tests/test_shared_reply_interactions.py`, and then wrote
  **correctly** into new fact 234 about 200 lines below. **This file carried
  the right citation and the wrong citation for one construct at the same
  time** — because the sweep was scoped to the two test files, not to the
  document that is supposed to be their durable store.

The remaining ~13 are owed to D532's sweep and are NOT corrected here; that is
round-sized work with a non-uniform offset, and doing it under time pressure is
how fresh errors get introduced. **What is fixed here is the completeness this
fact was claiming and did not have.**

**`app/shared/reply.py` citations can be stale by three different amounts.**
This round's own `87f3027f` inserted two lines twice more, so from `903dab20^`
to HEAD the offset is **+14** below old `:504`, **+16** from old `:504` and
**+18** from old `:529` — and a citation written BETWEEN the two commits is off
by 0, +2 or +4 instead. There is no single number to add.

**Re-measured at `724312cb`, after sub-project 41 closed Groups E and F.** The
rule holds and the numbers moved; three scopes, `--cov=app.shared.reply
--cov-branch`, 375 statements and 198 arcs:

| scope | tests | missing stmts | missing arcs | `percent_covered` |
|---|---|---|---|---|
| the 2 `test_shared_reply_*.py` files | 91 | 133 | 73 | 64.049 |
| those 2 plus the 2 `test_api_reply_*` files | 93 | 126 | 68 | 66.143 |
| all `test_api_*` + `test_shared_*` + `test_ap_*` + `test_inbox_*` + `test_post_*` + `test_backfill_reply_visibility.py` | 2426 | 126 | 68 | 66.143 |

Same seven statements and five arcs, at their new numbers. Rows two and three
agree exactly, which is the useful part: **93 tests reach everything 2426 do.**
The reason is structural and cheaper to check than to measure — only two
modules in `app/` import from `app/shared/reply.py`
(`/usr/bin/grep -rn 'app\.shared\.reply' app/ --include=*.py` returns
`app/api/alpha/utils/reply.py` and `app/post/routes.py`), and only four files
under `tests/` reach either. Enumerate the importers first; it turns a
four-minute run into a one-second grep.

**The "three reply files" instruction above is now FOUR.**
`tests/test_shared_reply_moderation.py` did not exist when this fact was
written; it is where Groups E and F live and it carries 51 of the 93 tests.

**233. `force_locale(get_recipient_language(...))` NEEDS NOTHING FROM A FACTORY
USER, AND THE REASON IS NOT THE ONE YOU WOULD GUARD AGAINST.** `choose_answer`
(`app/shared/reply.py:560`) wraps its notification title in
`with force_locale(get_recipient_language(post_reply.user_id)):`, and
`get_recipient_language` (`app/utils.py:4782-4801`) queries the `Language`
table at `:4790`. The obvious hazard is a factory user whose `language_id`
points at a `Language` row no test seeded, which would die on `lang.code`.
**That hazard does not exist, because `language_id` is never set at all.**
`make_user` (`tests/factories.py:41-67`) passes `user_name`, `email`,
`instance_id`, `verified`, `banned`, `private_key`, `public_key`, `ap_id`,
`ap_profile_id`, `ap_public_url` and `ap_inbox_url` — neither `language_id` nor
`interface_language` — and `app/models.py:1037-1038` declare both columns with
no `default=`, so both are `None` on INSERT. `:4789` is False, `:4794` is
False, and `:4799` returns `'en'` without the `Language` table ever being
touched. **So: seed no `Language` row for any test that calls `choose_answer`,
and do not add one "to be safe" — it would change the branch taken.** The
conclusion is structural, not "it did not raise once": the `elif` and `else`
arms reference `Language` nowhere.
**THE PRICE IS A PERMANENT BLIND SPOT, AND IT IS REGISTERED (D528(d)) RATHER
THAN HIDDEN.** Because every factory user resolves to English, two different
users are indistinguishable through `force_locale`, and the mutant
`get_recipient_language(post_reply.user_id)` -> `get_recipient_language(user.id)`
survives the whole file. Closing it needs a second locale with a compiled
catalogue, which is a translation-fixture project, not a test.

**234. WITNESSING `lock_post_reply`'s `@>` CASCADE TAKES A HAND-BUILT `path`
AND A BYSTANDER, AND NEITHER IS OPTIONAL.** `lock_post_reply:503-504` is the
module's only containment query —
`update post_reply set replies_enabled = :replies_enabled where path @> ARRAY[:parent_id]`
bound with `{'parent_id': post_reply.id}` — and it is raw SQL, so nothing in
the ORM layer will build the state for you. Two separate requirements:
**(a) `make_post_reply` does not set `path` at all**, so a descendant must be
given production's shape by hand, `[0, parent.id, child.id]`, per
`app/models.py:3058-3065` (`[0, reply.id]` at the root, parent's path plus own
id below it). **(b) A BYSTANDER ROW IS THE ONLY WITNESS FOR THE `where`
CLAUSE.** Assert only on the descendant and a mutant that drops the `where`
flips every reply in the table while the test stays green; only a row that
should NOT have changed catches it. `:502` sets `replies_enabled` on the locked
reply in Python, so **the locked reply itself witnesses nothing about the raw
UPDATE** — assert on rows reached only through it. See
`TestLockPostReply::test_locking_cascades_to_a_descendant` and
`test_the_cascade_keys_on_the_replys_own_id_not_the_posts`.

**235. `set_collapse_post_reply`'s TWO `task_selector` CALLS ARE COMMENTED OUT,
AND THE COMMENTED TEXT NAMES THE WRONG TASKS.** `app/shared/reply.py:540` and
`:544` read `#task_selector('lock_post_reply', ...)` and
`#task_selector('unlock_post_reply', ...)`. Two consequences for anyone writing
tests here. **First, collapse federates nothing**: a `recording_task_selector`
assertion against this function must assert the EMPTY list, and a test that
expects a dispatch is asserting a defect rather than the behaviour. Its twin
`lock_post_reply:513`/`:517` does dispatch, so the two Group F verbs are not
symmetric on this axis however similar they read. **Second, the comments are
not a dormant correct implementation waiting to be switched on.** No collapse
task exists anywhere — `/usr/bin/grep -rn collapse app/shared/tasks/` returns
nothing, and `app/shared/tasks/__init__.py:39-40` registers only
`lock_post_reply` and `unlock_post_reply`. Uncommenting either line would
federate a LOCK for what is a local display preference. Registered as D527.

**236. `_seed_moderated_reply` LEAVES `post.id == reply.id == author.id == 1`,
AND THAT COLLISION HID FOUR MUTANTS ACROSS THREE FUNCTIONS.** The fixture in
`tests/test_shared_reply_moderation.py` seeds one instance, one author, one
actor, one community, one post and one reply into a database whose sequences
`tests/conftest.py:131` resets to 1 between tests. Every id that should be
distinguishable therefore is not, and the failure mode is silent: `add_to_modlog`
(`app/utils.py:3574-3581`) resolves each object to `x.id if x else None` before
building the `ModLog`, so **a wrong object with the right id is invisible even
to a test that asserts every column of the row.** Four confirmed hiding places,
all found by mutation and all now defended: `lock_post_reply:504`'s
`'parent_id': post_reply.id` (transposable with `post_reply.post_id`),
`choose_answer:564`'s `'post_id': post_reply.post_id`, and the `link` argument
of both `add_to_modlog` calls in `mod_remove_reply`/`mod_restore_reply`, whose
`f'post/{reply.post_id}#comment_{reply.id}'` yields the identical string when
the halves are swapped. **The remedy in the tests is to act on a SECOND reply**
so the acting reply's id and its `post_id` differ, with an explicit
`assert target.id != s.post.id` in the test body so the guard fails loudly if
the fixture ever changes. That is a guard rail, not a fix: offsetting one
sequence in the fixture would remove the whole class, and until someone does,
**any assertion in this area that names an id should be read as unproven.**
**Four is the CONFIRMED count, not the size of the class** -- any object
substitution resolving to the same id is invisible the same way, and
`post=reply.community` / `target_user=reply.community` in the `add_to_modlog`
calls both survive even the every-field-asserted tests. Nobody has enumerated
the rest. Registered as D533.

**237. A USER WITH id 1 IS AN ADMIN OUTRIGHT, SO ANY MODERATOR-GUARD TEST WHOSE
ACTOR IS THE FIRST USER THE FIXTURE MINTS PASSES FOR THE WRONG REASON -- AND
KNOWING THAT IS NOT THE SAME AS CHECKING IT.** `User.is_admin`
(`app/models.py:1259-1265`) opens `if self.id == 1: return True`, before any
role lookup. `tests/conftest.py:131`'s teardown ends in
`SELECT setval(c.oid, 1, false) FROM pg_class c WHERE c.relkind = 'S' AND
c.relnamespace = 'public'::regnamespace`, which resets EVERY sequence to 1
after EVERY test (`:189-190` says so, because fixtures hardcode
`instance_id=1`). So the first user a fixture mints is id 1 in every test,
**deterministically -- not a coin flip that depends on run order**, which is
the harder case to notice because nothing ever fails. The remedy in
`tests/test_shared_reply_make.py:247` is `_burn_a_seed()`: it advances the
user, community and post sequences by one full `_seed_for_reply` unit before
the real seed, so the acting user is never id 1. **THE PART WORTH CARRYING IS
THE PROCESS FAILURE, NOT THE TRAP.** Sub-project 42 identified the trap in its
Task 2, built `_burn_a_seed` for it, and had a reviewer prove the helper
load-bearing by mutation -- and three `TestEditReply` tests then shipped
WITHOUT the call, through two reviews. A probe at Task 8 replaced
`reply.community.is_moderator(user)` with `False` at BOTH `edit_reply:224` and
`:239` at once, and **all 38 tests passed**; one of the three claimed in its
own docstring to prove `:223` gates `:224`, which it could not. The control
that proves the fault was in the tests rather than the fixture or the probe:
the same substitution against `make_reply:177`, whose actor is `s.actor` at id
2, dies immediately. **A trap you have named and tooled for is still open until
something CHECKS for the tool's absence** -- so when a fixture has an id-1
hazard, write the mutation that makes the guard invisible and require it to
die, per test, rather than trusting the helper to be remembered. Registered as
D540.

**238. `PostReply.new` HAS SEVEN `PostReplyValidationError` RAISES, NOT FIVE,
AND ONLY THREE ARE REACHABLE FROM A PLAIN FACTORY SEED.** `/usr/bin/grep -n
"raise PostReplyValidationError" app/models.py` returns `:2983` Comments are
disabled, `:2986` Banned from commenting, `:3025` Blocked phrase, `:3036`
Replier blocked, `:3039` Duplicate reply, `:3046` Gif comment ignored, `:3049`
Low quality reply. **Three are witnessed** by `tests/test_shared_reply_make.py`
-- `:2983`, `:3025` and `:3039`. **Four are not, each for a stated reason.**
`:3046` and `:3049` are gated on `site.enable_gif_reply_rep_decrease` and
`site.enable_this_comment_filter`, both `db.Column(db.Boolean, default=False)`
at `app/models.py:3967` and `:3969`; `:3042`'s fallback `Site()` is unflushed,
so both read falsy. **A test that merely posts gif-like content witnesses
nothing** -- it needs a `Site` row with the flag explicitly `True`. `:2986` is
shadowed: `make_reply:192`'s `can_create_post_reply` refuses the same user
first, with a DIFFERENT message, which is what lets the two be told apart at
all. **And reaching a real `PostReply.new` at all needs two prerequisites
neither the module nor any plan states**, both in
`tests/test_shared_reply_make.py:204`'s `_clear_creation_guards`: a non-None
`private_key` on a local user (`app/utils.py:2554` returns False without one --
a sentinel string is enough, `make_user(with_keys=True)` costs about a second
per call for a real keypair that nothing here checks), and a `Site` row,
because `PostReply.new` calls `blocked_phrases()` at `app/models.py:3023` for
any reply with a non-empty body (`:3022` is the only gate, and every test here
supplies one), and `app/utils.py:1751-1753` does
`db.session.query(Site).get(1).blocked_phrases` with no None guard. The
helper is deliberately **opt-in
rather than folded into the seed**, so tests that want those guards live still
exercise them.

**239. ON ITS ORDINARY PATH `report_reply` NEEDS NEITHER A `Site` ROW NOR KEYED
USERS, WHERE `make_reply` NEEDS BOTH -- PROBE IT, DO NOT COPY THE OTHER
FIXTURE'S HELPER.** The two functions live in the same module and read as
siblings, so the natural move is to reuse `_clear_creation_guards`; it is
unnecessary on `report_reply`'s ordinary path and would hide two guards.
**CORRECTED AT SUB-PROJECT 42's FINAL FIX WAVE: THIS FACT WAS NARROWED AFTER
REVIEW, AND THE NARROWING IS FALSE. THE FLAT CLAIM IT REPLACED WAS RIGHT, AND
IT IS RIGHT ON THE ADMIN-NOTIFICATION PATH TOO -- SO THE HEADLINE'S "ON ITS
ORDINARY PATH" IS CONSERVATIVE, NOT WRONG.** The sentence that stood here read,
verbatim:

> **NARROWED AFTER REVIEW, BECAUSE THE FIRST VERSION OF THIS SENTENCE SAID
> "`report_reply` NEEDS NEITHER" FLATLY AND THE SAME FILE CONTRADICTS IT**: the
> admin-notification arm does need a `Site` row, which is why
> `tests/test_shared_reply_report.py:98`'s `make_site_admin` creates one -- the
> block at `:380-388` calls `Site.admins()`, and a user that method will
> actually return needs both a `Site` row and a `Role` row.

**The `Site`-row half of that is false. `Site.admins()` reads no `Site` row at
all** (`app/models.py:4006-4012`): its `g.admin_ids` branch queries `User` by
id, and its else-branch queries `User` joined to `user_role` -- **the word
`Site` does not appear inside the method body.** Established by execution, not
by argument: **neutralising ALL FOUR `make_site()` calls in
`tests/test_shared_reply_report.py` (`:125` in `make_site_admin`, and the
inline ones at `:655`, `:810`, `:1114`) leaves the file at 28 passed**, the
admin-notification tests included. The `Role` half IS true and load-bearing --
see (b) and (c) below -- so what the admin arm needs is a `Role` row, not a
`Site` row. **`make_site()` inside `make_site_admin` is decorative**, and that
helper's docstring advertised the decoration as a mechanism until this was
caught. The calls are left in place deliberately: a whole-line deletion in a
cited file is never free (register entry D545), and this fact and fact 240 both
cite line numbers below `:125`. **THE SHAPE IS THE LESSON, AND THIS ROUND HIT
IT SEVEN TIMES: A CORRECTION CAN INTRODUCE THE DEFECT IT WAS WRITTEN TO
REMOVE.** (An earlier wording of this sentence said FOUR. That count was
inherited from a controller ruling and never re-derived -- which made the
sentence naming this defect class the class's own seventh instance. Two of the
seven occurred inside the commit that added this fact, and the round's
line-number discipline was applied to every citation and to no tally.
**A COUNT IS A CLAIM: re-derive it like a line number.**)

A reviewer read `make_site_admin`, saw `make_site()`, and inferred a
requirement from a call -- **a call is evidence that somebody wrote it, not
evidence that anything needs it**; the only way to tell the two apart is to
take it away and run. **WHAT SURVIVES FROM THE NARROWING, BECAUSE IT IS TRUE
AND WORTH KEEPING**: the headline's claim is about the guards `make_reply`
trips on the way to `PostReply.new` (`private_key`, `blocked_phrases`), not
about every line `report_reply` can execute, and **a "needs nothing" claim is a
claim about a PATH and should name the path.** That discipline is right; it was
applied to the wrong dependency. What `report_reply` DOES need is a community with **one local
and one remote moderator, as four distinct users** (`tests/
test_shared_reply_report.py:51`): `:361`'s loop branches per moderator and
`:382`'s admin block skips anyone already notified, so a fixture reusing one
user cannot tell those arms apart. Three further levers in this area are not
what they look like. **(a) `is_local()` reads `ap_id`, not `instance_id`** --
`app/models.py:1251-1252` is `return self.ap_id is None or
self.ap_profile_id.startswith(current_app.config['SERVER_URL'])` -- so a
"remote" user built by setting `instance_id` alone stays local and the test
silently misses its branch. **(b) A `Role` must carry `id=ROLE_ADMIN`
(`4`, `app/constants.py:81`), not merely `name='Admin'`**: `Site.admins()`
filters on `user_role.c.role_id == ROLE_ADMIN` while `User.is_admin()` matches
on the role NAME, so a wrongly-numbered row satisfies one predicate and not the
other. **(c) `Site.admins()`'s else-branch INNER JOINs `user_role`**
(`app/models.py:4011-4012`), so a role-less id-1 user is dropped before the
`or_(..., User.id == 1)` disjunct is ever reached -- that user is an admin by
`User.is_admin()` and not by `Site.admins()`. See fact 216 and register
entries D295/D442: **a claim about "admin" that does not name its predicate is
not a claim.**

**240. TO FORCE SEEDED IDS APART, BURN SPARE ROWS AND THEN ASSERT THE RESULT --
THE ASSERTION IS THE POINT, AND IT CAUGHT ITS OWN AUTHOR'S FIXTURE BUG THE
FIRST TIME IT RAN.** D533's collision class (fact 236) is removed for a single
fixture by minting throwaway rows so each table's sequence sits at a different
offset when the real rows are created. `tests/test_shared_reply_report.py:69-78`
is the worked pattern: **two** burner communities and **one** burner post give
`community.id == 3`, `post.id == 2`, `reporter.id == 1`, followed by
`assert len({community.id, post.id, reporter.id}) == 3`. **The plan this came
from burned ONE community, which puts `community.id` and `post.id` both on 2**
-- and the guard assertion failed on its own author's fixture, empirically,
before anything else did. `tests/test_shared_reply_make.py:1547-1569` is the
same pattern one table over: two burner **replies** so a newly created reply
cannot land on `s.post.id`, guarded by `assert reply.id != s.post.id`; that one
also failed first time, on `assert 2 != 2`. **Two rules follow.** First, the
offsets are deterministic BECAUSE `conftest.py:131` resets every sequence
between tests (fact 237) -- burning is reproducible, not a gamble. Second,
**name the pairs the test actually depends on rather than asserting a blanket
distinctness**: an early version asserted five ids pairwise distinct and failed
on `len({1, 2, 3}) == 5`, which is a fixture fact, not the property any mutant
turned on. A burn without an assertion is a hope; the assertion is what fails
loudly when a factory changes underneath it.

**241. A LINE NO INPUT CAN REACH IS NOT A LINE NO TEST CAN REACH -- AND WHEN A
MODULE HAS A TWIN, CHECK THE TWIN BEFORE RULING ANYTHING UNCOVERABLE.**
**THIS FACT PREVIOUSLY CLAIMED THE OPPOSITE AND THE CLAIM WAS FALSE.** It read:
*"`app/shared/reply.py` CANNOT REACH 100% WITHOUT A PRODUCTION EDIT... the
module therefore measures 99.6545768566494%... and the floor is 99, not 100...
a coverage round whose production budget was spent elsewhere must say the
module is not at 100 rather than round past it."* The module is at **100.0%**
with `missing_lines []` and `missing_branches []`, closed by an ordinary test,
and the floor is **100**.

What was true: `extra_rate_limit_check` (`app/shared/reply.py:148-153`) is a
docstring and `return False`, so `make_reply:159` is never true on any input
and `:160`'s `raise Exception('rate_limited')` is unreachable **while the stub
returns a constant**. What did not follow: that no test could cover it. **The
twin module had the answer the whole time.** `app/shared/post.py:167-172` is
the same stub one word different, `coverage_floors.ini` has carried
`app/shared/post.py = 100` since sub-project 39, and
`tests/test_shared_post_make.py:409-434` closes the identical line by saving
`post_module.extra_rate_limit_check`, replacing it with `lambda user: True`,
asserting the raise and restoring it in a `finally`. Its docstring states the
principle outright -- *"the monkeypatch is not a convenience here; it is the
only way in, and that fact is registered rather than hidden"* -- and it
cross-references the reply twin by name. The reply mirror is now
`tests/test_shared_reply_make.py::TestMakeReply::test_a_rate_limited_api_user_is_refused`.

**Three things to carry, none of them about rate limiting.** (1) **A surviving
mutant is ambiguous evidence**: it means the mutant is equivalent OR that no
test exists yet, and the mutation pass classed this one equivalent without
asking whether the twin already had the test. (2) **The prescribed remedy would
not have worked either**: `# pragma: no branch` suppresses the ARC but leaves
the statement in `missing_lines`, because `.coveragerc` excludes only
`pragma: no cover`. Check what your `exclude_lines` actually contains before
recommending a pragma. (3) **Diff the twin first.** `app/shared/post.py` and
`app/shared/reply.py` are this campaign's canonical mirrored pair and the
duplication of this very stub is a registered finding (D406); the cheap check
was one grep and it came after the ruling instead of before it. Registered as
**D539**, which is a retraction rather than a finding.

**242. `block_another_user` COMPARES RAW `role_id` INTEGERS, SO `grant_permission`
CAN MAKE YOUR SUBJECT STAFF OR ADMIN BY ACCIDENT -- THIS IS FACT 103 RUNNING IN
THE OPPOSITE DIRECTION.** `ROLE_STAFF` is **3** and `ROLE_ADMIN` is **4**
(`app/constants.py:80-81`), and `app/shared/user.py:33-35` reads
`SELECT role_id FROM "user_role" WHERE user_id = :person_id` with `.scalar()`
and compares the result to those two **integers** -- it never touches
`User.is_admin()` or `Site.admins()`, so neither fact 103 nor fact 217 governs
it. `grant_permission` (`tests/factories.py:365`) mints a **fresh `Role` per
call with an auto id**, and `tests/conftest.py:131-132` resets every sequence
between tests, so the calls in a single test are numbered 1, 2, 3, 4, ... **The
third `grant_permission` call in a test makes its subject STAFF by id and the
fourth makes it ADMIN by id**, and `block_another_user` then refuses the block
for a reason the test never intended. That lands in one of two ways and the
first is the dangerous one: **a test asserting a REFUSAL passes for the wrong
reason**, having witnessed the role guard at `:35` while claiming to witness
whatever it meant to test, and a test asserting a SUCCESSFUL block fails with
no `UserBlock` row and no obvious cause. Fact 103 warns that `grant_permission`
gives you too little
privilege for `Site.admins()`; this is the same auto-id mechanism giving you too
much. **Two rules.** Create the role with an EXPLICIT id --
`Role(id=ROLE_ADMIN, name='role-with-id-4', weight=0)` -- so the privilege is
deliberate and the count of earlier `grant_permission` calls stops being
load-bearing. And **name it something that is NOT `'Admin'` or `'Staff'`**, so
the test proves the id comparison at `:35` rather than accidentally satisfying
`User.is_admin()`'s role-NAME path at `app/models.py:1263`, which is a different
predicate entirely (fact 217, fact 239).

**243. `run_tests.sh:83` RUNS `flask db upgrade` BEFORE EVERY PYTEST
INVOCATION, SO A NEW MIGRATION NEEDS NO MANUAL APPLICATION.** The line is
`$COMPOSE exec -T test-runner flask db upgrade`, and `:85` is the
`exec ... pytest "$@"` immediately after it. Add a revision under
`migrations/versions/` and the very next `./run_tests.sh` applies it; there is
no separate step to remember and no state to clear first. This is the practical
half of the schema note earlier in this file (the schema comes from
`flask db upgrade`, not `db.create_all()`): it is not only where the schema
comes from, it is re-derived on every single run. The corollary is the part
that bites in the other direction -- **a migration that fails to apply fails
the run before pytest is ever reached**, so a traceback with no test output at
all is a migration error, not a collection error.

**244. THE REPOSITORY *IS* SHARED WITH THE CONTAINER; WHAT IS NOT SHARED IS
`/tmp`.** `compose.test.yaml:67` is `- ./:/app:z` on the `test-runner` service,
so a file written on the host with ordinary tools appears at `/app/<path>`
inside the container immediately -- writing a probe test with `Write` and then
running `./run_tests.sh tests/test_probe_thing.py` works, and the
stdin-piping-into-`python -c` dance some plans prescribe for creating files is
unnecessary complexity that can fail for its own reasons. The rule that DOES
hold is narrower than "the host and container do not share a filesystem", which
is simply false: **the container's `/tmp` is outside that one bind mount**, so a
coverage json written to `/tmp` lands in the container and must be read back
with an inlined container Python. Fact 154 is the same boundary seen from the
other side, for a test that needs a real file on disk. State which of the two
directions you mean; "the container cannot see my file" is ambiguous and is
wrong for repo paths.

**245. `ban_user`'s WEB ARM TAKES A WTForms OBJECT AND `unban_user`'s TAKES A
DICT -- SIBLING FUNCTIONS, DIFFERENT INPUT SHAPES.** `app/shared/user.py:151`
reads `input.person_id` as a **plain attribute**, which exists only because
`app/user/routes.py:782` sets `form.person_id = user.id` on the form object
before calling -- it is not a WTForms field and there is no `.data` on it, while
the four lines under it (`:152-155`) DO read `.data` off real fields
(`input.purge.data`, `input.ip_address.data`, `input.reason.data`,
`input.flush.data`). `unban_user:219`, four lines further down the same file,
reads `input['person_id']` off a dict on its web arm, identically to its own API
arm at `:216`. So a `SimpleNamespace` double works for `ban_user`'s web arm and
a plain dict does not, and the reverse holds for `unban_user`. Neither shape is
inferable from the other, and neither is inferable from the API arm, which takes
a dict in both functions.

**246. `send_message` HAS `current_user` AS A DEFINITION-TIME DEFAULT
ARGUMENT.** `app/chat/util.py:12` is
`def send_message(message: str, conversation_id: int, user: User = current_user, src=SRC_WEB)`.
Python evaluates that default **once, when the module is imported**, so the
parameter is bound to the `LocalProxy` object itself and not to any user -- it
resolves per call at attribute access, which means it resolves to whatever the
CALLER's context has, never to whatever the caller passed as its own `user`.
`app/shared/user.py:335` calls it without overriding the argument. Two
consequences worth carrying: a caller that already holds an authorised `User`
model silently does not pass it, and outside a request context the proxy is not
an anonymous user but plain `None` in this codebase's flask_login
configuration -- which is what makes `bot_challenge_user`'s API arm fail with
`AttributeError: 'NoneType' object has no attribute '_sa_instance_state'` two
lines BEFORE `send_message` is reached (register entry D555).

**247. A NARROW `--cov` MAKES `check_coverage_floors.py` REPORT FALSE
VIOLATIONS FOR EVERY OTHER FLOORED MODULE.** `violations()`
(`tests/check_coverage_floors.py:65-78`) does
`actual = entry['summary']['percent_covered'] if entry else 0.0` -- **a floored
module absent from the report scores 0.0 rather than passing**, deliberately, so
that a rename or an import failure cannot satisfy the ratchet silently. That
behaviour is correct and it means the floors check is only meaningful against a
report that covers every floored module. Run the ratchet against a
`--cov=app.shared.<one module>` json and it reports a violation for each of the
other floored modules: at sub-project 43 that would have been **21** of the 22
entries, which looks exactly like a catastrophic regression. **Measure with
`--cov=app` for the ratchet, and take the narrow `--cov=app.<module>` run
separately when you want the module's own figures fast.** Fact 168 covers which
FIELD the checker compares; this is about which FILES have to be in the report
for that comparison to mean anything.

**248. `git commit --amend` WITH NOTHING STAGED CHANGES THE MESSAGE AND NOTHING
ELSE, AND EVERY WORKING-TREE CHECK STILL PASSES.** The trap is that the
evidence you would naturally reach for is all derived from the working tree: a
grep finds your edit, a test run exercises your edit, `git status` is clean
because the file matches... the INDEX, which you never updated. The commit
object contains none of it. This is the same boundary as facts at
`git show HEAD:` elsewhere in this file, arriving from the authoring side rather
than the mutation side: **during an amend, the tree is not the artifact.**
Verify against the commit object -- `git show HEAD:<path>`,
`git diff --numstat <base> HEAD`, `git show --stat HEAD` -- before reporting a
commit as carrying a change, and `git add` the paths explicitly rather than
relying on `-a` or on the previous commit's staging.

**249. CHOOSING A MUTATION ORACLE THAT DOES NOT EXECUTE THE FUNCTION UNDER TEST
SILENTLY CONVERTS EVERY MUTANT THERE INTO A SURVIVOR.** A survivor means "the
mutant is equivalent OR no test kills it" (fact 241) -- but only if a test ran
through the line at all. If the oracle never enters the function, every mutant
in it survives for a third reason that looks identical in the results table and
is evidence about nothing. Sub-project 43's dispatch prescribed three
`tests/test_shared_user_*.py` files as the oracle for all of
`app/shared/user.py`; those three cover the module at **78.18%** with
`subscribe_user`'s `:90-138` entirely missing, so **34 mutants would have
survived vacuously**, the kill rate would have read 63.3% instead of 76.5%, and
the two mutants in that function the round called genuinely equivalent would
have been buried among 34 identical-looking ones with recipes attached for
tests that already exist. (**Those two were not equivalent either** -- see fact
250 and the retracted D564; the point this fact makes about vacuous survivors
is unaffected.) **The check costs one command and belongs BEFORE the first mutant:
measure the proposed oracle against the module and read `missing_lines`.** If a
region is missing, either widen the oracle or label the region's mutants with
the oracle that judged them and report both count sets -- which is what was
done, and why register entry D574 records the correction rather than a bad
number.

**250. FACT 241 WAS CITED FOR ITS FIRST HALF AND IGNORED FOR ITS SECOND, IN THE
NEXT ROUND, ON THE SAME CANONICAL MIRRORED TRIO -- WITH THE REMEDY ALREADY
COMMITTED.** Fact 241's headline is two clauses: *"A LINE NO INPUT CAN REACH IS
NOT A LINE NO TEST CAN REACH -- AND WHEN A MODULE HAS A TWIN, CHECK THE TWIN
BEFORE RULING ANYTHING UNCOVERABLE."* Sub-project 43 quoted the first clause in
register entry D564, correctly, as the reason a surviving mutant is ambiguous
evidence -- and then ruled `app/shared/user.py:107` and `:115` unkillable
anyway, set the module's floor to 98, and wrote "this module is finished" into
the register, a test file's module docstring and the round summary. It never
ran the second clause.

**The twin check was one grep and it would have ended the question.**
`subscribe_user` (`app/shared/user.py:89-138`) is a line-for-line twin of
`subscribe_post` (`app/shared/post.py:127-164`) and of `subscribe_reply`, the
campaign's own canonical mirrored trio. Both twins had the IDENTICAL two
statements closed already, by the identical technique, down to the constant:
`tests/test_shared_post_interactions.py:577` and
`tests/test_shared_reply_interactions.py:1018`, both named
`test_a_third_source_reaches_the_flash_branches_the_web_arm_cannot`, both
passing `SRC_PLD`, and both modules already at floor 100 in
`coverage_floors.ini`. The remedy was not merely possible; it was committed,
passing, and two files away.

**The mechanism, stated so it is checkable rather than memorable.** The proof
D564 gave was real and it proved the WRONG PROPOSITION. It established that
`:107`/`:115` cannot be reached **from either production caller** -- under
`SRC_WEB`, `:93-94` recompute `subscribe` from the same query `:96-97` uses, so
the two move in lockstep; under `SRC_API`, `:104`/`:112` always take the raise.
None of that constrains a test. `src` is an ordinary parameter of a
module-level function, and `app/constants.py:94-95` defines `SRC_PLD = 4` and
`SRC_PLG = 5`. Pass a third value: `:93` is skipped so the caller's `subscribe`
argument survives, `:104`/`:112` take their `else` arms, and both statements
execute. **PRODUCTION-UNREACHABILITY IS NOT UNKILLABILITY**, and that sentence
is the whole of what separates D539 and D564 from being right.

**What to do about it, in order.** (1) Before writing "unreachable",
"equivalent" or "the floor is not 100" about any line, `grep` the function's
name for callers AND `diff` its twin's test file for the same function name.
(2) If the line is behind a `src ==` fork, list every constant in
`app/constants.py` that the fork's `else` admits before concluding anything --
the `else` is the contract, and it admits every source the `if` does not name.
(3) A floor below 100 is a claim about the whole campaign, not just the module;
it should be the hardest thing in a round to get past review, not the
conclusion a round reaches on its own proof. `app/shared/user.py` is at
**100.0** with `missing_lines []` and `missing_branches []`, and the campaign
has still never closed a module below 100. Registered as the rewritten
**D564**, which -- like **D539** before it -- is a retraction rather than a
finding.

**251. FACT 75 HAS NO CAUSE 4(c), AND THE LABEL THAT DOES NOT EXIST WAS CITED
BY TWO SUB-PROJECTS RUNNING BEFORE ANYONE RE-READ THE SOURCE.** Fact 75's
catalogue is 1, 2, 3, 4(a), 4(b), 5, 6, 7, 8. Item 4, "Tautology", has exactly
two shapes and **there is no 4(c)**. The shape the label keeps getting reached
for -- a `try`/`except` whose body can never run because the callee inside the
`try` has no raising path for the argument shape the call site can produce -- is
**cause 8, "Unreachable handler"**, added by sub-project 19 for exactly that
purpose. The wrong label originates at
`tests/test_shared_tasks_send_reply.py:1584`, a committed artifact of
sub-project 20, **which mis-cited the very cause its immediate predecessor had
just added**; sub-project 44's spec and `global-constraints.md` inherited it
verbatim and were corrected mid-round (`8f8402c7`), while the source citation is
still wrong and is registered as **D577** with a proposed fix rather than
quietly repaired. **The substance was right in all three places and only the
number was wrong**, which is precisely why it survived two reviews: a reader who
checks the argument and not the number sees nothing amiss. Two rules follow.
**A cause number handed to you in a brief is a claim like any other -- open fact
75 and read the item before citing it**, the same discipline this file already
demands for line numbers and test counts. And **having learned 4(c) does not
exist, do not now force-fit cause 8**: it is scoped to a `try`/`except`, and a
bare `if <cond>: raise` with no handler anywhere near it is not cause 8 however
unreachable it is -- `app/shared/upload.py:120-121` is that shape and is filed
under 4(b) instead (**D587**). Sibling of fact 250, which is the same failure
one item over: a fact cited for the half that suited the conclusion.

**252. THE MIRROR IMAGE OF CAUSE 4(b) -- AN INVARIANT THAT MAKES A CONDITION A
TAUTOLOGY AND STRANDS ITS *FALSE* ARM -- IS NOT IN FACT 75's CATALOGUE, AND TWO
TASKS REACHED FOR THE WRONG NUMBER INDEPENDENTLY BECAUSE OF IT.** 4(b)'s literal
text is *"The condition is falsified by an invariant established BEFORE the
guard runs -- by a caller, or by an enclosing guard -- so its **True** branch is
dead code."* `app/shared/auth.py`'s three residual dead branches
`[[54,57],[74,77],[111,-18]]` are the opposite polarity. `:21-39` is an
exhaustive `if src == SRC_WEB / elif src == SRC_API / else: return None` and
`src` is a parameter never reassigned in the body, so every line after `:39`
runs with `src` provably in `{SRC_WEB, SRC_API}`; each later
`elif src == SRC_API:` is reached only when its paired `if src == SRC_WEB:` was
false and is therefore **always true when reached**, leaving the **False arm**
dead. **The first draft of that ruling filed it under 4(b) by quoting 4(b) with
a bracketed "[non-matching]" substituted for the source's literal "True"** -- a
bent quotation that concealed the polarity mismatch, caught in review. **Filing
this shape under 4(b) as written is a mis-shelving; filing it under cause 8 is
worse** (there is no handler); **and there is no 4(c) to file it under** (fact
251). Registered as **D578** with a proposed amendment -- a new sub-shape under
cause 4, or an explicit note extending 4(b) to its mirror -- ~~**which has NOT
yet been folded into fact 75**, so until it is, cite this fact rather than a
number.~~ **ENACTED BY SUB-PROJECT 49 AS FACT 75's CAUSE 9, on a fourth instance
and the first outside `auth.py` (`app/shared/feed.py:487`). Cite the number now:
cause 9. It is NOT numbered 4(c), and the reason -- that a stale `4(c)` citation
must keep looking wrong rather than start looking valid -- is recorded in cause
9's own text; fact 251 is unamended and still true.**
The proof obligations are 4(b)'s either way: name the construct that establishes
the invariant and prove it **against every branch of that construct**, not
against a sample. And note what this is NOT: a third `src` value genuinely can
be passed (`test_log_user_in_refuses_an_unknown_source` passes one), and the
proof is that it is **intercepted and returned at `:39`** rather than that no
caller supplies it -- which is the distinction fact 241 exists for.

**253. A MODULE'S COVERAGE IS OFTEN NOT PRODUCED BY THE TEST FILE NAMED AFTER
IT, SO DERIVE THE ORACLE INSTEAD OF INFERRING IT FROM THE FILENAME.** Fact 249
says an oracle that does not execute the function turns every mutant into a
survivor; this is the near-miss that produces it. Two instances verified at
source in sub-project 44: **`app/shared/auth.py`'s entire `SRC_WEB` arm is
exercised by `tests/test_redirect_targets.py:234-265`**
(`TestSharedAuthNextPageIsChecked`), not by `tests/test_shared_auth_login.py`
alone -- omit it and `:99-105` look uncovered and every mutant there survives
vacuously. And **`app/shared/upload.py`'s SVG paths come from
`tests/test_utils_security.py`**, which imports `process_upload` at `:35` and
drives it for real around `:993-1049`; measured at the same moment in the round,
`tests/test_shared_upload.py` alone reported **92.1875%** with
`missing_lines [53, 55, 65, 121]` where the pair reported **96.875%** with
`missing_lines [121]` -- three of the four apparent gaps belonged to the file
that is not named after the module. **The check costs one command
and it is the same one either way: measure the proposed oracle against the module
with `--cov=<dotted module>` and read `missing_lines` BEFORE trusting a gap it
shows you** -- an oracle that reproduces the module's full-suite figure is the
only one whose survivors mean anything. The negative control is worth keeping in
mind for what it looks like: in the same round, a domain-only oracle reported
`app/shared/upload.py` at **9.375% with 76 missing lines**, which is what a
non-executing oracle really looks like and is nothing like a subtle gap.

**254. `git commit --amend` ALWAYS TARGETS HEAD, SO A FIX ROUND ON AN EARLIER
TASK'S COMMIT AMENDS THE WRONG COMMIT THE MOMENT A LATER TASK HAS LANDED.** Fact
248 covers the amend that stages nothing; this is the amend that stages correctly
and hits the wrong object. Reconstructed from the reflog in sub-project 44:
`HEAD@{4}` Task 1's commit, `HEAD@{3}` Task 2's commit on top, `HEAD@{2}` **Task
1's fix round running `git commit --amend` while HEAD was Task 2's commit, so it
amended TASK 2's commit with TASK 1's message**; the implementer noticed and
reset to its own base, which dropped Task 2 from the branch, and Task 2's commit
had to be recovered from the reflog and cherry-picked back. **The root cause was
the dispatch, not the implementer**: "amend your existing commit" was true when
written and false by the time it ran. Three rules. **`--amend` is safe only when
the target IS HEAD** -- check `git rev-parse HEAD` against the SHA you mean to
amend, and check it at the moment of the amend, not at the moment of the
instruction. **A fix round on a commit that is no longer HEAD commits a NEW
commit on top** and lets whoever owns the branch decide about squashing;
`git rebase` is not the remedy either. And the controller-side rule that
prevents it entirely: **do not run an implementer for one task while an earlier
task's fix round is still open**, because the reasoning "they touch different
files" is correct about files and irrelevant to git.

**255. A STATUS-CODE ASSERTION PAIRED WITH A FLASH-TEXT ASSERTION CANNOT
DISTINGUISH A REFUSAL FROM A FALL-THROUGH INTO THE SUCCESS PATH, BECAUSE BOTH
PRODUCE THE SAME 302 AND THE FLASH HAS ALREADY BEEN EMITTED.**
`app/shared/auth.py:53`'s `return redirect(url_for('auth.login'))` could be
deleted with the whole suite green: a **web login with a wrong password** then
flashes `'Invalid password'` at `:52`, falls out of `:46`'s block, past the ban
check at `:57`, into `:80`'s `login_user(user, remember=True)`, and returns the
ordinary success redirect. **A wrong password logged the user in.** The test
asserted `response.status_code == 302` and `flashed == ['Invalid password']`,
**and the bypass satisfies both** -- the success path is also a 302, and the
flash was emitted one line before the deleted return, so no further flash is
added. This is false-witness mechanism (a) -- asserting on state the success path
sets just as unconditionally -- and it is easy to miss because the assertions
look specific: an exact flash list feels like a strong assertion, and here it is
strong about the wrong thing. **The remedy is an assertion about the OUTCOME the
refusal exists to prevent, not about the refusal's cosmetics**: capture
`logged_in = '_user_id' in flask_session` inside the request context and assert
it is `False`, which is the same probe the file's success-path test uses with the
opposite expectation, so the pair is symmetric. **Generalise it: on any refusal
path that shares a status code with the success path, assert the thing that
would be true only if the refusal had NOT happened.** Registered with the round's
mutation pass at **D585**; the sibling at `:51` is a genuinely milder gap (no
bypass, because `:53` still returns) and was closed differently, by pinning the
exact flash list -- **checked rather than assumed, which is why the two got
different remedies.**

**256. A GUARD THAT FLASHES A REFUSAL AND DOES NOT RETURN IS INVISIBLE TO
COVERAGE, BECAUSE THE FLASH LINE AND THE FALL-THROUGH LINE BOTH EXECUTE AND
BOTH REPORT COVERED.** Sub-project 45 shipped two instances of this in one
round. `app/shared/site.py:14-20`'s `block_remote_instance` flashed `'You
cannot block the local instance.'` and then, for lack of a `return`, fell
through into the block-creation code that follows -- the local instance got
"blocked" on the web arm despite being warned against it. One function over,
`app/shared/community.py:60`'s `leave_community` guard read `not cm.is_owner
and not cm.is_moderator` -- correct -- but an earlier reading of the same
guard as `not cm.is_owner or not cm.is_moderator` shows the failure mode from
the other direction: under `or`, a plain moderator's row (`is_owner=False,
is_moderator=True`) evaluates the guard `True`, taking the free-leave branch
instead of the refusal, and the guard is dead for exactly the population --
moderators without ownership -- it exists to catch. **In both cases every
line runs on every test, line and branch coverage report 100%, and nothing
about the coverage tooling can tell you the refusal refuses nothing.** Fact
255 established this same shape for a status-code-plus-flash-text assertion
that a fall-through into a *different* path satisfies just as well; this is
its sibling for the case where the refusal never reaches a `return` or a
`raise` at all. **The mechanical remedy, stated once so a third and fourth
instance recognise it as a class rather than as luck**: a test of any
guard whose failure mode is "did nothing, or did too much" must assert the
row or session state the guard exists to protect -- `InstanceBlock` row count,
`CommunityMember` row count, `'_user_id' in flask_session` -- never merely
that a message was flashed or an exception's text matched. This is now the
third consecutive round to ship exactly this shape (sub-project 43's
`if SRC_WEB:`, sub-project 44's mis-nested ban refusal, and this round's two);
see **D597**.

**257. `not A or not B` IS `not (A and B)`, NOT `not (A or B)` -- SO A DE
MORGAN SLIP ON A TWO-FLAG GUARD MAKES IT FIRE ONLY WHEN *BOTH* FLAGS ARE SET,
THE OPPOSITE OF THE USUAL "REFUSE IF EITHER" INTENT.** `app/shared/community.py:60`
guards a refusal (`leave_community` must bar an owner or a moderator from
leaving without stepping down first) and the population it needs to catch is
"has EITHER role" -- `is_owner OR is_moderator`. The free-leave condition
(the guard must be False, refusing, for anyone with either role) is
`not (is_owner or is_moderator)`, whose De Morgan expansion is
`not is_owner AND not is_moderator`. Read
it with the operator flipped -- `not is_owner OR not is_moderator` -- and the
free-leave branch now fires whenever EITHER flag is false, which is every
population except "has both roles simultaneously": a plain moderator
(`is_owner=False, is_moderator=True`) satisfies `not is_owner` and is waved
through. **The population size matters here as much as the algebra**: for a
local community, ownership and moderation are usually correlated (the
creator is both), so a fixture built only from local owners would never
notice the guard was upside down; the guard is dead specifically for
federated communities' moderators, who are moderators without local
ownership by construction. A two-flag "refuse if either" guard should be
read as a conjunction of negations, and a reviewer checking one should
write out both De Morgan forms and confirm which population each admits,
rather than trusting that `or` "sounds like" the permissive direction. See
**D596**.

**258. THE ID-1 SEED BURN IS LOAD-BEARING TWICE OVER FOR ANY TEST THAT CALLS
`make_community`, NOT JUST ONCE.** Fact 216 established that `User.is_admin()`
returns `True` unconditionally for `id == 1` (`app/models.py:1259-1261`), so a
test that wants an unprivileged actor must burn the first seeded seat with a
throwaway user before minting its real actor. Fact 21 separately established
that `make_community` (`tests/factories.py:124`) hardcodes `instance_id=1,
user_id=1` and never takes either as an argument, so an `Instance` and a
`User` must already occupy id 1 -- with a real foreign key -- before it can be
called at all. **The two facts describe the SAME burned seed doing DOUBLE
DUTY, and sub-project 45 is the first round to state that explicitly rather
than satisfy each requirement separately.** A test that seeds a bystander
user first (to dodge the admin trap) and asserts `burn.id == 1` has, in the
same act, also supplied the row `make_community`'s hardcoded `user_id=1`
needs to exist -- so the community `make_community` builds is silently owned
by the burned bystander, not by whichever user the test seeds next. Confirmed
at source by Task 3's review reading `tests/factories.py:124` and
`app/models.py:1258-1261` side by side. The corollary for anyone auditing a
community fixture: check what role id 1 plays in BOTH senses before assuming
a seeded "throwaway" user is inert -- it is simultaneously the admin-trap
dodge and the community's owner of record.

**259. INSTANCE ID 1 IS THE LOCAL INSTANCE THROUGHOUT THIS CODEBASE, AND A
GUARD WRITTEN AS A HARDCODED `== 1` IS REACHABLE FROM AN ORDINARY LOCAL
ACTION, NOT JUST FROM A CONTRIVED FIXTURE.** `app/shared/site.py:14`'s
`block_remote_instance` guard, `if instance_id == 1:`, is a literal integer
comparison rather than a lookup against `Instance.is_local()` or similar --
and the campaign has repeatedly confirmed that literal is safe to treat as
"the local instance" because nothing in this codebase assigns any other
value to a local `Instance` row. The production route that makes the guard
reachable from an ordinary user action, not merely from a test that passes
`instance_id=1` directly, chains through three files: `app/post/routes.py:1480`
calls `block_remote_instance(post.instance_id, SRC_WEB)`; `post.instance_id`
is set once, at creation, in `app/shared/post.py:218` as
`instance_id=user.instance_id`; and a LOCAL user's `instance_id` is 1 by the
same construction this fact's sibling facts document for `Community` and
`User` rows built by this suite's own factories. So any post authored by a
local user, later "blocked" via this route, drives `instance_id == 1` through
an ordinary call chain -- the guard's trigger is not a fixture artifact, it is
the common case. See **D595**.

**260. AN ARGUMENT THAT IS `None` CANNOT PROVE AN OVERRIDE DRIVES THE
OUTCOME, BECAUSE `None == False` IS `False` AND BOTH TAKE THE SAME ARM --
TO PROVE AN OVERRIDE DRIVES THE OUTCOME, PASS THE VALUE THAT WOULD TAKE THE
OTHER PATH IF THE OVERRIDE WERE ABSENT.** `app/shared/community.py:398-399`
(`subscribe_community`) and `:445-446` (`favorite_community`) each
unconditionally recompute their `subscribe` parameter under `SRC_WEB`,
discarding whatever the caller passed. Two tests in
`tests/test_shared_community_membership.py` were written to demonstrate this
by passing `None` as `subscribe` and asserting the create path ran anyway --
but `if subscribe == False:` (`:403`/`:449`) treats `None` exactly like
`True`: `None == False` evaluates to `False`, so `None` takes the SAME arm as
the override's `True` result whether or not `:398-399`/`:445-446` ever run.
Deleting the override left both tests green. This is false-witness mechanism
(d) -- an input that takes the same path under both the mutant and the fix --
and it had gone unnoticed inside a docstring that explicitly (and
incorrectly) claimed the opposite. **The remedy generalizes**: to prove code
recomputes/overrides a value regardless of what the caller passed, the
argument must be the value that would produce a DIFFERENT, DISTINGUISHABLE
result if the override were deleted -- here, `False` (which the override's
absence routes to the delete/"did not exist" arm, while the override's
presence routes to the create arm) rather than `None` or any other value
that happens to compare unequal to `False` under `==`. Passing a value
merely because it "isn't `True`" is not enough; it must be a value the
surrounding code treats DIFFERENTLY depending on whether the code being
tested runs.

**NONE OF FACT 75'S CATALOGUED CAUSES FIT THIS SHAPE, AND NONE SHOULD BE
FORCED TO.** This is not cause 4 (there is no cause 4(c); causes 4(a) and
4(b) are about a guard whose own body or an enclosing invariant makes it a
tautology -- nothing here is a tautology, both arms of `:403`/`:449` are
genuinely reachable and genuinely distinguishable). It is not cause 8 (no
`try`/`except` is involved). It is not cause 7 either, despite `:399`/`:446`
being conditional-expression arms: cause 7 covers an arm that is
UNKILLABLE because its sibling's callee already computes the identical
value for every input; here both arms of the ternary compute genuinely
different, killable values, and the tests simply chose an argument (`None`)
that could not tell them apart. **The defect was reachable and simply
unasserted by a bad test choice -- neither configuration-scoped nor a void
`try`/`except`.** Say so plainly rather than mis-filing it under a cause
that does not describe it.

**261. A SUCCESS THAT DID NOT SUCCEED IS THE MIRROR IMAGE OF A REFUSAL THAT
DOES NOT REFUSE, AND IS EQUALLY INVISIBLE TO COVERAGE: ASSERT THE AUDIT
RECORD, NOT THE FLASH.** `app/shared/community.py:622`'s
`remove_mod_from_community` had an `if existing_member:` with no `else`.
Removing a moderator who held no `CommunityMember` row wrote nothing, yet
the function still flashed `'Moderator removed'`, still wrote a `ModLog`
row via `add_to_modlog('remove_mod', ...)` naming the target, and still
dispatched the `remove_mod` task -- every line executed on every run, so a
test asserting only the flash text or only "no exception was raised" would
have passed against the bug. Facts 256/257 and their sibling defects
(D576, D595, D596) established that a refusal's own flash firing is not
evidence the thing it refuses was prevented; this is the same blind spot
approached from the opposite direction -- a success's own flash and modlog
write firing is not evidence the thing it claims happened, happened. The
remedy generalizes the same way in both directions: for a refusal, assert
the state it exists to prevent (a row not created, not deleted); for a
success, assert the state it claims to have produced (here, a `ModLog` row
that actually exists, or -- as the inverted test does -- a query for it
returning zero). See **D611**, **D613**.

**262. WHEN A ROUTE AND A SHARED FUNCTION BOTH IMPLEMENT ONE INVARIANT,
THEY DRIFT, SO TEST THAT THEY AGREE RATHER THAN TESTING EITHER ONE ALONE.**
`app/community/routes.py:1477` (`community_remove_owner`) already refused
to strip a community's last owner, checking `community.num_owners() == 1`
before clearing the owner flag. `app/shared/community.py`'s
`remove_mod_from_community` did the identical flag-clear with no such
check at all -- reachable from a *different* route, removing a moderator
who happened to also be the sole owner. The codebase therefore
contradicted itself about whether an ownerless community is legal: one
path said no, the other said yes, and nothing before this round's fix
tested that the two paths agreed. A guard duplicated between a route and a
shared function is not one invariant maintained twice; it is two
invariants that happen to currently coincide, and only a test that checks
both paths against the same rule -- not a test of either path in isolation
-- will notice when they stop. See **D612**.

**263. A GREP FOR A FUNCTION NAME FINDS EVERY MODULE THAT DEFINES A
FUNCTION OF THAT NAME, SO CONFIRM THE IMPORT, NOT THE STRING.**
`/usr/bin/grep -rln "delete_community\|restore_community" tests/` returns
both `tests/test_shared_community_moderation.py` (the correct oracle for
`app/shared/community.py`'s Group C) and `tests/test_shared_tasks_deletes.py`,
whose `:55-58` imports `delete_community` and `restore_community` from
`app.shared.tasks.deletes` -- a different module's ActivityPub Celery
functions of the identical names. A grep-only check would have reported
Group C as already carrying a second oracle; reading the import line
instead showed it does not. This is the third grep-shaped oracle trap
caught in three consecutive rounds (fact 253's test-file-not-named-after-
its-module is the previous instance; before that, a match landed inside a
docstring rather than an import) -- common enough now to name as a class:
**a name match in `grep` output is a candidate, never a conclusion, until
the import statement is read.** See **D617**.

**264. `make_community_member` HARDCODES `is_owner=False`, SO ANY TEST
NEEDING AN OWNER MUST SET IT EXPLICITLY AFTERWARD, NOT PASS IT AS AN
ARGUMENT.** `tests/factories.py:384-394` takes `is_moderator` as a
parameter but writes `is_owner=False` unconditionally into the
`CommunityMember` it builds and returns. Every test in this round's
suite that needs an owner (e.g. `test_remove_mod_from_community_api_owner_alone_removes_and_returns_user_id`
and its `SRC_WEB` sibling) therefore calls `make_community_member(...)`
and then sets `<member>.is_owner = True` followed by `db.session.commit()`
as a second step -- there is no factory argument that produces an owner
row directly. A test that assumes passing some flag to the factory
produces an owner, without checking the factory's own body, will silently
build a plain moderator instead and exercise the wrong branch of any
owner-versus-moderator guard.

**265. A MUTANT KILLED ONLY BY AN UNCAUGHT EXCEPTION IS NOT KILLED UNDER
THIS CAMPAIGN'S RULES; FIND OR WRITE THE NON-CRASHING VARIANT BEFORE
BANKING THE SCORE.** Deleting `and community.num_owners() == 1` from
`app/shared/community.py:623` (leaving `if existing_member.is_owner:`,
refusing any owner's removal regardless of how many owners remain) was
killed by exactly one test in the 29-test suite that existed at the time
of the mutation pass, and only because that test used `SRC_API`: the
mutated guard's refusal path is `raise Exception(msg)`, which the test's
assertions never got a chance to run against because the exception
propagated uncaught. No `SRC_WEB` counterpart existed -- one that removes
an owner while a second owner remains -- to surface the identical mutant
as a plain `AssertionError`. The mutation pass flagged this itself rather
than reporting a clean score, and the remedy was one additive test,
seeded identically but routed through the web arm, where the same mutant
now produces `assert flashed == ['Moderator removed']` failing against the
last-owner refusal message instead -- a crash-free kill. **The check
generalizes**: before crediting a mutation as killed, confirm at least one
of its kills is an assertion failure, not merely that the process exited
nonzero -- an uncaught exception proves the mutant changed behaviour, not
that a test would have noticed had the SUT's own designed control flow
not happened to convert that behaviour change into a crash. See **D619**.

**266. FACT 75's CAUSE 3 IS CATALOGUED FOR CONJUNCTS; A SURVIVOR CAN ALSO
BE ITS DISJUNCTIVE DUAL, AND THE CATALOGUED PROSE DOES NOT YET SAY SO.**
Cause 3's text (`:2997-3001` above) reads "a later conjunct implies this
one" -- the shape `A and B` where a later B implies an earlier A, making A
redundant. `app/shared/community.py:494` and `:523`'s
`community.is_owner(user) or community.is_moderator(user) or
user.is_admin_or_staff()` is the mirror: `A or B or C` where the *earlier*
disjunct A (`is_owner(user)`) is proved, algebraically, to *imply* the
next one B (`is_moderator(user)`) -- `app/models.py:740`'s `is_moderator`
tests membership in a list (`:716-722`) already built from `is_owner OR
is_moderator`, and the `(user_id, community_id)` primary key rules out a
second row that could make the two diverge for the same user. So `A or B`
reduces to `B`, and it is the *earlier* term that is redundant, not the
later one -- same principle as cause 3, mirrored across the operator, and
proved the same way cause 3 demands: algebraically, from the two method
bodies, never from a mutant's silence. **Cause 6 does not apply**, checked
rather than assumed: cause 6 (`:3025`) opens with "the only cause on this
list that is not about a clause," and `community.is_owner(user) or` is
exactly a clause -- a disjunct. An earlier draft in this campaign cited
cause 6 anyway, and a scoped re-review certified it by quoting cause 6
with an ellipsis that elided the disqualifying sentence; the mistake cost
a fix round before a stronger-model re-review caught it. **This gap sits
beside two other registered-but-unenacted taxonomy extensions rather than
replacing either**: fact 252/D578 (a tautology's mirror image, under cause
4) and D589 (configuration-scoped unkillability, proposed as a possible
ninth cause). Until fact 75 itself is amended, cite this fact and cause 3
together rather than reaching for a number that does not exist. See
**D616**.

**267. A LOOKUP THAT FINDS A ROW IS NOT THE SAME AS A LOOKUP THAT FINDS THE
RIGHT KIND OF ROW -- WHEN A GUARD'S ERROR MESSAGE NAMES A ROLE, CHECK THE
QUERY FILTERS ON THAT ROLE.** `remove_mod_from_community`'s
`existing_member` lookup (`app/shared/community.py`) filtered on
`user_id` and `community_id` alone and used the result to decide whether
to refuse with "That user is not a moderator of this community." The
message names a role, `moderator`; the query does not check for one --
any `CommunityMember` row, including the bare, both-flags-False row
`join_community` creates for every plain subscriber, satisfied it. A
prior fix (`43b18996`) had already narrowed the guard's `if existing_
member:` with an `else`, closing the no-row case, and was recorded as
"FIXED" on the strength of that -- but a query finding *a* row is a
weaker fact than a query finding a row *of the kind the message claims
to check*, and nothing in that fix touched the query itself. **The
tell**: read the guard's refusal string before trusting the query above
it. If the string names a role, a status, or any other qualifier ("is
not a moderator," "is already banned," "is not the owner"), the query
that produced the value the guard tests must filter on that same
qualifier -- not merely on the identity/scope columns (here, `user_id`
and `community_id`) that get you *a* row. A `.filter(...).first()` or
`.one()` that reads as an existence check is not automatically a
membership-in-the-right-set check; verify the `WHERE` clause names
every condition the error message claims to be reporting on. Two more
of this shape follow from the identical root cause once named: the same
lookup's `community_id` predicate had no test giving a user membership
in two communities (fact 75 cause 2 -- an unmutated clause, not an
equivalence), and the two refusal branches' flash categories
(`'error'`/`'warning'`) had only one pinned via `with_categories=True`
in the whole file, so a swap between them was undetectable. See D621,
D622, D623.

**268. A DECOUPLING TABLE MUST BE KEYED BY BRANCH SITE, NOT BY CONDITION
NAME -- AGGREGATING SAME-NAMED CONDITIONS HIDES THE ESCAPE THE TABLE
EXISTS TO FIND, AND A SITE NO TEST REACHES MUST BE LEFT BLANK, NOT
MARKED `False`.** Four of five tasks in one round each shipped a
mechanism-(e) lockstep survivor -- a mutant conjoining two conditions
(`src == SRC_WEB`/`SRC_API` with a guard the round's own tests never
varied independently) that passed every test in the file. Each
implementer built a condition-decoupling table in good faith and each
still missed a gap, and the common cause was the table's own shape:
`app/shared/community.py`'s `invite_with_chat` has **three separate
`apply` checks** (`:145`, `:152`, `:161`), and a table with one row
labelled `apply` aggregates all three -- reporting "decoupled" because
some instance of `apply` varies independently of `src` somewhere in the
file, while the *specific* instance a given test set reaches (`:145`)
does not. **The fix, and the reason it matters**: build one row per
branch *site* (a source line, not a condition name), and mark each
cell from the actual test set -- `True`/`False` when a test reaches that
site with that value, and **blank, not `False`, when no test reaches it
at all**. The blank/`False` distinction is exactly where two of the
round's five escapes hid: a condition that looks "checked" in an
aggregated table can be a site with zero reaching tests on one side,
which is indistinguishable from "checked and found decoupled" unless
the table can say so. Once the table was rekeyed this way, it
immediately surfaced a third gap (`:161`) that neither the original
implementer nor the first reviewer had found by inspection -- the
method was wrong, not the people applying it. See D627.

**269. WHEN A GUARD'S CONDITION IS UNSATISFIABLE BECAUSE A CALLEE
INVOKED IN THE STATEMENT IMMEDIATELY BEFORE IT CANNOT RETURN THE VALUE
BEING TESTED, NO CATALOGUED FACT 75 CAUSE FITS -- CITE THIS FACT, PER
FACT 252's PRECEDENT, RATHER THAN FORCING A NUMBER.** `app/shared/
community.py:696-699` (`comm_flair_ap_format`) is `if not flair.ap_id:
ap_id = flair.get_ap_id(); if not ap_id: return`. `get_ap_id()`
(`app/models.py:4305-4313`) either raises `TypeError` (via `None +
str` inside `Community.local_url()`, `:800`) or returns a non-empty
string ending in `/tag/{id}` (`:802`) -- proved from the callee's own
body, over every branch of it, not sampled -- so it cannot return a
falsy value, and `:698`'s `if not ap_id:` is always `False` when
reached. **Discriminate this from its three nearest neighbours by
reading each one's own text, not by shape alone**: it is **not cause
3** (subsumption is about a later conjunct implying an earlier one
within one clause, not a callee's return-value proof); **not cause
4(b)**, whose establisher is explicitly *"a caller, or an enclosing
guard"* -- here the establisher is a **callee invoked at the guard
site**, a third kind of establisher fact 75's "NAME THE ESTABLISHER"
note does not yet enumerate; **not cause 6**, which opens *"the only
cause on this list that is not about a clause"* -- this is an `if`,
exactly a clause, and citing cause 6 here would repeat the mistake
fact 252 already records costing a fix round; **not cause 7**, which
is *"the only cause on this list that is about an ARM OF A CONDITIONAL
EXPRESSION"* -- `:696`/`:698` are plain `if` statements, not a
ternary. **This sits beside three other registered-but-uncatalogued
taxonomy extensions rather than replacing any of them**: fact
252/D578 (a tautology's mirror image stranding a False arm), D589
(configuration-scoped unkillability, a possible ninth cause) and fact
266/D616 (cause 3's disjunctive dual). Until fact 75 itself is amended
to add a establisher category for "a callee invoked at the guard
site," cite this fact and say plainly that none of the eight
catalogued causes fits, exactly as fact 252 already models for its own
shape. See D628.

**270. A PIN THAT ASSERTS ONLY AN EXCEPTION'S *TYPE* IS BLIND TO SIDE
EFFECTS THAT HAPPENED BEFORE THE EXCEPTION WAS RAISED -- ASSERT THAT
NOTHING WAS WRITTEN, TOO.** A bare `pytest.raises(SomeError)` proves
only that some statement raised `SomeError` somewhere in the call;
it says nothing about what ran, and what it wrote, in the statements
between the start of the function and the raise. `app/shared/
community.py:130`'s pin (`invite_with_chat`, missing community) is the
worked example: a mutant that moves the guard so the exception fires
one branch later than expected lets a `Conversation` row get created
and committed first, and a pin asserting only `pytest.raises(
NoResultFound)` still passes, blind to the spurious row -- a moderation
or audit-adjacent side effect that ran and was never observed, the
identical shape fact 214(a) names for `add_to_modlog`'s unconditional
commit. **The generalisable check**: whenever a pin covers a function
whose early lines might already have inserted, updated or dispatched
something before the line that eventually raises, add a query-based
assertion for the state the raise is supposed to have prevented (here,
`db.session.query(Conversation).count() == 0`) alongside the exception-
type assertion, not instead of it. This is the same principle fact
215 states for crash kills generally (a crash is evidence about *an*
observable difference, not about every observable difference) applied
specifically to the moment a pin is first written, before any mutation
pass has a chance to expose the gap by accident. See D625.

**271. AN ASSIGNMENT INSIDE ONE BRANCH OF A STRING-BUILDING FORK
(`message = ...`, REPLACING RATHER THAN `message += ...`, APPENDING) IS
ONLY PINNED BY ASSERTING THE EARLIER TEXT IS *GONE*, NOT BY ASSERTING
THE LATER TEXT IS *PRESENT*.** `app/shared/community.py`'s
`invite_with_chat` builds most of its message with `+=` (`:146`,
`:149`, `:153`, `:157`, `:159`, `:162`, `:165`), but its `:166-168`
`else` arm (unrecognised remote instance software) does `message =
render_template(...)`, which **replaces** the greeting `:141`/`:143`
already built rather than appending to it. Both the campaign's own
production template and `:141`'s greeting text happen to open with
similar prose ("Hi there,"), so a test asserting only `'expected
template phrase' in message` is satisfied whether the mutant that
turns `:167`'s `=` into `+=` is present or not -- the appended-onto
message would contain BOTH the old greeting and the new template text,
and a bare presence check cannot see the leftover. **The assertion
that actually kills that mutant is a negative one, naming the specific
phrase the earlier branch would have left behind if it had not been
replaced** -- `'check it out' not in message`, `:141`'s public-
community greeting phrase, with the fixture deliberately left non-
private so that specific phrase, not `:143`'s private-community
variant, is the one that would leak. **The general form: when a
branch's job is to REPLACE state a sibling branch builds, the pin must
assert the sibling's marker is absent, not merely that the branch's
own marker is present** -- presence-only assertions are invisible to
an `=`-for-`+=` swap by construction, because the swap's only visible
effect is something extra being there, never something expected being
missing. See `tests/test_shared_community_invites.py:1875-1924`.

**272. A FIXTURE THAT MINTS ITS ROWS IN LOCKSTEP MAKES `user.id ==
community.id` TRUE EVERYWHERE, AND THAT COINCIDENCE IS A PROPERTY OF
THE SEED HELPER, NOT OF THE SITES WHERE IT BITES -- SO FIX IT AT THE
SEED OR IT COMES BACK IN A FUNCTION YOU ALREADY "FIXED".**
`tests/test_shared_community_lifecycle.py`'s `_seed()` burns `User` id 1
via `_burn_a_seed()` and `Community` id 1 via a bystander community, then
mints the user under test and the community under test -- so both land
at id 2, and **every** assertion in the file that pairs a user id with a
community id is satisfied by a mutant that swaps them. In one round it
struck **four regions**: `make_community:270`'s `CommunityMember(user_id=,
community_id=)` construction, `:288`'s `return user.id, community.id`
tuple, and -- after the first two were fixed -- `edit_community:382`'s
`task_selector(..., user_id=, community_id=)` kwargs and `:391`'s `return
user.id`. **The decoy minted for `make_community`'s tests did not reach
`edit_community`'s**, proved by a diagnostic assertion that fired in one
function and survived in the other, which is exactly why this is a
property of the seed rather than three separate mistakes. **The remedy,
and both halves are load-bearing**: mint a decoy row first so the two
sequences desynchronise, **and** put a live `assert a.id != b.id` in the
test body -- not in a docstring, not in a comment -- so that a later
change to the seed fails loudly instead of silently restoring the
coincidence. The direct proof that the fix took is in the failure output:
`{'user_id': 2, 'community_id': 3} != {'user_id': 3, 'community_id': 2}`,
where the 2 and the 3 are the ids being visibly different. **Any test in
a file whose seed mints in lockstep inherits this**, so the check to run
when adding one is not "does my test pass" but "would it still pass if
the two ids were swapped". Sibling of fact 33 (the factory always
produces the matching value) one level up: here the factory is fine and
the *sequence* is what conspires. See D647 and D650, and mechanism (b), fixture
coincidence.

**273. A TEST WHOSE NAME CLAIMS A PROPERTY IT CANNOT OBSERVE IS WORSE
THAN NO TEST, BECAUSE IT SPENDS THE REVIEWER'S ATTENTION AS WELL AS
FAILING TO CATCH THE BUG.** `test_edit_community_task_selector_called_
with_real_ids_not_literal` asserted the **exact kwargs tuple** --
`assert calls == [(('edit_community',), {'user_id': s.user.id,
'community_id': s.community.id})]` -- which is about as strong as an
assertion looks, and it genuinely did catch the `community_id=1`
hardcoded-literal mutant its docstring described. It could **not** catch
an argument swap, because both sides of that comparison moved together
while `s.user.id == s.community.id` (fact 272). The docstring was not
wrong; **the name was broader than the docstring**, and a reader
skimming the file saw a test claiming the ids were real and distinct
when only "real" was ever checked. It was **renamed in place** to
`test_edit_community_task_selector_and_return_use_distinct_user_and_
community_ids` once the fixture was desynchronised and the return value
asserted too, so the name now states exactly what the body can witness.
**The check, and it costs one question**: for each claim in a test's
name, ask which assertion would fail if that claim were false -- and if
the answer is "none", either strengthen the body or narrow the name
before the test lands. A wrong name outlives a wrong report, because the
next reader greps for the property and finds a test that appears to
cover it. Same family as fact 268/D638's observability rule, one level
up: 268 is about what a decoupling *table* may record, this is about
what a test's *name* may claim. See D647.

**AND THE POSTSCRIPT THAT IS THE MOST USEFUL PART OF THIS FACT: THE
AUDIT THIS FACT PRESCRIBES WAS NEVER RUN OVER THE FILE THE FACT WAS
WRITTEN ABOUT.** The renamed test above was the one instance anybody
went looking for. The final whole-branch review of the same sub-project
ran the check across the rest of `tests/test_shared_community_
lifecycle.py` and found the same defect in the file's **two oldest
tests** -- `..._reads_all_ten_keys_and_authorises_user` and its web
sibling -- and proved it by execution rather than by reading: three of
the ten keys the first name claims are read had no assertion behind
them at all, and `app/shared/community.py:303` replaced by `pass`
left the test green. Three further names in the same file were broader
than their bodies. **A rule authored in a round is not self-applying to
the round that authored it**, and "we wrote the fact" is not evidence
that the fact was applied. When a round adds a naming rule, the same
round runs it over its own file, test by test, and says so. See D653.

**274. ASSERTING THE PERSISTED ROW CAN BE A FALSE CLOSURE WHEN A
DOWNSTREAM CALLEE REWRITES THE SAME COLUMNS -- CHECK WHAT RUNS BETWEEN
THE READ YOU ARE TESTING AND THE RETURN YOU ARE ASSERTING ON.** A fix
round set out to close a swapped-reads mutant at `app/shared/
community.py:220-221` and `:233-234` (`make_community` reading
`restricted_to_mods` and `local_only`) by asserting the two columns on
the final persisted `Community` row. **That test would have PASSED with
the mutant in place, and would have been a false closure**: `:282`
passes the very same `input` on to `edit_community`, whose own arm
re-reads the same two keys and **rewrites both columns at `:366-367`**,
so a swap confined to `make_community`'s reads is corrected before the
function returns. The only point at which those reads are observable is
the `Community(...)` construction at `:254-255`, so the closing tests
patch `edit_community` out on `app.shared.community` and observe what
was actually constructed: `assert [(False, True)] == [(True, False)]`.
**This is false-witness mechanism (a) in production form** -- asserting
on state that something else sets unconditionally -- and it is the same
blindness fact 271 names for a `=`-versus-`+=` fork, with a whole
function in place of a single statement. **It was found by EXECUTING the
fix, not by reasoning to it**, which is the transferable part: a fix
that is only argued for is a hypothesis, and this one would have shipped
a green test proving nothing while carrying a name that claimed
otherwise. **The check: before asserting on a persisted row, list every
write to that column between the code under test and the assertion --
including writes inside callees the function invokes on its way out.**
See D647.

**275. A MUTANT CAN BE CONJOINED-ONLY BY CONSTRUCTION, AND A PATCHED
HELPER THAT RETURNS A CONSTANT CANNOT WITNESS A CROSSING.**
`app/shared/community.py:310-311` is a pair of ternaries, `icon_url =
process_upload(uploaded_icon_file, ...) if uploaded_icon_file else None`
and the banner's twin. Every single-site variant **dies**: the `:310`
argument alone, the `:311` argument alone, the `:310` condition alone,
and each condition blinded to `True` and to `False` -- seven kills. Only
the **simultaneous** swap, each ternary taking the other's condition
*and* argument, survives. Two independent reasons, and both must be
fixed to close it: **each test supplied exactly one file**, so under the
crossing the icon test's single file is simply consumed by the banner
ternary and `process_upload` is still called once with the same
argument; **and the patched `process_upload` returned `None` regardless
of its input**, so the two resulting locals were indistinguishable even
in principle. The existing docstring's defence -- that the two tests
exercise both ternaries at both truth values "with the OTHER ternary
held at the opposite value each time" -- is true and **still not
enough**, because holding the other ternary at the opposite value in
*separate calls* never puts both files in one call, which is what a
crossing needs to be visible. The closing test supplies **both** files
in a single call and patches `process_upload` to return a value
**derived from its argument**
(`f'https://uploads.example/{uploaded_file}.png'`), so the two `File`
rows can be told apart: `At index 0 diff: ('FAKE_BANNER_FILE',
'communities') != ('FAKE_ICON_FILE', 'communities')`. **The general
rules. (a) When two sites consume two different inputs through the same
helper, the mutant that matters may exist only as a pair, so an
exhaustive conjoined sweep is not optional -- this was the campaign's
seventh mechanism-(e) lockstep gap and, unlike the other six, no
single-site mutant hints at it. (b) A stub whose return value does not
depend on its argument erases the very distinction a crossing test
exists to observe** -- make the stub's output a function of its input,
or the test is structurally blind however many assertions it carries.
Note also fact 87: coverage emits no arc for a ternary, so neither
figure can ever show this. See D647.

**276. MARK EVERY HAND-APPLIED MUTANT WITH A `# MUT` COMMENT, SO A
WORKING-TREE ARTIFACT IS SELF-IDENTIFYING AND GREPPABLE.** A mutation
pass edits production files in place and reverts them, and the window
between those two moments is where this campaign's recurring hazards
live: a background security scanner raising an alarm against a mutant
(twenty-two such false positives in a single round, D652), a revert that
refuses because its replacement text became non-unique, and the worst
case, a mutant that is never reverted and rides into a commit. Marking
each mutated line with a trailing `# MUT` costs nothing and turns "is
the tree clean?" from a question answered only by `git diff` into one
answerable by a **positive grep for a token that must not exist**:
`/usr/bin/grep -rn "# MUT" app/` returning nothing is an independent
second proof alongside `git diff --quiet -- app/` and
`git show HEAD:<path> | diff - <path>`, and the three fail in different
ways. The marker also makes a scanner alarm instantly triageable -- a
flagged line carrying `# MUT` is a mutation-window artifact by
construction. **This is a convention, not a measurement**: its support
is that this round's clean-tree proof used it as one of three
independent checks, not a demonstration that it caught something the
other two missed. Adopt it for the same reason one-mutant-at-a-time is
adopted -- a halted batch is recoverable, a stacked or committed mutant
is not.

**277. A FUNCTION THAT TAKES AN ID PARAMETER AND ALSO READS THE REQUEST
GLOBAL FOR THE SAME USER IS BROKEN ON EVERY NON-REQUEST CALLER. THE
PARAMETER IS THE ONLY VALUE IT MAY TRUST.** `_feed_add_community`
(`app/shared/feed.py:386`) takes `user_id`, uses it correctly at `:429`
(`CommunityMember.query.filter_by(user_id=user_id, ...)`), and then read
`current_user.feed_auto_follow` on the very next line. Two statements one
line apart disagreed about whose subscription was being decided. **This is
not a style point, it is a reachable 500**: the shared layer's `SRC_API`
arm resolves its user through `authorise_api_user(auth)` and never
establishes a `current_user`, so on that path `current_user` is an
`AnonymousUserMixin`, which has no `feed_auto_follow`, and the call raises
`AttributeError`. **The test that exposes it needs a request context with
NO logged-in user** -- `app.test_request_context('/')` supplies the
context; what must be absent is the user, and a test that logs someone in
"to make the fixture work" destroys the very condition it is testing.
**How to find the outlier rather than assert it**: grep the module for the
attribute and for `current_user` together, and separate the legitimate
reads from the defect by their position -- a read inside an
`if src == SRC_API: ... else:` dispatch is the web arm's proper source,
while a read in a function that has already been handed the id is the bug.
In `feed.py` that grep put the preference reads at `:49`, `:87`, `:135`,
`:456` and `:551` off resolved user objects and the remaining
`current_user` reads at `:120`, `:180`, `:257` and `:360` inside src
dispatches, leaving exactly one site unaccounted for. See D655.

**278. A GUARD THAT COMPARES TWO CALLER-SUPPLIED VALUES TO EACH OTHER HAS
NO AUTHORIZATION CHECK IN IT, HOWEVER MUCH IT LOOKS LIKE ONE.**
`app/feed/routes.py`'s `feed_add_community` read `user_id` from the query
string and then checked `if Feed.query.get(feed_id).user_id != user_id:
abort(404)` under a comment reading "make sure the user owns this feed".
Both sides are attacker-controlled, so the check constrains only their
relationship to each other, and an attacker supplying both can always
satisfy it: a victim's id plus one of that victim's own feed ids passes.
`@login_required` establishes only that *someone* is signed in. **The
diagnostic question is not "does this compare an identity?" but "how many
of these values could the caller choose?" -- if the answer is all of them,
there is no check present at any strength.** The repair is not a stronger
comparison but a different operand: stop reading the identity from the
request at all (`user_id = current_user.id`), so the route has no
caller-supplied identity left to be confused by, and then check every
OTHER caller-supplied id against it -- the bypass here had a second half,
a `current_feed_id` that was never ownership-checked at all and that
deleted `FeedItem` rows from feeds the caller did not own. **Two test
consequences.** (a) The inverted test must sign in as the LEGITIMATE
owner, so the first guard passes and the second one is actually reached;
signing in as the attacker makes the test pass for the wrong reason, which
is false-witness mechanism (d). (b) Note separately whether the route is a
`GET` -- if it is, everything above is also reachable by CSRF, and closing
the spoofing half does not close that. See D657, D664.

**279. TWIN FUNCTIONS THAT HAVE ALREADY DIVERGED MUST BE COVERED BY
SEPARATE TESTS, NEVER BY A SHARED HELPER -- AND THE DIVERGENCE CAN EXTEND
TO HOW THEY MUST BE *MOCKED*, NOT MERELY TO HOW THEY READ.** A shared
helper over a diverged pair hides the NEXT divergence, which is the whole
argument for writing two tests that look almost identical. `feed.py`'s two
announce tasks are the worked example:
`announce_feed_add_remove_to_subscribers:548` reads
`User.query.get(fm.user_id)` off the request session, while
`announce_feed_delete_to_subscribers:597` reads
`session.query(User).get(fm.user_id)` off the task session the function
itself opened. **The second-order consequence is the part that surprises,
and it is a stronger argument than the readability one**: a rollback test
that fully mocks `get_task_session` is harmless in the twin that reads its
user off the real session, but in the twin that reads through the mocked
session it makes `fm_user` a generic `MagicMock` whose `is_local()` is
truthy -- so the member is skipped, the delivery is never reached, and the
`pytest.raises` the test exists for cannot fire. The test passes and
proves nothing. The fix is a `query.side_effect` keyed on model class,
returning a real row per model. **So when covering a twin pair, do not
only ask whether the two read the same way; ask what each one's mock does
to the path under test, because a mock that is correct for one can be
silently path-destroying in the other.** See D663.

**280. ASSERT A `.delay` ARM AS *DISPATCHED*, NEVER AS EXECUTED.** A
function that forks on `current_app.debug` -- calling the task inline on
one arm and `task.delay(...)` on the other -- has two arms that must be
told apart, and under this suite's eager Celery `.delay()` RUNS THE TASK
INLINE. So an oracle that asserts the task's effects cannot distinguish
the arms: it holds on both, and the fork the test is named for is
unwitnessed. Patch the task object and assert the DISPATCH -- the full
argument tuple on `.delay`, plus `task.call_count == 0` to prove the
synchronous arm was not taken -- and drive each arm explicitly with
`patch.dict(app.config, {'DEBUG': ...})` rather than relying on the
default. **Two traps this round hit around exactly this.** (a) `DEBUG` is
NOT set in the test config; it appears in neither `tests/conftest.py` nor
`config.py`, so Flask's default `False` applies and any test relying on
the synchronous arm being the default relies on the opposite of the truth.
(b) `patch.object(app, 'debug', False)` DOES NOT WORK: `Flask.debug` is a
class property with no deleter and mock always `delattr`s a non-local
attribute on cleanup, so the context manager raises on exit. Use
`patch.dict(app.config, ...)`. **And count the arms before trusting a
test's name** -- a function with two `.delay` sites needs both driven, and
per-function coverage is what says so: a single dispatch test here left
`missing_lines=[407]` until the test was rewritten to drive the move path
as well. See D660, and facts 87 and 276 for the neighbouring measurement
traps.

**281. A COUNTING ORACLE CANNOT WITNESS IDENTITY: `call_count == 1` HOLDS
WHETHER THE RIGHT PARTY OR THE WRONG ONE WAS REACHED.** This was the
dominant test-authoring defect of sub-project 49 and it has a clean
exhibit. `announce_feed_add_remove_to_subscribers:549` skips the feed's
owner; inverting its `==` to `!=` leaves a test asserting only
`send.call_count == 1` PASSING in isolation, because both members are
remote and exactly one send happens under either reading -- owner skipped
and non-owner sent (correct), or owner sent and non-owner skipped (the
bug). The mutant dies only when the whole file runs, caught by unrelated
tests, which is fix-catching and not a unique kill. **The exhibit:** after
the repair, under the same mutant, the isolated run fails at
`send.call_args.args[0] == other_instance.inbox` while the line
immediately above it, `send.call_count == 1`, SILENTLY PASSES. Two
adjacent lines, one run, one verdict each. **The rule: whenever a test's
name contains "skips", "only", "for the right", or any other word naming a
PARTY rather than a QUANTITY, the assertion must name that party too.**
**And make the discrimination structural rather than lucky**: insert two
rows with distinct hardcoded values and live-assert both their ids and
their distinguishing fields differ BEFORE exercising the code, so a
fixture change that collapses them fails loudly at setup instead of
degrading the test quietly back into a counting oracle. See D658, and fact
272 for the lockstep-fixture version of the same blindness.

**282. ASSERT THE CREDENTIALS, NOT ONLY THE PAYLOAD.** A federated
delivery call carries its signing identity in arguments that are not the
body -- `send_post_request(inbox, activity_json, private_key, key_id,
...)` -- and a test that inspects only `activity_json` leaves `args[2]`
and `args[3]` free. Sub-project 49 found all three of `feed.py`'s
`send_post_request` sites covered to `[]`/`[]` with the credentials
asserted at exactly ONE: swapping `feed.private_key` for `user.private_key`
and `feed.ap_profile_id` for `user.ap_profile_id` survived at two of them,
and at the third the destination inbox survived being swapped too. **The
sharpest form of the defect is a test whose own docstring names the
distinction it fails to check** -- one there stressed "the actor is the
USER, not the feed" and then asserted only the JSON body, which is the
very place that distinction is NOT carried. A regression signs an activity
with the wrong key and ships silently under a 100%-in-range coverage
figure. **This is the assertion-strength gap the campaign holds to be more
dangerous than an execution gap, precisely because the coverage number
says covered.** Two working rules. (a) For every outbound call, enumerate
the arguments and ask which are asserted -- destination, body, key, key
id -- rather than assuming the interesting one is the body. (b) **A gap
found at one member of a diverged twin pair is a hypothesis about the
other member**: when `:605`'s gap was found, the prediction that `:561`
carried it with the operands reversed was written into the next task's
brief rather than chased immediately, and the mutation pass confirmed it
and found the same gap at an unexamined third site. Pin the control by
construction -- both factories default `private_key` to `None`, so the
test must set both sides to distinct non-`None` literals and live-assert
they differ before patching. See D658.

**283. `all([])` IS `True`, SO AN `all(...)` ASSERTION OVER A CALL LIST
PASSES VACUOUSLY WHEN NO CALL WAS MADE -- COMPARE THE LIST INSTEAD.**
`assert all(c.kwargs['joined_via_feed'] for c in sub.call_args_list)`
holds when `call_args_list` is empty, so it passes both when every call
was right and when the code never ran at all -- the two outcomes a
mutation-hunting assertion most needs to separate. Write
`assert [c.kwargs['joined_via_feed'] for c in sub.call_args_list] ==
[True]`, which pins the value AND the number of calls in one comparison
and fails informatively on either. The same trap shape covers `any()`
(`False` on empty, so a negative assertion built on it is the vacuous
one), a `for` loop whose body holds the only assertion, and a generator
expression passed to `assert` at all. **The general form: an aggregate
over an empty sequence has a defined value, and it is usually the value
that makes your assertion pass.** Reach for a list comparison, or assert
the length first. See D659, and fact 281 for the neighbouring case where
the count is present but names no party.

**284. A TEST FILE NAMED AFTER A MODULE IS NOT EVIDENCE IT COVERS IT, AND
PARTIAL COVERAGE IS A BETTER DISGUISE THAN NONE -- MEASURE THE ORACLE WITH
PER-TEST CONTEXTS.** Before sub-project 50, `join_feed` showed 23 executed
lines and `delete_feed` 9, which reads as "partly tested". Every one of those
lines was executed by `tests/test_redirect_back.py`, whose subject is `back()`'s
referrer policy; not one of its eleven tests asserts anything about feeds.
`leave_feed` and `make_feed` had never been executed at all. Ten test files
carry "feed" in the name and none of them touched the module. **The recipe,
since `.coveragerc` is tracked and must not gain `dynamic_context`:** write an
untracked `.coveragerc.ctx` with `dynamic_context = test_function` under
`[run]` and `show_contexts = True` under `[json]`, give its `[json] output` a
path that is **not** `coverage.json` (or it overwrites the report the floors
check reads), run `./run_tests.sh tests/ -q --cov=app.shared.<module>
--cov-context=test --cov-config=.coveragerc.ctx --cov-report=json`, and read
`files[<path>]["contexts"]` per line. Delete the config afterwards. The tests
that turn up incidentally are also the round's regression tripwire: run that
file on its own at the end rather than trusting the full suite to surface it.
See D677.

**285. A MUTANT THAT WIDENS A QUERY IS OFTEN INVISIBLE IN THE ROWS AND VISIBLE
ONLY IN A COUNTER.** Dropping the `community_id` filter from
`_feed_remove_community`'s member sweep makes it iterate every
`CommunityMember` row in the database -- and changes nothing about which rows
are deleted, because the delete inside the loop names the community explicitly.
The closing test D661 prescribed asserted memberships and survived. What the
wider sweep actually changes is `community.subscriptions_count`, decremented
once per qualifying member, so the counter is the assertion that kills it.
**The general form: when a filter drop looks equivalent, look for the
per-iteration SIDE EFFECTS -- counters, cache busts, dispatched tasks -- rather
than only the rows the statement names.** See D680.

**286. A CLOSING TEST CARRIED FORWARD FROM ANOTHER ROUND IS A HYPOTHESIS UNTIL
IT HAS BEEN RUN.** Sub-project 49 registered twenty surviving mutants with a
closing test written out for each, derived from the mutant rather than guessed.
Seven of the eight were never executed, and when sub-project 50 ran them,
**three were wrong**: one (item K) failed against the unmutated tree, three
needed a `local=True` user because `make_user` leaves `ap_id` set and the guard
under test opens with `user.is_local()`, and one survived its own mutant (fact
285). The author of a recipe is exactly the person who cannot see what it
assumes. **Run every inherited recipe against the unmutated tree first, then
against its mutant, before recording it as closed.** See D680.

**287. A FUNCTION THAT ENDS IN `finally: db.session.remove()` DETACHES
EVERYTHING ITS CALLER HOLDS -- RE-QUERY, AND EXPECT THE ROLLBACK ABOVE IT TO BE
UNTESTABLE.** `app/shared/feed.py`'s `join_feed` does this. A test that reads a
factory row after calling it gets `DetachedInstanceError`, so every assertion
has to go through a fresh query and any value needed afterwards (an id, a
`public_url()`) must be captured before the call, inside the request context
that `current_app.config` lookups need. The same `finally` also makes the
`db.session.rollback()` in the `except` arm unobservable from outside: the
session is discarded on every path, so pending state is gone whether or not the
rollback ran, and its mutant is unkillable rather than merely unkilled. See
D679.

**288. A GREP FOR A FUNCTION'S NAME IS NOT AN ORACLE -- THE MENTION-ONLY TRAP.**
Before sub-project 51, `/usr/bin/grep -rln "edit_feed" tests/` returned four
files and **none of them called it**: three mentioned it in prose inside
docstrings about neighbouring functions, and the fourth was about `feed_edit`,
the route with a similar name. Per-test coverage contexts reported **zero**
contexts for the whole function. The previous trap shape (fact 284) was a test
file named after a module; this one is worse, because the grep that would
normally correct that mistake produces hits too. **The rule: an oracle is a
test that EXECUTES the code, and only a contexts run or a deliberate deletion
proves one exists.** See D688.

**289. AN UNFILTERED `.delete()` IS INVISIBLE TO A FIXTURE THAT HOLDS ONE ROW,
AND THAT IS THE SHAPE THAT DESTROYS OTHER PEOPLE'S DATA.**
`edit_feed` ran `db.session.query(FeedMember).filter(FeedMember.is_owner ==
False).delete()` with no `feed_id` filter, so making one feed private
unsubscribed every non-owner member of **every** feed on the instance. Every
test that could have caught it would have needed a second feed in the fixture,
and none existed. The companion lines were the same shape: a delete filtered by
the wrong id, and a `count()` that was global and read `0` only because the
delete above it had just emptied the table. **Three working rules.** (a) For any
`delete()` or bulk `update()`, name the rows it must NOT touch and put one in
the fixture. (b) A count assigned to a row's own column is part of the same
statement -- scope both or neither, because scoping one alone turns a
globally-wrong number into another row's number. (c) When the delete and the
count read the same table, a test that asserts only the count can pass while the
delete is wrong, and vice versa: assert both. See D689.

**290. ASSIGNING A FOREIGN KEY ATTRIBUTE LOSES TO A LOADED RELATIONSHIP AT
FLUSH.** `feed.icon_id = file.id` followed by `db.session.delete(old_file)` left
the feed with `icon_id` **None** and the new `File` orphaned, because
`Feed.icon` is declared `single_parent=True, cascade="all, delete-orphan"` and
the loaded relationship still pointed at the old row. The banner arm, declared
without a `backref`, survived the identical sequence -- so the two arms of one
function disagreed. **Assign through the relationship (`feed.icon = file`) when
one exists, and do any disk cleanup BEFORE the assignment, because the
delete-orphan cascade removes the old row as soon as the new one is attached.**
The defect was found by a test asserting the new file's `source_url` and
getting `AttributeError: 'NoneType' object has no attribute 'source_url'`; no
amount of reading the block would have shown it. See D691.

**291. `g.site` IS NOT POPULATED INSIDE A BARE `test_request_context`.**
`before_request` -- which sets it in the real app -- does not run there, so any
production code reading `g.site` raises unless the test assigns it:
`make_site()` first, then `g.site = Site.query.get(1)` inside the context. The
row's own switches also default off (`enable_nsfw`, `enable_nsfl`), so a test
that wants to observe a guarded write has to turn them on and say which way it
is testing. See D688 and D698.

**292. A ROUTE TEST CANNOT RENDER THESE TEMPLATES -- PATCH `render_template`
AND ASSERT ON THE FORM IT WAS HANDED.** Any GET that re-renders a feed form
raises `jinja2.exceptions.UndefinedError: 'app.feed.forms.AddCopyFeedForm
object' has no attribute 'csrf_token'`, because the test config disables CSRF
for forms while the template asks for the field. Patch
`app.<blueprint>.routes.render_template`, keep the `form` kwarg, and assert on
its fields. **That is not a workaround, it is the only way to see a whole class
of defect**: sub-project 52's NSFL pre-fill bug lives entirely in the form
object a GET hands the template, and no assertion on a response body would have
caught which column it read. See D701.

**293. A POST ROUTE TEST NEEDS A REAL CSRF TOKEN, AND A DISABLED INPUT SUBMITS
NOTHING AT ALL.** `app.utils.login_required` calls flask_wtf's `validate_csrf`
directly on every POST and ignores `WTF_CSRF_ENABLED`, so a POST test mints a
token/session pair (`tests/test_redirect_back.py:37-48` is the helper). And
when a route disables a widget, the browser omits the field entirely: the form
sees `None`, not `''`. `EditFeedForm.validate` guards its required-field check
with `if self.url.data is not None` for exactly that reason, so a test that
posts an empty STRING gets 'This field is required.' and never reaches the
route's branch. Post the field ABSENT to exercise the disabled-widget path. See
D700.

**294. A FORM FIELD THAT IS REQUIRED CAN DRAG A NETWORK CALL INTO A ROUTE
TEST.** `AddCopyFeedForm.communities` is required, and a real value sends
`form_communities_to_ids` into `search_for_community`, which attempts a live
webfinger and trips respx with `AllMockedAssertionError`. Patch
`app.shared.feed.form_communities_to_ids`. **The general rule: before writing a
POST test, read the form's own validate() -- what it demands decides what the
test has to stub, and a required field is a dependency the route never
mentions.**

**295. `render_kw = {'disabled': True}` IS NOT A SERVER-SIDE RULE, AND THIS
CAMPAIGN HAS NOW FOUND THREE.** D675 (`is_instance_feed` on create), D696 (the
rename guard) and D702 (NSFW/NSFL on create) are the same defect three times: a
route disables a widget for users who may not set a field, and nothing checks
the submitted value. **When a route sets `render_kw` for a permission or a
policy, look for the matching check in the handler; when writing one, put the
rule in the handler and let the widget follow it.** The test that catches it is
a POST carrying the field the widget would have hidden.

**296. A REPAIR REACHES ONE SITE; THE DEFECT MAY HAVE COPIES.** Sub-project 53
found three defects still live in `feed_copy` that this campaign had already
repaired elsewhere -- `is_instance_feed` taken from the caller (D675, fixed in
`make_feed`), NSFW/NSFL taken past the site's switches (D702, fixed in
`feed_new` one round earlier), and an NSFL pre-fill reading the NSFW column
(D701, fixed in `feed_edit` -- the same two-word slip, verbatim). `feed_copy` is
the FOURTH implementation of "create a feed" in this codebase and reaches none
of the shared code. **When you repair a defect, grep for its SHAPE before
closing it**: the field name (`is_instance_feed=`), the pair that should have
been guarded (`nsfw=` with no `g.site` read nearby), the two columns that look
alike (`.nsfl` beside `.nsfw`). Then say in the register which sites you
checked, so the next round knows whether "fixed" meant one site or all of them.
See D712.

**297. A FILE INPUT IS FALSY WHEN NOTHING WAS PICKED, SO THE FILENAME TEST
BESIDE IT IS USUALLY REDUNDANT.** `werkzeug.datastructures.FileStorage.__bool__`
returns `bool(self.filename)`, so `if part and part.filename != '':` has a
second operand that can never change the answer -- an equivalent mutant at every
such site, and this codebase has several. A browser posts the part whether or
not the user picked anything, so the empty-filename case is the ORDINARY one and
still needs a test; what it cannot do is kill a mutant that drops the redundant
half. Assert `save_*_file` was not called, and record the equivalence rather
than chasing it. See D717.

**298. A ROUTE THAT READS `request.files[...]` DIRECTLY DEMANDS A MULTIPART
BODY.** A urlencoded POST to such a route is a bare 400 from
`BadRequestKeyError`, before any form logic runs -- so a route test has to send
the file parts even when it is testing something else entirely. Send them empty:
`{'icon_file': (io.BytesIO(b''), '')}`. The first version of sub-project 53's
copy tests posted urlencoded and got 400s that looked like validation failures.
See D718.

**299. A ROUTE'S DECORATORS RUN BEFORE ITS BRANCHES, INCLUDING WHEN YOU CALL
THE FUNCTION DIRECTLY.** `show_feed` is decorated with
`login_required_if_private_instance`, which reads the SAME `CONTENT_WARNING`
setting the route branches on -- so setting it in `app.config` to reach those
branches makes the decorator redirect to `/content_warning` first, and calling
`show_feed(feed)` from a test does not help, because the decorators live on the
function object. **Patch the config at the ROUTE MODULE'S binding**
(`patch('app.feed.routes.current_app', SimpleNamespace(config=..., debug=...))`),
which leaves the decorator's view of the setting alone. Three other decorator
facts from the same round: `Site.private_instance` defaults **True**, so an
anonymous route test measures the login redirect unless the fixture opens the
instance; `validation_required` needs `user.verified`; and `approval_required`
needs a non-None `user.private_key`. See D729.

**300. THIS TEST CLIENT DELIVERS NO COOKIES.** *(CORRECTED by fact 314 --
the jar works when its domain is the app's SERVER_NAME. The rest of this entry
stands.)* Neither
`client.set_cookie(...)` nor a `Cookie:` request header reaches
`request.cookies` -- a throwaway probe route that echoed `dict(request.cookies)`
returned `{}` for both. Any route branch gated on a cookie (`low_bandwidth`,
`warned`) therefore cannot be reached through the client, and the branch has to
be driven another way: patch what the route reads, or patch the module-level
object it reads it from. Do not spend an afternoon on the cookie. See D729.

**301. AN UNTESTED ROUTE IS WHERE THE CHEAP DEFECTS LIVE.** `feed_list` is
twelve statements and carried three: it filtered on a `user_id` taken from the
query string with no ownership check and no `public` filter, so any logged-in
account could read any other account's private feed titles; three unguarded
`int()` calls made a request without arguments a 500; and a user-supplied feed
title went into returned HTML unescaped. **A route that builds its own HTML and
reads its own query parameters is worth reading line by line before writing a
single test** -- all three were visible in the first five lines, and all three
had been there since the route was written. See D725, D726, D727.

**302. PATCH `sleep` WHEREVER A RETRY LOOP CAN REACH IT.**
`app/feed/util.py`'s `search_for_feed` retries a failed webfinger after
`sleep(randint(3, 10))` -- on the request thread -- so a test that reaches that
path pays up to ten seconds unless it patches `app.<module>.sleep`. Patch it in
every test that can reach the loop, not only the one that tests the retry: the
give-up path runs the same sleep. See D738.

**303. A `pytest.raises(..., match=...)` IS A REGEX SEARCH, SO A PREFIX
MATCHES.** `match='quiet.example is blocked.'` passes against
`'quiet.example is blocked. Reason: None'`, which is exactly what a mutant that
always appends the reason produces -- so the assertion could not tell the two
apart and the mutant survived. **When the difference between right and wrong is
a SUFFIX, assert the string, not a match**: capture with
`pytest.raises(Exception) as exc` and compare `str(exc.value)`. See D736.

**304. A UNIQUE COLUMN DECIDES WHICH FIXTURES ARE POSSIBLE, AND THEREFORE HOW A
FILTER CAN BE TESTED.** `Feed.name` is unique, so the obvious fixture for
"a lookup filtered on `ap_id=None` must not return a remote feed" -- a local
feed and a remote feed with the same name -- is an `IntegrityError`. The filter
is observable only as the difference between answering with the remote row and
answering with nothing, which needs the remote row to be the ONLY one with that
name. **Read the model's constraints before designing the decoy**; the campaign
has now built the impossible fixture twice. See D737.

**305. `.all()` ON A RAW `db.session.execute` RETURNS `Row` TUPLES, SO
`some_id in rows` IS ALWAYS FALSE.** `app/chat/routes.py`'s `new_message`
compared `current_user.id` against rows like `(2,)`, so the guard that existed
to redirect a pair back into the conversation they already had could never be
true, and every visit to the form made another conversation. The probe is one
line -- `PROBE p2 rows: [(2,), (3,)] alice.id in rows: False`. **Take the column
off the row (`{row.user_id for row in rows}` or `.scalars().all()`) before
comparing**, and treat any `in` test against a raw-SQL result as suspect until
the element type is checked. See D742.

**306. A FLASK VIEW THAT FALLS OFF THE END RETURNS `None`, WHICH IS A 500 AND
NOT A REFUSAL.** `TypeError: The view function ... did not return a valid
response. The function either returned None or ended without a return
statement.` Three routes in `app/chat/routes.py` guard with
`if current_user.is_admin() or conversation.is_member(current_user):` and have
no `else`: `chat_conversation` returns `''`, and `chat_options` and
`chat_report` return nothing at all. **A test that only asserts "not 200" will
pass on both**, so assert the status the refusal is meant to have. See D745.

**307. `trustworthy_account_required` FAILS A FRESHLY BUILT FIXTURE USER.**
`User.trustworthy()` is false for an account created within 7 days whose
reputation is under 100 (`app/models.py:1286-1291`), and `make_user` creates
the account now -- so a test of any route carrying that decorator measures the
redirect to `/auth/not_trustworthy` rather than the route. **Age the account**
(`user.created = utcnow() - timedelta(days=30)`) or give it reputation 100. The
sibling `can_send_pm_to` uses `created_very_recently`, one DAY, so the same
ageing satisfies both -- which is why a test of the `can_send_pm_to` refusal
has to reach for something else, such as reputation at or below -10. See D741.

**308. THIS CODEBASE HAS TWO NOTIONS OF ADMIN AND A ROUTE TEST OFTEN NEEDS
BOTH.** `User.is_admin()` (`app/models.py:1259-1265`) is id == 1 or a role
literally NAMED `'Admin'`. `Site.admins()` (`app/models.py:4007-4012`) and the
`g.admin_ids` the request hook computes (`app/request_hooks.py:99-106`) ask
instead for a role whose ID is `ROLE_ADMIN`, the constant 4, and never read the
name. A fixture role satisfying only the first passes an `is_admin()` gate and
then leaves `Site.admins()` returning the id-1 seat alone -- so a test of a
route that notifies admins silently measures one notification instead of three.
**Build the role with both**: `Role(id=ROLE_ADMIN, name='Admin')`. And remember
user 1 is an admin by id alone, so a seed that burns the id-1 seat has an admin
in it whether it meant to or not. See D753.

**309. A MUTANT PATTERN THAT IS A SUBSTRING OF A DEEPER-INDENTED COPY MATCHES
TWICE, AND THE HARNESS MUST REFUSE IT.** `app/chat/util.py` carries the same
guard at eight spaces in `update_message` and at sixteen in `send_message`, and
the eight-space text is a substring of the sixteen-space line -- so a mutation
harness replacing "the" occurrence silently mutates the wrong function, or
both. The `assert source.count(old) == 1` in the harness is what catches it;
anchor the pattern with a leading newline when two copies differ only by
indentation. Three of this campaign's mutation rounds have now hit it.

**310. A MUTANT THAT NOTHING CAN KILL IS AN EQUIVALENT MUTANT, AND THE ANSWER
IS A PROOF, NOT A TEST.** Three of sub-project 58's survivors could not be
killed by any fixture: `http_status_code = 404` on a path whose retry gate
rejects both `None` and `404`, and two header-name branches that werkzeug's
case- and underscore-tolerant lookup makes indistinguishable (`CONTENT_LENGTH`,
`Content-Length` and `content-length` all returned `'2'` from the same
request). **Before writing a test to kill a survivor, ask whether the two
programs differ at all**; if they cannot, register the equivalence with the
probe that shows it, the way a proved-unreachable arc is registered. Chasing an
equivalent mutant with a fixture produces a test that asserts an
implementation detail and nothing else. See D766.

**311. THE INBOX'S FIRST GATE CATCHES ONE EXCEPTION TYPE, SO ANYTHING ELSE
RAISED INSIDE IT IS A 500 TO A REMOTE HOST.** `app/activitypub/routes.py:687-691`
wraps `HttpSignature.precheck` in `except VerificationFormatError`. Any other
exception a helper raises there -- `ValueError` from a date parser, `TypeError`
from a naive-vs-aware comparison -- escapes as a traceback. **When covering a
function called from a gate like that, test what it raises for a peer's
malformed input, not only what it returns for well-formed input**, and assert
the exception TYPE: the unrepaired code raises too, so a `pytest.raises(
Exception)` pin proves nothing. See D762.

**312. A `Community` OR `Feed` WITH `ap_id` None IS LOCAL, WHATEVER ITS
INSTANCE SAYS.** `Community.is_local()` and `Feed.is_local()`
(`app/models.py:1848`, `:3206`) are `self.ap_id is None or
self.ap_id.startswith(SERVER_URL)` -- the instance_id is never consulted. So a
fixture that sets `instance_id` to a peer and `ap_profile_id` to a remote url,
but leaves `ap_id` None, is a LOCAL actor: every `if not actor.is_local()`
path skips it and the test measures nothing. `User.is_local()`
(`app/models.py:1251`) has the same shape. **Give a remote Community or Feed
an `ap_id`**, not only a profile url. See D772.

**313. `CACHE_TYPE = 'NullCache'` MAKES EVERY CACHE-FLAG GUARD INVISIBLE.**
`cache.set` is a no-op and `cache.get` always answers None
(`tests/conftest.py:68`), so a guard of the shape "if this flag is not set,
set it and do the work" does the work every time and its de-duplication cannot
be observed. **Patch the module's `cache` to test it**, and assert the WRITE as
well as the read -- a guard that reads a flag nothing ever sets passes the
read-only half of the test. See D772 and fact 276's neighbourhood.

**314. THE TEST CLIENT'S COOKIE JAR WORKS -- WITH THE RIGHT DOMAIN. CORRECTS
FACT 300.** `client.set_cookie('low_bandwidth', '1')` defaults the domain to
`localhost`, while the client requests the host in `config['SERVER_NAME']`,
`test.piefed.local` -- so the cookie never matches and `request.cookies` is
empty, which is what fact 300 recorded. **Pass the domain**:
`client.set_cookie('low_bandwidth', '1', domain='test.piefed.local')` and the
route sees it. A `Cookie:` header and an `environ_base={'HTTP_COOKIE': ...}`
injection are both still dropped -- the jar overwrites them -- so the jar is
the only way in. See D779.

**315. A MANUAL SESSION LOGIN DOES NOT TAKE AFTER AN ANONYMOUS REQUEST IN THE
SAME TEST.** Give each authorisation phase its own TEST. Measured: two
logged-in clients in one test are fine, and a login-first client is fine, but
once an anonymous request has been served, a client created afterwards and
given `sess['_user_id']` still reads `current_user.get_id() is None` inside the
route -- and the test then measures the anonymous path twice while looking
like it measures both. Flask-Login's `session_protection` is `'basic'` here, so
that is not the cause and the cause is not established; the shape is. See D778.

**316. THE SAME PAGINATION GUARD IS REDUNDANT IN ONE MODULE AND LOAD-BEARING IN
ANOTHER, AND ONLY THE PAGE'S BASE TELLS THEM APART.** `if paginated.has_prev and
page != 1` appears in several route modules. flask-sqlalchemy's paginator is
**1-based**, so where the page handed to `paginate()` is the one the url
carries, `has_prev` is already False on page 1 and the second operand decides
nothing (`app/domain/routes.py`, proved in sub-project 61). Where a **0-based**
page reaches the same paginator, page 0 and page 1 both render page 1 and the
guard suppresses a link that should exist (`app/topic/routes.py`, D782).
**Before writing a test for that expression -- or a mutant against it -- find
out which base the route uses**; a fixture built for the wrong one proves
nothing either way. See D788.

**317. THE TEST DATABASE IS BUILT BY MIGRATIONS, SO `create_all`-ONLY SQL IS
MISSING FROM IT.** `make_searchable` (`app/__init__.py:82`) registers
sqlalchemy_searchable's helper functions as a `before_create` DDL listener, so
they exist in a database built by `create_all` and **not** in one built by
`flask db upgrade` -- which is how `run_tests.sh` builds this one. Every
`.search()` call therefore died with `psycopg2.errors.UndefinedFunction:
function parse_websearch(unknown) does not exist`. `tests/conftest.py` now
installs them once per session. **The general shape is worth remembering: any
SQL the app creates through a create_all hook is absent here**, and its absence
shows up as a database error rather than as a missing fixture. See D791.

**318. A RANKING ASSERTION IS AN ORDERING ASSERTION, AND TIES BREAK BY ID.**
A row asserting that the better full-text match comes back first passed alone
and failed in the full suite: two documents containing the same lexeme can rank
EQUAL, and Postgres then returns them in whatever order the plan produces --
here, id order. **Assert the route's own decision** (the `sort` argument it
passes) rather than the order the database chose, unless the fixture makes the
ranks provably different. The full-suite run is what caught this; the scoped
run was green. See D797.

**319. `assert wanted in results` TESTS THAT A FILTER ADMITS, NEVER THAT IT
EXCLUDES.** Sub-project 63's five state-filter rows each asserted that the
right instance was among the results. Four mutants dropping half a filter
(`online` and `dormant` each losing `gone_forever == False`, `gone_forever`
losing its condition entirely) survived behind that single weakness: the wanted
row was still there, alongside everything the filter should have removed.
**Assert the whole result set** -- `sorted(results) == sorted(expected)` --
whenever the point of the code under test is to leave something out. Doing so
usually forces the fixture to become honest too: here it had to admit that the
LOCAL instance is online as well. See D803.

**320. WHEN THE CODE UNDER TEST DECIDES WHAT IS LOGGED, THE LOG IS THE
OBSERVABLE.** Sub-project 64's rows for a debug flag asserted that a hook still
registered under each spelling of `FLASK_DEBUG` -- and it does, whatever the
flag says, because the flag controls **logging**. Four mutants narrowing the
accepted spellings survived behind that single misplaced assertion, and three
more survived where two code paths differ only in the message they log
("this plugin has no __init__.py" versus "this plugin failed to import"): both
end in the same return value, and only the log says which happened.
**Use `caplog`** -- `caplog.set_level('WARNING', logger='app.plugins')` and
assert the message -- whenever the branch under test chooses between messages
or decides whether to log at all. See D809.

**321. A ROW THAT RAISES BEFORE REACHING THE CODE IS NOT A KILL.** Sub-project
65 wrote two rows to kill the mutants dropping `and form.validate()` from a
button guard, by posting a forged CSRF token. Both passed -- and neither
reached the route: `login_required` (`app/utils.py:1979`) validates the token
itself and RAISES, so the view never ran and the mutants were equivalent all
along. The scoped run hid it because the raise still failed the mutated code
for an unrelated reason; **the full-suite run is what exposed it.** When a
mutant dies, check that the test reached the mutated line -- a `pytest.raises`
or a 500 in the row that "kills" it is the signal to look. See D818, and
fact 320's neighbourhood: both are about assertions that measure something
other than the code under test.

**322. A SECOND REQUEST IN THE SAME TEST IS ANSWERED AS THE FIRST REQUEST'S
USER.** `tests/conftest.py`'s `app` fixture yields inside
`with application.app_context()`, so an app context is pushed for the whole
session, and Flask reuses an already-pushed context rather than making a fresh
one per request. `g` therefore outlives the request -- and flask_login caches
the authenticated user in `g._login_user`. A row that logs in, requests a page,
then builds a **brand new `test_client` with no cookies** and requests the same
page anonymously gets the LOGGED-IN answer: measured in sub-project 66, where
the anonymous half of a moderation-ids row returned the moderator's `[1]` until
`del g._login_user` preceded it. It does not leak BETWEEN tests -- `g` is clean
at the start of the next one -- so the fix is to **split the two requests into
two tests**, which is what that round did. Reach for `del g._login_user` only
when one test genuinely has to see both.

**323. FEEDGEN PRINTS ENTRIES IN THE REVERSE OF THE ORDER THEY ARE ADDED, AND
DROPS A NAME-ONLY AUTHOR.** `fg.add_entry()` PREPENDS by default, so a route
that queries `desc(posted_at)` and adds entries in that order publishes its
channel oldest-first. Every RSS route in this repo has the shape. A row
asserting "the newest post is first in the body" therefore fails against
correct code; assert instead on **which** posts the query selected -- with more
than the limit in the database, so an `asc` mutant drops the newest ones -- and
on relative order only if you have accounted for the reversal. Separately,
RSS 2.0's `<author>` is an email address: `fe.author(name=...)` with no email
produces **no output at all**, so nothing a route does with the author is
observable to a subscriber and no assertion can pin it. See D823.

**324. `permission_required` REDIRECTS; IT DOES NOT ANSWER 401.** A route behind
`@permission_required('manage users')` answers a reader without the permission
with **302 to `auth.permission_denied`** (`app/utils.py:1961`), not 401 and not
403. A row asserting a status code for the refusal will fail against correct
code; assert the redirect target, and assert the thing the route would have
changed is unchanged, which is what actually proves the refusal. The
neighbouring decorators disagree with each other and with this one:
`login_required` redirects to the login page, and a `trustworthy()` check
inside a view body (`tags_blocked_list`) calls `abort(404)` so the page does
not admit it exists. Read the decorator before writing the assertion.

**325. A SURVIVING MUTANT MAY BE REPORTING A DEFECT IN THE CODE, NOT A GAP IN
THE SUITE.** Sub-project 68 wrote a row specifically to kill a mutant that
dropped an access-control term from a subquery, and the mutant **still**
survived. The equivalence write-off was available and would have been wrong:
the term could only matter in one of three branches, and that branch's output
was **always empty** because of a separate, unnoticed bug (`community_ids` was
a one-shot `.scalars()` cursor read twice -- see fact 326). Fixing the bug
killed the mutant. **When a mutant survives a row aimed straight at it, ask why
the mutated line cannot matter before concluding the two programs agree** --
instrument the three cases and compare, which is what found it here. Equivalence
is a claim about the code and needs the same evidence as any other. See D840,
D839, and fact 321's neighbourhood -- all three are about a mutation result that
means something other than what it first looks like.

**326. `.scalars()` IS A ONE-SHOT CURSOR; `list()` IT IF IT IS READ TWICE.**
`db.session.execute(text(...)).scalars()` returns a `ScalarResult`, which is
consumed by the first thing that iterates it. A function that passes it to two
different queries gets the right answer once and an **empty `IN`** the second
time, with no error anywhere. In `app/tag/routes.py` that silently emptied a
whole feature -- a topic's tag cloud drew no relationships at all, while the
community and feed clouds, which build real lists, drew them correctly. The
same `.scalars()` shape is still present in `show_tag` and `tag_posts`, where
each reads it exactly once; both are one added read away from the same bug.
See D839.

**327. A REPAIR THAT MAKES TWO PATHS AGREE CAN DESTROY THE OBSERVABLE THAT
DISTINGUISHED THEM.** `app/errors/handlers.py`'s 404 guard used to return a
rendered page on one arm and the bare string `'not found'` on the other, so any
row asserting the body could tell the arms apart. Sub-project 69 repaired that
-- correctly -- and **both arms then answered 404 with the same page**. Six
mutants, one per dropped operand of the guard, survived a suite whose rows
asserted status and body, because after the repair those tell you nothing about
which arm ran. The remaining observable was the side effect the guard exists
for: the fast path SKIPS a `CmsPage` query. The rows had to plant a `CmsPage`
**at** each guarded path and assert it is not served. **When a fix makes two
branches return the same thing, re-derive what still distinguishes them and
re-run the mutants -- the tests written before the fix may have stopped testing
the guard without failing.** See D844. Related: fact 325, where the mutation
result also meant something other than it first appeared.

**328. THREE HANDLERS ARE ONLY REACHABLE FROM A REQUEST THAT FAILS, AND TWO
OBVIOUS ROUTES IN ARE BLOCKED.** To exercise `app_errorhandler(401/429/500)`:
`app.route` **cannot** be called after the first request -- the `app` fixture is
session-scoped, so `AssertionError: The setup method 'route' can no longer be
called on the application` -- and `app.handle_http_exception()` from a
`test_request_context` skips the `before_request` hook that sets `g.site`, so
`errors/401.html` and `errors/500.html`, which extend `base.html`, die with
`UndefinedError: 'flask.ctx._AppCtxGlobals object' has no attribute 'site'`.
Make an **existing** route fail instead: patch the `render_template` it uses
with a `side_effect` and set `PROPAGATE_EXCEPTIONS = False` for the call
(restore it in a `finally`, or every later test in the session swallows its own
errors). The whole lifecycle then runs, which is the point.

Note also that the `db.session.rollback()` in those handlers **is** observable,
unlike D832's class: a session poisoned by a failed statement raises on its next
query, and the rollback is what clears it. Poison it inside the `side_effect`,
then assert a query works afterwards -- with a control row proving the poison
works, or the assertion is about nothing.

**329. A REPAIR CAN MAKE A LIVE BRANCH UNREACHABLE, AND ONLY THE FLOOR WILL SAY
SO.** Sub-project 70 replaced `session.query(User).get(follower.remote_user_id)`
-- which warns when the nullable FK is NULL -- with an early
`if not follower.remote_user_id: continue`. It reads correctly and every test
passed. It also dropped the module from 100.0 to **99.602**, because the
existing `if user_details:` false arm had been reached BY the null row, and with
the row skipped earlier that arm is reachable only through a dangling FK the
database forbids. The repair had converted a tested branch into dead code. The
shape that keeps both arms alive is to guard the ARGUMENT, not the iteration:
`user_details = (... .get(x) if x else None)`. **After any repair, re-read the
module's coverage before committing** -- a floor that only rises is what turns a
silently-worse repair into a loud failure. See D848.

**330. AN UNORDERED `.first()` IN A TEST IS A FLAKE WITH A LONG FUSE.** A row in
`tests/test_shared_community_invites.py` killed a mutant by relying on
`.filter_by(name=...).first()` -- no `order_by` -- returning the decoy rather
than the target. Its author asserted the assumption rather than trusting it, and
the assertion held for every isolated run of that file before failing once
inside a full suite (`assert 4 == 3`), because PostgreSQL is free to answer
either row once the table has seen enough insert/delete churn. Running the file
alone will NOT reproduce it. **Derive the ordering instead of asserting it**:
run the query first, designate whatever it returns as the row you want to be
wrong, and build the fixture around that -- then the kill is deterministic in
either order. See D847.

**331. MIGRATE WITH AN AST, NOT A REGEX -- AND EXPECT THE TEST DOUBLES TO BREAK.**
Sub-project 71 moved 841 call sites off SQLAlchemy's legacy `Query.get()`. The
textual version of the same rewrite reported **927** changes in 138 files; the
AST version reported 841 in 109. The 86-site difference was entirely
DOCSTRINGS, COMMENTS and one test whose subject is the old API -- prose a regex
cannot tell from code. Locate each call as an `ast.Call` node, rewrite by source
offsets, and `ast.parse` the result before writing it.

Then expect failures that are not regressions. Five of this round's six were
TEST DOUBLES: a fake session wrapping `.query(X).get(id)`, two `File.query`
replacements faking an orphaned FK, and a `MagicMock` configured through
`.query(model).get(...)`. **A double is a copy of an API's shape, so a migration
invalidates it exactly as it invalidates a call** -- and a double that stops
intercepting does not fail loudly, it silently stops standing in for anything
and the test fails somewhere else entirely. See D852 and D854.

**332. A WARNING COUNT THAT WILL NOT FALL TO THE PREDICTED FLOOR IS A FINDING.**
After sub-project 71 had migrated "all 171" `get_or_404` sites,
`flask_sqlalchemy/query.py:30` was still in the warning summary. Two live calls
remained -- `db.session.query(Post).get_or_404(post_id)` -- in a shape the
migrator's own finder did not handle. **Both files were floored at 100% and
every test passed**, so nothing in the suite could have reported it; the only
signal was a number that did not reach where it had been predicted to reach.
Predict the floor before a mechanical change, compare against it afterwards, and
treat the gap as a defect rather than as noise. See D853.

**333. `hasattr` CANNOT SEE A WTFORMS FIELD REMOVAL.** `RegistrationForm.__init__`
calls `delattr(self, 'captcha')` when the site turns the captcha off. WTForms'
`__delattr__` takes the field out of the form's registry but leaves the name
resolving to **`None`** rather than raising, so `hasattr(form, 'captcha')` is
True whether or not the field was removed -- a row asserting it is False fails
against correct code, which is how sub-project 72 first read a working
`delattr` as a defect. Assert on **`form._fields`**, or on the names the form
iterates: those are what the template walks and what validation reads. See D859.

**334. A CASE-FOLDING TEST THAT ONLY VARIES THE INPUT TESTS HALF THE
COMPARISON.** `func.lower(User.email) == func.lower(email.data.strip())` folds
BOTH sides. A fixture that stores `taken@example.com` and submits
`TAKEN@Example.COM` exercises only the input's `lower()`: the stored side is
already lowercase, so dropping `func.lower(User.email)` changes nothing and the
mutant survives. Sub-project 72 had this at three sites -- the email lookup, the
community-name lookup, and by extension any `func.lower(Column)` in the repo.
**Store the mixed case, not just submit it.** See D860.

**335. WTFORMS BINDS AN INLINE VALIDATOR BY NAME AND SAYS NOTHING WHEN IT
CANNOT.** `validate_<field name>` is looked up against the form's fields; if no
field has that name, the method is simply never called and **nothing warns**.
`app/user/forms.py` carried `validate_matrix_user_id` for a field called
`matrixuserid`, so every Matrix ID was accepted and the method's lines read as
uncovered with no explanation. `tests/test_post_and_user_forms.py` now asserts
over the AST of all of `app/` that every undecorated `validate_*` names a real
field -- ignore DECORATED methods (marshmallow's `@validates_schema` binds
differently) and accept any call ending in `Field` (`DateTimeLocalField` was
missing from a first attempt and produced a false positive). See D863, D868.

**336. `Optional()` READS `raw_data`, NOT `data` -- SO SETTING `.data` IN A TEST
DISABLES THE FIELD'S VALIDATORS.** WTForms' `Optional` inspects
`field.raw_data`, which is populated by FORMDATA and left empty when a test
assigns `field.data = ...` directly. It then clears the field's errors and
raises `StopValidation`, so every validator after it -- including the form's own
`validate_<name>` -- never runs, and the field reports no errors no matter what
it contains. Build the form with `formdata=MultiDict({...})` when the assertion
is about validation. Its `string_check` also strips, so a whitespace-only value
is blank to it as well, which makes an inline empty-guard unreachable through a
form and worth exercising by direct call. See D865, D866.

**337. `app.get_locale()` CAN RETURN `None`.**
`request.accept_languages.best_match(...)` returns `None` rather than raising
when a request sends no `Accept-Language` header or one matching nothing, and
the function's `except:` fallback only catches exceptions. Callers that pass the
result somewhere strict get a failure at a distance: `dateparser.parse(...,
languages=[None])` raises, and in `validate_remind_at` a bare `except Exception`
turned that into "Invalid." for every reminder anyone tried to set. Repaired
with `or 'en'` at the source. Note `str(get_locale())` does NOT fix it -- it
produces the string `'None'` -- and that spelling is already in use at
`app/request_hooks.py:92`. See D864.

**338. `Flask.logger` IS SHARED BY EVERY APP IN THE PROCESS, AND `create_app`
ATTACHES AN SMTPHandler TO IT.** `Flask.logger` is
`logging.getLogger(app.name)`, and every app built from this package is named
`app` -- so an app a test builds and the session's `app` fixture are the SAME
logger object, and handlers accumulate on it globally. `create_app` adds an
`SMTPHandler` at `ERROR` level whenever `MAIL_SERVER` and `ERRORS_TO` are set,
and **`MAIL_SUPPRESS_SEND` does not apply to it**: it is a `smtplib` client, not
Flask-Mail. A row that builds such an app without cleaning up leaves every later
test in the session one `app.logger.error(...)` from a real SMTP connection
attempt. Restore the handler list in a FIXTURE, not a `finally` -- see
`restores_the_shared_logger` in tests/test_app_factory.py, and the ordered pair
of rows that assert the teardown ran. Building a second app is otherwise cheap:
0.33s. See D871.

**339. FOR A MODULE EVERYTHING IMPORTS, ONLY THE FULL-SUITE COVERAGE NUMBER
MEANS ANYTHING.** Measured from its own test file alone, `app/__init__.py` reads
84.1% with `get_ip_address` and `StripCookieVaryForAnonymous` apparently
uncovered -- both are exercised by other suites, and `tests/test_client_ip.py`
tests the first by name. A round that trusted the single-file number would have
written rows for code that was already covered, and might have "repaired"
something to make them pass. Take the target list from the full-suite JSON;
use a scoped run only to iterate. See D872.

**340. A TEST THAT REIMPLEMENTS THE CODE UNDER TEST PASSES AND PROVES NOTHING --
AND ONLY THE COVERAGE NUMBER SAYS SO.** Sub-project 75 wrote a row for
`markdown_extras.add_attrs` that defined its own copy of the callback inside the
test, ran `re.sub` with it, and asserted on the result. It passed. The module
stayed at 96.396% and the production function was never called. **A green row
plus an unchanged percentage is the signature** -- if a round's coverage does not
move after adding rows that claim to cover something, the rows are testing
something else. Drive the real entry point, even when the internal helper is
easier to call.

**341. AN EXTRA OR PLUGIN MAY NEED A MARKER BEFORE IT ENGAGES AT ALL.**
markdown2's `enhanced-images` extra processes an image only when its alt text
carries ` :: ` -- `![A cat :: width=200px](cat.jpg)`. Without it markdown2
renders an ordinary `<img>`, none of the extra's branches run, and a row written
without the marker passes while exercising nothing. Sub-project 75 hit this
immediately after fixing fact 340's problem in the same rows: **two
independently vacuous versions of the same assertion, both green.** Check the
existing tests for the invoking syntax before writing new ones --
tests/test_enhanced_images.py had it right all along. See D877.

**342. A `LocalProxy` CAPTURED BY A MOCK IS `None` ONCE ITS CONTEXT HAS EXITED.**
`app/api/alpha/utils/upload.py`'s auth fallback passes `current_user` ITSELF
into `process_upload`, so a mock records the PROXY, not the `User` behind it.
Asserted after the `with app.test_request_context(...)` block has closed, the
proxy resolves to `None` and the row fails with
`AttributeError: 'NoneType' object has no attribute 'id'` -- against correct
code, which reads exactly like a bug in the thing under test. **Assert inside
the context**, or capture `.id` while the proxy is still bound. The same applies
to anything a mock records that is really `g`, `session`, `request` or
`current_app`. See D883.

**343. TWO ADJACENT FILTERS NEED TWO ASSERTIONS.** `get_topic_list` fetches
`blocked_community_ids` and `blocked_instance_ids` from two different helpers
and passes them to `topic_view` as two different arguments. A suite covering the
first said nothing about the second: the mutant replacing
`blocked_or_banned_instances(user_id)` with `[]` survived rows that correctly
proved a blocked COMMUNITY was hidden. Sub-project 72 hit the same shape with
`func.lower()` on two sides of a comparison. **When two narrowing filters sit
side by side, assume covering one leaves the other untested until a mutant says
otherwise.** See D882.

**344. A `lazy='dynamic'` RELATIONSHIP IS ALWAYS TRUTHY.** `User.passkeys` is
`db.relationship('Passkey', lazy='dynamic', ...)`, so the attribute is an
`AppenderQuery` rather than a list -- and a query object is truthy whether or
not it would return rows. `if not user.passkeys:` in
`app/auth/passkeys.py` was therefore ALWAYS False and its whole arm was dead,
while the `else` beside it produced a near-identical refusal that hid the fact.
Use `.count()`, or `.first() is None`. `User` carries other dynamic
relationships; grep for `lazy='dynamic'` before writing a truthiness test
against one. See D884.

**345. WHEN A LIBRARY IS MOCKED, ITS ARGUMENTS ARE THE BEHAVIOUR.** The
WebAuthn verifier cannot be exercised for real in this suite -- a genuine
assertion needs a signing key and a live credential -- so
`verify_authentication_response` is patched. That makes the OUTCOME
uninformative: with the verifier stubbed, a wrong `expected_rp_id`, a wrong
`expected_origin` or a missing `expected_challenge` still "verifies", and
sub-project 77 reached 100% coverage with all three free to change. **Assert the
call's arguments whenever the call is the security property.** That is not a
violation of the campaign's "never assert a mock was called" rule -- it is the
documented exception, for the case where the call IS the thing under test. See
D889.

**346. A RISING WARNING COUNT AFTER A COVERAGE ROUND IS NOT AUTOMATICALLY A
REGRESSION -- BUT ALWAYS ATTRIBUTE IT.** Sub-project 77 took the suite from 247
warnings to 255. All eight new instances came from
`flask_login/login_manager.py:488`, reached because `login_user(remember=True)`
on the passkey path was covered for the first time: new coverage reaches new
third-party code, which emits its own deprecations. Check the distinct-site
listing (`grep -oE "^ +/[^:]+:[0-9]+: [A-Za-z]+Warning"` over the warnings
summary) and confirm nothing under `app/` or `tests/` appears, rather than
assuming either way. See D890.

**347. USER ID 1 PASSES EVERY `user_access` CHECK.** `app/utils.py:1650-1653`
short-circuits: `if user_id == 0: return False` then `if user_id == 1: return
True`, before any role is consulted. It is the instance-owner bootstrap, and it
means **`api_baseline.user1` is omnipotent**: it has no roles, the test database
holds no `role_permission` rows, and `user_access('anything at all', 1)` is
still True. **Any authorization row using `user1` as the unprivileged party
asserts nothing.** Build a separate user for the refused side, and assert
`user.id != 1` in the helper that makes it. See D892.

**348. AN AUTHORIZATION TEST NEEDS A CALLER WITH EXACTLY THE PERMISSION UNDER
TEST.** Testing a guard with "an admin" on one side and "a user with no
permissions" on the other proves only that SOME check exists -- not which. In
sub-project 78 the permitted caller was user 1 (passes everything, fact 347)
and the refused caller was roleless (fails everything), so a mutant changing
`user_access("approve registrations", ...)` to
`user_access("some other permission", ...)` **survived all 34 rows**: an admin
endpoint could have been checking a permission every user holds, with the suite
green. Grant the caller exactly one permission -- the one the guard names --
and the string becomes load-bearing. See D893.

**349. TWO FORMS RENDERED ON ONE PAGE MUST SHARE NO FIELD NAME.** A view that
instantiates two `FlaskForm`s binds **both to the same request body**, and
`SubmitField.data` is true whenever the field name is present -- so if both
declare `submit`, submitting either one sets it on both. In `admin_misc` that
made an ordinary settings Save take the close-the-instance branch, pausing
federation for ten years. The convention already in the codebase is a distinct
name per button (`PreLoadCommunitiesForm.pre_load_submit`,
`CloseInstanceForm.close_submit`). Assert the disjointness structurally --
`set(A()._fields) & set(B()._fields) == set()` -- because a behavioural pin can
be satisfied by a mutant that adds a dummy field to absorb the rename. See
D899, D902.

**350. AN ASSERTION ABOUT A FIELD'S FINAL VALUE IS VACUOUS UNLESS THE ROW SET
IT TO SOMETHING ELSE FIRST.** `Site.registration_mode` is already `'Closed'` in
the factory, so `assert site.registration_mode == 'Closed'` after the
close-instance POST passed whether or not the code ran -- and the mutant
deleting that assignment survived. The same trap made the first probe of D899
inconclusive. This is fact 347's shape applied to state instead of to
permissions: **establish the negative before asserting the positive.** See
D903.

**351. WHERE A BARE `except` COVERS THE DIFFERENCE, ASSERT THE CALL, NOT THE
VALUE.** `admin_home` guards its outbound LibreTranslate call with `if
current_app.config['TRANSLATE_ENDPOINT']:` inside a `try: ... except
Exception: pass`. Deleting the guard is invisible by result -- the unguarded
client raises, the except swallows it, and `translation_languages` is `None`
either way. Assert `api.called is bool(endpoint)`: that the client was never
**constructed**. See D904.

**352. PIN THE CLOCK TO TELL `>` FROM `>=`.** `admin_home` computes
`utcnow() - cron_task.last_run` itself, so a row that builds `last_run` from
its own `utcnow()` is always a few microseconds short of equality and the two
operators are indistinguishable. `patch('app.admin.routes.utcnow',
return_value=<fixed>)` with `last_run = fixed - frequency` makes the boundary
exact: due, not late. See D905.

**353. A DEFAULT LIKE `field.data or ''` CAN ONLY BE TESTED BY OMITTING THE
FIELD.** A present-but-blank `StringField` already has data `''`, so posting
`{'elevator_pitch': ''}` exercises nothing -- the mutant deleting the `or ''`
survives. Only an absent key leaves `.data` at `None`. The same distinction is
what makes an unvalidated `IntegerField` a 500 rather than a form error
(D900): WTForms errors on a key that is *present and unparseable*, and does
nothing at all for a key that is missing. See D906, D900.

**354. DO NOT OPEN A NESTED `test_request_context` INSIDE A TEST.** The `app`
fixture pushes one app context for the whole session; a nested
`app.test_request_context()` pushes and then **pops** an app context, and the
pop runs `teardown_appcontext`, which calls `db.session.remove()`. That closes
the session the fixture is running on. Every later request in that test then
resolves `current_user` to anonymous, so an authorized route answers
`302 /auth/permission_denied` and any assertion about the view is vacuous --
measured as `PROBE user_access call 'change instance settings' None False`
with the client session still holding `_user_id: '3'`. Build form payloads
with `SomeForm(formdata=None, ...)`, which is what FlaskForm's default
`formdata=_Auto` needs a request for, and set choices directly instead of
calling helpers that read `current_user`.

**355. `login_required(csrf=True)` VALIDATES CSRF ITSELF.** It does not consult
`WTF_CSRF_ENABLED`, which `tests/conftest.py` sets `False`, so every POST to a
route using it needs a real token even though WTForms' own validation is off.
Without one the POST is refused **before** any authorization check runs, which
makes an authorization row pass while actually testing CSRF. Generate the token
in a throwaway context and write the raw value into the client session; see
`csrf()` in `tests/test_admin_routes_entry.py`.

**356. A `SelectField` WITH EMPTY CHOICES FAILS SILENTLY IN A GENERATED
PAYLOAD.** A helper that walks a form and skips fields with no choices leaves
the payload missing a required key; `validate_on_submit()` then returns False,
the branch under test never runs, and the row passes asserting nothing.
`SiteMiscForm.language_id` is fed from the `language` table, which the test
database does not seed. Assert `field.choices` in the helper rather than
skipping, and assert `render.call_args.kwargs['form'].errors == {}` in rows
that depend on a POST having validated.

**357. DO NOT CHANGE DISK STATE BEFORE YOU KNOW THE UPLOAD IS USABLE.**
`admin_site` unlinked the seven files the current logo occupied and only then
handed the upload to Pillow, so an undecodable upload destroyed the existing
logo and left the row pointing at it. Capture what is to be superseded, process
the replacement, assign it, and delete last. The same ordering rule is why the
decode check uses `Image.open(...).load()` and not `.verify()`: `verify()`
reads headers only, and a truncated image passes it and then raises inside
`thumbnail()` -- after the deletions. See D912.

**358. A DERIVED FILENAME CAN COLLIDE WITH ITS OWN SOURCE.** `admin_site`'s
small-image arm saves to `<base>.png` and then deleted `<base>{file_ext}` --
the same path whenever the upload was a PNG, so the logo it had just stored was
unlinked in the same request. Whenever a routine writes a derived file next to
its source and then cleans the source up, compare the two paths rather than
assuming they differ. Pin both directions: "the source is still there" is
satisfied by never deleting anything. See D914.

**359. A FIX THAT WIDENS WHAT A BRANCH ACCEPTS WIDENS WHAT EVERY GUARD ON THAT
BRANCH MUST COVER.** Making the `.svg` branch case-insensitive was correct, and
it turned the case of the SANITIZE guard above it from redundant into
load-bearing: before, a `.SVG` upload crashed in Pillow; after, it is stored
verbatim and served from this origin. The mutant reverting that guard survived
every row, because the only unsanitizable-SVG row used a lowercase name. After
widening a branch, re-ask what each guard protecting it now has to hold for.
See D915.

**360. AN UPLOAD ROW MUST BUILD A REAL FILE OF THE FORMAT IT CLAIMS.** A stub
of bytes named `.png` exercises the error path, not the success path, and reads
as though it covered both. `_png()` in `tests/test_admin_site_profile.py`
renders an actual PNG with Pillow; the route then decodes it and writes six
thumbnails, each of which is asserted separately, because a single `site.logo`
assertion passes while five derivatives are missing.

**361. WHERE TWO LAYERS GUARD THE SAME THING, THE INNER ONE NEEDS A ROW THAT
RELAXES THE OUTER.** `SiteProfileForm.icon`'s `FileAllowed` list is a strict
subset of the route's `allowed_extensions`, so the route's `abort(400)` cannot
be reached by any submission. That is a reason to TEST it deliberately, not to
delete it: it is the second layer on a route that writes into a served
directory. The row clears the form validator, says in its docstring that it is
doing so, and checks the route refuses on its own -- so deleting the inner
guard as dead now fails a test. `SiteProfileForm.icon` is an `UnboundField`
until the form is instantiated, so the validators are in
`SiteProfileForm.icon.kwargs['validators']`, not on the attribute. See D918.

**362. EVERY POST TO `/admin/site` MUST BE MULTIPART.** The route reads
`request.files['icon']` unconditionally, and `request.files` raises
`BadRequestKeyError` for a request with no file part at all -- so a plain
form-encoded POST is a bare 400 before any of the behaviour under test runs,
and a row written that way asserts nothing. Rows that do not care about the
upload still pass an empty icon part. See D919.

**363. PATCH `render_template` FOR ANY ROW THAT RENDERS AN ADMIN FORM
TEMPLATE.** `tests/conftest.py` sets `WTF_CSRF_ENABLED` False, so FlaskForm
does not declare a `csrf_token` field, and a template calling
`form.csrf_token` raises `jinja2.exceptions.UndefinedError: ... has no
attribute 'csrf_token'`. That is a template-rendering failure with nothing to
do with the behaviour under test, and patching it also gives the row access to
the form object and its errors -- which fact 356 requires anyway.

**364. A DOMAIN INTERPOLATED INTO A REGEX IS A DOMAIN THE ADMIN CAN CRASH YOU
WITH.** `instance_banned` built a pattern from a blocklist entry with no
escaping, so every `.` in a wildcard ban matched any character and any other
metacharacter made `re.compile` raise -- out of a function that gates every
inbound activity and every outbound delivery, and that re-raises. `re.escape`
first, then reinstate the one wildcard you meant:
`re.escape(domain).replace(r'\*', '[a-zA-Z0-9]')`. See D920.

**365. `re.match` ANCHORS ONLY THE START.** A pattern built as `'^' + ... + '$'`
needs both ends asserted by a test, because dropping the `$` is invisible to any
row whose input is exactly the banned string. Unanchored, a ban on `ev*l.com`
also matches `evil.com.attacker.example` -- a domain the attacker owns. Ask for
a prefix case and a suffix case whenever a test covers a pattern match. See
D925.

**366. NORMALISE THE TRAILING DOT.** `evil.com.` is the fully-qualified form of
`evil.com`: DNS resolves them identically and TLS works either way, so a peer
can present either. They are different STRINGS, so a banned instance re-
federated by adding one character to its actor ids. Strip it where the rest of
the host normalisation lives -- `inbox_domain` -- so every caller gets it, and
strip it LAST so it applies to bare domains and to URLs alike. See D921.

**367. A NAME THAT DIFFERS BY ONE LETTER FROM AN IMPORTED SYMBOL WILL NOT RAISE.**
`isinstance(instance_allowed, list)` where the local was `instances_allowed`:
the singular is a FUNCTION imported at the top of the module, so the guard was
False for every input and the `and` short-circuited before `len()` on a
function could raise. The whole allowlist import had never done anything, with
no error, on any instance. **A branch nothing covers and nothing errors on is
indistinguishable from a branch that works.** See D922.

**368. WHERE ONE QUESTION HAS TWO IMPLEMENTATIONS, THE SECOND ONE IS WRONG.**
`admin_federation_preload` asked "is this instance banned?" with
`community['baseurl'] in <rows from banned_instances>`, while every other caller
asks `instance_banned()`. The membership test missed every WILDCARD ban -- a
pattern is not a domain -- and did no normalisation, so preload could subscribe
to communities on a defederated instance. Call the function, and take the
memoized cost. See D924.

**369. `.get(key, [])` WHEN EACH SECTION COMMITS SEPARATELY.** `import_bans_task`
commits after each of its five sections and read each key with `[...]`, so a
file missing one key applied the earlier sections and then raised -- a partly
updated database AND an exception, which in debug mode is a 500 in the admin's
face. Either make the whole thing one transaction or make a missing section a
no-op; the one thing to avoid is a partial write that also fails. See D923.

**370. A DEFECT'S REGRESSION TEST IS NOT INHERITED BY THE NEXT FILE TO TOUCH THE
FUNCTION.** The fail-closed answer for an absent domain is pinned elsewhere in
the suite, so the mutant restoring the old fail-open behaviour survived a
mutation pass scoped to this round's two files. Scoping a mutation pass to the
round's own files is right -- it measures the round -- but a survivor means "no
row HERE", not "no row anywhere", and the cheap resolution is to add the row
rather than to widen the pass. See D926.

**371. ASSERT THE ARGUMENTS, NOT THE CALL COUNT.** `do_subscribe.delay(...,
admin_preload=True)` survived losing its keyword because the row asserted
`call_count == 2`. That flag exists because subscribing as the admin's
alt_profile makes the later unsubscribe fail, which is exactly the kind of
consequence a count cannot see. This is D889's lesson in a new place: when a
call is mocked, the arguments ARE the behaviour.

**372. `patch.dict(app.config, {'DEBUG': True})`, NOT `patch('...current_app')`.**
`current_app.debug` reads `config['DEBUG']`, so patching the config is the whole
switch; replacing the `current_app` proxy also replaces every other config
lookup and template global the route touches, and the row then passes for
reasons unrelated to the branch.

**373. A PAGINATION LOOP DRIVEN BY A REMOTE SERVER NEEDS A PAGE CAP.**
`admin_federation_remote_scan` pages until a page comes back short, and the
remote server decides how long every page is -- so a server that always answers
with a full page kept the request running forever, holding a worker and growing
the holding list without bound. `while <remote says there is more>` is not a
loop with a bound; add one, and pin it by feeding the mock more pages than the
cap and asserting the request count. See D933.

**374. A NAME ASSIGNED ONLY INSIDE A LOOP IS UNBOUND WHEN THE LOOP DOES NOT
MATCH.** `remote_instanceinfo_url` was set inside `for e in
nodeinfo_dict['links']`, and the REMOTE server chooses what is in that list, so
an empty one produced `UnboundLocalError` and a 500 the admin could not tell
from a bug in their own instance. Initialise before the loop and check after
it. See D932.

**375. WHEN YOU FIX A DEFECT, GREP FOR ITS SHAPE BEFORE MOVING ON.** Slice C
replaced `baseurl in <rows from banned_instances>` with `instance_banned()` in
`admin_federation_preload`; the identical membership test was sitting in
`admin_federation_remote_scan`, 100 lines away in the same file, and a third
surface -- `admin_federation_mastodon_scan` -- had no check at all. One `grep`
for the query string would have found all three in the same round. See D931,
D934.

**376. AN ANCHOR THAT MATCHES TWICE IS A FINDING, NOT JUST A CHORE.** D833's
uniqueness pre-check exists to stop a mutation runner corrupting a file, but a
mutant with no unique anchor is also telling you the code is duplicated: the
lemmy and piefed scan branches turned out to be byte-identical for ~30 lines.
Read the collision before working around it. See D935.

**377. PARAMETERISE A THRESHOLD OVER EVERY BRANCH THAT IMPLEMENTS IT.** The
three scan branches each have their own post and user minimums, reading
different keys (`posts`/`users_active_week`, `post_count`/`active_weekly`,
`entryCount`/`subscriptionsCount`). One row covering the lemmy pair would have
left four mutants alive; parameterising the row over all three killed six.
Slice D's 41-of-41 result came from this and from fact 371, not from anything
about the code being simpler.

**378. `CACHE_TYPE` IS `NullCache` IN TESTS, SO NO STALENESS IS OBSERVABLE.**
`tests/conftest.py:68` sets it, and `@cache.memoize` therefore stores nothing:
a revoked permission reads correctly whether or not anything was invalidated,
and a mutant that breaks `cache.delete_memoized` passes every row that watches
the effect. Assert the CALL instead -- that the function, the key and every
affected id are what `user_access` is memoized under -- which is fact 371's
rule arriving from the other direction. See D939.

**379. A VOCABULARY DERIVED FROM ITS OWN DATA CAN ONLY SHRINK.**
`admin_permissions` listed the permissions it offers with `SELECT DISTINCT
permission FROM role_permission` and then deleted those rows, so unticking
every box for a permission removed the last row naming it and the page could
never offer it again. Whenever a form's OPTIONS come from the same table the
form rewrites, ask what happens when the last row goes. A permission nobody
holds is still a permission; the set belongs in code. See D937.

**380. REPLACING DERIVED DATA WITH A CONSTANT NEEDS A RATCHET IN THE SAME
COMMIT.** The constant is only right on the day it is written. Walk the source
with `ast`, collect the literal arguments of the calls that consume it, and
assert the two sets are equal in both directions -- unreachable values and dead
values are different bugs and both are worth naming. `ast`, not a regex: a
regex cannot tell a call from the same words in a docstring, and a test file
about permissions is full of the strings it is looking for. See D940.

**381. A SCOPED `DELETE` IS PART OF AN EDIT FORM'S CONTRACT.** The permissions
page rewrote roles 3 and 4 but deleted the whole `role_permission` table, so
every other role was silently stripped on every save. Where a form replaces a
subset, the delete must name that subset -- and the test must include a row
outside it, because a row that only checks the edited subset passes either way.
See D938.

**382. THE SECOND POSITIONAL ARGUMENT TO `login_user` IS `remember`.**
`login_user(user, False)` in `masquerade` is load-bearing: `True` writes a
`remember_token` cookie and the administrator stays logged in as the target
after closing the browser. A row asserting WHO is logged in cannot see the
difference; assert that no `remember_token` appears in `Set-Cookie`. See D941.

**383. `User.get_id()` RETURNS AN `int`.** Flask-Login's own `UserMixin`
returns `str(self.id)`, and `app/models.py:1163` does not, so
`session['_user_id']` holds whichever type last wrote it -- a string when a
test sets it by hand, an int after a real `login_user`. `load_user` does
`int(id)`, so both work; comparisons in tests need `str()` on both sides.

**384. A FORM FIELD NOBODY READS IS INVISIBLE UNTIL SOMEBODY TRUSTS IT.**
`AddUserForm` declared `banned` and `verified` and `admin_users_add` set
neither, so an admin who ticked "Banned" when creating an account got an active
one -- silently, because nothing errors on an unread field. When covering a
form handler, diff the form's fields against the attributes the handler
assigns; the gap is the bug list. See D944.

**385. `else:` AFTER A `validate_on_submit()` BRANCH ALSO RUNS FOR A REFUSED
POST.** The pre-fill arm belongs behind `elif request.method == 'GET':`. As an
`else` it overwrites the submission from the database, so an admin whose form
was rejected sees the stored values and loses what they typed -- and the page
gives no sign of it. This is the third time in `app/admin/routes.py` alone
(D907, slice C's `admin_federation`, D945). Grep a blueprint for
`validate_on_submit` followed by a bare `else` before assuming it is one bug.

**386. AN APOLOGY IN THE UI IS A BUG REPORT.** `admin_user_edit` flashed
"Permissions are cached for 50 seconds so new admin roles won't take effect
immediately" -- a message written instead of the two-line invalidation that
removes the problem, and the direction that matters is DEMOTION: an
administrator stripped of their role kept every permission for the length of
the timeout. Where the code explains a limitation to the user, ask whether the
limitation is real. See D946.

**387. AN AUDIT CALL INSIDE ONE BRANCH OF THREE IS AN AUDIT GAP.**
`add_to_modlog('delete_user', ...)` lived in the remote-user branch, so
deleting one of this instance's OWN accounts recorded nothing anywhere.
Parameterise the pin over every branch -- local-finalized, local-not-finalized,
remote -- because the defect is precisely that one of them had the call. Write
the entry before the row is destroyed, or `display_name()` and `link()` have
nothing to read. See D947.

**388. CHECK THE INDENTATION OF WHAT FOLLOWS A `if <row>:` GUARD.**
`unsubscribe_from_everything_then_delete_task` guarded its unsubscribe and
federation work with `if user:` and then ran `user.delete_dependencies()`
outside it. A task queued after its route has committed can always find the row
gone -- two clicks, or a retry -- and the answer was `AttributeError: 'NoneType'
object has no attribute 'delete_dependencies'`. See D948.

**389. PAGINATION LINKS ARE BUILT TWICE.** `next_url` and `prev_url` are
separate expressions, so a filter dropped from one may be present in the other
and a row that checks only `next_url` passes with half the defect in place.
Request page 2 as well. See D949, D953.

**390. A SEARCH OVER `or_(a, b)` NEEDS A TERM THAT MATCHES ONLY `a`.** Every
row searching `admin_users` used a term present in both the email and the user
name, so deleting either half of the `or_()` survived. Give one user an address
that shares nothing with its name, and search for each separately. See D953.

**391. AN EXCLUSION IS VACUOUS IF NOTHING WOULD HAVE BEEN INCLUDED.**
`instance.id != 1` -- do not send this instance its own Delete -- survived
because every instance in the row was offline, so nothing was sent to anything.
The row has to have live peers that DO receive it. Same shape as fact 350, for
a filter rather than a field. See D953.

**392. `Pagination.has_prev` IS `self.page > 1`.** So `users.has_prev and page
!= 1` cannot be distinguished from `users.has_prev`: `self.page` is the value
`paginate()` was given, and `paginate(page=0, error_out=False)` cannot produce
`has_prev`. Read the property before spending a row on the second test. See
D954.

**393. DO NOT `patch('...current_app')` TO WATCH THE LOGGER.** Mock
auto-creates `logger.exception` as an AsyncMock, and pytest then reports
`RuntimeWarning: coroutine 'AsyncMockMixin._execute_mock_call' was never
awaited` -- a warning this campaign counts. Patching the proxy also replaces
every config lookup the route makes. Use pytest's `caplog` fixture, which also
lets the row assert the detail reached the log instead of the browser.

**394. `tests.factories.make_instance` ALWAYS INSERTS.** `instance.domain` is
unique and the `site` fixture has already created the local instance, so a
second `make_instance('test.piefed.local')` is a `UniqueViolation`, not a
second row. Use a get-or-create helper in any file that builds users across
several instances.

**395. `login_required` VALIDATES CSRF ONLY FOR POST.** `app/utils.py`'s
decorator reads `if request.method == 'POST' and csrf:`, so a route that
carries `methods=['GET', 'POST']` and mutates unconditionally has **no CSRF
protection at all** on its GET path. `community_unban_user` had no form and
unbanned on whichever method arrived, so an `<img src>` tag was enough. When
covering a route, check its methods against whether it mutates before reading
anything else. See D955.

**396. A MISSING CSRF TOKEN USED TO BE A 500.** `validate_csrf` raises
wtforms' `ValidationError`, which is not an `HTTPException`, and this app
registers no `CSRFProtect` to convert it -- so every route using
`app.utils.login_required` answered a tokenless or stale POST with a server
error. It is a 400 now. A row that asserts "refused" has to say what refused
looks like; `>= 400` would have hidden this.

**397. THE `current_user.banned` CHECK IS PER-ROUTE, NOT A DECORATOR.** There
is no `@banned_users_refused`; each route writes `if current_user.banned:
return show_ban_message()` by hand, so the question for any new route is
whether somebody remembered. Ten state-changing routes on the community
blueprint had not. The ratchet in
`tests/test_community_moderation_authority.py` enumerates `app.url_map` and
requires every state-changing rule to refuse, with read-only and self-service
rules excluded BY NAME so a new route has to be classified rather than land on
the permissive side by default. See D956, D960.

**398. A BANNED ACCOUNT MAY STILL ACT ON ITS OWN RELATIONSHIPS.** Leaving a
community, blocking one, managing one's own flair and notification settings all
change state, and refusing them would trap somebody in a place they are already
barred from taking part in. That is why the ratchet needs two exclusion sets
and not one, and why both are written out rather than guessed from the HTTP
method.

**399. FILTERING ON AN UNJOINED TABLE IS A CROSS JOIN, NOT A NO-OP.**
`PostReply.query.filter(PostReply.user_id == u, Post.community_id == c)` puts
`Post` in the FROM clause on its own, so the condition holds whenever ANY post
exists in that community and every one of the user's replies matches. It reads
as a narrowing filter and is the opposite. Measured: a ban in one community
selected replies from another. Grep a query for model names that do not appear
in its `select_from`/`join`. See D959.

**400. `Community.moderators()` EXCLUDES BANNED MEMBERS.**
`app/models.py:722` filters `CommunityMember.is_banned == False`, so setting
that flag also removes the person from `is_moderator()` and `is_owner()`. A row
that wants somebody to be a banned MODERATOR has to create the `CommunityBan`
row without flipping the membership flag, or the guard it is testing stops
seeing a moderator at all.

**401. `is_admin_or_staff()` READS THE ROLE NAME.** `is_admin()` looks for a
role called `Admin` and `is_staff()` for one called `Staff`
(`app/models.py:1259-1275`), so `grant_permission(user, 'administer all
communities')` -- which makes a bespoke role -- does not satisfy it. A fixture
for staff has to attach the actual named role. See D965.

**402. HOISTING A GUARD CAN ORPHAN THE ONE BELOW IT.** Moving
`community_moderate_subscribers`'s authorization check above its form made the
function's own `elif community is not None:` and inner `is_moderator()` test
unable to be false, leaving an unreachable `abort(401)`. Re-read what a moved
check now dominates, and delete what it has made dead in the same commit --
otherwise the next round finds it as a defect. See D962.

**403. TEST EACH CLAUSE OF AN `and` WITH INPUT THAT ONLY IT REFUSES.** The row
for "an ordinary member cannot ban" aimed at a moderator, so
`not community.is_moderator(user)` did the refusing and deleting the entire
`(is_moderator() or is_admin_or_staff())` half survived -- meaning any logged-in
account could have banned any non-moderator with the suite green. Give each
clause a case the others would allow, and keep them as separate rows. See D966.

**404. WHERE A MUTANT'S EFFECT IS A NO-OP WRITE, THE RESPONSE IS THE ONLY
OBSERVABLE.** Dropping `and community.is_owner(user)` lets staff proceed to set
`is_owner = False` on somebody for whom it is already false -- nothing changes
in the database, so every row watching rows survives. Assert the status code,
and set the world up so the refusal cannot be confused with a different guard's
(a third owner, so `num_owners() == 1` is not what answered). See D967.

**405. A FIX TO SHARED INFRASTRUCTURE INVALIDATES THE PINS THAT MEASURED THE
OLD BEHAVIOUR.** D958 turned a propagating `ValidationError` into a 400, which
broke `tests/test_dev_tools.py::test_the_buttons_own_validate_call_is_belt_and_braces`
-- a row from an earlier round that asserted the exception. Ask whether the
pin's ARGUMENT survives or only its assertion: there, the reasoning (the token
is checked before the view, so the button's own `validate()` is unreachable)
was untouched and only the observable moved. Update it in place and record why.
See D968.

**406. A METHOD THAT TAKES ITS SCOPE AS AN ARGUMENT ANSWERS THE CALLER'S
QUESTION, NOT ITS OWN.** `CommunityWikiPage.can_edit(user, community)` never
consulted `self.community_id`, so it answered "may this user edit some page of
that community" while every caller meant "may they edit THIS page". The fix
belongs in the method: four routes and three templates ask it, and a template
that offers an edit link the route refuses is its own bug. Whenever an
instance method takes a parent as a parameter, check it against the instance's
own parent first. See D969.

**407. A CHILD RESOURCE FETCHED BY BARE ID IS NOT SCOPED BY ITS PARENT'S
AUTHORIZATION.** Five routes in this blueprint took `(community_id,
resource_id)`, checked authority over the community, and then acted on
`db.session.get(Resource, resource_id)`. Grep a blueprint for two ids in one
URL and confirm the second is filtered by the first. The flair delete also
cascaded into the other community's `post_flair`, `CommunityFlairBlock` and
`rss_feed` rows. See D970, D971.

**408. A FUNCTION THAT FALLS OFF ITS END IS A 500.** Flask answers a view
returning None with `TypeError: The view function ... did not return a valid
response`. All three community report handlers had at least one such path, so a
non-moderator got a 500 instead of a 401 and opening an already-handled report
was a 500 too. Every `if/elif` chain in a view needs a terminal `else`, and a
row per refusal path is what finds the missing ones. See D974.

**409. A SWEEP IS NOT A SUBSTITUTE FOR ACTING ON THE ROW YOU WERE GIVEN.**
`community_moderate_report_ignore` set the subject's counter and then updated
sibling reports by `suspect_post_id` or `suspect_post_reply_id` -- which
incidentally covered the report in hand whenever it had one of those, and never
when its subject was a USER. Parameterise over every subject kind: the post and
reply cases passed and only the user case failed. See D975.

**410. SIBLING HANDLERS NEED THE SAME ROW EACH.** `escalate` had a
cross-community row and `resolve` and `ignore` did not, so dropping
`in_community_id=community.id` from `resolve` survived -- any moderator could
clear any other community's queue. A row written for one of three near-identical
handlers is a row for one of three; parameterise over the set. See D978, and
D935 for the reason the three are near-identical in the first place.

**411. COMMUNITY id 1 IS AS DANGEROUS AS USER id 1.** A row that reported
`mine` -- the first community the fixture creates -- could not see a mutant
hard-coding `suspect_community_id=1`. Use the second object and assert its id
is not 1, exactly as fact 347 requires for users. Any fixture whose first row
is the one under test has this problem.

**412. `make_user` PRODUCES AN ACCOUNT THAT IS NOT `trustworthy()`.**
`User.trustworthy()` is False when `created_recently() and reputation < 100`,
and a fresh factory user is both. A row that needs a trusted non-moderator must
say so -- set `reputation` above 100 or push `created` back -- and a row that
needs an untrusted one should assert it rather than assume. Without both, the
`who_can_edit` levels 0, 1 and 2 are indistinguishable and two mutants survive.
See D977.

**413. A RATCHET THAT CANNOT FAIL FOR ITS OWN DEFECT IS WORSE THAN NONE.**
Slice A's blueprint scan flagged a rule only when it answered 200; every route
on that blueprint redirects on success, so it passed while eight state-changing
routes had no banned check. Fingerprint the STATE, not the response. And when
the strengthened version still cannot drive some routes -- a bare POST will not
satisfy a form, a slug or an actor -- say so in the docstring and pin those
routes individually, rather than leaving the ratchet's name to imply a coverage
it does not have. See D973.

**414. NORMALISE IN THE VALIDATOR, NOT AFTER IT.** `AddCommunityForm` checked
`url` for uniqueness and `add_local` then slugified it, and `slugify` is not the
identity on the strings the validator accepts: `'__general__'` became
`'general'`. A name the form approved therefore reached the INSERT as one that
already existed, and the 500 that followed was an `IntegrityError` the form was
written to prevent. Whenever a route transforms a field after validation, move
the transform into `validate()` and let every check run on the value that will
actually be stored.

**415. ORDER THE VALIDATOR: STRIP, THEN CHECK CHARACTERS, THEN NORMALISE, THEN
CHECK UNIQUENESS.** Putting the normalisation first turns `'has-a-hyphen'`
into `'has_a_hyphen'` and accepts it, instead of reporting "- cannot be in
Url". Putting the `/c/` strip in the route reproduces D980 exactly, one field
along. Both mistakes were made while fixing D980 and both were caught by rows
written for the character rules -- which is the argument for writing those rows
before touching the validator.

**416. AN `Optional()` FIELD'S DATA IS `None`, AND `None > 0` RAISES.**
`community.topic_id = form.topic.data if form.topic.data > 0 else None` is a
`TypeError` for any client that omits the field, even though the UI always
sends it. Reverting the guard failed five rows, not one: an unguarded
comparison on an optional field is not an edge case, it is the common path for
every non-browser caller. See D982.

**417. A `try/except` AROUND ONE READ OF `g.site` IS USELESS IF THE NEXT LINE
READS IT AGAIN.** `add_local` fell back to `db.session.get(Site, 1)` and then
dereferenced `g.site.enable_nsfw` three lines later, unguarded -- so the
fallback could not rescue the request it existed for. Fix it by using the local
rather than deleting it, and the branch becomes both meaningful and testable.
See D984.

**418. A SUBSTRING ASSERTION CANNOT TELL APART MESSAGES THAT SHARE A PREFIX.**
The nsfw variant of "Community not found." begins with the plain one, so
`assert 'Community not found.' in flashed` passed for both values of the
setting and the mutant forcing one branch survived a row parameterised over
both. Assert equality for user-facing strings. This is the eighth form of the
same family in sub-project 80: **a parameterised row proves nothing if its
assertion cannot distinguish the parameters.**

**419. THE ANCHOR PRE-CHECK KEEPS FINDING DUPLICATED CODE.** D833's uniqueness
requirement refused a mutant in `add_remote` because the identical
five-line `search_for_community` / `is blocked.` block appears twice in
`app/community/routes.py`, ~2600 lines apart. That is D935's finding again, in
a second module: when an anchor matches twice, read the collision before
working around it.

**420. SWEEP FOR A SHAPE ONCE YOU HAVE FOUND IT THREE TIMES.** D955's mutating
GET was found by hand in three blueprints and D907's discarded form input in
four places, always by covering the function rather than by reading it. Both
are detectable with `ast`: for the first, a route whose `methods` include GET
that reaches a `db.session` write with no `validate_on_submit()`; for the
second, an `if form.validate_on_submit():` whose `else` assigns `form.<x>.data`.
The sweeps took minutes and found 45 and 21 candidate sites, including one new
CSRF on an admin action. See D987, D988, D990.

**421. A FROZEN INVENTORY IS A RATCHET; IT IS NOT A SAFETY CLAIM.**
`KNOWN_GET_MUTATORS` lists every route matching D955's shape, and most of them
are fine -- a listing page that bumps a counter, an OAuth callback the provider
redirects to. The test fails when the set GROWS (justify the new one) and when
it SHRINKS (remove the fixed one), which is `coverage_floors.ini`'s discipline.
Say in the docstring that membership is not approval, or the list reads as one.
See D989, and D973 for what happens when a ratchet claims more than it checks.

**422. NAME THE DEFECTS A RATCHET WAS BUILT FOR, SEPARATELY.** A frozen-set
ratchet accepts a reintroduced defect as a "new entry" to be justified. A second
row that names `community_unban_user`, `community_moderate_report_ignore` and
`post_instance_sticky` explicitly is what makes reintroduction fail rather than
prompt. A third row asserts every listed endpoint still exists, because a frozen
set of endpoint names goes stale silently when a route is renamed.

**423. DO NOT FIX ELEVEN ROUTES' METHODS BLIND.** Changing a route from GET to
POST means changing every template that links to it, and this codebase has a
specific pattern for that -- `class="confirm_first send_post" href="#"
data-url="..."`, with `app/static/js/scripts.js:658` attaching the token from
the meta tag. Eleven such changes with no per-route rows is how a security fix
becomes an outage. Inventory them, fix each in the slice that covers its
blueprint, and let the ratchet hold the line meanwhile.

**424. A GUARD THAT FLASHES AND FALLS THROUGH IS NOT A GUARD.**
`do_subscribe` read the `CommunityBan` row, flashed "You cannot join this
community", and then created the membership anyway -- and the first gate's
bulk-import arm recorded `user_banned: True` and did the same. Both read as
refusals. When covering a branch that reports a refusal, assert the refusal
TOOK EFFECT as well as that it was reported; the second site here was found
only because the row checked both. See D991.

**425. A MEMOIZED AUTHORIZATION LIST IS A GATE WITH A CLOCK ON IT.**
`communities_banned_from` is `@cache.memoize(timeout=86400)` and is invalidated
in exactly one place. A ban that arrives any other way -- federated in, or
written by a tool that does not know to invalidate -- leaves the gate open for
a day. Where a cached list is the only check, the uncached one behind it has to
work; where both exist, test the second with the first patched to return the
stale answer, which is what a real 24-hour cache does. See D991.

**426. AN ACTOR TAKEN FROM A URL IS `None` UNTIL PROVEN OTHERWISE.**
`actor_to_community` and `search_for_community` both return None for a handle
they cannot resolve, and three functions in `app/community/routes.py`
dereferenced the result on the next line. A stale invite link or a mistyped
community name is an ordinary event, not an exceptional one. Grep a blueprint
for `actor_to_community(` and check the line after each. See D992.

**427. AN UNBOUNDED LOOP OVER USER INPUT THAT SENDS EMAIL IS A SPAM RELAY.**
`community_invite` called `invite_with_email` once per line of a textarea with
no cap, from a route any account can reach on a default community. Count the
recipients in the validator, drop blank lines before counting, and put the
limit in the message. The same question is worth asking of every loop that
sends, fetches or writes once per element of something a user submitted. See
D993.

**428. THE SAME QUESTION ANSWERED THREE WAYS IS TWO WRONG ANSWERS WAITING.**
One request path through `join_then_add` consults a memoized list, a direct
`CommunityBan` query and `Community.user_is_banned()` -- and D991 was two of
the three failing to act. Fact 368 said to keep one implementation; this is
what the second and third cost. See D995.

**429. A GUARD'S FALSE ARM MAY NEED STATE NOTHING ELSE PRODUCES.** Four of the
six survivors in slice D were guards whose false arm is reachable only through
a state the rows had not built: a `CommunityMember` flagged `is_banned` whose
`CommunityBan` row is gone (`User.subscribed()` then returns
`SUBSCRIPTION_BANNED`, which the outer guard does not skip), a feed the user
OWNS rather than merely follows, a POST where the row had only sent GET. Ask
what makes each condition false, and build exactly that -- the shape is fact
350's, one level deeper.

**430. A GUARD OFTEN PROTECTS MORE THAN THE STATEMENT UNDER IT.**
`join_then_add`'s `if not current_user.subscribed(...)` gates the join AND the
"You joined" flash, so removing it still produced no duplicate membership --
the `existing_member` check inside caught that -- but did tell an existing
member they had just joined. When a mutant on a guard survives, check every
statement it dominates, not just the one the guard appears to be about.

**431. A FILTER UPSTREAM CAN MAKE A GUARD DOWNSTREAM UNFALSIFIABLE.**
`community_leave_all`'s `subscription < SUBSCRIPTION_MODERATOR` cannot be false,
because `joined_communities()` already excludes moderators and owners
(`app/utils.py:2847`). That is a legitimate equivalent mutant and worth
registering rather than chasing: the guard is defence in depth against the
upstream filter changing. Read the producer before assuming the consumer's
check is testable.

**432. `del form.field` DOES NOT REMOVE THE ATTRIBUTE.** WTForms'
`Form.__delattr__` pops the field out of `_fields` and then sets the attribute
to `None`, so after `del form.flair` the field is gone from rendering and
validation while `hasattr(form, 'flair')` is still True. Assert on the value
(`form.flair is None`) or on `'flair' not in form._fields`, never on `hasattr`.

**433. AN UNSUBMITTED StringField IS `None`, NOT `''`.** WTForms initialises
a field's data with `process_data(None)` and only overwrites it from the
formdata when the key is present. So `form.choice_9.data.strip()` is an
`AttributeError` for any client that does not post all fifteen choice fields.
The browser form always posts them all, which is why D1003 survived: **the
site's own page is the one client that cannot reach the defect.**

**434. A VALIDATOR THAT APPENDS AN ERROR AND RETURNS True REFUSES NOTHING.**
`validate_on_submit()` reads the return value, not `form.errors`. D1001 was
three copies of `self.communities.errors.append(...)` with no `return False`
under them, so an instance setting recorded a complaint and accepted the
submission anyway. When covering a custom `validate()`, assert what the route
DID -- that `make_post` was not called -- and not merely that an error was
recorded.

**435. GIVE EACH POST TYPE ITS OWN REQUIRED FIELDS BEFORE BLAMING THE ROUTE.**
`add_post`'s six types build six different form classes, and a payload that is
valid for a discussion is refused for an event (start/end/timezone/online link),
a video (`video_url`'s Regexp fires on an empty string, with no DataRequired),
a link (`link_url`) or a poll (`mode`, `finish_in`, two choices). A refusal
looks exactly like the code under test not running. A `_type_extras(type_name)`
helper keeps that knowledge in one place.

**436. A MagicMock RETURN VALUE REACHES THE DATABASE.** Patching
`make_post` and letting it return a `MagicMock` gave
`InvalidRequestError: Incorrect number of values in identifier to formulate
primary key for session.get()` -- the route reads `post.sticky` and passes
`post.id` to `sticky_post`. A three-attribute stub class (`sticky`, `slug`,
`id`) is what the double actually has to be. A failure in the double reads
exactly like a failure in the route.

**437. WHEN A FIX'S CONDITION NAMES A CASE, PIN THE OTHER CASE TOO.** D1001's
condition is `community.is_local() and ...`. Every image row in the file used a
local community, so dropping `is_local()` -- which would make an instance that
refuses local image posts refuse remote ones as well -- changed nothing any
assertion could see. This is the ninth instance in this sub-project of an
assertion that cannot distinguish the thing it names.

**438. A ROW THAT WILL NOT PASS IS A PROBE WAITING TO BE WRITTEN.** Three of
slice E's six defects (D1001, D1002, D1003) were found because a row written
from the source would not go green, not because anything was read. The rule
that made it work: when a row fails, measure what actually happened before
changing the row.

**439. `Site.private_instance` DEFAULTS TO True.** (`app/models.py:4014`.) An
anonymous request to any page is redirected to the login form before the route
runs, so every row about an anonymous visitor has to turn it off first. A row
that does not looks exactly like the refusal it was written to test.

**440. `CONTENT_WARNING` REDIRECTS EVERYONE, NOT ONLY THE ANONYMOUS.**
`login_required_if_private_instance` (`app/utils.py:1930`) sends every visitor
without the `warned` cookie to `/content_warning`. A row about a community's
own nsfw/nsfl handling under that config has to set the cookie or it never
reaches the route.

**441. THE COMMUNITY FOUNDER IS ALWAYS A MODERATOR.**
`community_moderators` (`app/utils.py:2917`) synthesises the community's own
`user_id` into the list when no row holds it. A row asserting on the moderator
list has to assert about the moderator it added, not about the whole list.

**442. THE VIEWER CANNOT BE THE SUBJECT OF A `last_seen` TEST.** Flask-Login's
request handling stamps `last_seen` on the CURRENT user, so an admin who is
also a moderator makes themselves active again just by loading the page. The
`un_moderated` rows use an admin who moderates nothing.

**443. THE USER CONTENT PREFERENCES DO NOT DEFAULT TO OFF.** `hide_nsfw` and
`hide_nsfl` default to 1 and `hide_gen_ai` to 2 (`app/models.py:993-995`), and
every filter tests `== 1` -- so `hide_gen_ai = 2` means "label it", not "hide
it". A row that only sets the preference to 1 passes against a filter that
ignores the preference entirely. Assert both directions, and clear the other
three so one filter does not cover for another.

**444. A FILTER APPLIED IN TWO ARMS NEEDS A ROW IN EACH.** `Post.deleted` and
`PostReply.deleted` are each filtered once for anonymous visitors and once for
logged-in ones. Every row was anonymous, so deleting either logged-in copy
survived. **Duplication hides a gap the same way a weak assertion does**, and
neither is visible without the mutant.

**445. A ROW CAN FAIL BY TIMEOUT, AND THAT IS A REAL RESULT.** The topic and
feed cycle guards are pinned by rows that fail with pytest-timeout's 60-second
per-test limit when the guard is reverted, because the unguarded loop does not
end. A hang is a defect with a worse operational profile than a crash -- it
holds a worker instead of returning a 500.

**446. THE COMMUNITY BLUEPRINT'S PREFIX IS `/community`, AND SOME ROUTES
REPEAT IT.** `@bp.route('/community/<int:community_id>/feed/<int:feed_id>')`
is served at `/community/community/<id>/feed/<id>`. The doubling is real; a row
that "fixes" it gets a 404 that looks exactly like the refusal under test.

**447. TWO IDs IN ONE URL NEED A CHECK THAT THEY BELONG TOGETHER.** D1010 is
the shape: the route authorized `community_id` and then loaded `feed_id`
without relating them. Whenever a path carries a parent id and a child id, ask
what makes the child the parent's -- the authorization on the parent says
nothing about the child.

**448. A ROUTE THAT FALLS OFF THE END RETURNS None, AND FLASK CALLS THAT A
500.** `TypeError: The view function ... did not return a valid response`. An
`if` with no `else` in a view is a 500 waiting for the first caller who fails
the condition, and it reads in the logs as a fault rather than as the refusal
it was meant to be.

**449. ORDER IS AN ACCESS CONTROL.** D1013's two checks were both present and
both correct; the conditional-request check simply ran first, so a 304 was
returned to a caller the next check would have refused. Put every access check
above every short-circuit -- 304, cache hit, early return.

**450. A GUARD ADDED THIS ROUND CAN MAKE AN OLD FILTER UNTESTABLE.** D1014's
`if post.event is None: continue` hid the absence of `Post.type ==
POST_TYPE_EVENT` from every row in the file, because the two exclude the same
posts for different reasons. After adding a skip, re-run the mutation pass over
the filters NEAR it, not only over the new line.

**451. A THIRD-PARTY WARNING RAISED INSIDE THE LIBRARY IS NOT ALWAYS OURS TO
FIX.** `ics` calls `str()` on its own component while serializing an alarm. The
filter for it names the module, the category and the message text, so it cannot
hide anything of ours, and the reason sits next to it with the version that
will make it removable.

**452. `Community.is_moderator()` DOES NOT READ THE `is_moderator` COLUMN.**
It asks whether the user is in `moderators()` (`app/models.py:736`), which
selects on `is_owner OR is_moderator`. So it means "is on the moderation team",
and every owner satisfies it. A condition that says `is_owner or ...
is_moderator(user)` therefore has an unreachable first arm -- D1023.

**453. A BAN-CHECK ROW MUST START FROM A STATE THE REQUEST WOULD CHANGE.** A
row that makes the member an owner and then asserts they still are cannot see
the ban check at all: the assertion was true before the request. Set up the
state the successful request would move away from, then assert it did not
move.

**454. AIM A REFUSAL ROW AT THE ARM THAT COULD HAVE SUCCEEDED.** The
`remove_owner` ban row aimed at another account, which the authorization check
refuses whether or not the ban check runs. The only arm an owner can reach on
their own account is `user.id == current_user.id`, and that is the one the ban
check has to stop.

**455. A DETECTOR THAT READS THE FUNCTION BODY MISSES WHAT THE HELPERS DO.**
The D989 ratchet looked for `db.session` writes in the view and therefore
passed three live mutating GETs whose writes are in `app/shared/`
(D1022). When a ratchet is written against a code shape, list the indirections
that shape travels through, and re-run it after adding them -- doing so
surfaced three more routes immediately.

**456. A FRAGMENT ENDPOINT NEEDS THE PAGE'S REFUSALS.** `get_sidebar` renders
part of the community page and had none of `show_community`'s access control
(D1017). Any route that renders a piece of a protected page is a second front
door to the same data.

**457. ASK "ONLY THIS ONE'S?" OF EVERY LISTING.** Four of slice I's five
mutation survivors were the same gap: a queue or list whose scoping filter
could be deleted without any row noticing, because every row had only one
community in it. A listing row needs a second owner whose rows must NOT appear
-- otherwise it tests that the query returns something, not that it returns the
right something.

**458. A ROUTE THAT FETCHES A CALLER-SUPPLIED URL NEEDS A LOGIN.** Even with
`is_invalid_get_request_uri` keeping it off private ranges, an anonymous
endpoint that issues outbound GETs is a primitive anyone can drive from the
instance's own address (D1025). The question to ask of any new route is not
only "what can it read" but "what can it make this server do".

**459. TWO IDs IN ONE URL, AGAIN -- BUT THE OTHER WAY.** D1029 is fact 447's
mirror: the pair was `community_id` and `user_id`, and the id that needed
checking was the USER's, against the session. A path parameter naming a person
is an authorization question every time.

**460. A QUERY PARAMETER REACHES THE DATABASE WITH ITS TYPE.**
`request.args.get('communities')` is a string, and `db.session.get(Model,
'abc')` raises `DataError` from psycopg2 -- a 500, not a 404. `type=int` is the
whole fix, and it answers None for anything that is not a number.

**461. PAGINATION LINKS ARE ONLY BUILT ON PAGE TWO.** D1030's `BuildError`
could not happen until the report queue exceeded 1,000 entries, so the page was
correct in every test and in every quiet community, and broken in exactly the
one that was under attack. When a view builds a URL conditionally, cover the
condition, not just the view.

**462. THE FLOOR IS ON STATEMENTS *AND* BRANCHES.** `.coveragerc` sets
`branch = True`, so `percent_covered` -- the number
`tests/check_coverage_floors.py` compares -- combines the two. A module with
every statement covered can still read 98.75%, and the floor has to be set
against the measured combined figure, with the split recorded next to it.
Partial branches are the remaining work, not a rounding error.

**463. `# pragma: no cover` IS FOR A LINE A CONSTRAINT MAKES UNREACHABLE.**
`app/community/routes.py` closes at one uncovered line: a nil guard on a walk
whose foreign key forbids the nil. The marker carries the reason and names the
constraint, so a later reader can tell it from a line nobody got round to.

**464. A LOCAL USER IS NOT FINDABLE BY URL UNLESS `ap_profile_id` IS SET.**
`find_local_user` matches `ap_profile_id == actor_url` or `alt_user_name`
(`app/activitypub/actor.py:29`), and `make_user` leaves both None for a local
account -- so every `/u/<name>` route answers 404 before its own logic runs.
Registration sets `ap_profile_id`; a fixture that exercises these routes has to
do the same.

**465. THE LOOKUP CAN BE THE ACCESS CHECK.** A banned LOCAL profile is a 404 at
`find_local_user`, which filters `banned=False` unless `allow_banned=True`; a
banned REMOTE profile is found, because `find_remote_actor` does not filter for
a user URL. So `if not user.banned` further down is dead for one and
load-bearing for the other, and a row that does not know which is testing
nothing. Same reason `allow_banned=True` matters on one lookup arm and not the
other (D1040).

**466. `make_user` DOES NOT SET `ap_domain`.** `make_community` and
`make_feed` do. A route that reads `user.ap_domain` -- `user_block_instance`
does, into an `in` test -- is a TypeError on a factory-built remote user, and
that is the factory's gap rather than the route's.

**467. `current_user` IS A PROXY, AND A MOCK KEEPS THE PROXY.** Reading `.id`
off the `current_user` a `render_template` mock captured, after the request has
ended, answers None rather than raising. Assert on the template name, or on
something the route computed, rather than on the proxy.

**468. A GUARD READ AFTER A DELETE IS A GUARD ABOUT NOTHING.** D1038's warning
asked `user.is_admin()` after `delete_dependencies()` had removed the account's
`user_role` rows. When a function both destroys state and reports on it, read
the report's inputs first.

**469. CHECK WHAT ELSE ALREADY EXCLUDES THE CASE.** Three of this slice's four
mutation survivors survived because something upstream made the mutated line
irrelevant -- an inbox that was None, a lookup that already filtered, an
invariant that made a disjunct redundant. Before writing a row for a guard, ask
what would have to be true for that guard to be the thing that decides.

**470. A FIX CAN MAKE AN EXISTING BRANCH EQUIVALENT.** D1043 added `except
ValueError` around the notification-type parse, and that made the
`notif_type == 'Unread'` arm redundant: `int('Unread')` now falls into the same
answer. Nothing failed, and nothing would have shown it except the mutation
pass. Re-run the mutants over the code AROUND a fix, not only over the fix.

**471. AN EMAIL LINK CANNOT CARRY A CSRF TOKEN.** Making
`notifications_all_read` POST-only broke the "Mark all as read" button in the
notification email, which is a GET by construction. The options are a
single-use token in the link (what the unsubscribe links do) or removing the
action from the email; this round did the second and said so. A route reached
from an email is in D988's "GET by protocol" category or it is not reachable at
all.

**472. `request.files[...]` IS A 400, NOT A KeyError.** Werkzeug raises
`BadRequestKeyError`, which Flask turns into a 400 response -- so a route that
indexes `request.files` refuses every client that does not send that exact
field, and the browser form is the only one that always does. `.get()` answers
None, which the `if` below it usually already handles.

**473. A TASK'S SESSION IS NOT THE TEST'S SESSION.** `import_settings_task`
runs under `get_task_session()`, so an object handed to it through a patched
`find_actor_or_create` belongs to the test's session and writes to it are never
flushed by the task. Assert on rows the task itself created, not on counters it
incremented on a borrowed object.

**474. TWO IDENTICAL BOOKMARK TABLES, TWO ROWS.** `PostBookmark` and
`PostReplyBookmark` both lack a unique constraint, so the `if not
existing_bookmark:` in each is the only thing preventing a duplicate. Importing
the same file twice is the ordinary way a user reaches that, and no row did it
until the mutation pass asked.

**475. AN `and` CHAIN HIDES THE PROBE AS WELL AS THE DEFECT.** The first probe
for D1052 set one cookie and answered 302, which looked like "no defect here" --
`if restriction_cookie and current_max_hours and int(current_max_hours) > 0`
short-circuits before the parse. Set EVERY conjunct before concluding that a
line is unreachable.

**476. A MUTANT THAT CANNOT RUN IS NOT A SURVIVOR.** A deletion anchor that
omits the leading indentation leaves an IndentationError, and pytest then
prints no `passed`/`failed` line at all. The runner classifies anything without
`failed` as SURVIVED, so an invalid mutant reads as a gap. Treat `NO SUMMARY`
as "re-write the mutant", never as a result.

**477. A COOKIE THAT EXPIRES IN 2099 IS A PERMANENT INPUT.** D1051 and D1052
are ordinary `ValueError`s, and would be minor if the value were transient. The
cookies here are set to expire in 2099, so one corrupt value answers 500
forever, on the page the account would use to clear it.

**478. THE SAME FEATURE HAS TWO ENDS.** D1035 fixed "block this instance" on a
profile; D1053 is the same mistake on the settings form, which nothing about
the first fix touched. After fixing a defect in one entry point, grep for the
other entry points to the same action before closing the finding.

**479. A `strip()` BEFORE A `split()` EATS THE CASE YOU ARE TESTING.**
`form.urls.data.strip().split('\n')` never yields a leading or trailing empty
element, so a row that puts its blank line at the end of the box exercises
nothing. Put the blank line BETWEEN two values (D1063).

**480. NOT EVERY FIELD A GENERATOR ACCEPTS APPEARS IN ITS OUTPUT.** feedgen's
RSS writer emits no atom id, and the channel `<link>` carries whichever link
was set LAST -- so of three wrong urls in `show_profile_rss` only one could be
pinned. Check what the serialiser actually produces before claiming a row
covers a field.

**481. A FEED IS A SECOND COPY OF AN ACCESS DECISION.** D1057 is the third
surface where private-community content escaped: the cross-post form, the
sidebar fragment, and the author's own RSS. When a listing is built from `user.
posts` or `community.replies` rather than from the query the page uses, it has
no access control unless someone adds it.

**482. AN ABSENCE IS NOT REPORTED AS A BUG.** D1059 dropped every bodyless
post -- most link and image posts -- from every user feed on the instance. A
feed that is too short looks like an author who posts rarely, so nothing but a
row that asserts the bodyless post IS listed would have found it.

**483. A LIMIT CHECKED ON THE RENDER PATH IS NOT A LIMIT.** The upload quota
ran after the POST had stored the files and returned a redirect, so it could
only ever announce that the limit had been passed. Ask of every limit: which
request does it refuse?

**484. A HELPER THAT REPLACES MAKES THE CHECK AFTER IT DEAD.**
`safe_redirect_target` returns the default for an unsafe candidate, so
`if return_to.startswith('http'): abort(401)` below it could never fire
(D1066). When a guard follows a sanitiser, ask what the sanitiser can still
hand it -- and delete the guard if the answer is "nothing it would refuse",
because a dead check reads as the protection.

**485. A VALUE INTERPOLATED INTO A URL NEEDS A SHAPE, NOT A LENGTH.**
`instance_url` was `Length(min=3, max=50)` and went straight into
`https://{...}/...`. Length says nothing about paths, queries or authorities.
Validate what the field MEANS -- here, a hostname with an optional port.

**486. A COOKIE PRE-FILLS THE FORM THAT WROTE IT.** D1064's bad value is stored
for the next visit, so a single successful submission of a hostile value keeps
working. When a form remembers its input, the validation protects every later
visit as well as this one.

**487. FACT 476 REPEATS ITSELF.** The same indentation-less deletion anchor
produced another `NO SUMMARY` one slice after it was written down. When a
mutant deletes a whole line, the anchor must include the leading whitespace --
and `NO SUMMARY` is never a verdict.

**488. A ROUTE'S OWN FILTER CAN MAKE ITS BODY UNREACHABLE.**
`show_profile`'s `if user.deleted: flash(...)` cannot fire through
`/u/<name>`, because `activitypub.user_profile` filters `deleted=False,
banned=False` before calling it. The reachable path is `/user/<id>`, which
uses `db.session.get`. When a function has two callers, ask which one can
actually reach the branch before writing the row.

**489. SIX NEAR-IDENTICAL QUERIES NEED SIX ROWS.** `user_alerts` builds its
list in six arms (posts, comments, communities × mine/others/all, plus topics,
feeds and users). A "somebody else's alerts are not listed" row existed -- over
the COMMUNITIES arm -- and the posts arm's identical scoping could still be
deleted unseen. A row against one arm of a dispatch says nothing about the
others, however similar they look.

**490. A RULE WRITTEN DOWN IS NOT A RULE APPLIED.** Fact 478 ("the same feature
has two ends") was written in slice C and D1068 is the same miss two slices
later, in the same module. After fixing a parse or a guard, grep the module for
the same expression before closing the finding -- the habit has to be a step,
not a memory.

**491. A SAFE WRITE DOES NOT MAKE A SAFE PAGE.** `user_file_delete`'s DELETE is
scoped by user inside `process_file_delete`, so reading the route and checking
the destructive call would have concluded it was fine. The disclosure was the
confirmation page, which rendered the file for any id (D1071). Ask what a route
SHOWS as well as what it changes.

**492. A COPIED ROUTE KEEPS THE ORIGINAL'S VALUES.** `user_follow_request_reject`
is `user_follow_request_accept` with the activity type changed and the stored
value left behind, so it sent a Reject and recorded an acceptance (D1072). When
two routes are near-identical, diff them: the line that was supposed to differ
is the one to check first.

**493. THE COLUMN COMMENT IS THE SPECIFICATION.**
`is_accepted = db.Column(db.Boolean)  # None = request sent. True = accepted.
False = Rejected` said exactly what the reject route should write, and another
module already wrote it. A fix that has to invent a convention is usually a fix
that has not found the existing one.

**494. THE HABIT PAID.** Slice G's mutation pass killed all 23 on the first
run -- the first clean pass of this sub-project. The rows were written after
three separate "only this person's?" misses (D1032, D1063, D1069), so every
listing row was built with a second account's data in it from the start. The
gap-finding habit transfers; it just has to be applied while writing, not after.

**499. AN UNORDERED LIST LOOKS SORTED IF YOU INSERT IT SORTED.** D1076's arm
reached no `order_by`, and the row still passed -- because it created the oldest
post first and the database returned insertion order. A row that pins an
ordering has to insert the rows in the WRONG order, or it is testing the
insert.

**500. A SURVIVING MUTANT CAN MEAN THE ROW NEVER REACHED THE LINE.** The
`asc()` mutant survived not because the assertion was weak but because
`/read-posts/old` matched no arm at all -- the code under test was never
executed. Before strengthening an assertion, check that the request reaches the
line.

**495. TWO ROUTES ON ONE PREFIX SHADOW EACH OTHER.** `/read-posts/delete` is
POST-only, but a GET to it does not 405: `/read-posts/<sort>` matches first and
renders the history with `sort='delete'`. A POST-only route is only POST-only
if nothing else claims its path.

**496. AN OPTIMISATION AND THE PATH IT AVOIDS CAN GIVE THE SAME ANSWER.**
`lookup`'s `if exists:` shortcut returns the same redirect the search branch
would, so deleting it changes nothing observable in the response. Assert that
the expensive path was NOT taken -- `search.call_args is None` -- rather than
what came back (D1075).

**497. A GUARD CAN BE PRESENT AND STILL BE MISSING.** `user_upvotes` had
`if user is not None:` three lines BELOW the call that dereferenced `user`
(D1074). When a function checks for None, check where the first dereference
is, not whether the check exists.

**498. PIN A FINDING YOU ARE NOT FIXING.** D1073 is a login-flow decision, so
this round recorded today's behaviour in a row whose assertion says "update
this test (D1073)". That is the same pattern an earlier slice inherited for
D1001 -- and D1001 was found and fixed precisely because the pin turned red.

## Known noise

Two things show up in normal runs that are not bugs in this setup and do not
need re-investigating:

- Two `DeprecationWarning`s from `ldap3`/`pyasn1` (`tagMap`/`typeMap` are
  deprecated) appear in every pytest run. They come from a transitive
  dependency pulled in by LDAP support, unrelated to this test setup.
- A `FutureWarning` from `ics` 0.7.3, raised inside the library while
  serializing an event alarm, is filtered in `pytest.ini` rather than shown.
  Fact 451 and D1016 say why, and the filter names the module, the category and
  the message text so it cannot hide anything of ours.
- `./run_tests.sh --down` logs `StopSignal SIGTERM failed to stop container
  ...test-runner... resorting to SIGKILL`. `test-runner` idles on
  `sleep infinity`, which does not trap `SIGTERM`, so compose falls back to
  `SIGKILL` after its timeout. Cosmetic — the container still stops and no
  state persists (tmpfs).

**501. A FACTORY LEAVES THE RENDERED COLUMN EMPTY.** `make_post_reply` sets
`body`, not `body_html`, and the reply teaser template runs the stored HTML
through `community_link_to_href` -- which answers `TypeError: expected string
or bytes-like object, got 'NoneType'`. Any row that renders a thread has to set
`body_html` itself. The same trap sits one level up: `make_post` sets
`body_html` only for `microblog=True`.

**502. A PATCHED `render_template` HAS TO RETURN A RESPONSE.**
`continue_discussion` sets cache headers on what it renders, so
`return_value='rendered'` is `AttributeError: 'str' object has no attribute
'headers'` -- and the failure names the string, not the patch. Answer
`app.response_class('rendered')` for any view that touches the response after
rendering.

**503. `languages_for_form` SKIPS CODE `'und'`.** It is filtered out of the
"other languages" list by name, so a database whose only `Language` row is
`und` gives a `SelectField` with no choices and every `language_id`
`DataRequired` fails. A form row that has to submit a comment needs a real
language row as well.

**504. `@block_bots` IS NOT `@login_required`.** `post_reply_options` carries
only the first, so it answers anonymous callers -- and refuses them a deleted
reply's menu while serving it to any logged-in reader, because that menu is
where the restore link lives. Read the decorator list before assuming a route
has a user.

**505. `Label(field_id=...)` REACHES NOTHING IN A BOOTSTRAP-FLASK FORM.**
`post_reply_delete` rebuilds a field's label to say "my comments" rather than
"replies"; only the TEXT is observable, because `render_form` builds the
`<label>` itself and takes `for` from `field.id`. A mutant on the `field_id`
argument survives and is equivalent -- recorded rather than chased.

**506. INSERT ORDERING ROWS IN THE WRONG ORDER FIRST.** Fact 499 again, one
sub-project later: a poll row created its choices in `sort_order`, so removing
`order_by(PollChoice.sort_order)` returned the same list and the mutant
survived. Treat it as a step rather than a memory -- when a row asserts an
order, build the data in the order the query must *undo*.

**507. A SHORT-CIRCUIT HIDES THE TEST BEHIND IT.** `if post_reply.path and
len(post_reply.path) > 1:` -- every row had `path` unset, so the length test
was never reached and `> 1` could be mutated to `> 0` untouched. A falsy value
in the left operand covers the line and proves nothing about the right one.

**508. THE TEST SUITE DISABLES CSRF, AND THE TEMPLATES STILL ASK FOR IT.**
`WTF_CSRF_ENABLED = False` (tests/conftest.py) means a form has no
`csrf_token` field, so any template that calls `{{ form.csrf_token() }}` is
`jinja2.exceptions.UndefinedError: '...Form object' has no attribute
'csrf_token'`. On `show_post` the reply form is rendered **only for a
logged-in reader**, so an anonymous row renders the real page and a logged-in
one has to patch `render_template`. The error names the form, not the setting.

**509. `Headers.set` REPLACES EVERY VALUE FOR THAT NAME.** Two `set('Link',
...)` calls leave one Link header, not two -- and they also discard whatever
the framework put there earlier (the preload hints). `add` is what a
multi-valued header wants. D1086 was exactly this, and it cost the page its
ActivityPub alternate.

**510. `post_replies` RETURNS A TREE OF DICTS.** Its annotation says
`List[PostReply]`; it returns `[{'comment': PostReply, 'replies': [...]}, ...]`
built from `comments_dict`. A row that reads `.body` off a member gets
`AttributeError: 'dict' object has no attribute 'body'`. The annotation is
wrong, not the code.

**511. A CONDITIONAL EXPRESSION BINDS LOOSER THAN `+`.** `[a] + b if c else []`
is `([a] + b) if c else []`, not `[a] + (b if c else [])`. D1087 was that,
and the visible effect was a feature that silently did nothing for the common
case: `mark_post_read([], ...)` for every post with no cross-posts.

**512. A FOREIGN KEY MAKES THE "NOT FOUND" BRANCH UNREACHABLE.**
`show_post`'s `if lang:` after `db.session.get(Language, ...)` cannot be
exercised: `post.language_id` is a foreign key, so the row it names always
exists and PostgreSQL refuses `9999` outright
(`psycopg2.errors.ForeignKeyViolation`). Delete the row rather than mock the
database into an inconsistent state.

**513. THE TEST CLIENT'S COOKIE NEEDS THE SERVER NAME.**
`client.set_cookie('warned', '1')` defaults to `localhost` and is never sent
to `SERVER_NAME`, so a row that needs a cookie measures the case without it.
Pass `domain=app.config['SERVER_NAME']`.

**514. ONE AUTHENTICATED IDENTITY PER TEST.** Flask-Login caches the loaded
user on `g`, and the fixtures push ONE app context for the whole test, so the
FIRST request in a test fixes `current_user` for every request after it --
whatever client makes them. Measured: an authenticated client served
`public, max-age=30` (the anonymous branch) after an anonymous request ran
first, and an anonymous client rendering the logged-in reply form after a
logged-in request ran first. A row that needs both views of a page has to be
two rows.

**515. `login_required` VALIDATES CSRF ON EVERY POST, EVEN WITH
`WTF_CSRF_ENABLED = False`.** The decorator in `app/utils.py` calls
`validate_csrf` itself and `abort(400, ...)` on failure, and `validate_csrf`
does not consult that setting. A POST row without a token gets
`{"code":400,"status":"Bad Request"}` -- which reads like the view refusing
the request, not like a missing token. Use the `csrf()` helper for any POST to
a `login_required` route.

**516. PATCH `app.config['DEBUG']`, NOT `app.debug`.** `Flask.debug` is a
property over `config['DEBUG']`, so `patch.object(app, 'debug', True)` sets the
attribute on the CLASS and leaks into every test that follows -- the next row
saw exceptions propagate instead of being flashed. `patch.dict(app.config,
{'DEBUG': True})` is scoped to the block.

**517. `get_timezones()` OFFERS ONLY `Region/City`.** It skips any zone
without a `/`, so `'UTC'` is not among the choices and a form row submitting
it fails with `{'timezone': ['Not a valid choice.']}` -- and the failure
surfaces as the page being re-rendered, i.e. as a template error about
`csrf_token` (fact 508), not as a validation message. Use e.g.
`'Europe/London'`.

**518. `url_for('main.index')` IS `/home`, NOT `/`.** The index is registered
on both, and `url_for` builds the LAST-registered rule -- so a row asserting
where an htmx redirect lands has to expect `/home`. Asserting `'/'` fails with
`assert '/home' == '/'`, which reads like the route being wrong rather than
the expectation.

**519. A NOT-NULL COLUMN SHOWS UP AS A WARNING BEFORE IT SHOWS UP AS AN
ERROR.** Inserting `DomainBlock(domain_id=None)` logs
`SAWarning: Column 'domain_block.domain_id' is marked as a member of the
primary key ... no explicit value is passed` and only then raises
`psycopg2.errors.NotNullViolation` at flush. The warning names the column; the
traceback names the INSERT. D1099 was found by the latter, but the former was
in the log the whole time.

**520. A FIX LANDING IN A CLOSED MODULE NEEDS A ROW IN THAT MODULE'S FILE.**
D1102's guard went into `app/shared/post.py`, which is floored at 100%. The
suite passed -- 8306 rows green -- and the floor check then refused the round:
`app/shared/post.py: 99.84% is below its floor of 100.00%`, one uncovered
line, the `raise` on the API arm of the new refusal. The rows for the slice
were in the route's file; the arm the route never reaches had none. **When a
fix crosses into another module, look at that module's floor before running
the suite, not after.**

**521. AN `OPTIONS` ARM INSIDE A VIEW IS DEAD CODE.** `app/request_hooks.py`
answers every OPTIONS request in `before_request`, before any view runs, so a
`if request.method == 'OPTIONS': return ''` at the top of a route can never
execute. A row that sends OPTIONS passes -- the hook answers it -- and the
line stays uncovered no matter what the row does. D1115 was exactly this; the
arm was removed rather than left reading like the route's own contract.

**522. THE DETECTOR ARMS ARE `if` WITHOUT `else` TWICE OVER.** `post_check_ai`
tested `DETECT_AI_ENDPOINT`, then `is_ai.status_code == 200`, and had a return
for neither miss -- `TypeError: The view function ... did not return a valid
response` for a detector that answers 502. When a view is a chain of `if`s,
count the returns against the arms before writing the row (facts 448, 500).

**523. `BlockedImage.hash` IS `BIT(256)`.** Not a hex digest: PostgreSQL
refuses anything that is not 256 binary digits with
`psycopg2.errors.InvalidTextRepresentation: "a" is not a valid binary digit`,
and the error names the character rather than the column type. A row that
stands in for `retrieve_image_hash` has to answer `'1010' * 64` or similar.

**524. A HELPER THAT WRITES BEFORE IT CHECKS TURNS A 404 INTO A 500.**
`bookmark_post` called `mark_post_read` first, and `read_posts.read_post_id`
is a foreign key -- so an id that does not resolve was a
`ForeignKeyViolation` at flush, although the route around it already catches
`NoResultFound` to answer 404. The route's error handling was correct and
unreachable. D1125.

**525. A MUTANT IS KILLED BY THE FILE WHOSE ROW COVERS IT, NOT BY THE SLICE
THAT WROTE IT.** Slice F's pass ran against `tests/test_post_actions.py` and
reported one survivor -- a guard from slice D, whose row lives in
`tests/test_post_moderation.py`. Re-run against that file it died at once.
When a slice touches a line another slice pinned, point the runner at the
other slice's file rather than adding a duplicate row.

**526. `login_user` STORES THE ID AS THE COLUMN HOLDS IT.** A hand-built test
session sets `session['_user_id'] = str(user.id)`, but a real login stores
`user.get_id()`, which on this model is the integer. So a row that logs in
through the form and then asserts `session['_user_id'] == str(user.id)` fails
with `assert 2 == '2'`. Compare `str(session['_user_id'])` when the login went
through the real path.

**527. A PIN TURNING RED IS THE POINT OF A PIN.** D1131 changed the login
failure message deliberately, and four rows in
`tests/test_shared_auth_login.py` that pinned the old wording went red. They
were updated -- with the reason written into their docstrings -- rather than
worked around. A pin exists so that a change to the behaviour it records
cannot happen silently.

**528. `| head -N` KILLS A BACKGROUND RUN.** A mutation pass piped into
`head -4` stopped after its fourth line: `head` exits, the writer gets
SIGPIPE, and the runner dies with **exit code 0** -- so it looks finished
rather than truncated. Facts 476 and 487 recorded the same class of quiet
truncation from the other direction. Never pipe a long-running pass into
`head`; read the output file instead.

**529. `RegistrationForm` CARRIES A CAPTCHA UNLESS `captcha_enabled` IS
OFF.** `CaptchaField` with `DataRequired` is added in `__init__` and removed
only when `get_setting('captcha_enabled', True)` is false, so a registration
row that does not patch that setting has its POST refused by the form before
any route code runs -- and the route answers 200 (the form, re-rendered),
which reads like the route rejecting the registration rather than the form
never letting it through. Patch `app.auth.forms.get_setting`.

**530. `g.site` IS A TRANSIENT COPY, NOT THE ROW.** `before_request` builds
it as `Site(**get_site_as_dict())` -- a new object from a plain dict, never
added to the session -- so assigning to `g.site.<column>` changes the current
request and nothing else. D1137 was exactly that: the safety that closes an
abandoned open instance wrote only to `g.site`, so the door reopened on the
next request. Anything meant to persist has to load `Site` from the session
and commit, and clear `get_site_as_dict`'s 60-second memoization.

**531. PATCH THE LOGGER, NOT `current_app`.** `patch('...current_app')`
replaces a LocalProxy with a MagicMock, and `.logger.warning` off that mock
comes back as an **AsyncMock**: the call returns a coroutine nobody awaits,
and the row leaves `RuntimeWarning: coroutine
'AsyncMockMixin._execute_mock_call' was never awaited` behind -- three of
them, against a suite that is counted for warnings. `patch.object(app.logger,
'warning')` asserts the same thing and leaves nothing.

**532. A UNIQUENESS VALIDATOR ON `RegisterByMastodonForm` BREAKS LOGIN.**
That form is submitted by two different people: somebody registering, and
somebody whose account already exists coming back through the same route --
the existing-account branch sits *inside* `form.validate_on_submit()`. A
`validate_email` that refuses any address already in the database therefore
refuses the returning account its own address, and three slice B pins turned
red at once. The check belongs in the route's new-account arm, where the
question "is this somebody else's address?" is the one actually being asked.

**533. `main.index` IS `/home`, NOT `/`.** A row asserting
`response.headers['Location'] == '/'` after a successful login fails with
`assert '/home' == '/'`. `url_for('main.index')` is the only safe way to
write it, or the literal `/home`.

**534. `get_token_and_user_info` IS THE SEAM FOR ALL THREE PROVIDERS.**
Every network call Google, Discord and Mastodon make goes through it, and it
swallows every exception into `(None, None)`. Patching it with a
`(token, user_info)` pair drives the whole authorize callback without
touching `authlib`; patching `app.auth.oauth_util.oauth` is only needed for
rows about the function itself.

**535. A TEMPLATE CANNOT BE RENDERED FROM `app.test_request_context()`.**
`can_user_register`'s declined-country arm answers `render_template(...)`,
and calling it inside a bare test request context raises from inside the
template -- the page wants context the fixtures' request does not carry.
Drive the route instead and assert on `response.data`; the unit call is only
usable for the arms that answer True or a redirect.

**536. `ldap3` IS BEHIND ONE CLASS.** `app/ldap_utils.py` builds a `Server`
and a `Connection` and does everything else through the connection object, so
`patch('app.ldap_utils.Connection', return_value=MagicMock())` is the whole
seam: `conn.entries`, `conn.search.call_args`, `conn.modify`, `conn.add` and
`conn.unbind` are the entire protocol surface. Patch `Server` as well or the
constructor tries to resolve the host name.

**537. AN `ldap3` ATTRIBUTE IS NOT A STRING, AND IS NOT ONLY A MOCK.**
`sync_user_to_ldap` compares `getattr(entry, attr, None) != email` and
`login_with_ldap` reads `.value` off the same object. A `MagicMock` is never
equal to a string, so the "nothing to change" arm is unreachable with one; a
plain string has no `.value`. A `str` subclass carrying a `.value` property
satisfies both.

**538. A ROW THAT ASSERTS ONLY THE ANSWER DOES NOT PIN A SKIP.** Two rows
here checked that a disabled directory answers False -- and passed with the
`LDAP_READ_ENABLE`/`LDAP_WRITE_ENABLE` guard mutated away, because the
unpatched `ldap3` then failed to reach the host and returned the same False.
A row about something NOT happening has to assert that it did not happen:
`assert connection.call_args is None`.

**539. A PRODUCTION FUNCTION NAMED `test_*` GETS COLLECTED.**
`from app.ldap_utils import test_ldap_connection` binds that name at module
level in a test file, so pytest collects it, RUNS it as a test, counts it as
a pass and warns `PytestReturnNotNoneWarning: Test functions should return
None ... returned <class 'bool'>`. It was the 251st warning against a
baseline of 250. Import it under another name.

**540. A RUN THAT HITS `session_timeout` STILL EXITS 0, AND THE FLOOR CHECK
THEN READS A STALE `coverage.json`.** `pytest-timeout`'s session budget stops
the run and prints `!!! session-timeout: 1200.0 sec exceeded !!!`, but the
process exit status is 0, so `&&` carries on into
`check_coverage_floors.py` -- which reads the coverage.json left by the
PREVIOUS run. That produced `app/ldap_utils.py: 10.79% is below its floor of
100.00%` for a module the round had just taken to 100%: a floor breach
reported from a file written hours earlier. Read the reported test count and
the `modified` timestamp the checker prints, not just the last line. Fact 528
is the same trap wearing a different hat.

**541. A ROW THAT READS THE RENDERED PAGE CANNOT TEST WHAT THE TEMPLATE ALSO
LIMITS.** `topics_for_form` caps the topic tree at three levels and
`auth/choose_topics.html` renders three levels, so a row asserting a
fourth-level name is absent passed with the cap mutated away. The same held
for the country pre-selection: the name appears on the page whether or not it
is selected. Call the function and assert on the structure it returns.

**542. RENDERING A FORM PAGE NEEDS A CSRF TOKEN THAT THE TEST APP DOES NOT
MINT.** `auth/filter_selection.html` reads `form.csrf_token`, and with CSRF
off the field does not exist: `jinja2.exceptions.UndefinedError:
'FilterSetupForm object' has no attribute 'csrf_token'`. Either post a token
(fact 355) or patch the module's `render_template` and assert on the `form`
it was handed -- which is also the only way to see a default the template
does not render distinctly.

**543. RSA KEYPAIRS COME FROM A POOL, NOT FROM GENERATION.** A 2048-bit
keypair costs ~96ms in the test container (10 keys in 0.96s) and
`with_keys=True` appears at 407 call sites, so generating one per actor was
tens of seconds of every run spent on key material no assertion reads.
`tests/factories.a_keypair()` hands out one of sixteen generated on first
use, in rotation: a test building up to sixteen keyed actors still gets
sixteen different keys. A collision past that fails a row rather than passing
one falsely -- the rows that care assert a signature is REJECTED.

**544. A REAL `sleep` IN A RETRY PATH IS PAID BY THE SUITE.** `get_request`'s
two retry arms `sleep(random.randint(3, 10))` before the second attempt, and
three rows in test_fixup_url.py take those arms: 9.01s, 8.01s and 5.00s, 22s
of waiting whose length also varies run to run. `patch('app.utils.sleep')`
plus `assert waited.call_count == 1` keeps the backoff pinned and removes the
wait. Look for this shape whenever a row's duration is measured in seconds.

**545. THE TEST APP'S CACHE IS A NULL ONE.** `cache.get` always answers None
and `cache.set` keeps nothing, so a row that wants to prove something was
CACHED cannot do it by calling the function twice -- the second call misses
too and the row passes for the wrong reason, or fails for it. Patch the
module's `cache` and give `get` a dict's `.get`; assert on `cache.set`'s
arguments for the other half.

**546. `app.debug` HAS NO SETTER.** `patch.object(app, 'debug', True)` is
`AttributeError: property 'debug' of 'Flask' object has no deleter`, because
Flask reads it from the config. `patch.dict(app.config, {'DEBUG': True})`
does what was meant.

**547. A MagicMock SWALLOWS `del`.** `render_registration_form` does `del
form.terms`, and against a MagicMock that neither fails nor leaves a trace
worth asserting on -- `form.__delattr__.call_args_list` is not a mock. A row
that pins a deletion needs a real object with the attribute on it.

**548. `Site.admins()` ANSWERS FROM `g.admin_ids` WHENEVER IT IS SET.** API
util tests set `g.admin_ids = []` because the view functions read it, and
that same assignment makes `Site.admins()` answer **nobody** -- so a row that
creates an admin and asserts they were notified fails, with the role rows
correctly in place. Add the new admin's id to `g.admin_ids` as well.

**549. `user_access` ANSWERS TRUE FOR USER 1, WHATEVER THE PERMISSION.**
`if user_id == 1: return True` (app/utils.py). `api_baseline.user1` is
therefore an administrator for every check, so a row proving an endpoint
REFUSES an ordinary account has to use user2 or user3.

**550. A MUTANT THAT ADDS A NO-OP CANNOT FAIL.** `filter(X != None, True)`
is the same filter: SQLAlchemy drops the literal. A surviving mutant is only
evidence of a gap once the mutant is known to change behaviour -- check the
mutation, not just the survival.

**551. `edit_feed` WRITES nsfw/nsfl ONLY WHEN THE INSTANCE ALLOWS THEM.**
`if g.site.enable_nsfw: feed.nsfw = nsfw` (app/shared/feed.py:377). A row
asserting that an edit keeps or changes either flag passes whatever the code
does unless the fixture's Site enables them, because the write never happens.

**552. `join_feed` ENDS WITH `db.session.remove()`.** Its `finally` discards
the whole scoped session (app/shared/feed.py:110), so every ORM object the
caller was holding is DETACHED once it returns: an attribute written on one
afterwards is never flushed, and the endpoint's own `db.session.get` reads
the unchanged row. A test that has to change a row after calling it needs an
explicit `query(...).update({...})`, and should assert the new value landed.

**553. THE DATABASE SAYS "too long" TOO.** A row asserting that an
over-length value is refused, matched on the substring `'too long'`, passes
with the application's own guard removed: Postgres answers `value too long
for type character varying(50)` for the same input. Assert the exact message
the guard raises, and that nothing was written.

**554. A SORT ROW THAT ASSERTS A COUNT TESTS NOTHING.** Four rows here
parametrised over Hot/Top/Old/New and asserted `len(...) == 2`, which every
sort satisfies -- all four passed with the sort clause deleted. Build rows
whose ORDER differs per sort (a high `ranking`, a high vote count, an old
`posted_at`, a new one) and assert which one comes first.

**555. A FALLBACK NEEDS TWO ROWS THAT DISAGREE.** `file.file_name or
str(furl(file.source_url).path).split('/')[-1]` cannot be tested with a file
whose stored name equals the last segment of its url: both halves answer the
same string. Make them differ.

**556. A FOREIGN KEY CAN MAKE A NIL GUARD UNREACHABLE.** `if remove_file:`
after `db.session.get(File, user.avatar_id)` cannot be false, because
`user_avatar_id_fkey` stops the File row being deleted while the account
points at it -- a row that sets up the dangling id gets
`psycopg2.errors.ForeignKeyViolation` instead. Check the constraint before
deciding a defensive branch is a coverage gap; the honest answer is a
partial branch with its reason recorded, not a test of an impossible state.

**557. `get_resolve_object` ANSWERS FROM ITS FIRST LOOKUP.** It opens with
`filter_by(ap_id=query)` / `filter_by(ap_profile_id=query.lower())` for
replies, posts, communities, users and feeds -- so a fixture whose actor
carries the very url the row is about is answered there, and the dispatch the
row means to exercise never runs. Give the fixture a different
`ap_profile_id` when testing how a url is DISPATCHED.

**558. A LOOP'S `break` NEEDS A CANDIDATE AFTER THE ONE THAT FILLS THE
LIST.** `get_suggestion`'s second loop breaks at seven, and it is entered
only when fewer than seven are held -- so the break fires only if the list
reaches seven with candidates still to come. A row that simply supplies nine
names never reaches it: the query's own `limit(7)` runs out first. Give the
loop a candidate that sorts FIRST and is new (`reputation` decides the
order), with the rest already in the list.

**559. `api_baseline`'s ACCOUNTS CANNOT WRITE COMMENTS AS THEY STAND.**
`can_create_post_reply` refuses a local account whose `private_key` is None
(app/utils.py:2574), and the baseline's users have no keys -- so a row that
posts a comment fails with "You are not permitted to comment in this
community", which reads like a membership problem and is not one. Set
`private_key` on the actors, or build them with `make_user(..,
with_keys=True)`.

**560. `db.session.get(Model, None)` WARNS.** `SAWarning: fully NULL primary
key identity cannot load any object.` A nullable foreign key has to be tested
before the lookup, not after -- the suite is counted for warnings, so this
turns up as a count regression rather than a failure.

**561. A FACTORY-BUILT `PostReply` HAS NO `path`, AND THAT IS DELIBERATE.**
`PostReply.new` sets `path` and `root_id` for every comment it creates --
`[0, reply.id]` for a top-level one -- and nothing else does, so a
factory-built comment carries NULL: a state the product cannot reach, which
`get_reply_list`'s depth-first branch walks into as `TypeError: 'NoneType'
object is not iterable`.

Setting them in `make_post_reply` looks like the fix and is not:
tests/test_shared_reply_make.py's
`test_replying_to_a_parent_sets_the_path_and_the_parent_id` uses a path-less
parent ON PURPOSE, to witness `PostReply.new`'s own else-branch, and that
witness disappears the moment the factory pre-builds one -- the pin turned
red and `app/shared/reply.py` fell below its floor in the same run. The
factory is left alone; `api_baseline` sets the pair (its comment is fixed
scenery for other files), and a test that needs a realistic path builds it
itself.

**562. "IS IN THE ANSWER" DOES NOT PIN A FILTER.** Nine rows in one slice
asserted that the wanted comment was present, and every mutant that WIDENED
the filter still satisfied them. A filter is pinned by what it excludes: put
a second row in the database that the filter must drop, and assert its
absence alongside the first one's presence.

**563. coverage.py's TERMINAL COLUMN ROUNDS; THE FLOOR FILE DOES NOT.** A
module showing `99%` in `--cov-report=term-missing` can measure 98.67 in
`percent_covered`, and a floor taken from the displayed figure fails the very
run that set it. Read the number the checker reads -- the JSON report's
`percent_covered` -- before writing a floor.

**564. THE HOST'S CLOCK IS NOT UTC, AND A TEST CAN PROVE IT.** Columns in
this schema hold UTC because every writer goes through `utcnow()`, so a
`datetime.now()` anywhere near one is a bug waiting for a server outside
Greenwich. It is testable in-process: `os.environ['TZ'] = 'JST-9'` plus
`time.tzset()` moves `datetime.now()` nine hours east and leaves `utcnow()`
alone. `JST-9` is a POSIX TZ string -- a name and the offset to ADD to local
time to reach UTC -- so no zoneinfo files have to be installed in the image.
Assert the shift actually took before probing anything, or the test passes
vacuously on a host that ignored the variable, and restore the previous value
in `__exit__`.

**565. A VIEW CAN COMMIT.** `CommunityFlair.get_ap_id` (app/models.py)
computes the identity, **assigns it, and commits** -- and `flair_view` calls
it on the way out. So a response carrying an `ap_id` proves nothing about
whether the endpoint stored one, and neither does a `db.session.rollback()`
in the test, because the getter already committed. Two mutants that deleted
the endpoints' own assignments survived for this reason (D1207). When a
response field is computed by the view, pin the column, not the field -- and
check first whether the view's getter is really a getter.

**566. `CommunityMember` AND `CommunityBan` HAVE NO `id`.** Both are keyed by
`(user_id, community_id)`, so `db.session.get(Model, row.id)` is an
`AttributeError` and a re-read after the code under test has committed has to
name both columns: `CommunityMember.query.filter_by(user_id=..., community_id=...).one()`.

**567. THE ALPHA API TURNS EVERY EXCEPTION INTO A 400 CARRYING `str(e)`.**
`shared_error_handler` (app/api/alpha/__init__.py) is registered for
`Exception` on all eleven blueprints. There is no 500: an `AttributeError`
from an unchecked `db.session.get` reaches the caller as
`'NoneType' object has no attribute 'is_owner'` with a 400, having first
logged a traceback and, where `SENTRY_DSN` is set, filed a Sentry event. So
an id nobody holds is indistinguishable from a real refusal at the wire, and
the operator pays for it twice. Guard the lookup and raise a named message;
the two are told apart by what the message says.

**568. THE `app` FIXTURE IS SESSION-SCOPED, SO A CONFIG WRITE IS FOREVER.**
`current_app.config['PAGE_LENGTH'] = 2` inside one test left PAGE_LENGTH at 2
for every test that ran after it -- seven failures in two unrelated files
(`test_community_show.py`, `test_feed_reading_routes.py`) and two modules
dropped below their floors, none of them anywhere near the test that did it.
Write config through `monkeypatch.setitem(current_app.config, key, value)`,
which restores the previous value at teardown. The symptom is a full-suite
failure that does not reproduce when the offending file is run alone.

**569. `hide_nsfw`, `hide_nsfl`, `hide_gen_ai` AND `ignore_bots` ARE NOT
FLAGS.** All four are four-valued integers: 0 Show, 1 Hide completely, 2 Blur
(or Label, for gen-AI), 3 Semi-transparent -- see `hide_type_choices` in
app/auth/forms.py. Reading one for truthiness treats "show it, blurred" as
"hide it", which is the opposite of what the reader asked for. Compare
against the value you mean: `== 1` for "is it hidden", `== 2` for "is it
blurred" (which is what `Post.blurred` does). The default is 1.

**570. A LISTING FILTER FLAG THAT DEFAULTS TO TRUE FILTERS NOTHING.** The
community listing's `show_genai` defaulted to True, so `if user.hide_gen_ai
and not show_genai` could never fire -- and no request schema carried the
field, so no caller could set it either. When a request flag exists only to
OVERRIDE an account setting, its default is the one that leaves the setting
in charge.

**571. `DefaultSchema.Meta.unknown = EXCLUDE` MAKES A KEY THE CODE READS
UNREACHABLE.** `get_community_list` reads `data['q']` and `data['show_genai']`,
and `ListCommunitiesRequest` (app/api/alpha/schema.py) declares neither, so
marshmallow drops both before the function sees them: over HTTP that whole
search block is dead code. When a test calls one of these functions directly
it can pass keys no client can, which is how the block gets covered -- worth
saying out loud in the test, because coverage of it proves nothing about the
endpoint.

**572. THE API AND THE WEB DO NOT SHARE THEIR GUARDS.** `do_subscribe`
(app/community/routes.py) refuses a banned account twice over; the API's
`post_community_follow` went through `join_community`, which checks nothing,
so the same request the web refused was granted. When a rule is enforced in a
route rather than in the shared function both paths call, assume the other
path does not enforce it and probe it.

**573. THE VOTE QUOTA LIVES IN REDIS AND OUTLIVES THE DATABASE.**
`votes_cast_today` (app/models.py) reads `votes_cast_{today}_{user_id}` from
the SHARED test Redis, and `Post.vote`/`PostReply.vote` increment it, so
`db_session`'s truncation never resets it: the counters for the low user ids
climb across every run of the suite, and once one passes `VOTE_QUOTA` (240)
the next vote by that id is `429 Too Many Requests` -- in whichever test
happens to run next, with nothing in that test to explain it. `db_session`
now deletes `votes_cast_*` before each test. When a limit is enforced from
Redis rather than from a table, assume it survives the fixture and clear it.

**574. `ModLog.type` IS 'mod' OR 'admin'; THE ACTION IS IN `ModLog.action`.**
`add_to_modlog(action, ...)` (app/utils.py) writes the action name into
`action` and puts the actor's standing into `type`, so
`ModLog.query.filter_by(type='delete_post')` silently matches nothing. Filter
on `action` and the row is there.

**575. A FLAG THE RESPONSE DOES NOT SHOW IS PINNED THROUGH THE TASK.**
`private` on a vote and `report_remote` on a report both decide only what gets
federated -- `vote_for_post` passes `federate=`, `report_post` passes
`instance_ids=` -- and the JSON that comes back is identical either way. Two
mutants survived on rows that asserted the response. Patch
`app.shared.post.task_selector` and read `call_args.kwargs`.

**576. `edit_post` ONLY WRITES THE ALT TEXT BACK WHEN THE POST HAS A URL.**
The `if url and post.image:` block is what copies `image_alt_text` onto the
File, so a test that gives a post a picture but no url cannot see the
endpoint's alt-text default at all -- and the default matters, because the
block OVERWRITES whatever description is there with what it was handed.

**577. `get_post_list` BUILDS TWO QUERIES AND RUNS ONE.** It assembles a
sqlalchemy query AND a raw SQL string in parallel, and `use_faster_query`
picks between them -- so a filter added to only one of them is silently
dropped for half the requests. Two of this module's defects were exactly
that (D1227, the private communities; D1228, the url search). When testing
this function, ask the same question BOTH ways: the front page runs the raw
SQL, and narrowing by community, feed, topic, person, search text,
`liked_only` or `saved_only` switches it off.

**578. ON THE FAST PATH THE MODULE'S `order_by` IS THROWN AWAY.** The raw
SQL's own ORDER BY decides which 1000 rows are fetched, and the page is then
re-sorted by `post_ids_to_models(post_ids, sort)` (app/utils.py), which
re-implements hot/new/old/top/active/scaled and knows nothing about stickies.
So an ordering assertion on the front page proves nothing about this module,
and an instance sticky does NOT lead the page it asked to lead. Assert
ordering on the community-narrowed path.

**579. A SORT CHAIN THAT ENDS IN A FALLBACK NEEDS A ROW PER ARM.**
`elif sort.startswith("Top")` closes the Top* chain with a one-day window, so
every test whose data only reaches back a day cannot tell TopHour, TopDay,
TopWeek or TopMonth from the fallback -- eleven mutants survived on that.
Give the fixture one post per window, and assert the COUNT each window
reaches.

**580. AN UNORDERED QUERY COMES BACK IN INSERTION ORDER, WHICH IS USUALLY
SOMEBODY'S EXPECTED ORDER.** A sort whose arm is mutated away leaves the
query with no ORDER BY at all, and Postgres then returns the rows roughly as
they were written -- which matched 'New' exactly, so that mutant survived a
full-sequence assertion. Build the fixture's rows in an order that matches
neither the ascending nor the descending expectation.

**581. `get_post_list2` RUNS ITS FILTERS TWICE.** liked_only, saved_only,
hide_read_posts, the community keyword filter and the whole sort chain each
appear in two copies inside that one function. A mutant that removes one copy
survives because the other still does the work, so a single-copy mutation
pass measures nothing there -- mutate BOTH copies, or the pass will tell you
the tests are weak when they are not. The copies are not identical, and where
they disagreed the second one silently won (D1232, the title-only search).

**582. `desc()` TAKES ONE ARGUMENT.** `order_by(desc(a, desc(b)))` is not
`order_by(desc(a), desc(b))` -- it is one call to `desc()` with two arguments
and `TypeError: desc() takes 1 positional argument but 2 were given`, raised
when the query is built rather than when it runs. Eleven sorts in one
function were written that way and none of them had ever been executed by a
test. Any `desc(` with a comma inside it is worth a second look.

**583. KEYSET PAGINATION WARNS ABOUT NULLABLE ORDER-BY COLUMNS, AND MEANS
IT.** sqlakeyset emits `UserWarning: Ordering by nullable column post.score
can cause rows to be incorrectly omitted from the results` for every nullable
column in the ORDER BY. It inspects the SCHEMA, not the data, so columns that
always have a Python-side default still trigger it -- and the hazard is real
if a NULL ever lands there. Covering a keyset-paginated listing therefore
raises the suite's warning count until the columns are made `nullable=False`
by migration. Do not silence it; it is the library telling you the page can
lose rows.

**584. `Community.is_moderator()` WITH NO ARGUMENT ASKS ABOUT THE WEB
SESSION.** It falls back to `current_user.get_id()`, so in an API path -- where
the viewer is a User object the caller was handed, not a logged-in session --
it answers about the wrong person, and with no request context at all
(a function called directly, which is how these tests reach it) `current_user`
is None and it is an `AttributeError`. The same is true of `is_owner()` and
`is_member()`. Always pass the user. A test that calls a shared web/API helper
directly is the only thing that finds this, which is why D1241 survived until
`get_post_replies` got covered.

**585. `if max_depth:` IS NOT `if max_depth is not None:`.** A depth of 0 is a
real request -- the top level and nothing under it, which is how a collapsed
thread is drawn -- and it is falsy. The same trap waits on any numeric
parameter whose zero is meaningful: limits, offsets, thresholds, scores. When
the same function ALSO has an `is None` test further in, one of the two is
wrong; here the inner one was right and unreachable.

**586. A PREFETCH THAT THE VIEW CAN DO WITHOUT IS AN EQUIVALENT MUTANT.**
`post_view` re-queries the vote when `my_vote == 0`, and `reply_view`
re-queries the bookmark when it is handed None -- so deleting either prefetch
from the listing changes no answer, only the query count. Three mutants across
two slices survived on this. Either assert the query count (nothing here does)
or record them; do not chase them with more assertions on the response, which
cannot tell the difference.

**587. A COLUMN OF FREE-FORM JSON IS A ROW OF LANDMINES.**
`InstanceChooser.data` is filled in by whoever adds the row, and
`get_site_instance_chooser_search` read three keys out of it directly -- so
ONE malformed row was a KeyError that took the whole listing down for every
caller until somebody edited that row. Read such a column with `.get`, expect
a value of the wrong TYPE as well as a missing one (a `language` that is
already a string rather than an object), and give the endpoint a test with a
half-filled row in it. `Notification.targets` (D1184) is the same trap in
another table.

**588. `db.session.get(Model, None)` IS A WARNING, NOT AN ERROR.** It answers
None after `SAWarning: fully NULL primary key identity cannot load any
object`, so a guard that skips the lookup and a guard that handles the None
produce the SAME response and differ only in the warning. A mutation pass
cannot tell them apart from the answer alone -- assert the warning's absence
with `warnings.catch_warnings(record=True)` if the guard is meant to prevent
the lookup rather than survive it.

**589. THE ALPHA API IS OFF IN THE TEST ENVIRONMENT.** `enable_api()` reads
`current_app.debug or config['ENABLE_ALPHA_API'] == 'true'`, and the test
config sets neither -- so every route under `/api/alpha` answers
"alpha api is not enabled" and the body of each one is unreachable. A test
that drives these routes over HTTP has to
`monkeypatch.setitem(current_app.config, 'ENABLE_ALPHA_API', 'true')` first.
Note the STRING: the boolean `True` does not turn it on.

**590. SCHEMA VALIDATION RUNS BEFORE THE VIEW.** A flask-smorest route
decorated with `@bp.arguments(...)` rejects a request that does not match the
schema before the function body -- and therefore before `enable_api()`. So a
sweep that sends empty bodies to every route sees "Validation failed" from
most of them and cannot assert the gate's own message; assert the property
that matters (nothing answers below 400 with the API off) and pin the exact
message on the routes that need no arguments.

**591. `app.url_map` IS A BETTER TEST TABLE THAN A LIST.** The gate sweep
enumerates routes from the url_map and filters by blueprint
(`rule.endpoint.split('.')[0]`), so a route added tomorrow is covered the day
it is added rather than the day someone remembers to add a row. It also
separates the two kinds of route in that file: the gated API and the
`not_yet_implemented` placeholders on the plain blueprint.

**592. `required=True, allow_none=True` MEANS "ALWAYS SEND THIS KEY, NULL IS
FINE".** A view that writes the key only when it has a value breaks it, and
marshmallow's complaint names the field rather than the row -- so ONE
applicant with no recorded IP address turned the whole registration queue into
`400 {'registrations': {0: {'ip_address': ['Missing data for required
field.']}}}` (D1251). The mirror image is `required=True` with no
`allow_none`, which refuses a null the view has every right to send (D1250).
Driving a route over HTTP is the only thing that checks either: the utils
function's own tests never see the schema.

**593. DRIVE THE API ROUTES WITH `app.test_client()`, NOT THE UTILS
FUNCTIONS.** `tests/test_api_*.py` call `app/api/alpha/utils/*.py` directly,
which covers the logic and skips three things the route does: reading the
Authorization header, validating the request against its schema, and
validating the RESPONSE against its schema. Those three are where the
schema-versus-view defects live.

**594. THE API'S REQUEST FIELD NAMES ARE NOT GUESSABLE.** Three comment
routes name the comment `comment_reply_id` while fourteen call it
`comment_id`; `/user/follow` wants `user_id` where its neighbours want
`person_id`; `/domain/block` wants the domain's NAME; `/private_message`
wants `content` where the model says `body`; `/post/poll_vote` wants
`choice_id` as a LIST. Read the schema in app/api/alpha/schema.py before
writing the request -- a wrong guess is a 400 that names the missing field
but not the one you sent.

**595. A ROUTE TEST IS A SCHEMA TEST.** Driving a route over HTTP validates
the request against its schema, runs the body, and validates the RESPONSE
against its schema. The last of those is what the utils-level tests cannot
do, and it is where this campaign found a schema that refused its own view's
answer (D1250) and a view that omitted a key its schema required (D1251).

**596. `api_baseline`'s SITE IS PRIVATE.** `g.site.private_instance` is True
there, so every anonymous page under `login_required_if_private_instance`
redirects to `/auth/login` -- which looks like a broken route rather than a
working gate. A test of a public page sets `g.site.private_instance = False`
first; a test of the gate puts it back. `CONTENT_WARNING` is the other
redirect on that decorator, and it is 0 in the test config.

**597. AN ETag IS AN ACCESS CHECK'S BLIND SPOT.** Three times now this
campaign has found `if request_etag_matches(...): return return_304(...)`
sitting ABOVE the check that decides whether the caller may see the thing at
all -- the community feed, and now the front page's RSS. A 304 carries no
body, but it confirms the ETag, and the ETag is derived from data (a
`last_active` timestamp) the caller is not supposed to have. Refuse first,
then answer conditionally, and pin it with a test that fetches the ETag while
the door is open and presents it after it shuts.

**598. A FILTER THAT DOES NOTHING LOOKS EXACTLY LIKE A FILTER THAT MATCHES
EVERYTHING.** The modlog's moderator filter searched for the wrong variable
and returned the unfiltered log (D1254), and no assertion on "the page still
lists things" would have caught it. Pin a filter with data it must EXCLUDE:
two rows differing only in the field being filtered, and an assertion that one
of them is gone.

**599. THE HONEYPOT BANS THE TEST CLIENT'S IP FOR FOUR WEEKS.** `/honey`
(app/main/routes.py) counts visits in `honeypot:{ip}` and writes `ban:{ip}`
on the third within 24 hours -- and every test client shares one address, so
three honeypot rows in one file banned every test that ran after them, in
every file, with the symptom being an unexplained 403 from an unrelated page.
`db_session` clears `honeypot:*` and `ban:*` along with `votes_cast_*`. This
is the second kind of Redis state to catch this campaign out (fact 573); when
a feature enforces something by IP, look for where it stores that and clear it.

**600. THE GUARD IS USUALLY ON THE NEXT LINE ALREADY.** Three times now a key
has been read out of federated JSON without a membership test while the
adjacent clause tested one: `mods_data['type']` beside
`'orderedItems' in mods_data` (D1259), `announce['object']` beside
`'object' in announce` (D1260), `data['registration_mode']` beside code that
knew better (D1245). When reviewing a line that indexes a remote payload, read
the lines around it -- the codebase's own answer is generally right there, and
its absence on one line is a slip rather than a decision.

**601. A WEBHOOK THAT ANSWERS 500 IS A WEBHOOK THAT KEEPS ARRIVING.** Stripe
retries a failed delivery for days, so an unguarded key in
`/stripe_webhook` is not one error but a repeating one, and the transaction
it aborts takes the request with it. When covering a provider callback, the
question is not only "does it refuse forgeries" but "what does it do with a
payload shaped differently from the one example in the docs" -- the answer
should be 200 and a no-op, never a traceback.

**602. SIGN THE PAYLOAD; DO NOT MOCK THE CHECK.** `tests/test_user_subscription.py`
builds the `Stripe-Signature` header itself with `hmac.new(secret, b'%d.%s' %
(timestamp, payload), hashlib.sha256)`. That costs four lines and means every
good-payload test also exercises `construct_event`, so the signature path is
covered by all fifty-odd of them rather than by the three that attack it.
Patching `stripe.Webhook.construct_event` would have covered neither.

**603. `respx` CANNOT ASSERT A ROUTE WAS NOT CALLED.** `http_mock` runs with
`assert_all_called=True`, so registering a route in order to assert
`route.call_count == 0` fails the test for the opposite reason. The way to
pin "this must not reach the network" is to register nothing and assert
`len(http_mock.calls) == 0`: an unexpected request then fails as unmocked.

**604. AN EQUIVALENT MUTANT IS A FINDING, NOT A FAILURE.** Dropping the
`if subscription_id` guard in the Stripe webhook survives because writing
null over null emits no UPDATE. The honest response was to say so in the
module docstring and the findings table, and to correct the claim the test
had been making -- not to invent an assertion that could not be true.

**605. A SET IS NOT AN ORDER, AND `next(iter(a_set))` IS A DIFFERENT ANSWER IN
EVERY PROCESS.** `safe_order_by` chose its fallback sort column that way
(D1266). Python randomizes string hashing per process, so two workers sorted
the same page differently and one in eight raised `AttributeError` on a name
the model did not have. The suite caught it by failing on a run where nothing
had changed -- which is the only way it CAN be caught, because a rerun in the
same process is a rerun with the same seed. When a fallback has to pick one
of several names, sort them.

**606. A FAILURE IN A FILE YOU DID NOT TOUCH IS STILL YOURS TO EXPLAIN.** The
run that was meant to ratchet sub-project 88's floors failed in
`test_main_modlog.py`, committed days earlier. The cheap reading is "flaky,
rerun it". Reproducing it standalone took one command and found a production
defect reachable from a URL. Rerun to CONFIRM, never to dismiss.

**607. READ THE TEST COUNT, NOT THE LAST LINE (again -- fact 540).** That
same run also printed `1 failed, 9642 passed` against a suite of ~10,340 and
exited 0, because `session_timeout` had been spent. Two distinct problems in
one summary line, and the exit code reported neither.

**608. A KEYWORD ARGUMENT PASSED POSITIONALLY IS A SILENT REORDERING.**
`purge_content(self, soft=True, flush=True)` called as `purge_content(flush)`
put the CDN flag into `soft` (D1273), so a moderation action did the opposite
of what was asked on both axes and hard-deleted content irreversibly. The
sibling call three files away passes it by keyword. When a function's
parameters are two booleans in a row, pass them by name and assert
`call_args.kwargs` rather than `call_args.args` in the test -- which is what
caught this one.

**609. WHEN TWO FUNCTIONS ARE NEAR-COPIES, FIX BOTH.** `search_for_community`
and `search_for_user` share a shape line for line. D1258 fixed the handle
unpack in one of them; D1268 is the same line, still unfixed, in the other,
found a fortnight later. After fixing a defect, grep for its shape before
closing the round.

**610. TWO GUARDS CAN COVER FOR EACH OTHER AND HIDE A MUTANT.** Removing the
`isinstance(links, list)` check survived, because the per-link
`isinstance(links, dict)` check caught every input the tests had. The killing
case was `{'links': 5}` -- not iterable at all, so the per-link check never
runs. When a mutant on a guard survives, ask which OTHER guard is covering
for it, and find the input that only the first one catches.

**611. THE TAB THAT IS SHOWN FIRST IS THE ONE TO CHECK.** The user profile
has three lists of the same content: posts, replies, and the two interleaved.
Two of them filtered the private flag and the one shown by default did not
(D1271). Where the same data is assembled more than once, compare the
filters side by side rather than reading each on its own -- the odd one out
is the finding.

**612. WHEN A FIX HAS A SHAPE, SWEEP FOR IT THE SAME DAY.** `name, domain =
handle.split('@')` was fixed four times in four rounds (D1174, D1258, D1268,
then seven more sites in D1275) because each round fixed only the copy it
tripped over. One `grep -rn "\.split('@')"` found the rest in a minute. A
defect with a greppable shape is a sweep, not a finding.

**613. A GUARD THE TEST CANNOT KILL MAY BE UNREACHABLE, NOT UNTESTED.** Two
of the seven guards survived every mutant because an earlier guard on the
same path always fired first. The answer was to delete them and say why in a
comment, not to contrive a test. Check what is upstream before assuming a
surviving mutant means a missing case.

**614. REMOVING STATEMENTS CAN DROP A PERCENTAGE WITH NOTHING UNCOVERED.**
`app/api/alpha/utils/misc.py` fell from 98.03% to 97.994% and failed its
floor with `missing_lines: []` -- two covered statements had been deleted, so
the same partial branches were a larger share of a smaller denominator. The
fix is to cover one more arc, never to lower the floor.

**615. A BRANCH WITH NO COVERAGE MAY HAVE NO CALLERS THAT SURVIVE IT.**
`feed_view`'s private branch raised `IndexError` on its second line for every
feed the instance hosts (D1276), so it had never returned since it was
written. Nothing in the suite reached it and nothing in production could get
past it. When a branch shows 0% and the feature it serves is one people use,
the question is not "which test is missing" but "does this branch work at
all".

**616. SERIALISERS ARE WHERE ACCESS IS DECIDED, NOT JUST WHERE DATA IS
SHAPED.** `app/api/alpha/views.py` holds four "you are not a member" refusals
that no route repeats. Covering a route does not cover them, because the
route hands the view an id and the view decides. Cover the view function
directly, from both sides, for every variant that has one.

**617. `query.filter(...)` WITHOUT THE ASSIGNMENT IS A NO-OP, AND IT READS
LIKE A FILTER.** `comments.filter(PostReply.score > -20)` appears twice in
`app/post/util.py` and has never run (D1277). The lines around it all say
`comments = comments.filter(...)`. When covering a query builder, check that
every filter is assigned -- a dropped result is invisible in review, invisible
in coverage (the line IS executed), and only a test that asserts the filtered
row is absent will catch it.

**618. A NO-OP WHOSE REPAIR CHANGES THE PRODUCT IS RECORDED, NOT FIXED.**
Making that filter work would hide whole subtrees from logged-out readers and
answer a permalink with nothing. That is a decision for whoever owns the
product. The campaign's job there is to pin today's behaviour, say in the
test's own docstring that it is pinning a no-op, and write the finding -- so
the change is visible when somebody makes it.

**619. A METHOD NOTHING CALLS CAN BE WRONG IN WAYS NOTHING NOTICES.**
`set_cc_bcc` raised `AttributeError` on its second line, ignored both of its
arguments, and would have put a list where an address belongs (D1278). It sat
there for as long as the file has existed. When a module's coverage gap is a
method with no callers, check whether it works before writing a test for it --
the answer here was to delete it.

**620. MOCK THE LIBRARY, NOT THE MODULE UNDER TEST.** `tests/test_email.py`
patches `smtplib.SMTP` and `boto3.client` and then asserts on the message
that WOULD have gone out -- headers, recipients, return path. Patching
`send_email` instead would have covered the four callers and nothing of the
sending, which is where all the behaviour is.

**621. `current_user` IN A CELERY TASK IS None.** `delete_post_from_community_task`
asked it for a block list (D1280); `post_replies` asked `is_moderator()` with
no argument, which falls back to it (D1241). Both ran fine in the web request
that queued them and failed in the worker that ran them. When covering a
task, call it DIRECTLY -- no request context -- and any reference to the
session user fails immediately instead of only in production.

**622. A DEDUPE THAT COMPARES OBJECTS IS NOT A DEDUPE.**
`tag_to_append not in return_value` looked right and did nothing, because the
factory it calls queries without flushing and hands back a fresh pending row
each time (D1282). The test that caught it committed and then counted the
rows -- `len(result) == 1` alone would have passed on the object identity
while the database grew a duplicate. When a function dedupes rows it also
creates, assert on the DATABASE after a commit, not on the list it returned.

**623. AN ALLOWLIST SHARED BY TWO FUNCTIONS IS A CONTRACT NEITHER OF THEM
STATES.** `allowed_extensions` is used by `save_icon_file`, which has an
`.svg` branch, and by `save_banner_file`, which has none -- so the list
promised something one of its two readers could not do, and an SVG banner was
a 500 (D1283). When one module-level constant gates two code paths, check
that BOTH paths handle every value in it.

**624. PILLOW'S FORMAT NAME IS NOT THE FILE EXTENSION.** `img.format` is
`HEIF` for a `.heic` file, `JPEG` for `.jpg`, `MPO` for some JPEGs. Code that
compares `'.' + img.format.lower()` against a list of extensions silently
refuses the ones whose two spellings differ (D1284). Test every format the
allowlist names, with a real file of that format -- the mismatch is invisible
in review.

**625. A THIRD-PARTY WARNING GETS A PINNED FILTER, NOT A BLANKET ONE.**
`botocore` added 20 `DeprecationWarning`s the moment the S3 branch was
covered. The filter added for it names the module, the category AND the
message, so a DeprecationWarning with the same text from our own code still
shows. That is the third such filter in `pytest.ini` and each one carries the
count it was measured against.

**626. `return` IN A LOOP OVER REMOTE DATA IS A BLAST RADIUS.** The backfill
answered one unusable outbox entry with `return`, so everything after it was
discarded -- and an outbox arrives in whatever order the remote sent (D1286).
The branch six lines above it had been given a `continue` two rounds earlier.
When a loop processes items from outside, every refusal in it should be
`continue`; a `return` is a claim that the rest of the batch is worthless.

**627. AN UNFLUSHED ROW HAS NO id, AND READING IT LOSES DATA SILENTLY.**
`find_language_or_create` adds a new `Language` without flushing, so
`language.id` was None and the reply kept no language (D1287). Nothing
raised. The test that catches this asserts on the RELATIONSHIP
(`reply.language.code`), not on the id it was given -- an assertion on
`language_id is None` would have looked like the correct answer.

**628. A NAME USED BEFORE IT IS ASSIGNED IS INVISIBLE UNTIL THE LINE RUNS.**
`extra_args = {'ContentType': content_type}` sat above every assignment to
`content_type` (D1288), so the whole task raised `NameError` on the instances
it exists for -- and nothing in the suite reached it, because nothing covered
the S3 branch. Python will not tell you at import time. When a function has a
branch that only a particular configuration takes, cover that configuration
or the branch is not code, it is a guess.

**629. A MUTANT THAT SURVIVES BECAUSE THE TEST'S FIXTURE IS TOO QUIET.** Four
survived here for that reason alone: the community had no topic, so
"silencing clears the topic" could not fail; the remote instance had no
inbox, so "a remote account is not announced" could not fail. The fix is in
the test, not the assertion -- set the state the assertion is about, then
assert.

**630. A GATE IS ONLY TESTED WHEN SOMEBODY IS TURNED AWAY BY IT.** Every
admin route in sub-project 98 is exercised by an account holding exactly ONE
permission -- the one the route names for the positive case, a DIFFERENT one
for the negative. A test that only signs in as user 1 (who passes every check,
fact 347) proves nothing about which permission the route asked for, and the
mutant that swaps one gate for another survives it.

**631. `permission_required` REDIRECTS; IT DOES NOT ABORT.** A refusal is a
302 to `auth.permission_denied`, not a 401, and on most admin routes it is the
OUTER decorator, so it answers before `login_required` -- a logged-out visitor
is told the permission is missing rather than asked to log in. Assert the
redirect target, not a status code, or the test passes on any 302 including
the login one.

**632. `session['_user_id']` IS AN int AFTER `login_user` AND A str AFTER A
HAND-WRITTEN COOKIE.** `User.get_id()` returns an int in this codebase, so a
test that signs somebody in by writing the session and then checks what
`login_user` wrote compares `'2'` against `2`. Normalise both sides.

**633. `@cache.cached` ON A ROUTE MAKES THE SECOND TEST READ THE FIRST'S
ANSWER.** `/sitemap.xml` is cached for 6000 seconds, so a test that asserted a
post was absent was reading a body rendered before the post existed -- and the
mutant that dropped the `indexable` filter survived because of it. Clear the
cache at the top of each test that exercises a cached route, and assert on
something the template actually emits (the sitemap prints urls, not titles).

**634. A DEBUG-ONLY ENDPOINT IS STILL AN ENDPOINT.** `/test_email`,
`/test_s3`, `/test_ldap` and `/test_ldap_login` send mail, write to object
storage and bind to a directory server. `debug_mode_only` is the whole of
their protection, so it gets a test on both sides for each of them -- 403 in
production, and in debug the thing it claims to test actually being called
with what it was given.

**635. TO ASSERT A REQUEST WAS NEVER MADE, DO NOT USE `http_mock`.** It runs
with `assert_all_called`, so registering the route you expect nobody to call
fails the test for the opposite reason, and NOT registering it makes an
unexpected call fail as unmocked -- which looks the same as the guard working.
`tests/test_ap_new_instance_profile.py` drives that one case through
`patch('...get_request', side_effect=record)` and asserts on the list of urls
asked for. Fact 603 is the simple version of this; this is what to do when the
code under test swallows the unmocked-request error.

**636. A GUARD WHOSE EXCEPTION IS ALREADY SWALLOWED IS AN EQUIVALENT MUTANT.**
`if 'software' in node_json` sits inside a bare `except: return`, so removing
it changes nothing observable -- the KeyError lands in the same place the
guard's absence does. Recorded as equivalent rather than chased, which is the
same disposition as rounds 113, 118 and 121.

**637. TWO BRANCHES THAT BUILD THE SAME THING ARE WHERE THE COPY-PASTE BUG
IS.** `make_image_sizes_async` writes a medium copy and a thumbnail with
near-identical blocks. The medium one recorded `medium_image.width`; the
thumbnail one recorded `image.width` (D1291). Reading either block alone shows
nothing. Diff the two, or assert a property that distinguishes them -- here,
that the thumbnail's recorded width is no bigger than the thumbnail width
asked for.

**638. A VARIABLE ASSIGNED INSIDE AN `if` AND READ OUTSIDE IT IS A BUG WAITING
FOR THE DEFAULT CONFIGURATION.** `medium_image` was assigned only when a
resize or a re-encode was needed, and read unconditionally (D1292). It never
fired in development, because a configured `MEDIA_IMAGE_MEDIUM_FORMAT` keeps
the branch true -- it fires on an instance that has configured nothing, with a
small image. Test with the config EMPTY as well as set.

**639. A SUBCLASS THAT LOOSENS A VALIDATOR INHERITS THE PARENT'S ASSUMPTIONS.**
`CreateImageForm.image_file` is `DataRequired()`, so its `validate` may read
`request.files['image_file']` by key. `EditImageForm` makes the same field
`Optional()` and inherits that method, so the key it relies on is gone and the
edit is a 400 (D1293). When a form subclass changes a field's validators, test
the INHERITED validate against the new possibility.

**640. A CHECK BEFORE `super().validate()` RETURNS IS DEAD IF A FIELD
VALIDATOR ALREADY COVERS IT.** Three `if x.data.strip() == '':` arms and one
`if not password.data: return` were all unreachable behind `DataRequired()`.
Before writing a test for a validator's first branch, ask what the field's own
validators have already refused -- if the answer is "this exact input", the
branch is dead code, not a gap.

**641. A MEMBERSHIP TEST IN A LOOP'S CONDITION COVERS ONE ITEM, NOT THE
LOOP.** `Post.new` guards with `'type' in request_json['object']['attachment'][0]`
and then reads `attachment['type']` for every entry (D1294). The first
attachment is the only one that was ever checked. When a condition indexes
`[0]` and the body iterates, the body needs its own guard.

**642. PATCH WHERE THE NAME IS DEFINED WHEN THE MODULE IMPORTS IT LATE.**
`app/models.py` imports `blocked_phrases` and `opengraph_parse` from
`app.utils` INSIDE `Post.new`, so `patch('app.models.opengraph_parse')` raises
`AttributeError: does not have the attribute` -- the name never exists on the
module. Patch `app.utils.opengraph_parse` instead. A function-level import is
the tell.

**643. THE TEST CACHE DOES NOT CARRY A VALUE BETWEEN TWO CALLS IN ONE TEST.**
`PostReply.new`'s "only report this account once" rule reads a redis key the
first report writes. Asserting it by making two replies and counting reports
fails -- the cache the suite uses answers None every time. Patch
`app.models.cache.get` for the already-reported case and
`app.models.cache.set` for the writing case, and assert each separately.

**644. SETTINGS THAT GATE A REFUSAL NEED BOTH SIDES, OR THE `and` IS FREE.**
`if reply_is_just_link_to_gif_reaction(...) and site.enable_gif_reply_rep_decrease:`
-- a test that only turns the setting ON leaves the mutant that drops the
setting from the condition alive. Every gated refusal here is tested with the
setting on AND off, with the detector forced true both times.

**645. A GUARD ON A PATH IS ONLY TESTED IF THE FILE EXISTS.** Three
`move_file_to_s3` tests asserted a path was left alone -- with paths that were
not on disk, so `os.path.isfile` was doing the work and the mutant that drops
the `app/static/media` prefix check survived. Create the file, then assert the
path is unchanged AND the file is still there.

**646. `.days` ON A timedelta FLOORS, SO MIXING MIDNIGHT WITH A REAL TIME
LOSES A DAY.** `days_to_add_for_next_month` subtracted a scheduled datetime
from a midnight one (D1297): 30 days 12 hours reads as 30. Any arithmetic that
means "how many days between these two DATES" should subtract `.date()` from
`.date()`, and the test that catches it needs a time of day that is not
midnight.

**647. TWO TESTS WITH THE SAME NAME IN ONE CLASS: THE FIRST SILENTLY VANISHES.**
`tests/test_ap_instance_metadata.py` had `test_a_request_that_names_no_addresses_at_all`
twice in one class -- once for the IP endpoint, once for the email one. Python
kept the second, pytest reported one test, and the count still went up. The
mutation pass is what found it: the mutant on the IP endpoint survived while
the identical one on the email endpoint died. A surviving mutant whose twin
dies is a strong hint that the test you think covers it is not running.

**648. A SWEEP THAT SENDS NOTHING TESTS THE VALIDATOR, NOT THE ROUTE.** The
alpha API gate sweep posted `{}` to every route, so flask-smorest refused most
of them at 422 and the `if not enable_api()` line below was never executed --
the property held, and ninety refusals stayed uncovered. flask-smorest records
the schema on the view (`view._apidoc['arguments']['parameters']`), so a
minimal valid payload can be synthesised from it: required fields only, values
from the field type and from any `OneOf`, `Range` or `Length` validator. Assert
how many routes reached the gate, not just that none answered.

**649. A NEW TEST MUST NOT ADD COPIES OF A WARNING THE SUITE ALREADY COUNTS.**
Exercising `/api/alpha/post/list2` with the API on adds three sqlakeyset
nullable-column warnings -- the same kind the campaign has recorded as needing
a migration. The count is a ratchet like the floors, so that route is excluded
from the half of the sweep that turns the API on, with the reason written
beside it, rather than the filter being widened to hide them.

**650. A TEST THAT WRITES INTO `app/static/media` MUST CLEAN UP.** The
ban-list import saves its upload there before handing it to the task, and
`tests/test_admin_federation.py` asserts that directory holds no `*.json` --
so a new test that imported a list broke an old one that had nothing to do
with it. Record what is in the directory before, delete the difference in a
`finally`.

**651. ONE FIX, FIFTEEN COPIES LEFT.** `request.files['name']` was found in
2026 as D1047 and fixed in one file, with a comment explaining the failure.
Fifteen other routes kept it (D1299). The sweep that finds them is one
`grep -rn "request.files\["`, and the test that keeps them gone is a property
over the source of the six files -- both cheaper than the four rounds it took
to notice.

**652. A TYPO IN ONE BRANCH OF THREE IS WHAT THE OTHER TWO ARE FOR.**
`process_report` builds nearly the same `targets_data` three times; two spell
it `user.user_name` and the third `user.name`, which does not exist (D1300).
Reading the third alone shows nothing wrong. When a function repeats a dict
literal per type, diff the copies -- and give each branch a test, because the
branch that is wrong is the one nothing exercised.

**653. AN ARM A FOREIGN KEY MAKES UNREACHABLE IS STILL WORTH A TEST.**
`process_report`'s `source_instance` can only be None if the Instance row is
gone while an account still points at it, which the schema forbids. The test
simulates it at the session -- a small wrapper whose `get` answers None for
`Instance` and delegates everything else -- rather than pretending the
database can produce it. That says exactly how reachable the guard is.

**654. WHEN TWO COUNTERS MOVE TOGETHER, TEST BOTH OR NEITHER IS TESTED.**
`Post.vote` moved the score by 2 on a reversal and the author's reputation by 1
(D1302), and every test of voting that existed looked at the score. The score
was right. Two quantities updated from one event need an assertion each, and the
useful one is the property that ties them: the same votes standing must give the
same reputation whatever order they were cast in.

**655. A METHOD WITH A TWIN: DIFF THEM BEFORE TESTING EITHER.**
`Post.vote` and `PostReply.vote` are the same method written twice, and three of
this round's five defects are things one of them does and the other does not
(D1303, D1304, D1305) -- a refusal, an exemption, a gate. The same shape gave
D1196 in an earlier round on the same pair. `diff <(sed -n 'a,bp' file) <(sed -n
'c,dp' file)` is a minute, and every difference it shows is either deliberate or
a defect.

**656. A GATE ON ONE PATH IS NOT A GATE.** `vote_for_reply` checked
`can_upvote`/`can_downvote` on its API path and not on its web path (D1306), so
the permission was real for scripts and absent for browsers. When a function
serves both `SRC_API` and `SRC_WEB`, read the two branches side by side and test
every gate through both -- and when the twin function (`vote_for_post`) gates
both, that is the specification.

**657. AN INTEGER COLUMN QUIETLY ROUNDS A FLOAT.** `Post.score` is
`db.Column(db.Integer)` and `spicy_effect` is a float, so `SPICY_UNDER_60=1.5`
stores 2. A test that asserted 1.5 would be a test of a fiction. Measure what
the database gives back, not what the Python line computes.

**658. A TREE IN ONE INTEGER COLUMN HAS NO TREE INVARIANTS.**
`Topic.parent_id` is `db.Column(db.Integer)` -- no foreign key, so nothing says
the parent exists, and nothing says following parents terminates. Both
assumptions were made: deleting a parent hid its children (D1310) and a cycle
hung `Topic.path()` for ever (D1308). For any self-referential column with no
constraint behind it, test three cases -- a parent that is gone, a cycle of one,
and a cycle of two -- and put a bound on every walk that follows it.

**659. A FORM'S CHOICES ARE A PERMISSION.** `topics_for_form` excluded the topic
being edited and then listed its children anyway, so the edit form OFFERED the
cycle (D1307). A `SelectField` validates what it is given against its choices,
so withdrawing the offer was also the fix for the POST -- and asserting the
choices is a cheaper test than asserting the refusal. Test both: the list, and
the request that the list rejects.

**660. `flash(_('...', 'error'))` IS A 500, NOT A CATEGORY.**
`flask_babel.gettext` takes one positional argument, so a category written
inside `_()` instead of beside it is `TypeError: Domain.gettext() takes 2
positional arguments but 3 were given` (D1309). `grep -rn "_('.*', '\(error\|warning\|success\)')" app/` finds the shape, and the test that
catches it is any test of the branch that flashes.

**661. A CASCADE MAKES A GUARD LOAD-BEARING, SO SAY SO IN THE TEST.**
`Topic.communities` cascades `"all, delete-orphan"`, so the "cannot delete topic
with communities assigned to it" branch is all that stands between an admin's
click and the deletion of every community in that topic. The test asserts the
communities are still there, not only that the topic is.

**662. `int(request.args.get(...))` IS A 500 WAITING FOR AN EMPTY SELECT.**
Nine sites did this (D1311-D1313). The value that finds them is not a word but
`''` -- what a `<select>` submits when nothing is chosen -- and a missing
parameter gives `TypeError` rather than `ValueError`. `request.args.get(name, 0,
type=int)` answers the default instead. Sweep with
`grep -rn "int(request.args.get(" app/` and keep a property test over the files
that read query arguments.

**663. AN ID FROM THE CALLER NAMES A ROW THAT MIGHT NOT EXIST.**
`db.session.get(Feed, feed_id).user_id` sat one line above `abort(404)`
(D1314), and `db.session.delete(item)` sat below a `.first()` that can answer
None (D1316). Both are 500s for a value anybody can type. For every id that
arrives in a request: test the id that resolves, the id that does not, and -- if
a pair is looked up -- the pair that is not there.

**664. A STATE-CHANGING GET MUST BE IDEMPOTENT, SO TEST IT THREE TIMES.**
`/feed/add_community` is a GET, and running it twice made a second FeedItem and
counted it again (D1317). The test asks for the same thing three times and
asserts one row and one count. Any route that writes on GET deserves that shape.

**665. A KILLED MUTATION RUN LEAVES ITS MUTANT BEHIND.**
The restore in the runner's `finally` (and its sha256 check) does not run if the
process is killed. After any interrupted mutation pass, `git diff` the files it
touches before trusting a green suite -- one mutant was still applied here, and
the tests passed with it in place, which is exactly what the surviving-mutant
report then showed.

**666. `current_user` IS CACHED ON THE APP CONTEXT, WHICH A TEST HOLDS OPEN.**
flask-login stores the resolved user on `g` (`_login_user`), and a test that
makes two requests with two clients reuses the app context the fixture pushed --
so the second client is answered as the FIRST user. Two probe rounds read as an
authorisation swap because of it. `g.pop('_login_user', None)` before each
request, or one client per test. The symptom to watch for: a permission answer
that is right for the caller you logged in first and wrong for every caller
after.

**667. A USER ID IN A BOOLEAN COLUMN IS A `ValueError`, NOT A TRUTHY 1.**
`is_owner=new_owner_user.id` looks like it would coerce. SQLAlchemy's Boolean
refuses it: `StatementError: (builtins.ValueError) Value 6 is not None, True, or
False` (D1318). Under a bare `except` that is silence. When a column is Boolean,
assert the stored value `is True`, not that it is truthy.

**668. A FORM THAT VALIDATES THE RAW VALUE VALIDATES THE WRONG THING.**
`validate_new_url` compared the field against existing names while the route
slugified before writing (D1319). Any transform between validation and the
INSERT has to be applied in the check too. The test that finds it submits a value
that differs from its own slug -- 'My Community' against an existing
`my_community`.

**669. FORCE A FAILURE THE WAY THE REAL ONE FAILS.**
A monkeypatched `commit` that raises `IntegrityError` does NOT poison the
transaction, so it cannot show that a missing rollback breaks the session -- the
mutant survived against it. A real `UniqueViolation` does. Where the point is the
state the database is left in, cause the error in the database (here by
switching off the form check that normally prevents it) rather than in Python.

**670. THE SUITE RUNS IN PARALLEL, AND WHAT THAT COSTS TO KEEP.**
`./run_tests.sh tests/` runs on four workers (`-n 4 --dist loadgroup`); a run
that NAMES a path stays serial, so every mutation pass keeps the old, cheap
path. Each worker gets its own database, copied from the migrated one with
`CREATE DATABASE ... TEMPLATE` (about a second, against eight for a migration
replay), and its own pair of Redis databases. Measured 23:14 serial against
7:58 parallel, same 11,909 tests. What a new test has to respect: it must not
depend on another test file's rows, and if it touches `app/static/` it must be
in the shared-static group (conftest groups it automatically if it NAMES the
path; if it only reaches that tree through app code, add it to
SHARED_STATIC_MODULES -- re-measure with `PYFEDI_WATCH_STATIC=1`).

**671. VERIFY THE MECHANISM BEFORE TUNING ITS INPUTS.**
Three full parallel runs were spent adding modules to an `xdist_group` that was
never honoured: xdist reads that mark in its OWN
`pytest_collection_modifyitems` and rewrites the node ids, so a mark added by a
later hook is invisible and the scheduler quietly falls back to distributing
test by test. `@pytest.hookimpl(tryfirst=True)` fixed it. One `-v` run grepped
for `[gw0]`/`[gw1]` would have shown a single module running on two workers at
once -- check that the machinery does what you think before deciding your
inputs to it are wrong.

**672. A BEFORE/AFTER LISTING MISSES THE FILE THAT WAS THERE IN BETWEEN.**
The first attempt to find which tests write to `app/static/` snapshotted the
tree around each test and compared. That finds nothing for a test that writes a
file and cleans it up -- which is exactly the upload tests, and exactly the
files another worker trips over. Intercepting `builtins.open`, `os.unlink` and
friends finds them, and attributes each call to the process that made it, so
the measurement can run in parallel itself.

**673. A WARNING RAISED AT IMPORT IS COUNTED ONCE PER PROCESS.**
`ldap3`'s two pyasn1 deprecations were 2 warnings serially and 10 on four
workers plus the controller, which made the suite's warning count -- a ratchet
in this campaign -- depend on how it was run. They are pinned in pytest.ini's
`filterwarnings` with that reasoning. Any warning emitted at import time
behaves this way; a per-test one does not.

**674. A DEPRECATION WARNING IS A DATED OUTAGE.**
`httpx_client.request(..., data=body_bytes)` worked and warned; `data=` is
httpx's form-encoding argument, and raw bytes belong in `content=`. It was 199
of the suite's 455 warnings AND every outbound federation request, so the day
httpx drops the compatibility this instance stops federating (D1322's round).
Treat a third-party deprecation in OUR call as a defect with a deadline, not as
noise to filter.

**675. A COMPARISON THAT NEVER MATCHES MAY BE HOLDING THE SYSTEM UP.**
`if method == "POST"` never fired, because the type is `Literal["get", "post"]`
and every caller passes lowercase. Correcting it to `.lower()` would have been a
regression: the caller reads 4xx RESPONSES to mark a peer gone forever, repair a
membership and process a ban, and a raise lands in its `except Exception`
instead (D1322). Before waking dead code up, read what the caller does with the
value it currently gets.

**676. COVERING A LINE IS NOT THE SAME AS ASKING WHO REACHES IT.**
A branch raising on a 4xx POST had a passing, parametrised test with a careful
docstring -- which reached it by passing `method='POST'`, a value no caller in
app/ passes (D1322). The test made dead code look load-bearing and would have
failed anybody who removed it. When a test has to supply an unusual value to
enter a branch, say in the docstring which caller supplies that value in
production; if none does, that is a finding, not a test.

**677. A NULL IN A KEYSET SORT COLUMN HIDES THE ROW FROM EVERY PAGE.**
sqlakeyset pages with `WHERE (sort columns) < (the last row's values)`, and a
NULL makes that predicate NULL rather than true -- so the row appears on no page
while the count still includes it (D1323). Its warning is not noise. When a
column is ordered by, it wants NOT NULL in the DATABASE and `nullable=False` in
the MODEL: the first stops the data, the second is what sqlakeyset reads.

**678. A COLUMN ONLY IN SOME QUERIES NEEDS THE QUERY THAT USES IT.**
`sticky` and `instance_sticky` enter the post ORDER BY only for a
community-scoped list with stickies left in, so the six plain sorts could not pin
their nullability and a mutant restoring it survived. When pinning a column-level
property, check which call shapes actually mention the column -- `grep -n
'<column>' <module>` and read the conditions above each hit.

**679. MAKING A COLUMN NOT NULL IS A MIGRATION WITH AN OUTAGE IN IT.**
`SET NOT NULL` scans the table under ACCESS EXCLUSIVE, which on a big table is a
write outage. PostgreSQL 12+ accepts a validated CHECK constraint as proof
instead: `CHECK ... NOT VALID`, `VALIDATE CONSTRAINT` (SHARE UPDATE EXCLUSIVE,
reads and writes continue), `SET NOT NULL`, `DROP CONSTRAINT`. Migration
c4f1a9d7e2b8 does it that way and says so, because the next NOT NULL will be
copied from it.

**680. NOT NULL WITHOUT A SERVER DEFAULT BREAKS EVERY RAW INSERT.**
Making seven `post` columns NOT NULL was not enough: the ORM's `default=` is
Python-side, so a raw `INSERT INTO post` that omits them fails with `null value
in column "score" ... violates not-null constraint`. Migration c4f1a9d7e2b8 sets
the same defaults in the schema, and a test inserts a row with `text()` to pin
it. When adding NOT NULL, add the server default in the same migration and test
the raw path, not only the ORM one.

**681. A RUN THAT LOST ITS WORKERS REPORTED "7073 passed".**
Four xdist workers died, xdist quietly started replacements, and its own
scheduler then raised `KeyError: <WorkerController gw5>` -- after printing a pass
count 4,800 tests short of the suite. `--max-worker-restart 0` makes a dead
worker fail the run instead. Any harness that can silently run FEWER tests than
it collected needs that setting, because a short green run is worse than a red
one.

**682. `patch('...current_app')` GIVES YOU AN AsyncMock.**
`unittest.mock` picks AsyncMock when `_is_async_obj(original)` is true, and that
asks `inspect.isawaitable`, satisfied by anything with `__await__` -- which
werkzeug's LocalProxy defines. `asyncio.iscoroutinefunction(current_app)` is
False, so the obvious check does not explain it. Every attribute of the result is
an AsyncMock too, so `current_app.logger.exception(...)` builds a coroutine
nobody awaits: `RuntimeWarning: coroutine 'AsyncMockMixin._execute_mock_call' was
never awaited`. Pass `new_callable=MagicMock`. The same applies to any proxy --
`request`, `session`, `g`.

**683. A URL FROM A PEER IS NOT A PATH, AND `replace` IS NOT A HOST CHECK.**
`File.delete_from_disk` built a local path with
`source_url.replace(f"{SERVER_URL}/", 'app/')` behind `SERVER_NAME in source_url`
and unlinked it -- remote arbitrary file deletion, because `source_url` comes
from a peer's `image.url` (D1324). To turn a URL into a path: parse it, compare
the HOST for equality (against `hostname` and `netloc`, so userinfo cannot spoof
it), unquote the path (`%2e%2e` is `..`), resolve it, and require the result to be
inside the root WITH a trailing separator -- `startswith('/app')` accepts
`/appendix`. All five steps have a mutant here, and the separator one survived the
first pass.

**684. ASK WHO WRITES THE COLUMN BEFORE TRUSTING IT.**
The hole was invisible while reading `delete_from_disk` alone; it needed
`grep -rn "source_url=" app/` to show that three of its writers take the value
straight out of a peer's JSON. For any column a destructive operation reads, list
its writers first -- the question is not what the code does with the value but
who chose it.

**685. A `str.replace` THAT MATCHES NOTHING SUCCEEDS.**
The round 142 floor ratchet silently did not happen: the edit script called
`s.replace(old, new)` on an anchor that had drifted, wrote the file unchanged,
and reported nothing. `coverage_floors.ini` still said 77 after a commit whose
message said 78. Every scripted edit in this campaign asserts
`s.count(old) == 1` before replacing -- the two that skipped it are the two that
went wrong (this, and an earlier probe that patched a comment that was no longer
there).

**686. A MEMBERSHIP TEST CAN RAISE.**
`'url' in activity_json['icon'][-1]` looks like a guard and is two operations: the
index runs first, so `icon: []` is an IndexError and `icon: [5]` a TypeError
(D1325). Six copies of it existed. When a guard subscripts the thing it is
guarding, the guard is the bug -- and the shapes that find it are the empty
container and the container of the wrong element type, not the missing key.

**687. THE SWEEP IS NOT DONE WHEN THE FUNCTIONS YOU READ ARE DONE.**
Three refresh tasks were repaired, and the property test then failed on
`actor_json_to_model`, which holds three more copies of the same read. Reading
the functions named in the coverage gap would never have shown them. Write the
property test BEFORE believing a sweep is complete, and make it scan the file
rather than the functions you happened to open.

**688. A REMOTE HEADER MUST NOT CHOOSE A FILENAME EXTENSION.**
`url_to_thumbnail_file` built one from the peer's `Content-Type`
(`'.' + content_type.split('/')[-1]`) and wrote the peer's body under it, inside a
served directory: `image/html` gave a `.html` file full of script on our own
origin (D1327). Map a content type through an ALLOWLIST to an extension, and give
anything unrecognised a name no server will execute. The inputs that find this are
`image/html`, `image/php`, `image/` and a 200-character subtype.

**689. TWO REPAIRS CAN HIDE EACH OTHER FROM THE TESTS.**
The allowlist stops a `.html` file being created; the cleanup removes it whatever
it is called. Either alone closes the hole, so three mutants survived against
tests that asserted only on what was left on disk. The test that kills them
records the path handed to `Image.open` -- the state DURING the operation, not
after it. When two fixes overlap, find an observable that only one of them
produces.

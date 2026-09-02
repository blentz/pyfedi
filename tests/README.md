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
note for what actually goes slow and how the suite handles it.

**The suite resets the database itself when it goes slow.** You should not need
`--down` for speed; reach for it only to recover a genuinely wedged stack.

The mechanism, measured 2026-08-31. `db_session` truncates all ~90 tables after
EVERY test, so one full run issues about a quarter of a million table
truncations, and Postgres degrades badly under that: a single such TRUNCATE
costs about **0.05ms against a fresh database and about 124ms after two or three
full runs** -- roughly 2500x. At 2964 tests that is the difference between a
~190s suite and one that cannot finish inside ten minutes. It is invisible in
`--durations`: the cost lands on every test's teardown evenly, so the slowest
twenty tests still total under a minute while the run as a whole crawls.

Two things that are NOT the cause, both checked: user-table bloat (zero dead
tuples across all 90 tables) and coverage instrumentation (`--cov=app
--cov-branch` costs only about 1.4x). `VACUUM` does not recover it either --
`VACUUM FULL` on `pg_class` and the other catalogs left TRUNCATE at ~124ms.
Only a fresh database helps.

So `run_tests.sh` probes `pg_total_relation_size('pg_class')` before each run --
about 300kB fresh, tens of MB once TRUNCATE has gone slow -- and restarts
`test-db` when it exceeds 4 MB (override with `PYFEDI_TEST_STALE_KB`). That
discards the tmpfs volume, and `flask db upgrade` rebuilds the schema in about 8
seconds, which is far cheaper than the run it saves. pg_class's size is an
odometer for relfilenode churn, not the cause; vacuuming it away does not make
TRUNCATE fast again, which is exactly why the fix is a reset rather than a
vacuum.

Proof it is self-maintaining: two full runs back to back took 254.66s and
245.27s, the second having reset itself after detecting 45 MB.

**A run over ten minutes is a broken environment, not a slow suite.**
`pytest.ini` sets `session_timeout = 600` alongside the per-test `timeout = 60`.
The per-test limit only ever catches ONE hung test; the degradation above makes
every test slow and would otherwise be waited out in silence. If you hit the
session budget with a freshly reset database, suspect the environment -- host
CPU governor and power profile, and `podman stats` -- not the tests.

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
visible to it because the `db_session` fixture (`tests/conftest.py:117`)
truncates tables rather than rolling back a transaction, and the factories
`commit()`, so by the time the dispatcher's own session queries the database
the rows are durably there for any session bound to the same engine to see —
no transaction-visibility trick is involved.

**3. The request-context asymmetry.** `patch_db_session`
(`app/utils.py:3664`) only replaces `db.session` when `has_request_context()`
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
backends idle-in-transaction holding relation locks, and every later run
then blocks on the `db_session` fixture's teardown `TRUNCATE`
(`tests/conftest.py:143`) — producing hangs and, once connections are
cleared, failures from half-truncated tables that look exactly like real
regressions. Recovery is `./run_tests.sh --down` plus a rebuild, which
replays ~269 migrations. This cost 5c a long detour.

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
resets state by `TRUNCATE`-ing every table after every test, which assumes
exclusive access to the database. Two concurrent sessions — a full-suite run
and a single-file run, say — corrupt each other in two ways at once: one
session's `TRUNCATE ... CASCADE` deletes rows the other just committed, and
their identical seed values collide (`duplicate key value violates unique
constraint "ix_instance_domain"`, `Key (domain)=(peer.example) already
exists`). Worse, they can deadlock outright: one backend sits `idle in
transaction` while the other's `TRUNCATE` blocks on `Lock: relation`, and
neither progresses. Observed twice on 2026-09-01, once costing about fifteen
minutes before it was recognised, and both times the failures looked like
real test regressions rather than contention.

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
Since `db_session`'s `TRUNCATE ... RESTART IDENTITY` leaves both tables
empty at the start of every test, calling `make_community(...)` before any
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
a passing implementation.

**29. Stopping `run_tests.sh` on the host does not kill pytest inside the
container -- check for and kill survivors before starting another run.**
Cancelling a run from the host leaves the container's pytest process alive,
still holding the test database. Starting a second run then puts two
pytest sessions against one Postgres instance at once (see fact 18 above),
and the second run's `db_session` teardown `TRUNCATE` can block for many
minutes -- nearly ten, once -- behind the survivor's `idle in transaction`
session. The image has no `kill` binary, so list and kill survivors through
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

**32. `requestor_domain()` (`app/utils.py:5721-5728`) reads the `User-Agent`
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

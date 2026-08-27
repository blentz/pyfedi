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

Sub-project 1b-ii (`tests/test_feed_sorts.py`, `tests/test_feed_top_windows.py`,
`tests/test_feed_visibility_filters.py`, `tests/test_feed_display_preferences.py`,
`tests/test_instance_stickies.py`, `tests/test_possible_communities.py`, plus new
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

- **The empty-`community_ids` early return** (`app/utils.py:3792-3793`). No
  earlier test called the function with an empty community list.
  `test_empty_community_ids_returns_an_empty_list_without_querying`
  (`tests/test_factories_feed.py`) covers it; neutralizing the guard makes the
  very next branch index `community_ids[0]` on an empty list and raise
  `IndexError`, which is what makes the mutation observable rather than merely
  changing a return value.
- **The Redis cache-HIT read path** (`app/utils.py:3801-3803`). Task 1 proved
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

`get_deduped_post_ids` now carries exactly three documented-uncovered regions,
all legitimately out of this sub-project's scope and unchanged by the fix round
or by the cache-key fix below: the `hashtag` filter (`3833-3837`), the
anonymous-viewer private-community branch (`3860-3861`), and the
unrecognized-`sort` fallthrough (`3939->3943`, mirrored in `post_ids_to_models`
at `3966->3968`). Those line numbers moved by seven when the cache-key fix landed
and are re-derived from `coverage.json`, not carried forward.
`instance_sticky_posts` and
`get_instance_stickies` are 100% statement and branch. `possible_communities`
carries its two documented-dead branches (`4344->4343`, `4351->4350`), unchanged.

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

**Still open, found while fixing the above and NOT fixed here.** The
private-community filter is inside a walrus guard:

    if private_community_ids := community_membership_private(current_user.id):
        post_id_where.append('(c.private is false OR c.id IN :private_community_ids) ')

A viewer who belongs to no private community at all makes that guard falsy, so
the clause is never appended and posts from EVERY private community appear in
their feed. Verified by probe against this suite: a private community, one member
(Alice) and one non-member (Bob) with no private memberships -- Bob's
`get_deduped_post_ids` returns Alice's private-community post. This is why
`tests/test_feed_cache.py`'s leak test discriminates on `hidden_posts` (appended
unconditionally for every authenticated viewer) rather than on private-community
membership, which would not have discriminated at all.

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

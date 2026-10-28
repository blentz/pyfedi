# AP Conformance Suites Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the two automatable third-party ActivityPub conformance suites linked from activitypub.rocks (socialweb.coop **activitypub-testing** and **FediTest**) against PieFed inside the existing interop harness. Gate the run on a committed baseline of expected failures.

**Architecture:**
- A new `conformance-runner` container joins the `pyfedi-interop` podman-compose network. It trusts the Caddy CA and runs both CLIs against `piefed.test`, with `mastodon.test` as FediTest's partner node.
- A pure-Python checker normalises both result streams to `{test_id: outcome}` and diffs them against `interop/conformance/baseline.json`.
- FediTest's two-node system tests drive PieFed through a new in-repo `PieFedNode` driver. The driver writes through `/api/alpha` and reads state from the PieFed DB, as `interop/test_microblog_mastodon.py` already does.

**Tech Stack:** podman-compose, Python 3.12, feditest (git SHA `41cfd61d6a5640e3b2a05c8af5a79768cc15b13d`), feditest-tests-fediverse (git SHA `cdbb4672437b2e09e7c1305a91a12d1081ca811b`), Node LTS + `activitypub-testing@0.16.1`, `jq`, httpx, psycopg2, pytest.

**Spec:** No separate spec doc. The evaluation and owner rulings below are the spec. Related: `docs/PYFEDI_GAPS.md`, `docs/superpowers/specs/2026-10-10-ap-gaps-roadmap.md`, `interop/README.md`.

## Evaluation (why these two suites)

| Resource | Verdict |
|---|---|
| activitypub-testing (npm 0.16.1, AGPL, last release 2024-10-09) | **Adopt.** 10 actor and collection checks, output as NDJSON EARL. `outbox-post-servers-must-return-a-201-created-http-code` is C2S, which we leave out on purpose (PYFEDI_GAPS §6), so it is baselined. |
| FediTest (develop 0.6; PyPI 0.5 is stale; upstream quiet since 2024-12-29) | **Adopt, pinned.** `tests/webfinger/server/*` (11 RFC 7033 tests, no driver needed), `tests/system2/*` (7 two-node tests), `tests/system3/*` (3 three-node tests). Output as TAP, HTML and JSON. |
| BrowserPub | Reject. Interactive only: no validation, no API, needs a public host. Use it by hand against the production instance. |
| httpsig.org | Reject. Interactive RFC 9421 playground. `tests/fixtures/rfc9421/` already covers its vectors. |

## Global Constraints

- Owner ruling 2026-10-18: the FediTest PieFed driver lives **in-repo** (`interop/conformance/`). Upstreaming comes later.
- Owner ruling 2026-10-18: the run is a **local gate with a committed expected-failure baseline**. It is not wired to CI, the same as `interop/run.sh test`.
- Pin every external tool by exact version or SHA: the two git SHAs and the npm version above. No floating `develop` or `latest`.
- Gate semantics: exit non-zero on (a) any test whose outcome is `failed`/`error` and that is not in the baseline, (b) any baseline entry whose test now passes, and (c) any baseline entry whose test did not run at all. `skipped` counts as a non-failure, but it must also be baselined, so new skips are noticed.
- Every baseline entry carries a non-empty `reason` string. Reasons that cite a deliberate gap point at its `docs/PYFEDI_GAPS.md` section.
- A real conformance failure is a defect. Fix it via the defect-fix workflow (TDD, one commit per defect). Do not baseline it unless the owner rules it out of scope.
- Inline imports only when technically unavoidable (memory: inline-imports-rule). Gates run from the project dir with committed tools, never `/tmp` scripts (memory: gates-live-in-repo).
- Work on a branch or worktree cut from `main`, not `feat/onion-storefront`, because another session is active there.
- Results go to `interop/conformance/results/`, which is gitignored.

## Review Focus

1. **A suite crashes or emits nothing** (bad CA, DNS, CLI exit ≠ 0). The gate must fail, not report "0 regressions". This is pinned by `test_empty_results_fail_the_gate` (Task 2) and the run script's `set -euo pipefail` (Task 3).
2. **The same test id appears for several actors or role pairings** (aptest per actor; system2 in both directions). Ids must be qualified (`aptest/<actor>/<slug>`, `feditest/<session>/<test>`), or one actor's pass hides another's failure. This is pinned by `test_ids_are_qualified_per_actor` (Task 2).
3. **PieFed renders microblog content as title+body.** FediTest's `note_content` equality checks may see extra text. The driver must strip PieFed's title rendering, or the failures must be triaged as real. This is pinned by `test_note_text_roundtrip` (Task 5).
4. **Federation is asynchronous.** A slow celery queue makes `actor_has_received_object` flap. The driver returns `None` while it waits (FediTest's `poll_until` retries); it does not raise. This is pinned by `test_unknown_object_is_none_not_error` (Task 5).
5. **Stale seeded state across runs** (alice already follows bob from an earlier run). `make_follow` must be idempotent, and the session must start each run with `interop/run.sh down` + `up`, or stale follows must be reset. This is pinned by the clean-stack verification in Task 7.

---

## File Structure

| Path | Responsibility |
|---|---|
| `interop/conformance/__init__.py` | Package marker, so `tests/` can import the checker. |
| `interop/conformance/Containerfile` | Runner image: Python 3.12, feditest@SHA, tests@SHA, Node, activitypub-testing@0.16.1, jq. |
| `interop/conformance/baseline.py` | Pure parsers (`parse_tap`, `parse_aptest_ndjson`) and `compare(results, baseline) -> list[str]`. CLI `python -m interop.conformance.baseline`. |
| `interop/conformance/baseline.json` | Committed expected non-passes: `{test_id: {"outcome": ..., "reason": ...}}`. |
| `interop/conformance/run.sh` | Runs inside the runner: both suites into `results/`, then the checker. |
| `interop/conformance/feditest/webfinger.testplan.json` | WebFinger server testplan (PieFed via `FediverseSaasNodeDriver`, Imp client). |
| `interop/conformance/feditest/system2.testplan.json` | Two-node plan, PieFed↔Mastodon, both directions. |
| `interop/conformance/feditest/nodedrivers/piefed.py` | `PieFedNodeDriver`, `PieFedNode(FediverseNode)`. Imports feditest, so it runs only in the runner. |
| `interop/conformance/piefed_mapping.py` | Pure helpers the driver uses (acct ↔ ids, content stripping). No feditest import, so it is unit-testable. |
| `interop/clients.py` | Extended `PieFed` client: `resolve`, `follow_user`, `unfollow_user`, `like_post`, `reply`. |
| `compose.interop.yaml`, `interop/run.sh` | `conformance-runner` service, `conformance` verb. |
| `tests/test_conformance_baseline.py`, `tests/test_piefed_mapping.py` | Unit tests (run by `./run_tests.sh`). |

---

### Task 1: Runner image, compose service, `run.sh conformance` verb

**Files:**
- Create: `interop/conformance/__init__.py`, `interop/conformance/Containerfile`
- Modify: `compose.interop.yaml` (add the service after `interop-runner`), `interop/run.sh` (case arm and usage line), `.gitignore` (`interop/conformance/results/`)
- Test: `tests/test_interop_compose_project.py`

**Interfaces:**
- Produces: service `conformance-runner`, profile `conformance`, image `localhost/piefed-conformance-runner:latest`. It inherits `<<: *trust` and `entrypoint: ["/trust_ca.sh"]` and the `DATABASE_URL` env exactly like `interop-runner`. It mounts `./interop:/app/interop` **read-write**, because results are written there, and `caddy-data:/caddy-data:ro`. `REQUESTS_CA_BUNDLE`/`SSL_CERT_FILE`/`NODE_EXTRA_CA_CERTS` point at the system bundle that `trust_ca.sh` updates.
- Produces: `interop/run.sh conformance [args]` = `up`, then `$COMPOSE --profile conformance run --rm conformance-runner interop/conformance/run.sh "$@"`.

- [ ] **Step 1: Write failing test** `test_conformance_runner_is_in_the_interop_project` in `tests/test_interop_compose_project.py`. Assert that `conformance-runner` is in `yaml.safe_load(compose.interop.yaml)['services']`, that its `profiles == ['conformance']`, and that it shares `interop-runner`'s networks.
- [ ] **Step 2:** `./run_tests.sh tests/test_interop_compose_project.py`. Expected: FAIL, KeyError `conformance-runner`.
- [ ] **Step 3:** Write the Containerfile.
  - Base `python:3.12-slim`. apt-get `nodejs npm jq git ca-certificates`.
  - `pip install "git+https://github.com/fediverse-devnet/feditest@41cfd61d6a5640e3b2a05c8af5a79768cc15b13d" httpx psycopg2-binary`.
  - `git clone` feditest-tests-fediverse and `git checkout cdbb4672437b2e09e7c1305a91a12d1081ca811b` into `/opt/feditest-tests-fediverse`.
  - `npm install -g activitypub-testing@0.16.1`.
  - Then add the service and the verb.
- [ ] **Step 4:** `./run_tests.sh tests/test_interop_compose_project.py`. Expected: PASS. Then run `interop/run.sh conformance --version-check`, where a temporary `run.sh` only prints `feditest version` and `activitypub-testing help | head -1`. Expected: both print, exit 0.
- [ ] **Step 5: Commit** `feat(interop): conformance-runner image with pinned feditest and activitypub-testing`

### Task 2: Baseline checker

**Files:**
- Create: `interop/conformance/baseline.py`, `interop/conformance/baseline.json` (`{}` for now)
- Test: `tests/test_conformance_baseline.py`, plus fixtures `tests/fixtures/conformance/{sample.tap,sample.ndjson}`

**Interfaces:**
- Produces: `Outcome = Literal['passed', 'failed', 'skipped', 'error']`
- Produces: `parse_tap(text: str, prefix: str) -> dict[str, Outcome]`. Ids are `f'{prefix}/{test name}'`. `ok` maps to passed, `not ok` to failed, `# SKIP` to skipped, and `Bail out!` to error with id `f'{prefix}/__bail__'`.
- Produces: `parse_aptest_ndjson(text: str, actor: str) -> dict[str, Outcome]`. Ids are `f'aptest/{actor}/{slug}'`, where slug is the last path segment of `.test.url`. EARL outcomes map as: `passed` to passed; `failed` to failed; `inapplicable` and `cantTell` to skipped; anything else to error.
- Produces: `compare(results: dict[str, Outcome], baseline: dict[str, dict]) -> list[str]`. It returns human-readable problem lines; an empty list means the gate passes.
- Produces: CLI `python -m interop.conformance.baseline RESULTS_DIR`. It reads `RESULTS_DIR/*.tap` (prefix = file stem) and `RESULTS_DIR/aptest-<actor>.ndjson`, loads `baseline.json`, prints problems, and exits 1 if there are any.

- [ ] **Step 1: Write failing tests:**
  - `test_tap_outcomes`: `ok`, `not ok`, `ok # SKIP` give passed, failed, skipped.
  - `test_aptest_outcomes`: fixture lines with `passed`/`failed`/`inapplicable` map as above.
  - `test_ids_are_qualified_per_actor`: the same slug for `alice` and `microblogs` gives two distinct keys.
  - `test_new_failure_fails`: `compare({'x': 'failed'}, {})` contains `'x'`.
  - `test_baselined_failure_passes`: `compare({'x': 'failed'}, {'x': {'outcome': 'failed', 'reason': 'r'}}) == []`.
  - `test_fixed_failure_must_leave_baseline`: `compare({'x': 'passed'}, {'x': {...failed...}})` is non-empty.
  - `test_missing_baselined_test_fails`: `compare({}, {'x': {...}})` is non-empty.
  - `test_new_skip_fails_until_baselined`.
  - `test_baseline_entry_without_reason_fails`.
  - `test_empty_results_fail_the_gate`: `compare({}, {})` is non-empty, with the message `no results`.
- [ ] **Step 2:** `./run_tests.sh tests/test_conformance_baseline.py`. Expected: FAIL, ImportError.
- [ ] **Step 3:** Implement the signatures above in `interop/conformance/baseline.py`.
- [ ] **Step 4:** `./run_tests.sh tests/test_conformance_baseline.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(interop): conformance baseline checker`

### Task 3: activitypub-testing against PieFed actors

**Files:**
- Create: `interop/conformance/run.sh` (replaces the Task 1 stub)
- Modify: `interop/seed_piefed.py` (seed a local feed `conf-feed` owned by alice, if none exists, mirroring the community seeding at L38-45), `interop/conformance/baseline.json`

**Interfaces:**
- Consumes: the Task 2 CLI.
- Produces: `results/aptest-{alice,microblogs,conf-feed,instance}.ndjson`, for actor URLs `https://piefed.test/u/alice`, `/c/microblogs`, `/f/conf-feed`, and the instance actor URL (read from `https://piefed.test/.well-known/nodeinfo` or the `/actor` route, whichever `app/activitypub/routes.py` serves as the instance actor).

- [ ] **Step 1:** Write `run.sh`, with `set -euo pipefail`, `rm -rf results && mkdir results`.
  - Loop over actors: `activitypub-testing test actor "$url" > "results/aptest-$name.ndjson"`.
  - Then `python -m interop.conformance.baseline interop/conformance/results`.
- [ ] **Step 2:** `interop/run.sh conformance`. Expected: FAIL, listing every non-pass as "not in baseline" (the gate works).
- [ ] **Step 3:** Triage each non-pass.
  - The outbox-POST 201 test, on every actor, goes in the baseline with reason `C2S not implemented: PYFEDI_GAPS §1 Client-to-server API (§6)`.
  - Any other failure is a defect: stop, fix it TDD in its own commit, and re-run.
- [ ] **Step 4:** `interop/run.sh conformance`. Expected: exit 0.
- [ ] **Step 5: Commit** `feat(interop): activitypub-testing actor checks in the conformance gate`

### Task 4: FediTest WebFinger server tests

**Files:**
- Create: `interop/conformance/feditest/webfinger.testplan.json`
- Modify: `interop/conformance/run.sh`, `interop/conformance/baseline.json`

**Interfaces:**
- Produces: `results/webfinger.tap` (stem `webfinger`, so ids are `webfinger/<test>`).
- Testplan:
  - Constellation role `server` = `FediverseSaasNodeDriver`, `hostname: piefed.test`, account `{role: "default", uri: "acct:alice@piefed.test"}`, non-existing account `acct:nobody-conf@piefed.test`.
  - Role `client` = `ImpInProcessNodeDriver`.
  - Session template = every test `feditest list-tests --testsdir /opt/feditest-tests-fediverse/tests` prints under `webfinger.server`.
  - Generate it with `feditest create-constellation` / `create-session-template` / `create-testplan` inside the runner, then commit it.

- [ ] **Step 1:** Generate and commit the testplan. Add `feditest run --testsdir /opt/feditest-tests-fediverse/tests --testplan interop/conformance/feditest/webfinger.testplan.json --tap results/webfinger.tap --html results/webfinger.html` to `run.sh`.
- [ ] **Step 2:** `interop/run.sh conformance`. Expected: FAIL, listing the WebFinger non-passes.
- [ ] **Step 3:** Triage. Expected candidates to check against `app/activitypub/routes.py:142`: §5.1 CORS header, §4.2-9 content type `application/jrd+json`, §4.2-4 malformed-resource 400, §4.2-14 HTTPS-only redirects. Each real failure is a defect, fixed TDD in `tests/test_webfinger.py`. Baseline only what the owner rules out.
- [ ] **Step 4:** `interop/run.sh conformance`. Expected: exit 0.
- [ ] **Step 5: Commit** `feat(interop): FediTest WebFinger server tests in the conformance gate`

### Task 5: PieFed FediTest node driver

**Files:**
- Create: `interop/conformance/piefed_mapping.py`, `interop/conformance/feditest/nodedrivers/piefed.py`
- Modify: `interop/clients.py` (`PieFed` class, L70-101)
- Test: `tests/test_piefed_mapping.py`

**Interfaces:**
- Produces in `interop/clients.py` `PieFed`. Payload names come from `app/api/alpha/schema.py`:
  - `resolve(q: str) -> dict`: GET `/resolve_object?q=`. Returns the response with `person`/`post`/`comment` keys.
  - `follow_user(user_id: int)`: POST `/user/follow` `{user_id}`.
  - `unfollow_user(user_id: int)`: POST `/user/unfollow` `{user_id}`.
  - `like_post(post_id: int, score: int)`: POST `/post/like` `{post_id, score}`.
  - `like_comment(comment_id: int, score: int)`: POST `/comment/like`.
  - `reply(post_id: int, body: str, parent_id: int | None = None) -> dict`: POST `/comment`. Verify the field names in `schema.py` `CreateCommentRequest`.
- Produces in `piefed_mapping.py`, all pure:
  - `acct_to_userid(acct_uri: str) -> str`: `'acct:alice@piefed.test'` gives `'alice'`. Raises `ValueError` for another host.
  - `note_text(html: str, title: str) -> str`: strips PieFed's rendered title prefix and outer `<p>`, so it compares equal to the text that was posted.
  - `microblog_title(content: str) -> str`: the title PieFed requires, taken from the first 50 characters of the content.
- Produces in `nodedrivers/piefed.py`:
  - `class PieFedNode(FediverseNode)` and `class PieFedNodeDriver(NodeDriver)`, whose node parameters are `hostname`. Account fields are `userid` + `password`, read from the testplan.
  - Override only what PieFed can do. Anything not overridden keeps FediverseNode's `NotImplementedByNodeError`, which FediTest records as **skipped**.
  - Writes go through `interop.clients.PieFed`. Reads (`actor_is_following_actor`, `actor_is_followed_by_actor`, `actor_has_received_object`, `object_author`, `direct_replies_to_object`, `object_likers`) use psycopg2 on `DATABASE_URL`, the same pattern as `interop/test_microblog_mastodon.py`.
  - `make_create_note` posts to the `microblogs` community (`PieFed.microblogs_id()`) and returns `post['post']['ap_id']`.
  - `set_auto_accept_follow` is a no-op, because PieFed auto-accepts user follows. Confirm that in `app/activitypub/routes.py` follow handling.
  - `announce_object`/`unannounce_object` are not overridden: there is no alpha boost endpoint.

- [ ] **Step 1: Write failing tests:**
  - `test_acct_to_userid`.
  - `test_acct_other_host_rejected`.
  - `test_note_text_roundtrip`: `note_text(rendered, title)` equals the original content for plain text, for a link (`preserve_links`), and for a title that is a prefix of the content.
  - `test_microblog_title_is_bounded`.
  - `test_unknown_object_is_none_not_error`: a pure helper `received_ap_id(rows: list[tuple]) -> str | None` returns `None` on `[]`.
- [ ] **Step 2:** `./run_tests.sh tests/test_piefed_mapping.py`. Expected: FAIL, ImportError.
- [ ] **Step 3:** Implement `piefed_mapping.py`, the client methods and the driver.
- [ ] **Step 4:** `./run_tests.sh tests/test_piefed_mapping.py`. Expected: PASS. Then, in the runner, `feditest list-nodedrivers --nodedriversdir interop/conformance/feditest/nodedrivers` lists `PieFedNodeDriver`.
- [ ] **Step 5: Commit** `feat(interop): PieFed FediTest node driver`

### Task 6: system2 PieFed↔Mastodon (and system3 if feasible)

**Files:**
- Create: `interop/conformance/feditest/system2.testplan.json`
- Modify: `interop/conformance/run.sh`, `interop/conformance/baseline.json`

**Interfaces:**
- Consumes: `PieFedNodeDriver` (Task 5), and the built-in `MastodonSaasNodeDriver` (`hostname: mastodon.test`, `verify_api_tls_certificate: true`, account `{userid: bob, oauth_token: <mastodon_bob from interop/.tokens.json>}`).
- The token is generated per stack, so `run.sh` renders the testplan from a committed `system2.testplan.template.json` with `jq --arg tok`. Only the template is committed.
- Produces: `results/system2-piefed-to-mastodon.tap` and `results/system2-mastodon-to-piefed.tap`, one run per direction, so the TAP file stem qualifies the ids. The two runs come from one template, swapping `sender` and `receiver`.

- [ ] **Step 1:** Write the template and the run line: `feditest run ... --nodedriversdir interop/conformance/feditest/nodedrivers --tap results/system2-$dir.tap --html results/system2-$dir.html --json results/system2-$dir.json`, looping `dir` over both directions.
- [ ] **Step 2:** `interop/run.sh conformance`. Expected: FAIL, listing the system2 non-passes.
- [ ] **Step 3:** Triage. Skips caused by unimplemented driver methods get the baseline reason `driver: no alpha endpoint for <method>`. Failures caused by PieFed behaviour are defects, fixed TDD, each in its own commit.
- [ ] **Step 4:** system3. Its roles need three distinct nodes. If FediTest accepts two PieFed roles on one host, add `system3.testplan.template.json`. Otherwise add one baseline-free note to `interop/README.md` saying that system3 needs a second PieFed stack, and stop there (YAGNI).
- [ ] **Step 5:** `interop/run.sh conformance`. Expected: exit 0.
- [ ] **Step 6: Commit** `feat(interop): FediTest system tests PieFed<->Mastodon in the conformance gate`

### Task 7: Docs and gate proof

**Files:**
- Modify: `interop/README.md` (add `conformance` to the Use list and a "Conformance suites" section: what runs, how to read `results/*.html` and the NDJSON with `jq 'select(.result.outcome != "passed")'`, and how to change the baseline), `docs/PYFEDI_GAPS.md` (add a §2 "External conformance suites" row listing each baselined item with its reason)

- [ ] **Step 1:** Write the docs.
- [ ] **Step 2: Clean-stack run:** `interop/run.sh down && interop/run.sh conformance`. Expected: exit 0, and `results/` holds `aptest-*.ndjson`, `webfinger.{tap,html}`, `system2-*.{tap,html,json}`.
- [ ] **Step 3: Gate mutation proof.** Revert each change after testing it:
  - (a) Delete one baseline entry. Expected: exit 1 naming it.
  - (b) Add a fake expected-fail entry for a passing test. Expected: exit 1, "now passes".
  - (c) Point the WebFinger plan at a bogus hostname. Expected: exit 1, with an error or `no results`, not a pass.
- [ ] **Step 4:** `./run_tests.sh tests/test_conformance_baseline.py tests/test_piefed_mapping.py tests/test_interop_compose_project.py`. Expected: PASS.
- [ ] **Step 5: Commit** `docs(interop): conformance suites and baseline workflow`

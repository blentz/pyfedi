# Fuzz every external input surface — plan

Spec: `docs/superpowers/specs/2026-10-13-fuzz-all-input-surfaces-design.md`.
Execution: subagent-driven, sequential in the main checkout (one shared test
stack). Rules for every task:

- Tests run only via `./run_tests.sh ...` or `podman-compose -f compose.test.yaml
  exec -T -w /app test-runner ...` from the repo root. No gate scripts in /tmp.
- Campaigns run in the test container, never under `--cov`:
  `podman-compose -f compose.test.yaml exec -T -w /app test-runner python -m tests.fuzz.run_campaign <target> -max_total_time=60`
  (start the stack with `podman-compose -f compose.test.yaml up -d` if needed).
- Every finding: minimised reproducer committed under
  `tests/fuzz/corpus/<target>/<descriptive_name>`, failing regression test in the
  module's normal test file first, narrowest fix, ONE commit per defect,
  message `fix: <what now refuses cleanly>`. If the fix would change behaviour
  beyond refusing/handling the bad input cleanly, STOP and report it as a
  product decision instead of fixing.
- Harness commits are separate from fix commits: `test(fuzz): ...`.
- A check asserts a property, never just "no crash", and never fires on inert
  output or on a documented refusal exception.
- Slow seeds/tests are bugs: replay of the whole corpus must stay well under a
  few seconds per target.
- 100% line+branch coverage of changed production lines.

## Task 0 — framework

1. Create `tests/fuzz/targets/` package: `Target` dataclass (spec), `REGISTRY`
   built from each surface module's `TARGETS`, duplicate-name error.
   Move the three existing checks into `targets/sanitizers.py` (surface
   `sanitizers`); `tests/fuzz/harnesses.py` becomes a re-export shim.
2. `tests/fuzz/surfaces.py` with `SURFACES` (start with `sanitizers`).
3. `run_campaign.py`: registry-driven, `needs_app` pushes a test app context
   (`create_app(TestConfig)` — check how tests/conftest.py builds it), default
   `-timeout` for budgeted targets, unchanged corpus/work/artifact layout.
4. Generalise `tests/test_utils_fuzz_corpus.py` over `REGISTRY`, with app
   context for `needs_app` targets (use existing fixtures if possible) and the
   `budget_ms` assertion.
5. `tests/test_fuzz_registry.py`: the completeness gate (spec, Gates 1).
6. `tests/fuzz/smoke.py` + `tests/test_fuzz_smoke.py` (spec, Gates 3).
7. Run the three existing targets 60 s each through the new runner: clean.
Commit(s): `test(fuzz): ...`.

## Tasks 1–7 — one per tier (T1..T7 in the spec)

For each tier: add `tests/fuzz/targets/<surface>.py`, its `SURFACES` entries,
hand-written hostile seeds per target (include every candidate below), replay
green except real findings, then 60 s campaign per target. Fix findings per the
rules above. Report: targets added, execs/cov per target, findings + commits.

### Candidate defects from the inventory read (unverified — seed them)

T1: `HttpSignature.parse_signature` catches only KeyError, bad base64 raises
`binascii.Error`; `verify_signature` does `cleartext.encode("ascii")`
(UnicodeEncodeError on non-ASCII header values); LD `expires +
LD_SIGNATURE_EXPIRY_SKEW` OverflowError near year 9999. Never import or call
`app/main/routes.py` `/test` route (it swaps the global JSON-LD loader to a
network one).
T2: `normalise_activity_types` on non-dict JSON (shared_inbox only checks
`'id' in request_json`, so a list/str passes) → AttributeError;
`models.parse_ap_timestamp` `astimezone()` outside try → OverflowError for
`0001-01-01T00:00+05:00` / `9999-12-31T23:59-05:00`; `shared_inbox`
`furl(request_json['actor']).host` ValueError under ALLOWLIST_STRONG.
`language_variants_from`/`posting_warning_from_ap` call allowlist_html without
test_env → patch `fediverse_domains`.
T3: `links_with_parens` `link.get("href").count` on `<a name=x>` →
AttributeError; `handle_blockquotes` recursion per `>` level → RecursionError
at ~1000 levels; `microblog_content_to_link` unguarded `furl(href).host`;
`discovery.credits._normal_url` urlsplit ValueError on `https://[x`.
ReDoS shapes: `LINK_PATTERN` (utils.py ~662), PERSON/COMMUNITY/FEED_PATTERN
(~676-678, long `@a` runs), `visibility.MENTION_PATTERN`,
`markdown_extras._md_img_with_attrs_re`, allowlist `re_spoiler` and `~~(.*)~~`,
`re_bold`/`re_em_bold`, blockquote regex, `_CASTOPOD_EPISODE_LINK`,
discovery `sources._HOSTNAME`, `EMAIL_RE`.
T4: unguarded `furl` ValueError in `process_webfinger_request` (routes.py ~167),
`requestor_domain`, `cli.strip_tracking`.
T5: `GROUP` with 0 or 2+ args (`_, group_name = self.data.split()`) ValueError;
`parse_datetime` ValueError while newnews/newgroups catch only TypeError;
`parse_range` unbounded (range loops at ~608/799/848); `_getline`
`decode("utf-8")` unguarded; `listgroup` `tokens[0]` on empty tokens.
T6: settings import `json.loads` of a top-level list then `.get` →
AttributeError; entries not type-checked; schema `validate_color_code` uses
`re.match` with `^...$` (trailing `\n` accepted).
T7: none known.

## Task 8 — close

README Fuzzing section rewritten (structure, all targets, gates, findings are
fixed per owner ruling 2026-10-13); 60 s smoke of every target clean; full
suite green; final review.

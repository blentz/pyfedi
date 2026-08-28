# Fixing the ActivityPub ingestion defects, tiers 1-4 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the fifteen defects in `app/activitypub/util.py` that sub-project 2a found and the owner has authorised, without changing behaviour nobody characterised.

**Architecture:** Every defect already has a test pinning the current, defective behaviour. Each fix therefore starts from a known-red state: change the code, watch a *named* test fail, update that assertion to the intended behaviour, and confirm nothing else moved. Four Tier 1 defects collapse into one shared host-comparison helper; the rest are local guards. Two ordering constraints are load-bearing and are stated at each affected task.

**Tech Stack:** Python 3, Flask, SQLAlchemy, pytest, respx, coverage.py. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-28-fix-activitypub-ingest-defects-design.md`

## Global Constraints

- `if TYPE_CHECKING` is always a bug. Never introduce it, and never add it to `.coveragerc`'s `exclude_lines`.
- Imports go at the top of the file. No inline imports.
- Named exceptions only — never a bare `except:`.
- Every pragma carries a written justification.
- No new runtime dependencies, no migration.
- **Do not cite line numbers** in docstrings or documentation. Identify code by name.
- A count quoted in prose is a claim. Derive it with a command and quote the command.
- Report defects; do not fix them without owner authorisation. Authorisation exists for the fifteen in this plan and for nothing else. Anything new you find gets registered, not fixed.
- **NO host Python.** Never run `pytest`, `python` or `flask` on the host. Use `./run_tests.sh [pytest args]`.
- **NEVER** run `./run_tests.sh --down` — it destroys the tmpfs DB and forces a ~269-migration replay.
- One test suite at a time. Check `pgrep -af '/venv/bin/pytest'` before starting; two concurrent runs corrupt the test DB.
- Never `git stash` (the stash stack is shared across worktrees). Never `git checkout -- app/`; restore a single file by name.
- Baseline at `added1b7`: **2549 passed, 3 skipped, 0 failed**.
- **Work in a git worktree, not the main checkout.** `podman-compose` names its project after the working directory, so each worktree gets its own isolated stack — which is why concurrent sub-projects have not collided. The main checkout's stack is not provisioned: starting it there and running the suite fails with `ModuleNotFoundError: No module named 'fakeredis'`. That is a stack that has never had dependencies installed, not a broken test.

## The rule that governs every task

**A fix that flips no test is a fix nobody characterised.** If you make a production change and the whole suite stays green, stop. Either the defect was never pinned — in which case pin it first, watch the new test fail, then fix — or your change does not do what it appears to. Do not proceed on a green suite you did not expect.

Equally: **a test failing that this plan did not predict means the change did more than intended.** Investigate before adjusting it.

## Ordering constraints

Two, and both are correctness rather than convenience:

1. **Task 2 before Task 5.** Task 5's guards skip a malformed entry silently. Task 2 is what gives a skip somewhere to be recorded. Landing them the other way round ships silent data loss.
2. **Task 5 before Task 7.** Task 7 adds `except KeyError` parity to the Group and Feed branches. If it lands while D13 and D16 still raise after their commit, that handler converts a loud partially-applied ingest into a silent one — a committed row, a `None` return, and no exception. That is strictly worse than the defect.

## File structure

| file | role |
|---|---|
| `app/activitypub/util.py` | every production fix in this plan |
| `app/activitypub/routes.py` | the single `verify_object_from_source` call site (Task 2 only) |
| `tests/test_ap_ensure_domains_match.py` | pins D1 |
| `tests/test_ap_verify_object_from_source.py` | pins D4, D5, D7 |
| `tests/test_ap_actor_json_person.py` | pins D10, D11 |
| `tests/test_ap_find_flair_or_create.py` | pins D9 |
| `tests/test_ap_actor_json_group.py` | pins D12, D13 |
| `tests/test_ap_actor_json_feed.py` | pins D14, D15, D16, D17 |
| `tests/test_ap_find_community.py` | pins D2, D3 |
| `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` | the defect register |

**On prescribed test bodies:** this plan names the exact tests that must flip and states what each must assert afterwards, but does not paste rewritten bodies. That follows sub-project 2a's precedent — prescribing bodies against code the implementer has not yet read is how five earlier enumerations in this campaign came out wrong. Derive the surrounding context, then edit.

---

### Task 1: The host helper, and `ensure_domains_match` (D1)

**Files:**
- Modify: `app/activitypub/util.py`
- Test: `tests/test_ap_ensure_domains_match.py`

**Interfaces:**
- Produces: `host_of(url_string: str) -> str` — module-level in `app/activitypub/util.py`. Consumed by Tasks 2 and 3. Returns the lowercased host, or `''` when there is none or the URL will not parse.

- [ ] **Step 1: Read the existing idiom before writing the new one**

`extract_domain_and_actor` in the same module already guards `urlparse` against a peer-supplied string, and carries the written justification for the value it degrades to. Read it. The new helper follows it.

- [ ] **Step 2: Run the test that pins the defect, and watch it pass**

```bash
./run_tests.sh "tests/test_ap_ensure_domains_match.py::TestSuspectedNetlocDefect::test_a_port_makes_the_same_host_compare_unequal" -v
```

Expected: PASS — it currently asserts the defective refusal. The class name contains `Suspected...Defect`; once the defect is fixed the name is wrong, so rename it in Step 6.

- [ ] **Step 3: Write the helper**

```python
def host_of(url_string: str) -> str:
    """The lowercased host of a URL, or '' when it has none.

    RFC 3986 section 3.2: the authority may carry userinfo and a port, neither
    of which identifies the host. urlparse's `hostname` strips both and
    lowercases what is left; `netloc` does not, which is the defect this
    closes at every call site.

    Every string reaching this function is chosen by a remote peer, and
    urlparse raises ValueError on a netloc it refuses -- an unbalanced IPv6
    bracket, two '::' runs, a host failing its NFKC confusability check. We
    degrade to '' rather than propagate, matching extract_domain_and_actor.

    '' rather than None is deliberate: callers compare two of these, and two
    failed parses must not compare equal to each other.
    """
    try:
        return urlparse(url_string).hostname or ''
    except ValueError:
        return ''
```

- [ ] **Step 4: Use it in `ensure_domains_match`**

Replace both `urlparse(...).netloc` reads with `host_of(...)`. Then add the guard that `''` requires: two unparseable URLs must not be accepted as a matching pair, so a comparison where either side is `''` refuses.

- [ ] **Step 5: Run the pinning test and watch it fail**

Same command as Step 2. Expected: FAIL. Record the assertion line.

- [ ] **Step 6: Update the pinned assertion**

The test now asserts that the same host on two ports **matches**. Its docstring says which defect the change corresponds to, by name and behaviour, never by ordinal or line number.

Check its neighbours in the same class — `test_userinfo_is_carried_whole_into_a_still_matching_comparison` and `test_userinfo_on_only_one_side_is_still_correctly_refused` must still pass unchanged. If either moved, the helper is wrong: `hostname` is a pure function of `netloc`, so userinfo behaviour is not supposed to change.

- [ ] **Step 7: Add a test for the unparseable pair**

A document whose `id` and `actor` both fail to parse must be refused, not matched. Name the production change that would make it fail: dropping the `''` guard added in Step 4. Run it against that mutation and confirm it fails.

- [ ] **Step 8: Run the file, then the suite**

```bash
./run_tests.sh tests/test_ap_ensure_domains_match.py -q
./run_tests.sh tests/ -q
```

Expected: green, with no test outside this file changing state.

- [ ] **Step 9: Commit**

```bash
git add app/activitypub/util.py tests/test_ap_ensure_domains_match.py
git commit -m "fix: compare hosts, not authorities, in ensure_domains_match

Closes the port false-reject: a peer inconsistent about its port was
refused. Adds host_of(), which Tasks 2 and 3 also consume.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: `verify_object_from_source` — D4, D5, D7

Three defects in one function plus its single caller. They land together because they touch the same body and separating them would mean three passes over the same code.

**This task is a prerequisite for Task 5.** Task 5 skips malformed entries silently; this is what gives a skip somewhere to be recorded.

**Files:**
- Modify: `app/activitypub/util.py`, `app/activitypub/routes.py`
- Test: `tests/test_ap_verify_object_from_source.py`

**Interfaces:**
- Consumes: `host_of` from Task 1.
- Produces: `verify_object_from_source(request_json)` now returns `(object_or_None, reason_or_None)`. The reason is a short string naming which refusal happened. This is a **breaking signature change** with exactly one caller.

- [ ] **Step 1: Confirm the caller count yourself**

```bash
grep -rn 'verify_object_from_source(' app/ --include=*.py | grep -v 'def '
```

Expected: one line. If more than one appears, stop — the tuple return was scoped on the assumption of a single caller, and that assumption is now false.

- [ ] **Step 2: Enumerate the refusal paths**

```bash
awk "NR>=$(grep -n '^def verify_object_from_source' app/activitypub/util.py | cut -d: -f1)" \
  app/activitypub/util.py | awk '/^def /{c++} c<=1' | grep -c 'return None'
```

Quote the number you get. Each one needs a distinct reason string.

- [ ] **Step 3: D5 — name the swallowed exception**

Both bare `except:` clauses wrap only the `.json()` parse. `JSONDecodeError` is already imported at the top of the module; use it, and add no import.

Do **not** repeat the claim that these two clauses block mutation testing. Sub-project 2a measured this pair specifically and found they do not, because the fetch is guarded separately by `except httpx.HTTPError`. The fix is worth making on its own terms.

- [ ] **Step 4: D4 — use `host_of` at both guards**

Replace the `netloc` reads with `host_of`. As in Task 1, a comparison where either side is `''` must refuse.

- [ ] **Step 5: D7 — return a reason**

Give every refusal path its own short reason string, distinct enough that an operator reading the log can tell which check failed. Return `(None, reason)` from each, and `(object, None)` from the success path.

In `routes.py`, unpack the tuple and pass the reason into the `log_incoming_ap` call already made there, replacing the single fixed message.

- [ ] **Step 6: Run the pinning tests and watch them fail**

```bash
./run_tests.sh tests/test_ap_verify_object_from_source.py -q
```

The two port tests — `test_a_port_on_the_object_uri_makes_the_pre_fetch_guard_refuse` and `test_a_port_only_on_attributed_to_makes_the_post_fetch_guard_refuse` — must fail on D4. Every test asserting a bare `None` return must fail on the signature change. Record the count; it is large and that is expected.

- [ ] **Step 7: Update the assertions**

The two port tests now assert acceptance. Every other test unpacks the tuple. Where a test characterises a specific refusal, assert **which reason** came back — that is the behaviour D7 adds, and leaving it unasserted means D7 ships untested.

- [ ] **Step 8: Verify each reason is reachable and distinct**

```bash
./run_tests.sh tests/test_ap_verify_object_from_source.py -q
```

Then derive the set of reason strings the tests actually observe and compare it against the set in the source. Quote both commands. A reason no test reaches is a path no test covers.

- [ ] **Step 9: Full suite, then commit**

```bash
./run_tests.sh tests/ -q
git add app/activitypub/util.py app/activitypub/routes.py tests/test_ap_verify_object_from_source.py
git commit -m "fix: compare hosts, name the swallowed error, and report which check refused

D4: the port false-reject, via host_of.
D5: the two bare excepts around the JSON parse now name JSONDecodeError.
D7: ten refusal paths returned one undifferentiated log message; the
function now returns a reason and the single caller logs it.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: The substring server gate (D10, D11)

**Files:**
- Modify: `app/activitypub/util.py`
- Test: `tests/test_ap_actor_json_person.py`

**Interfaces:**
- Consumes: `host_of` from Task 1.

- [ ] **Step 1: Re-run the probe from the spec against this checkout**

Confirm the three passing rows and the two rejected rows still behave as documented before changing anything. If they do not, the defect has moved and the plan needs revisiting.

- [ ] **Step 2: Run the three pinning tests and watch them pass**

In `tests/test_ap_actor_json_person.py`:
- `test_suspected_defect_subdomain_suffix_passes_the_substring_test`
- `test_suspected_defect_query_string_mention_passes_the_substring_test`
- `test_upper_cased_host_in_the_id_is_rejected`

- [ ] **Step 3: Replace the gate**

The gate currently asks whether `server` appears anywhere in the id string. It must instead ask whether the id's host **is** `server`.

`server` is a locally derived authority string that may carry a port, so normalise both sides through the same function rather than comparing a host against an authority:

```python
if host_of(activity_json['id']) != host_of(f'//{server}'):
    return None
```

Deriving `server`'s host the same way is what makes the comparison symmetric. Confirm by probe that `host_of('//peer.example:8443')` returns `'peer.example'` — a scheme-relative URL parses with an authority and no scheme, which is the shape needed here.

- [ ] **Step 4: Watch all three tests fail, then update them**

The two `suspected_defect` tests now assert **rejection**, and their names must change accordingly — a name containing `suspected_defect` describing fixed behaviour will mislead every later reader. The upper-cased test now asserts acceptance.

- [ ] **Step 5: Mutation — both directions**

Delete the gate entirely: the two rejection tests must fail. Broaden it to accept anything: the same tests must fail. Report both counts. A gate that only fails one direction is only half tested.

- [ ] **Step 6: Full suite**

The gate runs before the type dispatch, so a change here touches Person, Group and Feed alike. Any failure in the Group or Feed files is signal, not noise — investigate before touching it.

- [ ] **Step 7: Commit**

```bash
git add app/activitypub/util.py tests/test_ap_actor_json_person.py
git commit -m "fix: compare the actor id's host to the server, not as a substring

Closes cross-host actor smuggling: a suffix-extended host and a query
parameter both satisfied a substring test. Also closes the case-sensitive
false reject, since hostname lowercases.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: The read-only audit query

**Files:**
- Create: a query or management command; location is your call, provided running it cannot modify a row.
- Modify: `tests/README.md` (or wherever you site it), to say what it is for.

- [ ] **Step 1: Confirm the columns**

`User`, `Community` and `Feed` each carry `ap_profile_id` and `ap_domain`. Verify in `app/models.py` before writing the query.

- [ ] **Step 2: Write the query**

Report rows whose `ap_profile_id` host disagrees with the recorded `ap_domain`, across all three tables. It must be read-only: no `UPDATE`, no `DELETE`, no migration.

- [ ] **Step 3: Prove it is read-only and that it finds what it should**

Insert a row with a deliberately mismatched `ap_profile_id` in a test, run the query, assert it is reported, and assert the row count is unchanged afterwards. Then assert a consistent row is **not** reported — a query that reports everything is not a finding, it is noise.

- [ ] **Step 4: Commit**

```bash
git commit -m "feat: report actor rows whose profile host disagrees with their domain

Read-only. Task 3 stops new cross-host actors being minted; this is how you
find out whether any are already present.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: Guard and skip the partially-applied ingest (D9, D13, D16)

**Requires Task 2.** A skip that logs nothing is silent data loss.

**Files:**
- Modify: `app/activitypub/util.py`
- Test: `tests/test_ap_find_flair_or_create.py`, `tests/test_ap_actor_json_group.py`, `tests/test_ap_actor_json_feed.py`

- [ ] **Step 1: Run the three pinning tests and watch them pass**

- `tests/test_ap_find_flair_or_create.py::TestSuspectedMissingIdKeyCrash::test_missing_id_key_with_no_existing_ap_id_raises_keyerror` (D9)
- `tests/test_ap_actor_json_group.py::test_a_tag_without_a_display_name_raises_key_error` (D13)
- `tests/test_ap_actor_json_feed.py::test_a_followed_community_the_resolver_rejects_crashes_after_the_commit` (D16)

Each currently asserts the exception **and** the row counts that prove the ingest was partially applied. Those row-count assertions are the valuable part and must survive the rewrite in a changed form.

- [ ] **Step 2: Apply the guard-and-skip idiom**

In each case the unguarded read sits beside optional reads that are already guarded. Match the local idiom exactly:

```python
for flair in activity_json['lemmy:tagsForPosts']:
    if 'display_name' not in flair:
        continue          # legacy tag entry, nothing to name it by
    flair_dict = {'display_name': flair['display_name']}
```

- [ ] **Step 3: Log every skip**

A skipped entry is a peer's data being dropped. Use the reason-carrying channel Task 2 established, or the module's existing logger where no `log_incoming_ap` context is available. Say which entry was skipped and why.

- [ ] **Step 4: D9 must be fixed for the caller that can reach it**

D9 fires from `refresh_community_profile_task`, whose session comes from `get_task_session()` with autoflush at its default of **on**. It does not fire from `actor_json_to_model`, which uses `db.session`, where the application factory sets `autoflush: False`.

The test must exercise the autoflush-on path — two entries in one peer-supplied list sharing a `display_name`. A fix verified only against `db.session` will look correct and leave the reachable path broken.

- [ ] **Step 5: Watch all three fail, then rewrite the assertions**

Each test now asserts: no exception; the row exists; and the **good** entries were ingested while the malformed one was not. Keep the row counts — they are what distinguishes "skipped the bad entry" from "skipped the whole loop".

- [ ] **Step 6: Mutation — remove each guard**

Removing a guard must restore the old crash and fail the updated test. Report three counts.

- [ ] **Step 7: Full suite, then commit**

```bash
git commit -m "fix: skip malformed peer entries instead of raising after the commit

Three defects with one shape: a peer document committed a row and then
raised, leaving a half-written ingest and no record of it. Each unguarded
read now matches the guarded optional reads beside it, and every skip logs.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: `find_community` crashes (D2, D3)

**Files:**
- Modify: `app/activitypub/util.py`
- Test: `tests/test_ap_find_community.py`

- [ ] **Step 1: Run the pinning tests and watch them pass**

- `TestSuspectedMissingTypeKeyCrash::test_an_object_with_no_type_key_raises_keyerror` (D2)
- `TestSuspectedNonStringAddressingElementCrash::test_a_non_string_element_in_a_cc_list_raises_attributeerror` (D3)

- [ ] **Step 2: Fix both**

D2: reading `type` from an object that has none. D3: calling `.startswith` on a non-string element of a `cc`/`to`/`audience`/`target` list. Both must return `None` — the function's existing "no community found" answer — rather than raise.

- [ ] **Step 3: Watch both fail, then update**

Each now asserts `None`. Their class names contain `Suspected...Crash`; rename them, since they no longer describe a crash.

- [ ] **Step 4: The `.lower()` calls must survive**

Sub-project 2a pinned four `.lower()` sites here, each with its own uniquely-failing test. Your change must not disturb them. Re-run the four individual mutations and confirm each still fails exactly one test.

- [ ] **Step 5: Full suite, then commit**

---

### Task 7: `except KeyError` parity for Group and Feed (D12, D14)

**Requires Task 5.** If this lands while D13 and D16 still raise after their commit, the new handler turns a loud partially-applied ingest into a silent one: a committed row, a `None` return, no exception. Strictly worse than the defect.

**Files:**
- Modify: `app/activitypub/util.py`
- Test: `tests/test_ap_actor_json_group.py`, `tests/test_ap_actor_json_feed.py`

- [ ] **Step 1: Read what the Person/Service branch actually does**

It is the model for parity. Read its handler and reproduce its behaviour — do not invent a different one.

- [ ] **Step 2: Run the pinning tests and watch them pass**

Group: `test_missing_unconditional_key_raises_key_error` (parametrized), `test_missing_public_key_raises_key_error`, `test_public_key_without_a_pem_raises_key_error`. Feed: `test_a_missing_constructor_key_raises_key_error`, `test_a_missing_following_raises_before_the_following_fetch`.

Enumerate the full set with a command and quote it; the parametrized case expands to several.

- [ ] **Step 3: Add the handler to both branches**

Including the inbox-expression fallback the Person branch has and Group lacks.

- [ ] **Step 4: Confirm no commit precedes the new handler**

This is the check that keeps Task 5's work intact: for each branch, confirm no `db.session.commit()` runs before a statement the new `except KeyError` can catch. If one does, the handler must not swallow it — say so and stop rather than shipping a silent partial ingest.

- [ ] **Step 5: Watch the tests fail, update, mutate, full suite, commit**

Each now asserts `None` and a row count of zero. Deleting the handler must restore the raise.

---

### Task 8: Feed owner and attributedTo guards (D15, D17)

**Files:**
- Modify: `app/activitypub/util.py`
- Test: `tests/test_ap_actor_json_feed.py`

- [ ] **Step 1: Run the pinning tests and watch them pass**

D15: `test_a_non_200_owners_collection_raises_index_error`, `test_an_empty_owners_collection_raises_index_error`, `test_an_owner_the_resolver_rejects_is_appended_as_none`. D17: `test_neither_attributed_to_nor_moderators_raises`.

Note that `except KeyError` from Task 7 catches none of these — `IndexError` and `httpx.HTTPError` are different exceptions. They need their own guards.

- [ ] **Step 2: Guard the owner index**

Three distinct crashes reach it: a non-200 collection, an empty collection, and an entry the resolver rejects. All three occur before the commit, so returning `None` leaves no row. Confirm that with a row-count assertion rather than assuming it.

- [ ] **Step 3: Guard the third `attributedTo` arm**

A document with neither `attributedTo` nor `moderators` currently sends `None` into `get_request`. Refuse before the call.

2a recorded that this failure's exception *type* differs depending on whether `DEBUG` is on. Whatever you replace it with must not have that property — the same input must produce the same outcome in both modes. State how you confirmed that.

- [ ] **Step 4: Watch them fail, update, mutate, full suite, commit**

---

### Task 9: Register the out-of-scope `netloc` reads

**Report only. Fix nothing.**

**Files:**
- Modify: `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`

- [ ] **Step 1: Enumerate them**

```bash
grep -n '\.netloc' app/activitypub/util.py
```

Ten reads sit in `resolve_remote_post`, `create_resolved_object` and `resolve_remote_post_from_search`. Confirm the count and the function attribution yourself; quote the command.

- [ ] **Step 2: Do the call-site analysis for each**

This is the deliverable, not the list. For both defects the 2a spec named in advance, call-site analysis **reversed** the predicted severity: reading a comparison tells you what it does, reading its callers tells you what it is worth. For each read, establish where both compared strings come from and who controls them.

- [ ] **Step 3: Register them with severity, and say plainly what is untested**

These three functions have no test coverage from this campaign. A severity judgement made by reading alone is weaker than one backed by a probe; say which you have.

- [ ] **Step 4: Commit**

---

### Task 10: Update the register, confirm the floor, close out

**Files:**
- Modify: `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`, `tests/README.md`

- [ ] **Step 1: Mark the fifteen fixed, each with its commit**

Five remain open: D6, D8, D18, D19, D20. The register must make the split obvious at a glance, since its whole purpose now is telling a reader what is still true.

- [ ] **Step 2: Measure and check the floor**

```bash
./run_tests.sh tests/ -q --cov=app --cov-report=json
podman-compose -f compose.test.yaml exec -T -w /app test-runner \
  python tests/check_coverage_floors.py coverage.json coverage_floors.ini
```

The floor of 35 must hold or rise. These fixes add guarded branches, and a guard without a test lowers the number — if the floor fell, a guard went in untested. Find it.

Raise the floor to the new measured value, rounded **down**, and leave the other three floors untouched. Edit the file additively; a prior agent in this campaign emptied it wholesale.

- [ ] **Step 3: Confirm the suite and the prediction**

Full suite green. Then state, for the record, whether any test failed during this sub-project that the plan did not predict — and if so, what it turned out to mean.

- [ ] **Step 4: Commit**

---

## Plan Self-Review

**Spec coverage.** All fifteen authorised defects map to a task: D1 to Task 1; D4, D5, D7 to Task 2; D10, D11 to Task 3; D9, D13, D16 to Task 5; D2, D3 to Task 6; D12, D14 to Task 7; D15, D17 to Task 8. The spec's audit query is Task 4, its out-of-scope `netloc` finding is Task 9, and its verification section is Task 10. The five Tier 5 defects appear only as "still open" in Task 10, which is what the spec asks.

**Placeholder scan.** No TBD or TODO. Test bodies are named and their new assertions specified, but not pasted — deliberate, and justified in the File Structure section against this campaign's own history of prescribing against unverified context.

**Type consistency.** `host_of(url_string: str) -> str` is defined in Task 1 and consumed in Tasks 2 and 3 with that signature. The `verify_object_from_source` tuple return is defined in Task 2 and consumed nowhere else, which Step 1 of that task verifies before relying on it.

**Ordering.** The two constraints are stated at the top and repeated as a requirement line on Tasks 5 and 7, since an implementer sees only their own task.

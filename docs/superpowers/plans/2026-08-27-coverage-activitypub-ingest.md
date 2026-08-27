# Coverage Campaign 2a: Actor and Object Ingestion — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring the six actor/object ingestion functions in `app/activitypub/util.py` to full statement and branch coverage, and establish the file's first coverage floor.

**Architecture:** Nine tasks, ordered pure → DB-only → network → the large one → ratchet. Each function takes a dictionary parsed from a peer's JSON, so most tests need no network at all; the two that fetch use `respx` via the `http_mock` fixture. `actor_json_to_model` is split across three tasks by actor type, per the spec's instruction not to produce one unreviewable file.

**Tech Stack:** pytest, `respx` (the `http_mock` fixture), PostgreSQL test DB in podman, `./run_tests.sh`.

**Spec:** `docs/superpowers/specs/2026-08-27-coverage-activitypub-ingest-design.md`

## Global Constraints

- `if TYPE_CHECKING` is always a bug. Never introduce it, and never add it to `.coveragerc`'s `exclude_lines`.
- Imports go at the top of the file. No inline imports.
- **Named exceptions only — never a bare `except:`.**
- Every pragma carries a written justification.
- **Do not cite line numbers in docstrings.** Identify code by name and behaviour. Sub-project 1c spent four attempts correcting one citation, each correct when derived and stale when committed.
- Tests assert observable behaviour — what the function returned, which row exists — never on generated SQL, never on mocks. Asserting that respx received a call is a mock assertion.
- For every test, name the production change that would make it fail.
- **Both mutation directions per guard**: delete it (fails the absence test) and over-broaden it so it rejects everything (fails the presence test).
- Report defects; do not fix them. This sub-project has owner authorisation to test, not to change behaviour.
- No new dependencies, no migration.
- NO host Python. Use `./run_tests.sh [pytest args]`, FOREGROUND, blocking.
- Never run `./run_tests.sh --down`.
- Check `pgrep -af '/venv/bin/pytest'` before starting — one suite at a time.
- Baseline at `130cb3e5`: **2238 passed, 3 skipped, 0 failed**, command `./run_tests.sh tests/ -q`. The `--ignore=tests/test_activitypub_util.py` was removed at `0a20752f` and must not return.

---

## Two hazards that apply to several tasks

**`time.sleep(3)` in the retry paths.** `remote_object_to_json` and `verify_object_from_source` both sleep three seconds before retrying a failed fetch, and the retries nest. `pytest.ini` sets a 60s per-test timeout. A test exercising a retry path will either be slow or must patch `time.sleep` — decide per task and say which. Patching is a mock of the standard library, not of the code under test, so it does not violate the no-mocks rule; patching `get_request` would.

**Bare `except:` in both network functions.** Each wraps its `.json()` call in a bare `except:`. Sub-project 1c proved that a bare `except:` swallows test-infrastructure errors — respx's `AllMockedAssertionError` disappeared into one, so a test written to catch a mutation passed under that mutation. **If a mutation in these regions survives, investigate before concluding the code is unmutable.** Report the bare excepts; do not narrow them.

---

## File Structure

| file | responsibility |
|---|---|
| `tests/test_ap_ensure_domains_match.py` | **Create** — Task 1 |
| `tests/test_ap_find_community.py` | **Create** — Task 2 |
| `tests/test_ap_remote_object_to_json.py` | **Create** — Task 3 |
| `tests/test_ap_verify_object_from_source.py` | **Create** — Task 4 |
| `tests/test_ap_find_flair_or_create.py` | **Create** — Task 5 |
| `tests/test_ap_actor_json_person.py` | **Create** — Task 6 |
| `tests/test_ap_actor_json_group.py` | **Create** — Task 7 |
| `tests/test_ap_actor_json_feed.py` | **Create** — Task 8 |
| `tests/factories.py` | **Modify** — Tasks 2, 5, 6 may add factories |
| `coverage_floors.ini` | **Modify** — Task 9 only |
| `tests/README.md` | **Modify** — Task 9 only |

No production code changes. If a task finds a defect, it reports it.

---

### Task 1: `ensure_domains_match`

**Files:**
- Test: `tests/test_ap_ensure_domains_match.py` (create)

**Interfaces:**
- Consumes: nothing. Pure function over a dict — no DB, no network, no fixtures beyond `app`.
- Produces: nothing later tasks depend on.

This is the smallest function in scope and it holds one of the spec's two suspected defects. It answers one question: does an activity's `id` come from the same host as its `actor`?

It reads `id`, then `actor` or `attributedTo`. `attributedTo` may be a string, or a list containing strings or dicts with `type == 'Person'`. It returns `True` only when both are present and their **`netloc`** values are equal.

- [ ] **Step 1: Write the tests**

```python
"""ensure_domains_match decides whether an activity's `id` and its actor come
from the same host. It is an impersonation defence: a peer claiming an id on
one domain while attributing the content to an actor on another is refused.

Every case below is a peer-supplied document. Malformed and hostile shapes are
the point of the function, not edge cases.
"""
import pytest

from app.activitypub.util import ensure_domains_match


class TestMatchingDomains:
    """Mutation that fails these: changing the `id_domain == actor_domain`
    comparison to `!=`, or deleting the `return True`."""

    def test_actor_on_the_same_host_matches(self, app):
        assert ensure_domains_match({
            'id': 'https://peer.example/activities/1',
            'actor': 'https://peer.example/u/alice',
        }) is True

    def test_attributed_to_string_on_the_same_host_matches(self, app):
        assert ensure_domains_match({
            'id': 'https://peer.example/notes/1',
            'attributedTo': 'https://peer.example/u/alice',
        }) is True

    def test_attributed_to_list_of_strings_takes_the_first(self, app):
        assert ensure_domains_match({
            'id': 'https://peer.example/notes/1',
            'attributedTo': ['https://peer.example/u/alice', 'https://other.example/u/bob'],
        }) is True

    def test_attributed_to_list_of_dicts_takes_the_first_person(self, app):
        assert ensure_domains_match({
            'id': 'https://peer.example/notes/1',
            'attributedTo': [{'type': 'Person', 'id': 'https://peer.example/u/alice'}],
        }) is True


class TestMismatchedDomains:
    """The refusals. Mutation that fails these: making the comparison always
    true, or returning True at the end instead of False."""

    def test_actor_on_a_different_host_is_refused(self, app):
        assert ensure_domains_match({
            'id': 'https://peer.example/activities/1',
            'actor': 'https://attacker.example/u/mallory',
        }) is False

    def test_attributed_to_a_different_host_is_refused(self, app):
        assert ensure_domains_match({
            'id': 'https://peer.example/notes/1',
            'attributedTo': 'https://attacker.example/u/mallory',
        }) is False


class TestMissingParts:
    """Mutation that fails these: deleting the `if note_id and note_actor:`
    guard, so the function compares None against None and returns True."""

    def test_no_id_is_refused(self, app):
        assert ensure_domains_match({'actor': 'https://peer.example/u/alice'}) is False

    def test_no_actor_and_no_attributed_to_is_refused(self, app):
        assert ensure_domains_match({'id': 'https://peer.example/activities/1'}) is False

    def test_an_empty_attributed_to_list_is_refused(self, app):
        assert ensure_domains_match({
            'id': 'https://peer.example/notes/1',
            'attributedTo': [],
        }) is False

    def test_a_list_of_non_person_dicts_is_refused(self, app):
        """The loop breaks only on a Person dict or a string; a list of other
        dicts leaves note_actor None."""
        assert ensure_domains_match({
            'id': 'https://peer.example/notes/1',
            'attributedTo': [{'type': 'Service', 'id': 'https://peer.example/u/bot'}],
        }) is False


class TestActorTakesPrecedenceOverAttributedTo:
    """`actor` is checked first and `attributedTo` is only consulted when it is
    absent. Mutation that fails this: swapping the branch order."""

    def test_actor_wins_when_both_are_present(self, app):
        assert ensure_domains_match({
            'id': 'https://peer.example/notes/1',
            'actor': 'https://attacker.example/u/mallory',
            'attributedTo': 'https://peer.example/u/alice',
        }) is False
```

- [ ] **Step 2: Run and confirm they pass**

Run: `./run_tests.sh tests/test_ap_ensure_domains_match.py -q`

If any case fails, do **not** adjust the assertion to match. Establish whether the production code or your expectation is wrong, and report it either way.

- [ ] **Step 3: Probe the suspected defect and report it**

The spec records that this function compares `netloc`, not `hostname`. `netloc` carries userinfo and port:

```bash
podman-compose -f compose.test.yaml exec -T -w /app test-runner python -c "
from urllib.parse import urlparse
for u in ['https://peer.example/x', 'https://peer.example@attacker.example/x', 'https://peer.example:8443/x']:
    p = urlparse(u); print(f'{u:42s} netloc={p.netloc:32s} hostname={p.hostname}')
"
```

Write a test pinning **current** behaviour for the userinfo case and the port case, with a docstring stating plainly that it records a suspected defect and is not asserting intended behaviour. This campaign has twice had to rewrite tests that encoded a defect as settled fact; a docstring saying "this is current behaviour and it is reported" is what prevents the third time.

Then determine whether a peer can actually reach it: is `ensure_domains_match` called with attacker-controlled `id` and `actor`? Report the call sites and your conclusion.

- [ ] **Step 4: Both mutation directions**

```bash
# DELETE: change `if id_domain == actor_domain:` to `if False:`
./run_tests.sh tests/test_ap_ensure_domains_match.py -q
git checkout -- app/activitypub/util.py

# OVER-BROADEN: change it to `if True:`
./run_tests.sh tests/test_ap_ensure_domains_match.py -q
git checkout -- app/activitypub/util.py
git status --porcelain    # must be clean
```

Expected: delete fails the match tests only; over-broaden fails the mismatch tests only. Report both counts. If either fails nothing, say so — that is a finding.

- [ ] **Step 5: Confirm coverage and commit**

Run with `--cov=app.activitypub.util --cov-branch --cov-report=term-missing` and confirm `ensure_domains_match`'s range is fully covered.

```bash
git add tests/test_ap_ensure_domains_match.py
git commit -m "test: cover ensure_domains_match's impersonation check

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: `find_community`

**Files:**
- Test: `tests/test_ap_find_community.py` (create)
- Modify: `tests/factories.py` if a factory is needed

**Interfaces:**
- Consumes: `make_instance`, `make_community`, `make_user`, `make_post`, `make_post_reply` from `tests/factories.py`. Read their signatures rather than guessing.
- Produces: nothing later tasks depend on.

DB-backed, no network. It locates the `Community` an incoming activity belongs to, by three strategies in order:

1. **Addressing** — `audience`, `cc`, `to`, `target`, checked on the outer activity and then the inner `object`, matching `Community.ap_profile_id`. Values may be a string or a list. Two exclusions apply: anything starting `https://www.w3.org` (the Public collection) and anything ending `/followers`.
2. **`inReplyTo`** — resolve the parent `Post`, else the parent `PostReply`, and take its community.
3. **PeerTube `Video`** — `attributedTo` as a list, matching either a bare string or a dict with `type == 'Group'`.

- [ ] **Step 1: Enumerate the branches with a command**

```bash
podman-compose -f compose.test.yaml exec -T -w /app test-runner python -c "
import ast
src = open('app/activitypub/util.py').read()
for n in ast.walk(ast.parse(src)):
    if isinstance(n, ast.FunctionDef) and n.name == 'find_community':
        print('span', n.lineno, n.end_lineno)
        for s in ast.walk(n):
            if isinstance(s, ast.If):
                print('  If at', s.lineno, 'orelse=', len(s.orelse))
"
```

Put the output in your report. Do not enumerate by reading — seven enumerations in this campaign were wrong when re-derived.

- [ ] **Step 2: One test class per strategy, each with a match and a miss**

The miss case is what proves the match is conditional on the lookup rather than on the shape alone. Cover at minimum:

| case | expectation |
|---|---|
| `audience` string matching a community's `ap_profile_id` | that community |
| `audience` string matching nothing | falls through to the next strategy |
| `cc` list containing a matching id | that community |
| the outer activity has no addressing but `object` does | that community |
| `https://www.w3.org/ns/activitystreams#Public` present | ignored, not matched |
| an id ending `/followers` | ignored, not matched |
| `inReplyTo` naming a known `Post` | that post's community |
| `inReplyTo` naming a known `PostReply` | that reply's community |
| `inReplyTo` naming nothing known | `None` |
| `type: 'Video'` with `attributedTo` list containing a Group dict | that community |
| nothing matches anywhere | `None` |

Note the addressing loop checks **outer before inner** and returns the first hit. Write a case where both carry a different community and assert which wins — that ordering is behaviour, and a mutation swapping it must fail something.

- [ ] **Step 3: Probe two suspected crashes and report them**

Both are unguarded accesses to peer-controlled data. I confirmed the shapes:

```
a non-string in a cc/to/audience list  ->  AttributeError  (c.startswith)
an object with no 'type' key           ->  KeyError        (rj['type'])
```

Establish whether a peer can reach each. `find_community` receives the parsed activity, so the question is whether any caller validates `type` or the addressing list's element types first. Write tests pinning **current** behaviour — including that it raises, if it does — with docstrings saying these record suspected defects. Report the call-site analysis.

- [ ] **Step 4: Both mutation directions on two strategies**

Pick the addressing strategy and the `inReplyTo` strategy. Delete each (only its match test fails); over-broaden each (its miss test fails). Restore with `git checkout -- app/activitypub/util.py` — scoped, never `git checkout -- app/`. Report all four counts.

- [ ] **Step 5: Confirm coverage and commit**

```bash
git add tests/test_ap_find_community.py tests/factories.py
git commit -m "test: cover find_community's three resolution strategies

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: `remote_object_to_json`

**Files:**
- Test: `tests/test_ap_remote_object_to_json.py` (create)

**Interfaces:**
- Consumes: the `http_mock` fixture (respx) from `tests/conftest.py`; `make_site` from `tests/factories.py` for the 401 path, which reads `Site.query.get(1)`.
- Produces: nothing later tasks depend on.

Fetches a URI and returns parsed JSON, or `None`. Four outcomes: 200 with valid JSON, 200 with a body that will not parse, 401 (retried with a signed request), and anything else.

**Read the two hazards above before starting.** This function contains both: `time.sleep(3)` on every retry, and a bare `except:` around `.json()`.

- [ ] **Step 1: Decide the `time.sleep` strategy and say so**

The retry path sleeps three seconds. Exercising both the first failure and the retry means six seconds, inside a 60s per-test timeout — tolerable but wasteful, and it will grow if later tasks do the same.

Patch `time.sleep` for the retry tests. That is a standard-library patch, not a mock of the code under test. State this in the module docstring so a later reader knows the retry timing is not being asserted.

- [ ] **Step 2: Write the outcome tests**

Cover each with `http_mock`:

| response | expectation |
|---|---|
| 200, valid JSON body | the parsed dict |
| 200, body that is not JSON | `None` |
| 401, then a signed request returning valid JSON | the parsed dict |
| 401, then the signed request also failing | `None` |
| 404 (or any other status) | `None` |
| transport error, then a successful retry | the parsed dict |
| transport error twice | `None` |

`block_outbound_http` is session-scoped autouse and raises on any unmatched request, so a forgotten route surfaces as an error rather than reaching the network.

The 401 path calls `signed_get_request` and reads `Site.query.get(1)` for the private key — seed a `Site` and confirm what it needs before assuming.

- [ ] **Step 3: Both mutation directions**

Target the `status_code == 200` test and the `status_code == 401` test. Delete each; over-broaden each. Report counts.

**If a mutation survives, investigate before concluding.** The bare `except:` around `.json()` can swallow respx's own assertion error, exactly as sub-project 1c found in `fixup_url`. That is a finding about testability, and it should be reported rather than worked around.

- [ ] **Step 4: Confirm coverage and commit**

```bash
git add tests/test_ap_remote_object_to_json.py
git commit -m "test: cover remote_object_to_json's four fetch outcomes

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: `verify_object_from_source`

**Files:**
- Test: `tests/test_ap_verify_object_from_source.py` (create)

**Interfaces:**
- Consumes: `http_mock`, `make_site`. Same shape as Task 3.
- Produces: nothing later tasks depend on.

This is the impersonation defence for `Announce`-wrapped objects, and the most security-relevant function in the sub-project. It performs **two** domain comparisons around a fetch:

1. before fetching — the `object` URI's domain against the `actor`'s domain
2. after fetching — the URI's domain against the fetched object's `attributedTo` domain

On success it **replaces** `request_json['object']` with the fetched document and returns `request_json`. On any failure it returns `None`.

`attributedTo` is handled in four shapes: a string, a dict with `id`, a list (taking the first string or the first `type == 'Person'` dict), and anything else — which returns `None`.

- [ ] **Step 1: Cover both comparisons independently**

The two comparisons are separate guards and each needs its own absence and presence case. A test that fails the first never reaches the second, so a single "mismatched domain" test cannot prove both work.

| case | expectation |
|---|---|
| actor and object URI on the same host, fetched object attributed to that host | `request_json` with `object` replaced |
| actor on a different host from the object URI | `None`, **without fetching** |
| object URI with no domain at all | `None`, without fetching |
| fetched object attributed to a different host | `None` |
| fetched object missing `id`, `type`, or `attributedTo` | `None` |
| `attributedTo` as a dict with `id` | matches on that id's host |
| `attributedTo` as a list of strings | matches on the first |
| `attributedTo` as a list with a Person dict | matches on that dict's id |
| `attributedTo` as an integer or other type | `None` |

**Assert that no fetch happened** for the pre-fetch refusals — not by inspecting the mock, but by registering no route for that URL. `block_outbound_http` will raise if the code fetches anyway, which fails the test for the right reason.

- [ ] **Step 2: Pin the netloc-versus-hostname behaviour**

Same suspected defect as Task 1, in a more consequential place. `https://peer.example@attacker.example/x` has `netloc` `peer.example@attacker.example` and `hostname` `attacker.example`.

Construct the case where userinfo makes two different hosts compare equal, or two same hosts compare unequal via a port. Pin **current** behaviour with a docstring stating it records a suspected defect. Report whether a peer controls both strings.

- [ ] **Step 3: Both mutation directions on both comparisons**

Four mutations total. Report all four counts, and note whether the second comparison's mutations produce a wider radius than the first — the first guard returns before the fetch, so mutating it short-circuits everything downstream. That is chain geometry, and pairing the wide mutation with the narrow one on the same guard is what distinguishes it from fixture coupling.

- [ ] **Step 4: Confirm coverage and commit**

```bash
git add tests/test_ap_verify_object_from_source.py
git commit -m "test: cover verify_object_from_source's two domain comparisons

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: `find_flair_or_create`

**Files:**
- Test: `tests/test_ap_find_flair_or_create.py` (create)
- Modify: `tests/factories.py` if a `CommunityFlair` factory is needed — check whether `make_post_flair` already provides one.

**Interfaces:**
- Consumes: `make_community` and any flair factory. It takes an optional `session=` parameter defaulting to `db.session`.
- Produces: nothing later tasks depend on.

Resolves a flair dict to an existing `CommunityFlair` or creates one. Lookup is by `ap_id` first, then by `preferredUsername`, then by `display_name`. When found, it updates properties — and each property is accepted under **two spellings**, snake_case and camelCase (`text_color`/`textColor`, `background_color`/`backgroundColor`, `blur_images`/`blurImages`).

- [ ] **Step 1: Enumerate the property pairs with a command**

Derive the full list rather than trusting the three named above — read the function and list every `if "x" in flair: ... elif "y" in flair:` pair. Put the enumeration in your report.

- [ ] **Step 2: Cover lookup, creation, and both spellings**

Each spelling pair needs both halves tested: the snake_case key alone, and the camelCase key alone. A test using only one spelling leaves the other branch unexercised while branch coverage reports the `if` as covered — the failure mode this campaign has hit repeatedly with compound conditions.

Also cover: found by `ap_id`; found by `preferredUsername`; found by `display_name`; not found and created; the `session=` parameter being passed explicitly rather than defaulted.

- [ ] **Step 3: Both mutation directions**

Target the `ap_id` lookup and one spelling pair. Report counts.

- [ ] **Step 4: Confirm coverage and commit**

```bash
git add tests/test_ap_find_flair_or_create.py tests/factories.py
git commit -m "test: cover find_flair_or_create's lookup and dual-spelling updates

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Tasks 6, 7 and 8: `actor_json_to_model`, split by actor type

**Files:**
- Test: `tests/test_ap_actor_json_person.py` (Task 6), `tests/test_ap_actor_json_group.py` (Task 7), `tests/test_ap_actor_json_feed.py` (Task 8)
- Modify: `tests/factories.py` if a shared peer-actor document builder is warranted

**Interfaces:**
- Consumes: `make_instance` and a peer actor JSON fixture. **Task 6 produces the shared base document; Tasks 7 and 8 consume it.** Name it explicitly in Task 6's report so the later tasks can find it.
- Signature: `actor_json_to_model(activity_json, address, server)`.

This is the largest single function this campaign has targeted — 194 uncovered statements across roughly 372 lines, with three top-level branches on `activity_json['type']`:

| task | branch | produces |
|---|---|---|
| 6 | `'Person'` or `'Service'` | a `User` |
| 7 | `'Group'` | a `Community` |
| 8 | `'Feed'` | a `Feed` |

Two guards run before the type branch, and **both belong to Task 6** since it goes first: `'type' not in activity_json` returns `None`, and `server not in activity_json['id']` returns `None`.

- [ ] **Step 1 (Task 6 only): probe and report the suspected substring defect**

The second guard is a substring test, not a host comparison. Probed at spec time:

```
server=good.example  id=https://good.example/u/alice                passes  real host good.example
server=good.example  id=https://good.example.attacker.net/u/alice   passes  real host good.example.attacker.net
server=good.example  id=https://attacker.net/u/x?ref=good.example   passes  real host attacker.net
```

Same shape as the substring gate that produced a stored denial of service earlier in this campaign.

**Establish what `server` is at each call site.** If callers derive it from the URL they just fetched, the check is weaker than it looks but may not be independently exploitable; if a peer supplies it, it is. That determination is the deliverable — report it with the call sites, and pin current behaviour with a docstring saying it records a suspected defect.

- [ ] **Step 2: Enumerate your branch's optional fields with a command**

Each actor type reads many optional fields with `if 'x' in activity_json` or a conditional expression. Derive the list for your branch:

```bash
podman-compose -f compose.test.yaml exec -T -w /app test-runner python -c "
import ast
src = open('app/activitypub/util.py').read()
for n in ast.walk(ast.parse(src)):
    if isinstance(n, ast.FunctionDef) and n.name == 'actor_json_to_model':
        for s in ast.walk(n):
            if isinstance(s, ast.If):
                print('If at', s.lineno, 'orelse=', len(s.orelse))
"
```

Then restrict to your branch's line span. Put the enumeration in your report; the number of optional fields determines how many tests you need, and guessing it is how five enumerations in this campaign came out wrong.

- [ ] **Step 3: Cover present-and-absent for every optional field**

Each optional field needs both: present with a value, and absent so the default applies. A test that always supplies every field exercises none of the defaults, while branch coverage reports the `if`s as covered.

Also cover, per branch:
- the actor already exists (looked up by `ap_profile_id`) and is returned without creating a second
- the actor does not exist and is created
- required fields missing — establish what happens and pin it

Task 6 additionally covers: `'type'` absent → `None`; `server` not in `id` → `None`; `Person` versus `Service` (the latter sets `bot=True`); and the `PropertyValue` attachment handling.

- [ ] **Step 4: Both mutation directions**

Target your branch's type test and one optional-field guard. Report counts. Expect the type test's over-broadening to be wide — it redirects control to a different actor type entirely — and pair it with the narrow mutation on the same guard.

- [ ] **Step 5: Confirm coverage and commit**

Confirm your branch's span is fully covered. The three tasks together must leave `actor_json_to_model` with no uncovered statements; Task 8 confirms the whole function.

```bash
git add tests/test_ap_actor_json_<type>.py tests/factories.py
git commit -m "test: cover actor_json_to_model's <Type> branch

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 9: Measure, set the file's first floor, document

**Files:**
- Modify: `coverage_floors.ini`
- Modify: `tests/README.md`
- Modify: `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`

**Interfaces:**
- Consumes: all eight preceding test files.
- Produces: the first coverage floor for `app/activitypub/util.py`.

- [ ] **Step 1: Measure**

```bash
./run_tests.sh tests/ -q --cov=app --cov-report=json
podman-compose -f compose.test.yaml exec -T -w /app test-runner python -c "
import json; d=json.load(open('coverage.json'))
s=d['files']['app/activitypub/util.py']['summary']
print(s['percent_covered'], s['num_statements'], len(d['files']['app/activitypub/util.py']['missing_lines']))
"
```

`percent_covered` is the **blended statement+branch** figure and is what `tests/check_coverage_floors.py` reads. Say so in your report.

- [ ] **Step 2: Set the floor, rounded DOWN**

`app/activitypub/util.py` has no floor today; this establishes the first. Add it to `coverage_floors.ini`. **Do not round up.** Leave `app/utils.py`'s floor of 73 untouched.

Prove it bites both ways:

```bash
podman-compose -f compose.test.yaml exec -T -w /app test-runner python tests/check_coverage_floors.py coverage.json coverage_floors.ini
```
(both arguments are required; the script fails closed without them)

Set the floor one higher, confirm it exits 1 naming the module; restore, confirm it exits 0.

- [ ] **Step 3: Document**

Add a "Sub-project 2a" section to `tests/README.md` and to the campaign findings doc, covering:

- the two suspected defects, their probe output, and the call-site analysis that determined severity
- any further defects the eight tasks found
- that `time.sleep(3)` in the retry paths is patched in tests and why that is not a mock of the code under test
- that the bare `except:` clauses in the two network functions make those regions resistant to mutation testing, with whatever the tasks observed

**Cite no line numbers.** Identify code by name. If a historical reference is genuinely needed, anchor it to a named commit and quote the source text, which is self-verifying — that is the one safe form, established in sub-project 1c.

- [ ] **Step 4: Commit**

```bash
git add coverage_floors.ini tests/README.md docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md
git commit -m "test: set app/activitypub/util.py's first coverage floor at <N>

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Plan Self-Review

**Spec coverage.** Every spec section maps to a task: the six in-scope functions to Tasks 1-8; the two suspected defects to Tasks 1/4 (netloc) and Task 6 (substring); the fetch-driven testing shape to Tasks 3-4; document-driven to Tasks 1, 2, 5-8; the first floor to Task 9; the `actor_json_to_model` split the spec called for to Tasks 6-8. The spec's malformed-input requirement appears as explicit hostile cases in every task rather than as a general instruction.

**Placeholder scan.** No TBD/TODO. Tasks 2, 5, 6-8 give case tables and derivation commands rather than complete test bodies — deliberate, because each depends on an enumeration the implementer must derive first, and prescribing bodies against an unverified enumeration is how five earlier enumerations in this campaign came out wrong. Tasks 1, 3 and 4 give complete tables or bodies because their branch structure is fully known from the spec's exploration.

**Type consistency.** `actor_json_to_model(activity_json, address, server)` is used with that signature throughout Tasks 6-8. `find_flair_or_create(flair, community_id, session=None)` matches Task 5. `ensure_domains_match(activity) -> bool` and `find_community(request_json)` match Tasks 1 and 2. Factory names are stated as "read the signature rather than guessing" wherever a task depends on one, since `tests/factories.py` has grown across four sub-projects.

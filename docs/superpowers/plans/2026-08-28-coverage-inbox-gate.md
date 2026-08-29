# Inbox Gate Coverage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cover `app/activitypub/routes.py`'s inbox gate — `shared_inbox`, its three route aliases, and `replay_inbox_request` — to 100% statement and branch coverage or a documented reason, with mutation evidence per guard.

**Architecture:** Tests POST through Flask's test client to `/inbox`, because the thing under test is an endpoint's behaviour, not a function's return value. Signatures are real: `HttpSignature.signed_request(..., send_via_async=True)` returns headers without sending, so tests sign with production's own code and let production's own verification judge them. No mock stands between a test and the security decision it is checking.

**Tech Stack:** pytest, Flask test client, respx (via `http_mock`), fakeredis (via `redis_double`), podman-compose test stack.

**Spec:** `docs/superpowers/specs/2026-08-28-coverage-inbox-gate-design.md`

## Global Constraints

- **Report defects; do not fix them.** No authorisation exists for any defect found here. Register them; the tests are what make fixing them safe later.
- **Never monkeypatch `HttpSignature.verify_request`, `precheck`, or `LDSignature.verify_signature.** Faking the verifier makes every signature test vacuous. If a case cannot be built with a real signature, report that rather than mocking it.
- **A status code is not an assertion.** Six outcomes in this gate return 200. Every test asserts something that distinguishes its outcome from the others sharing its code: the `ActivityPubLog` message, a redis key, whether dispatch happened, or a row that was or was not written.
- **`http_mock`'s `assert_all_called` stays on.** It caught four vacuous tests in sub-project 3.
- **Every enumeration is derived in the task that uses it**, with the command and its output quoted in the module docstring. The spec's twenty-outcome table is a claim to be checked, not a fact to be copied.
- **Mutation per guard, both directions where they differ**, with counts reported and survivors explained rather than chased.
- Test command: `./run_tests.sh tests/<file> -q --no-cov`. Coverage: `./run_tests.sh tests/ -q --cov=app --cov-report=json`.
- One suite run at a time per worktree. Do not run `./run_tests.sh --down`.

---

## File structure

| File | Responsibility |
|---|---|
| `tests/factories.py` (modify) | `signed_inbox_post` and `inbox_activity` — the signing lever and the minimal valid activity, used by every later task |
| `tests/test_inbox_gate_refusals.py` (create) | Tasks 2–5: every refusal that happens before signature verification |
| `tests/test_inbox_gate_signatures.py` (create) | Task 6: HTTP signature, the LD fallback, the fediseer exemption |
| `tests/test_inbox_gate_dispatch.py` (create) | Task 7: the success path, instance bookkeeping, both dispatch targets, the route aliases, `replay_inbox_request` |
| `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` (modify) | Task 8: register whatever the tests found |
| `coverage_floors.ini`, `tests/README.md`, conftest docstring (modify) | Task 9: first floor for the module, the signing lever documented, the stale `redis_double` section corrected |

Three test files rather than one: the refusal cases need no signature at all, the signature cases need the full lever, and the dispatch cases need recorders. Splitting by what the test needs to set up keeps each file's fixtures honest.

---

### Task 1: The signing lever, and the spike that decides whether this sub-project is possible

The spec names one risk: the test client's view of host and path may disagree with what was signed. Everything else depends on this. **If it cannot be made to work, stop and report — do not reach for a mock.**

**Files:**
- Modify: `tests/factories.py`
- Test: `tests/test_inbox_gate_signatures.py` (create, one test for now)

**Interfaces:**
- Produces: `signed_inbox_post(client, activity, sender, *, path='/inbox', body=None, host=None) -> Response` and `inbox_activity(actor, *, activity_type='Like', object_uri=None, **fields) -> dict`. Every later task consumes both by these exact names.

- [ ] **Step 1: Confirm the signing chain lines up before writing a helper**

The four values that must agree between signing and verification, read from `app/activitypub/signature.py`:

| signed by `signed_request` | recomputed by `headers_from_request` |
|---|---|
| `(request-target)` = `f"{method} {uri_parts.path}"`, method `post` | `f"{request.method.lower()} {request.path}"` |
| `Host` = `uri_parts.hostname` | `request.headers.get('HOST')` |
| `Date` = `http_date()` | passed through as a header |
| `Digest`, `Content-Type` | `request.headers.get('Content-Type')` |

So the POST must go to the same path that was signed, and carry the `Host` header `signed_request` produced. Both fall out of passing its headers to the client verbatim.

- [ ] **Step 2: Add the two helpers to `tests/factories.py`**

```python
def inbox_activity(actor, *, activity_type: str = 'Like', object_uri: str = None, **fields) -> dict:
    """The minimum an activity needs to get past shared_inbox's field check.

    That check is `not 'id' in request_json or not 'type' ... or not 'actor'
    ... or not 'object'`, so all four are always present here and a test that
    wants one missing deletes it explicitly -- which reads as the deviation it
    is. `id` is unique per call because shared_inbox writes it to Redis for 90
    seconds to suppress duplicates; a fixed id would make tests interfere
    through Redis rather than through anything they assert about.
    """
    activity = {
        'id': f'{actor.ap_profile_id}/activities/{uuid.uuid4().hex}',
        'type': activity_type,
        'actor': actor.ap_profile_id,
        'object': object_uri or f'https://{actor.instance.domain}/objects/1',
    }
    activity.update(fields)
    return activity


def signed_inbox_post(client, activity: dict, sender, *, path: str = '/inbox',
                      body: bytes = None, host: str = None):
    """POST `activity` to `path` with a REAL HTTP signature made by `sender`.

    Uses production's own signing code. `signed_request(send_via_async=True)`
    returns (uri, headers, body_bytes) instead of sending, which is the whole
    reason these tests can be honest: the signature the gate verifies is one
    the application made, not one a test faked, and
    HttpSignature.verify_request is never patched anywhere in this suite.

    `sender` must have been built with `make_user(..., with_keys=True)` -- a
    keyless user dies at signing with "'NoneType' object has no attribute
    'encode'", because signed_request calls .encode() on the private key.

    `body` overrides the bytes actually sent while leaving the signature
    alone, which is how a test produces a request whose digest no longer
    matches its body. `host` overrides the Host header for the same reason,
    one field over.
    """
    from app.activitypub.signature import HttpSignature
    host = host or current_app.config['SERVER_NAME']
    _uri, headers, body_bytes = HttpSignature.signed_request(
        f'https://{host}{path}', activity, sender.private_key,
        f'{sender.ap_profile_id}#main-key', send_via_async=True)
    if host is not None:
        headers['Host'] = host
    return client.post(path, data=body if body is not None else body_bytes,
                       headers=headers, content_type='application/activity+json')
```

Add `import uuid` and `from flask import current_app` at the top of the file if they are not already imported.

- [ ] **Step 3: Write the spike test**

```python
"""The inbox gate's signature handling (app/activitypub/routes.py).

Every signature below is produced by HttpSignature.signed_request and checked
by HttpSignature.verify_request -- production's own code on both sides. No
test in this file patches either. A test that stubbed the verifier would
assert only that the stub was called, which is the failure mode this campaign
has already recorded once in another repository's suite.
"""

import pytest

from app.models import ActivityPubLog, Instance
from tests.factories import (inbox_activity, make_instance, make_site, make_user,
                             signed_inbox_post)

pytestmark = pytest.mark.usefixtures('redis_double')


@pytest.fixture
def peer_sender(db_session):
    """A remote actor with a real keypair, resolvable without an actor fetch.

    ap_fetched_at is stamped for the same reason sub-project 3's
    resolvable_remote_author stamps it: find_actor_or_create_cached would
    otherwise schedule a refresh that fetches the actor document inline under
    eager Celery.
    """
    from app.utils import utcnow
    from app import db
    make_site()
    instance = make_instance('peer.example')
    sender = make_user(instance, 'alice', with_keys=True)
    sender.ap_fetched_at = utcnow()
    db.session.commit()
    return sender


def test_a_genuinely_signed_activity_is_accepted(app, peer_sender, monkeypatch):
    """The spike this sub-project's feasibility rests on: a request signed by
    production's signer passes production's verifier through the test client.

    Asserts on the dispatch rather than on the 200, because a bare 200 is also
    what six refusal paths return.
    """
    dispatched = []
    monkeypatch.setattr('app.activitypub.routes.process_inbox_request',
                        lambda *args: dispatched.append(args))
    monkeypatch.setitem(app.config, 'DEBUG', True)

    with app.test_client() as client:
        response = signed_inbox_post(client, inbox_activity(peer_sender), peer_sender)

    assert response.status_code == 200
    assert len(dispatched) == 1
```

- [ ] **Step 4: Run it**

Run: `./run_tests.sh tests/test_inbox_gate_signatures.py -q --no-cov`

Expected: PASS. **If it fails on signature verification**, diagnose in this order before changing anything: print `request.headers['Signature']`'s `headers=` list, then compare `headers_from_request`'s reconstruction against the string `signed_request` signed. The likely culprits are the `Host` header and `SERVER_NAME`. If it cannot be made to pass with a real signature, **stop the sub-project and report** — the design says re-scope rather than mock.

- [ ] **Step 5: Commit**

```bash
git add tests/factories.py tests/test_inbox_gate_signatures.py
git commit -m "test: sign a real request into the inbox gate

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Re-derive the outcome table, and cover the pre-JSON refusals

**Files:**
- Create: `tests/test_inbox_gate_refusals.py`

**Interfaces:**
- Consumes: `inbox_activity` from Task 1.
- Produces: the derived outcome table, quoted in this file's module docstring. Tasks 3–7 cite it rather than re-deriving.

- [ ] **Step 1: Derive the gate's shape**

```bash
podman-compose -f compose.test.yaml exec -T -w /app test-runner python -c "
import ast
src = open('app/activitypub/routes.py').read()
n = next(x for x in ast.walk(ast.parse(src))
         if isinstance(x, ast.FunctionDef) and x.name == 'shared_inbox')
print('span', n.lineno, n.end_lineno)
print('If:', len([x for x in ast.walk(n) if isinstance(x, ast.If)]))
print('Try:', len([x for x in ast.walk(n) if isinstance(x, ast.Try)]))
print('Return:', len([x for x in ast.walk(n) if isinstance(x, ast.Return)]))
for r in [x for x in ast.walk(n) if isinstance(x, ast.Return)]:
    print('  line', r.lineno, ':', ast.unparse(r))
"
```

Quote the command and its output in the module docstring. **Compare the return statements against the spec's twenty-row table and report every disagreement** — the spec says explicitly that it expects to be wrong somewhere.

- [ ] **Step 2: Write the four pre-JSON tests**

```python
def test_an_unparseable_body_is_refused(app, site):
    with app.test_client() as client:
        response = client.post('/inbox', data='{not json',
                               content_type='application/json')

    assert response.status_code == 400


def test_a_json_null_body_is_refused(app, site):
    """`request.get_json` returns None rather than raising for a bare null."""
    with app.test_client() as client:
        response = client.post('/inbox', data='null',
                               content_type='application/json')

    assert response.status_code == 400


def test_a_paused_instance_returns_429(app, site, redis_double):
    redis_double.set('pause_federation', '1')

    with app.test_client() as client:
        response = client.post('/inbox', data='{"a": 1}',
                               content_type='application/json')

    assert response.status_code == 429


def test_a_closed_instance_returns_410(app, site, redis_double):
    redis_double.set('pause_federation', '666')

    with app.test_client() as client:
        response = client.post('/inbox', data='{"a": 1}',
                               content_type='application/json')

    assert response.status_code == 410
```

**Note on the pause tests:** both post a body that would fail the field check anyway, which is deliberate — it proves the pause switch fires *before* that check. If the pause value is read as bytes rather than str by the real client, the comparison `pause_federation == '1'` may not hold under fakeredis; if these tests fail, report which type each returns rather than adjusting the test to match the fixture.

- [ ] **Step 3: Run them**

Run: `./run_tests.sh tests/test_inbox_gate_refusals.py -q --no-cov`
Expected: 4 passed.

- [ ] **Step 4: Try the BlockingIOError arm and report**

`except BlockingIOError` catches a client disconnect mid-body. Attempt it by posting with a `Content-Length` larger than the body sent. If the test client cannot produce it, **say so in the docstring and leave the arm uncovered with that reason** — an honest gap beats a contrived test.

- [ ] **Step 5: Commit**

```bash
git add tests/test_inbox_gate_refusals.py
git commit -m "test: cover the inbox gate's pre-JSON refusals

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: The field check and the three Announce refusals

**Files:**
- Modify: `tests/test_inbox_gate_refusals.py`

**Interfaces:**
- Consumes: `inbox_activity`, and `object_has_missing_fields` from `app/activitypub/util.py` (read it; it returns False for an `OrderedCollection` regardless of the other keys, which is a case worth covering).

- [ ] **Step 1: Cover the minimum-field check**

Four tests, one per missing key, each deleting exactly one of `id`, `type`, `actor`, `object` from a valid activity and asserting 200 plus the log message `'Missing minimum expected fields in JSON'`. Enable the log with `monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)` and assert on `ActivityPubLog.query.one().exception_message`, because the bare 200 does not distinguish this outcome from five others.

```python
@pytest.mark.parametrize('missing', ['id', 'type', 'actor', 'object'])
def test_a_missing_minimum_field_is_refused(app, site, peer_sender, monkeypatch, missing):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    activity = inbox_activity(peer_sender)
    del activity[missing]

    with app.test_client() as client:
        response = client.post('/inbox', json=activity)

    assert response.status_code == 200
    assert ActivityPubLog.query.one().exception_message == 'Missing minimum expected fields in JSON'
```

- [ ] **Step 2: Cover the Announce-object refusals**

Three tests, distinguished by their log messages, all returning 200:

1. Announce whose dict object lacks required fields and is typed `Page` → `'Intended for Mastodon'`.
2. The same typed `Note` → also `'Intended for Mastodon'`.
3. The same typed anything else (use `'Like'`) → `'Missing minimum expected fields in JSON Announce object'`.

Build the object as `{'type': 'Page'}` — no `id`, no `actor`, no `object` — so `object_has_missing_fields` returns True.

- [ ] **Step 3: Cover the local-content Announce**

An Announce whose object dict carries an `actor` starting with `https://<SERVER_NAME>` is dropped with `'Activity about local content which is already present'`. The object needs the four fields present so it passes `object_has_missing_fields` first — this outcome sits *after* that check and a test that gets the order wrong will pass for the wrong reason.

- [ ] **Step 4: Cover the OrderedCollection exemption, and report it**

`object_has_missing_fields` returns False for an `OrderedCollection` **without checking any other key**, so an Announce wrapping an empty OrderedCollection passes the field check. Write the test that shows it reaching the next stage. If that turns out to be reachable with an object carrying no `id` at all, it is a finding for Task 8 — report it rather than judging it here.

- [ ] **Step 5: Run and commit**

Run: `./run_tests.sh tests/test_inbox_gate_refusals.py -q --no-cov`

```bash
git add tests/test_inbox_gate_refusals.py
git commit -m "test: cover the inbox gate's field check and Announce refusals

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Allowlist mode, duplicate suppression, and the PeerTube drop

**Files:**
- Modify: `tests/test_inbox_gate_refusals.py`

- [ ] **Step 1: Cover strong-allowlist rejection**

`g.site.allowlist_mode >= ALLOWLIST_STRONG` (`ALLOWLIST_STRONG = 1`, `app/constants.py`) plus an actor whose host is not allowed → **403**, and note that this path logs nothing, which is itself worth asserting (`ActivityPubLog.query.count() == 0` with logging enabled). Set the mode on the Site row, and read `instance_allowed` to see what makes a host allowed before writing the test.

- [ ] **Step 2: Cover duplicate suppression**

Post the same activity twice. The second must return 200 with `'Already aware of this activity'` and must NOT dispatch. Assert both, with a dispatch recorder:

```python
def test_a_repeated_activity_is_suppressed(app, site, peer_sender, monkeypatch, redis_double):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    monkeypatch.setitem(app.config, 'DEBUG', True)
    dispatched = []
    monkeypatch.setattr('app.activitypub.routes.process_inbox_request',
                        lambda *args: dispatched.append(args))
    activity = inbox_activity(peer_sender)

    with app.test_client() as client:
        first = signed_inbox_post(client, activity, peer_sender)
        second = signed_inbox_post(client, activity, peer_sender)

    assert first.status_code == 200 and second.status_code == 200
    assert len(dispatched) == 1
    assert 'Already aware of this activity' in [
        row.exception_message for row in ActivityPubLog.query.all()]
```

Import `signed_inbox_post` and the `peer_sender` fixture into this file — the duplicate check sits after the field check but before signature verification, so an unsigned post would be refused for the wrong reason on the *first* request.

- [ ] **Step 3: Cover the PeerTube drop**

An actor URI ending `accounts/peertube` → empty body, logged `'PeerTube View or CacheFile activity'`. Note the source returns `''` here with **no status code**, unlike its neighbours which return `'', 200` — Flask defaults it to 200, so the outcomes look identical from outside. Assert the log message, and record the inconsistency for Task 8.

- [ ] **Step 4: Run and commit**

```bash
git add tests/test_inbox_gate_refusals.py
git commit -m "test: cover the inbox gate's allowlist, duplicate and PeerTube paths

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Precheck, the Delete shortcut, and actor resolution

**Files:**
- Modify: `tests/test_inbox_gate_refusals.py`

- [ ] **Step 1: Cover `HttpSignature.precheck` failing**

Read `precheck` first and build a request that trips it *without* patching it — a malformed `Signature` header is the natural input. Expected 400, logged `'Precheck failed: '` plus the error text.

- [ ] **Step 2: Cover the Delete-of-unknown-account shortcut**

A `Delete` whose `object` is a string equal to its `actor`, for an actor with no `User` row → 200, logged `'Does not exist here'`, and **no dispatch**. This path is reached before signature verification, so the request need not be signed — assert that too, since an unsigned request reaching a 200 is exactly the kind of thing a reader will want proof of.

- [ ] **Step 3: Cover actor-not-found**

An activity from an actor that cannot be resolved → 200, logged with `'Actor could not be found 1 - '`. Resolution goes through `find_actor_or_create_cached`, which will attempt a fetch — register a 404 for the actor URI with `http_mock` so the failure is the fetch failing, not an unmatched request.

- [ ] **Step 4: Run and commit**

```bash
git add tests/test_inbox_gate_refusals.py
git commit -m "test: cover the inbox gate's precheck, Delete shortcut and actor lookup

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Signature verification, the LD fallback, and the fediseer exemption

**Files:**
- Modify: `tests/test_inbox_gate_signatures.py`

- [ ] **Step 1: Cover a signature that does not verify**

Sign with one key, store a different public key on the actor. Expected 400, logged `'Could not verify HTTP signature: '`. Build the mismatch by generating a second keypair with `RsaKeys.generate_keypair()` and assigning its public half to the sender row after signing.

- [ ] **Step 2: Cover a tampered body**

Sign the activity, then send different bytes via `signed_inbox_post(..., body=b'{"id": "tampered"}')`. The digest no longer matches. Expected 400. This is the test that proves the digest is actually checked rather than merely present.

- [ ] **Step 3: Cover the LD-signature fallback, both outcomes**

When HTTP verification fails and the document carries a `signature` key, the gate falls back to `LDSignature.verify_signature`. Cover: (a) an invalid LD signature → 400 logged `'Could not verify LD signature: '`; (b) a valid one → the request proceeds with `bounced` True. For (b), read `LDSignature` for how to produce a valid document signature; **if it cannot be produced with production's own code, report that and leave the arm uncovered** rather than patching the verifier.

- [ ] **Step 4: Cover the fediseer exemption**

An actor whose `ap_profile_id` is exactly `https://fediseer.com/api/v1/user/fediseer`, sending an unsigned `Create` of a `ChatMessage`, reaches the `...` branch and proceeds. Note the branch body is a literal `...` — it does nothing but decline to return, which is worth stating in the docstring.

- [ ] **Step 5: Mutation**

Mutate: the `bounced` flag forced False; the LD-fallback branch deleted; the fediseer condition's actor test dropped. Report counts and any survivor.

- [ ] **Step 6: Run and commit**

```bash
git add tests/test_inbox_gate_signatures.py
git commit -m "test: cover the inbox gate's signature verification and fallbacks

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: The success path, bookkeeping, dispatch, and the aliases

**Files:**
- Create: `tests/test_inbox_gate_dispatch.py`

- [ ] **Step 1: Cover the instance bookkeeping**

On success the gate sets `last_seen`, clears `dormant` and `gone_forever`, zeroes `failures`, and sets `ip_address`. Build an Instance with the opposite of each (dormant True, gone_forever True, failures 3) and assert every field afterwards. This is the privacy-relevant write the spec put explicitly in scope.

- [ ] **Step 2: Cover `ip_address` blanked when bounced**

The same path stores `ip_address() if not bounced else ''`. Reach it via the LD-signature fallback from Task 6 and assert `instance.ip_address == ''`. If Task 6 could not produce a valid LD signature, this arm is unreachable too — say so, and link the two.

- [ ] **Step 3: Cover both dispatch targets**

An account deletion (`Delete` where `object == actor`, for an actor that DOES exist) dispatches `process_delete_request`; everything else dispatches `process_inbox_request`. Recorders for both; assert the right one fired and the other did not.

- [ ] **Step 4: Cover the DEBUG split**

Same recorder shape as sub-project 3's `Recorder` class (`tests/test_ap_resolve_from_search.py`), with the same caveat in the docstring: under eager Celery `.delay()` runs inline and propagates, so this pins which call was made, not a behavioural difference.

- [ ] **Step 5: Cover the three route aliases**

`/site_inbox`, `/u/<actor>/inbox`, `/c/<actor>/inbox` all return `shared_inbox()`. One test each, asserting the same dispatch happens — cheap, and it is the only thing pinning that these routes are wired to the gate at all.

- [ ] **Step 6: Cover `replay_inbox_request`**

Read it first and derive its own branch table; it is a separate 55-line function that replays stored JSON. Cover its outcomes with the same standard as the gate's.

- [ ] **Step 7: Run and commit**

```bash
git add tests/test_inbox_gate_dispatch.py
git commit -m "test: cover the inbox gate's success path, dispatch and aliases

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: Whole-function confirmation and the register

**Files:**
- Modify: `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`

**Report only. Fix nothing.**

- [ ] **Step 1: Measure both functions whole**

```bash
./run_tests.sh tests/test_inbox_gate_refusals.py tests/test_inbox_gate_signatures.py tests/test_inbox_gate_dispatch.py -q --cov=app.activitypub.routes --cov-report=json
```

Then read `executed_lines` and `missing_branches` against each function's span, as sub-project 3 did. Report the figures and **explain every gap** — an unexplained remainder is not an acceptable outcome.

- [ ] **Step 2: File what the tests found**

Everything reported in Tasks 2–7: the `''`-without-status inconsistency on the PeerTube path, the `OrderedCollection` exemption if it proved reachable with a fieldless object, the fediseer branch's `...` body, and anything else. The campaign's next free number is **D41** — take numbers in order and update the allocation ledger in the same commit.

- [ ] **Step 3: Convert the reading-only reachability claims**

D21, D24 and D35 are all marked as reading-only for reachability. This sub-project has now executed the entry point they depend on. **Say precisely how far the tests reach** — they reach dispatch, not the handlers — and upgrade only what the tests actually support. Overstating this would be the exact error the campaign keeps correcting.

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md
git commit -m "docs: register the inbox gate's findings

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: Floor, documentation, and one correction

**Files:**
- Modify: `coverage_floors.ini`, `tests/README.md`, `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`

- [ ] **Step 1: Measure the full suite**

```bash
./run_tests.sh tests/ -q --cov=app --cov-report=json && \
podman-compose -f compose.test.yaml exec -T -w /app test-runner \
  python tests/check_coverage_floors.py coverage.json coverage_floors.ini
```

- [ ] **Step 2: Add the first floor for the module**

`app/activitypub/routes.py` has never had a floor. Add one line, set to the measured `percent_covered` rounded DOWN. **Edit additively** — an agent in this campaign once emptied this file, deleting three floors. Prove it bites: one point higher exits 1 naming the module, back down exits 0. End with `git diff coverage_floors.ini` showing one added line.

- [ ] **Step 3: Correct the stale `redis_double` section**

`docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` says `redis_double` covers `get_redis_connection` "not `redis_client`" and advises widening it. `coverage-utils-feed` already widened it and `tests/conftest.py` documents that. Correct the section and say when it changed, so the correction is dated rather than silent.

- [ ] **Step 4: Document the signing lever in `tests/README.md`**

A "Sub-project 4: the inbox gate" section covering: `signed_inbox_post` and why signatures are real rather than mocked; that a status code alone is not an assertion in this gate, with the count of outcomes sharing 200; the Redis id-uniqueness hazard; and what is deliberately not covered (`process_inbox_request`).

- [ ] **Step 5: Commit**

```bash
git add coverage_floors.ini tests/README.md docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md
git commit -m "docs: set the first floor for app/activitypub/routes.py

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Plan self-review

**Spec coverage.** All twenty outcomes map to tasks: 1–5 → Task 2; 6–9 → Task 3; 10–12 → Task 4; 13–15 → Task 5; 16–19 → Task 6; 20 and the dispatch pair → Task 7. The instance bookkeeping the spec put explicitly in scope is Task 7 Steps 1–2. The `redis_double` correction the spec made a deliverable is Task 9 Step 3. The signing risk the spec flagged is Task 1, with an explicit stop condition.

**Two arms may prove uncoverable**, and both are handled by reporting rather than by contrivance: `BlockingIOError` (Task 2 Step 4) and the valid-LD-signature path (Task 6 Step 3), which Task 7 Step 2 depends on and says so.

**Interface consistency.** `signed_inbox_post` and `inbox_activity` are defined once in Task 1 with the exact signatures later tasks use; the `peer_sender` fixture is defined in Task 1's file and Task 4 Step 2 notes it must be imported where reused.

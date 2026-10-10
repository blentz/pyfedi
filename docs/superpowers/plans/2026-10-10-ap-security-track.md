# ActivityPub Security Track Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the verified ActivityPub security gaps:

- unsigned-activity acceptance;
- private-activity leaks;
- weak signature checks;
- dedupe poisoning;
- forged Announces.

Then add RFC 9421 signatures with double-knocking, and opt-in authorized fetch.

**Architecture:**
- **New module `app/activitypub/entitlement.py`:** answers "who signed this GET" and "may that signer see this audience". The `/activities`, `/private_message` and post/comment routes and the secure-mode hook all use it.
- **Signature work stays in `app/activitypub/signature.py`,** plus a new standalone module, `app/activitypub/signature_rfc9421.py`.
- **Announce checks:** applied in `process_inbox_request` before dispatch, reusing `verify_object_from_source`.
- **Migrations:** two new columns, `Instance.signature_format` and `Site.require_signed_fetch`.

**Tech Stack:** Flask, SQLAlchemy/Alembic, Celery, `cryptography` 50.0.2 (no new dependencies), Redis (fakeredis in tests), pytest in containers.

**Spec:** `docs/superpowers/specs/2026-10-10-ap-security-track-design.md`. The roadmap is `docs/superpowers/specs/2026-10-10-ap-gaps-roadmap.md`.

## Global Constraints

- **One commit per task.** TDD: write the failing test, observe it failing, then implement.
- **Coverage:**
  - 100% line and branch coverage of new and changed lines: `python3 tests/check_changed_line_coverage.py coverage.json dafbb3aa0 --branches app/ fastapi_server.py`. `dafbb3aa0` is the spec commit.
  - New modules `app/activitypub/entitlement.py` and `app/activitypub/signature_rfc9421.py` get floor `100` in `coverage_floors.ini`.
  - No floor may drop. Existing floors: `signature.py = 100`, `routes.py = 99`, `util.py = 99`.
- **Phase gate:** at the end of every phase (the last task of S0, S1, S2, S3, S4, S5 and S6), run the full suite with coverage:
  1. `./run_tests.sh tests/ --cov=app --cov=fastapi_server --cov-branch --cov-report=json:coverage.json -q`
  2. the changed-line check above
  3. `python3 tests/check_coverage_floors.py coverage.json coverage_floors.ini`

  Read the passed-test count, not just the exit code: a run that hits the session budget still exits 0 (see `pytest.ini`).
- **Running tests:** Python tests run only in containers, via `./run_tests.sh <paths> -q`. Gate scripts run on the host with `python3`.
- **Imports:** no function-level imports in `app/` (`tests/test_no_inline_imports.py`). A function-level import that is unavoidable because of an import cycle gets a `# cycle:` comment.
- **Test honesty:** never patch `HttpSignature.verify_request`, `LDSignature.verify_signature` or the RFC 9421 `verify`. Make real signatures with `tests/factories.py:signed_inbox_post` or production signing code. Outbound HTTP goes through the `http_mock` fixture. Redis goes through the `redis_double` fixture.
- **No sleeping or retry loops on the request path** (rulings D738/D775). The only synchronous fetches allowed on the request path are the S2.3 key refetch, which is a single GET and rate-limited, and the S5 double-knock, which is a single extra attempt.
- **Hosts are compared with `host_of`** (`app/activitypub/util.py:657`). An empty host never equals anything.
- **`AS_PUBLIC`** (U:4750) is the only definition of the public addressee.
- **Commits:** Conventional Commits, ending in `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- **Migrations:**
  - Task 20 creates revision `c4a8e2f61d3b` with `down_revision = 'b7e1c2d9f4a6'`, the current head.
  - Task 24 creates revision `d9f3b7a15c2e` with `down_revision = 'c4a8e2f61d3b'`.
  - Both downgrades must be reversible.
- **Enforcement flag:** `SIG_REQUIRED_HEADERS_ENFORCE` stays off. Task 25 changes its default and is **blocked until the owner says go**, after reviewing production warnings.

## Plan clarifications (deviations from the spec, recorded)

1. **`signed_requester_actor()` lives in `app/activitypub/entitlement.py`, not R.** The spec put it next to `signed_requestor_domain` in R. Task 24's secure-mode hook also runs on the main blueprint (`app/main/routes.py`), and importing from R there would add an import cycle. R's `signed_requestor_domain` calls it from its new home.
2. **`request_key_id(request)` is added in Task 8 for cavage only.** Task 8 is its first consumer. Task 19 extends it to RFC 9421. Every keyId read goes through it from Task 8 onward. That includes `app/relays/inbound.py:_key_owner` and Task 2's `_signature_key_id()`.
3. **One key-refresh core.** Task 8 adds `verify_refreshing_key`, which both `verify_with_key_refresh` and the relay module call.
   - Relay refetch eligibility becomes the spec's host rule, which is slightly looser than today's exact-owner match.
   - A refetch stores only the public key. It does not run the full profile refresh, which downloads avatars and banners and is too much for the request path.
4. **New module `app/activitypub/signature_errors.py`** (floor 100). It breaks the `signature.py` ↔ `signature_rfc9421.py` import cycle.
5. **The RFC 9421 required sets drop `@authority`,** because `@target-uri` already contains it.
6. **A 401 refused in both formats is not queued.** `post_request` has never queued a 4xx, and the spec's error table says "existing failure handling". The spec's S5 test bullet about a SendQueue retry is corrected to match.
7. **Task 7: the dedupe `set(nx=True)` after verification is also the race gate.** A failed set means a concurrent delivery of the same id has just been verified.
8. **Task 4 also filters Undo Follow on `is_inward=True`.** Otherwise a remote Undo could delete our own outward pending follow of that user. Two existing tests that built the inbound row with `is_inward=False` are corrected.
9. **S3 details:**
   - `former_type_of_post` delegates to `post_to_page`, which emits only Page, Question or Event.
   - A deleted bot gets `formerType: 'Service'`, mirroring the live actor document.
   - `/actor/outbox` reuses `paged_outbox`.
   - `/private_message/<id>` serves only our own messages, whose sender is always local. The entitled requesters are therefore the recipient and signers on the recipient's host.
10. **S4 details:**
    - The check runs before the inner-actor lookup, so a spoofed actor is never fetched.
    - An inner activity without a string `id` passes through to the existing D127 refusal.
    - An announced Delete whose origin answers anything other than 404, 410 or 200 (401/403/5xx) is dropped with its status in the reason.
    - `process_inbox_request` is a Celery task, so S4 fetches run in a worker.
11. **S6 details:**
    - `post_to_page` and `comment_model_to_json` always address Public, so the addressees of a followers-only object are `[author.followers_url()]`. The served `to`/`cc` are rewritten to match.
    - The post and comment AP routes have no `@cache.cached` today. Task 22 adds `Signature` to their `Vary` (they already vary on the signer for D194) and pins "no server-side URL cache" with a ratchet test.
    - Secure mode is one hook in the app-wide `before_request` (`app/request_hooks.py`), not per-blueprint hooks, so AP GETs served by other blueprints are covered.
    - `admin_federation` now invalidates the memoized `get_site_as_dict`, which caches for 60 s.
12. **`host_of` is in `app/activitypub/util.py`,** and `audience_entitles` rule 4 uses the inbound follow shape: `UserFollower(local_user_id=author, remote_user_id=requester, is_inward=True, is_accepted=True)`.

## Interface contract

Names and signatures every task relies on. A task's implementer sees only their own task, so this table is how
neighbouring tasks agree.

| Name | Where | Signature / value | Introduced |
|---|---|---|---|
| `signed_requester_actor` | `app/activitypub/entitlement.py` | `() -> User \| Community \| None`. Uses the current `flask.request`. Never fetches. | Task 2 |
| `addressees_of` | `app/activitypub/entitlement.py` | `(doc: dict) -> list[str]`: union of `to`, `cc`, `audience`, `bto`, `bcc`. Strings and lists are accepted, dict entries use their `id`, non-strings are dropped. | Task 2 |
| `audience_entitles` | `app/activitypub/entitlement.py` | `(addressees: list[str], requester, local_actor=None) -> bool`. Implements the four spec rules. | Task 2 |
| `PRIVATE_HEADERS` | `app/activitypub/entitlement.py` | `{'Cache-Control': 'private, no-store', 'Vary': 'Accept, Signature'}` | Task 2 |
| `shared_inbox_of` | `app/activitypub/util.py` | `(actor_json: dict) -> str \| None` | Task 5 |
| `POST_REQUIRED`, `GET_REQUIRED` | `app/activitypub/signature.py` | `frozenset({'(request-target)', 'host', 'date', 'digest'})` and `frozenset({'(request-target)', 'host', 'date'})` | Task 6 |
| `HttpSignature.verify_request` | `app/activitypub/signature.py` | `(request, public_key, required: frozenset[str] = frozenset()) -> True`. `skip_date` is removed. | Task 6 |
| `SIG_REQUIRED_HEADERS_ENFORCE` | `config.py` | `os.environ.get('SIG_REQUIRED_HEADERS_ENFORCE', '0') == '1'` | Task 6 |
| `ACTIVITY_DEDUPE_SECONDS` | `app/activitypub/routes.py` | `86400` | Task 7 |
| `verify_with_key_refresh` | `app/activitypub/signature.py` | `(request, actor, required: frozenset[str] = frozenset()) -> None`. Raises `VerificationError`. | Task 8 |
| `KEY_REFETCH_SECONDS` | `app/activitypub/signature.py` | `300` | Task 8 |
| `request_key_id` | `app/activitypub/signature.py` | `(request) -> str \| None`. Cavage in Task 8; RFC 9421 added in Task 19. | Task 8 |
| `verify_refreshing_key` | `app/activitypub/signature.py` | `(request, stored_key, required=frozenset(), *, owner_url, rate_key, fetch_key, store_key) -> None`. The core that both `verify_with_key_refresh` and the relay module use. | Task 8 |
| `tombstone_response` | `app/activitypub/routes.py` | unchanged `(ap_id: str, former_type: str)`. Callers pass the real type. | Tasks 11–12 |
| `former_type_of_post` | `app/activitypub/routes.py` | `(post: Post) -> str` = `post_to_page(post)['type']` (Page, Question or Event) | Task 12 |
| `followers_only_addressees`, `followers_entitled` | `app/activitypub/routes.py` | `(obj) -> list[str]` (`[obj.author.followers_url()]`) and `(obj) -> bool` | Task 23 |
| `verify_announced_inner` | `app/activitypub/util.py` | `(announce: dict) -> tuple[dict \| None, str \| None]`: `(possibly-replaced inner activity, None)` to process, `(None, reason)` to drop | Tasks 15–17 |
| `content_digest` | `app/activitypub/signature_rfc9421.py` | `(body: bytes) -> str` | Task 18 |
| `sign` | `app/activitypub/signature_rfc9421.py` | `(method: str, url: str, body: bytes \| None, private_key_pem: str, key_id: str, created: int \| None = None) -> dict[str, str]` | Task 18 |
| `verify` | `app/activitypub/signature_rfc9421.py` | `(method, url, headers, body, public_key_pem, required=frozenset(), *, now: int \| None = None) -> frozenset[str]`: returns the covered components of the label that verified. Raises `VerificationError`. | Task 18 |
| `parse_key_id` | `app/activitypub/signature_rfc9421.py` | `(headers: Mapping[str, str]) -> str \| None` | Task 18 |
| `RFC9421_POST_REQUIRED`, `RFC9421_GET_REQUIRED` | `app/activitypub/signature_rfc9421.py` | `frozenset({'@method', '@target-uri', 'content-digest'})` and `frozenset({'@method', '@target-uri'})`. `normalise_covered` counts `@authority` + `@path` as `@target-uri`. | Task 18 |
| `parse_dictionary`, `signature_base`, `check_content_digest`, `normalise_covered` | `app/activitypub/signature_rfc9421.py` | public helpers; signatures as given in Task 18 | Task 18 |
| `VerificationError`, `VerificationFormatError` | `app/activitypub/signature_errors.py` (new, floor 100) | moved here; `signature.py` re-exports both | Task 18 |
| `Instance.signature_format` | `app/models.py` | `String(16)`, not null, server default `'cavage'`; values `'cavage'` and `'rfc9421'` | Task 20 |
| `HttpSignature.signed_request(..., signature_format='cavage')` | `app/activitypub/signature.py` | new keyword parameter | Task 20 |
| `double_knock` | `app/activitypub/signature.py` | `(uri, send) -> httpx.Response`. Reads and writes `Instance.signature_format` in short sessions of its own. | Task 20 |
| `chat_message_ap_object` | `app/chat/util.py` | `(reply: ChatMessage, recipient: User) -> dict`; `update_message` is refactored onto it | Task 14 |
| `Site.require_signed_fetch` | `app/models.py` | Boolean, not null, server default false | Task 24 |
| `SIGNED_FETCH_EXEMPT_PREFIXES` | `app/activitypub/entitlement.py` | `('/actor', '/.well-known/', '/nodeinfo/')`. `/actor` covers `/actor/outbox` and `/actor/inbox`. Matching is exact-or-prefix on `/actor` and `/actor/`. | Task 24 |
| `signed_fetch_exempt`, `signed_fetch_refusal` | `app/activitypub/entitlement.py` | `(path: str) -> bool` and `() -> Response \| None`; called from the app-wide `before_request` in `app/request_hooks.py` | Task 24 |

## Review Focus

1. **GoToSocial-style keyIds** (`https://host/users/name/main-key`, no `#`). They must still resolve in
   `signed_requester_actor`, still qualify for the S2.3 refetch, and still pass the S2.2 same-host check. Tests in
   Tasks 2, 8 and 9.
2. **A peer refetching one of our own activities to verify it,** signed by its instance actor, not the addressee.
   For example, Lemmy checks a vote we sent to a community it hosts. After S0.2 it must still get 200. Test in
   Task 3.
3. **Hosts that differ only by case or port** (`Peer.Example`, `peer.example:443`). Every host comparison must
   treat them as equal. Tests in Tasks 2, 9 and 15.
4. **Secure mode and a browser.** An HTML request to a post page, and an unsigned request to each exempt route,
   must keep working with secure mode on. A HEAD request to an AP route follows the same rule as GET. Test in
   Task 24.
5. **A peer that answers 401 for reasons unrelated to signature format,** such as an authorized-fetch block of
   our domain. The send makes exactly two attempts, leaves `signature_format` unchanged, and leaves the existing
   failure logging intact. Test in Task 20.

---

## File Structure

| File | Responsibility | Tasks |
|---|---|---|
| `app/activitypub/entitlement.py` (new) | signer identification, audience entitlement, private headers, signed-fetch exemptions | 2, 24 |
| `app/activitypub/signature_rfc9421.py` (new) | RFC 9421 and RFC 9530 sign/verify | 18 |
| `app/activitypub/signature.py` | cavage verify with required headers; key refresh; keyId parsing; format dispatch; outbound double-knock | 6, 8, 9, 19, 20, 21 |
| `app/activitypub/routes.py` | inbox gate (fediseer, dedupe, keyId, refresh), `/activities`, Undo Follow, tombstones, community outbox, private message route, Announce hook, followers-only unlock | 1, 3, 4, 7, 8, 9, 11–17, 22, 23 |
| `app/activitypub/util.py` | `shared_inbox_of`, `verify_announced_inner` | 5, 15–17 |
| `app/relays/inbound.py` | uses `verify_with_key_refresh` and `request_key_id` | 8, 9 |
| `app/main/routes.py` | `/actor/outbox`, secure-mode hook for instance-actor routes | 10, 24 |
| `app/models.py` | `Instance.signature_format`, `Site.require_signed_fetch` | 20, 24 |
| `migrations/versions/c4a8e2f61d3b_instance_signature_format.py` (new) | migration | 20 |
| `migrations/versions/d9f3b7a15c2e_site_require_signed_fetch.py` (new) | migration | 24 |
| `app/admin/forms.py`, `app/admin/routes.py`, admin federation template | secure-mode checkbox | 24 |
| `config.py` | `SIG_REQUIRED_HEADERS_ENFORCE` | 6, 25 |
| `coverage_floors.ini` | floors for the new modules | 2, 18 |
| `tests/test_ap_security_*.py` (new, one per task group) | tests | all |

## Task index

| Phase | Tasks |
|---|---|
| S0 critical | 1 fediseer · 2 entitlement helpers · 3 `/activities` gate |
| S1 bugs | 4 pending-follow Undo · 5 `shared_inbox_of` |
| S2 signatures | 6 required headers (flag off) · 7 dedupe · 8 key refresh · 9 keyId host |
| S3 fetch surface | 10 `/actor/outbox` · 11 user tombstone · 12 `formerType` · 13 community outbox gate · 14 `/private_message` |
| S4 Announce | 15 host consistency · 16 Create/Update verification · 17 Delete verification |
| S5 RFC 9421 | 18 module · 19 inbound dispatch · 20 migration + POST double-knock · 21 GET double-knock |
| S6 authorized fetch | 22 signer-safe caching on post/comment AP routes · 23 followers-only unlock · 24 secure mode |
| Follow-up | 25 enforce required headers (**blocked on owner**) |

## Phase S0: critical

### Task 1: Refuse the unsigned fediseer ChatMessage

**Files:**
- Modify: `app/activitypub/routes.py:811-815` (delete the fediseer `elif`), `app/activitypub/routes.py:2926` (drop the fediseer exemption in `process_chat`)
- Modify: `tests/test_inbox_gate_signatures.py` (replace `test_an_unsigned_fediseer_chat_message_is_exempted`, rewrite the fediseer passages of the module docstring)
- Modify: `tests/test_inbox_dispatch_chat.py:51-66` (`test_a_brand_new_sender_from_fediseer_is_exempt` becomes a refusal)
- Modify: `tests/test_inbox_gate_refusals.py:74` and `:91` (docstring rows that describe the exemption), `tests/README.md:2067` (one sentence)

**Interfaces:**
- Consumes: nothing new.
- Produces: nothing new. After this task every inbox request whose HTTP signature fails and that has no `signature` body key and no relay path gets `('', 400)` with log message `'Could not verify HTTP signature: ' + str(e)`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_inbox_gate_signatures.py`, replace the whole function `test_an_unsigned_fediseer_chat_message_is_exempted` with:

```python
def test_an_unsigned_fediseer_chat_message_is_refused(app, db_session, monkeypatch):
    """S0.1 (security track, owner ruling 2026-10-10). The fediseer `elif` used
    to fall out of the `except` with a literal `...` body, so an unsigned
    Create/ChatMessage that merely CLAIMED the fediseer actor was dispatched --
    anyone could DM any local user "from fediseer". It is now refused like
    every other unsigned activity.

    Three assertions together pin the refusal: 400, nothing dispatched, and the
    generic signature-failure log row (the exemption logged nothing at all).
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    dispatched = []
    monkeypatch.setattr('app.activitypub.routes.process_inbox_request',
                        lambda *args, **kwargs: dispatched.append(args))
    make_site()
    instance = make_instance('fediseer.com')
    fediseer = make_user(instance, 'fediseer')
    fediseer.ap_profile_id = 'https://fediseer.com/api/v1/user/fediseer'
    fediseer.ap_public_url = fediseer.ap_profile_id
    fediseer.ap_inbox_url = f'{fediseer.ap_profile_id}/inbox'
    fediseer.ap_fetched_at = utcnow()
    db.session.commit()
    activity = inbox_activity(fediseer, activity_type='Create',
                              object={'id': f'{fediseer.ap_profile_id}/objects/1', 'type': 'ChatMessage'})
    body_bytes = json.dumps(activity).encode('utf8')

    with app.test_client() as client:
        response = client.post('/inbox', data=body_bytes,
                               headers=unsigned_but_precheck_clean_headers(body_bytes),
                               content_type='application/activity+json')

    assert response.status_code == 400
    assert dispatched == []
    assert ActivityPubLog.query.one().exception_message == \
        'Could not verify HTTP signature: No signature header present'
```

In `tests/test_inbox_dispatch_chat.py`, replace `test_a_brand_new_sender_from_fediseer_is_exempt` with:

```python
def test_a_brand_new_sender_from_fediseer_is_refused_like_any_other(app, db_session, monkeypatch):
    """S0.1. The `user.ap_domain != 'fediseer.com'` conjunct is gone: with the
    unsigned path closed, a fediseer.com sender is an ordinary signed sender
    and a brand-new one is refused as 'Sender is too new' like any other.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, sender, recipient = seed_chat_pair(host='fediseer.com')
    sender.created = utcnow()
    sender.ap_domain = 'fediseer.com'
    db.session.commit()
    record_moderation(monkeypatch, 'publish_sse_event')

    dispatch(chat_activity(sender, to=recipient.ap_profile_id,
                           content='hello', id='https://peer.example/pm/1'))

    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Sender is too new'
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./run_tests.sh tests/test_inbox_gate_signatures.py::test_an_unsigned_fediseer_chat_message_is_refused tests/test_inbox_dispatch_chat.py::test_a_brand_new_sender_from_fediseer_is_refused_like_any_other -q`
Expected: FAIL. The first with `assert 200 == 400`; the second with `assert 'success' == 'failure'`.

- [ ] **Step 3: Implement**

In `app/activitypub/routes.py`, delete these lines from the `except VerificationError` block (currently R:811-815):

```python
        elif (
                actor.ap_profile_id == 'https://fediseer.com/api/v1/user/fediseer' and  # accept unsigned chat message from fediseer for API key
                request_json['type'] == 'Create' and isinstance(request_json['object'], dict) and
                'type' in request_json['object'] and request_json['object']['type'] == 'ChatMessage'):
            ...
```

Leave the commented-out PeerTube block and the final `else:` exactly as they are.

In `process_chat` (R:2926), change:

```python
        if sender.created_very_recently() and user.ap_domain != 'fediseer.com':
```

to:

```python
        if sender.created_very_recently():
```

Docstring/doc updates (no behaviour):
- `tests/test_inbox_gate_signatures.py` module docstring: in the "TASK 6" header change "the LD fallback, and the fediseer exemption" to "and the LD fallback"; replace the `731  The fediseer exemption: ...` paragraph and the sections headed "The fediseer exemption (Step 4) needs an actor row..." and "The fediseer branch's `...` body -- what it actually does" with this single paragraph:

```
The fediseer exemption that used to sit between the LD fallback and the final
`else` was removed by the security track (S0.1, owner ruling 2026-10-10): its
literal `...` body let an unsigned Create/ChatMessage claiming the fediseer
actor through. test_an_unsigned_fediseer_chat_message_is_refused pins that it
now gets the generic signature-failure 400.
```

- `tests/test_inbox_gate_refusals.py:74` and `:91`: replace the words describing row 19 as the "fediseer `ChatMessage` exemption" with "(removed by S0.1: the fediseer exemption no longer exists)".
- `tests/README.md:2067`: delete "(for example `process_chat`'s fediseer.com exemption," and its closing parenthesis, keeping the rest of the sentence grammatical.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./run_tests.sh tests/test_inbox_gate_signatures.py tests/test_inbox_dispatch_chat.py tests/test_inbox_gate_refusals.py -q`
Expected: PASS (all tests in the three modules).

- [ ] **Step 5: Commit**

```bash
git add app/activitypub/routes.py tests/test_inbox_gate_signatures.py tests/test_inbox_dispatch_chat.py tests/test_inbox_gate_refusals.py tests/README.md
git commit -m "fix: an unsigned ChatMessage claiming the fediseer actor is refused

The fediseer elif in the inbox signature check had a literal '...' body
and fell through, so anyone could DM any local user as fediseer without
a signature. It is removed; the request gets the generic 400.

process_chat's fediseer.com exemption from the new-account check is
removed with it: without the unsigned path it only exempted one domain
from spam control, and nothing shows fediseer still needs it.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Entitlement helpers (`app/activitypub/entitlement.py`)

**Files:**
- Create: `app/activitypub/entitlement.py`
- Modify: `app/activitypub/routes.py:2482-2496` (`signed_requestor_domain` uses `signed_requester_actor`), routes.py imports
- Modify: `tests/factories.py` (add `signed_get_headers`)
- Modify: `coverage_floors.ini` (add `app/activitypub/entitlement.py = 100` after the `app/activitypub/signature.py = 100` line)
- Test: `tests/test_ap_security_entitlement.py`

**Interfaces:**
- Consumes: `HttpSignature.parse_signature`, `HttpSignature.verify_request(request, public_key)` (Task 6 adds a `required` keyword; this task passes none), `find_actor_by_url` (`app/activitypub/actor.py:331`), `host_of`, `AS_PUBLIC` (`app/activitypub/util.py:657`, `:4750`).
- Produces (exact):
  - `signed_requester_actor() -> User | Community | None`
  - `addressees_of(doc: dict) -> list[str]`
  - `audience_entitles(addressees: list[str], requester, local_actor=None) -> bool`
  - `PRIVATE_HEADERS: dict[str, str] = {'Cache-Control': 'private, no-store', 'Vary': 'Accept, Signature'}`
  - test helper `tests/factories.py: signed_get_headers(path: str, signer, *, key_id: str = None) -> dict`
  - Task 8 replaces the keyId parse inside `_signature_key_id()` with `request_key_id(request)`; nothing else in this module reads the Signature header.

- [ ] **Step 1: Add the test helper to `tests/factories.py`**

Add `from unittest.mock import patch` to the module's top-level imports, then add after `signed_inbox_post`:

```python
def signed_get_headers(path: str, signer, *, key_id: str = None) -> dict:
    """Headers for a GET of `path` on this instance, HTTP-signed with `signer`'s
    REAL private key by production's own signer -- as a peer doing an
    authorized fetch would send them.

    `signed_request(..., send_via_async=True)` returns the headers instead of
    sending. Its outbound-URI guard refuses '.local' hosts (SERVER_NAME is
    test.piefed.local), so the guard is doubled for the duration of the call
    only. `key_id` overrides the keyId, which is how tests produce
    GoToSocial-style '/main-key' ids or a mixed-case/port host.
    """
    from app.activitypub.signature import HttpSignature
    host = current_app.config['SERVER_NAME']
    with patch('app.activitypub.signature.is_invalid_get_request_uri', return_value=False):
        _uri, headers, _body = HttpSignature.signed_request(
            f'https://{host}{path}', None, signer.private_key,
            key_id or f'{signer.ap_profile_id}#main-key', method='get', send_via_async=True)
    return headers
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_ap_security_entitlement.py`:

```python
"""Security track S0: who signed a GET, and may that signer see an audience.

Signatures are made by production's signer (tests/factories.py:
signed_get_headers) and checked by production's verifier; neither is patched.
"""
import pytest

from app import db
from app.activitypub.entitlement import (PRIVATE_HEADERS, addressees_of, audience_entitles,
                                         signed_requester_actor)
from app.models import Instance
from tests.factories import (make_community, make_community_member, make_follow, make_instance,
                             make_site, make_user, seed_community_owner, signed_get_headers)

pytestmark = pytest.mark.usefixtures('redis_double')

PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'


def _remote(domain, name, with_keys=True):
    instance = db.session.query(Instance).filter_by(domain=domain).first() or make_instance(domain)
    return make_user(instance, name, with_keys=with_keys)


def _local(app, name):
    user = make_user(None, name, local=True)
    user.ap_profile_id = f"{app.config['SERVER_URL']}/u/{name}".lower()
    db.session.commit()
    return user


def _requester_for(app, path, headers):
    with app.test_request_context(path, method='GET', headers=headers):
        return signed_requester_actor()


# ---- signed_requester_actor -------------------------------------------------

def test_a_validly_signed_get_names_its_signer(app, db_session):
    make_site()
    signer = _remote('peer.example', 'alice')
    assert _requester_for(app, '/post/1', signed_get_headers('/post/1', signer)) == signer


def test_an_unsigned_get_names_nobody(app, db_session):
    make_site()
    assert _requester_for(app, '/post/1', {}) is None


def test_a_signature_header_without_a_keyid_names_nobody(app, db_session):
    make_site()
    assert _requester_for(app, '/post/1', {'Signature': 'headers="date",signature="AAAA"'}) is None


def test_an_unknown_signer_names_nobody_and_fetches_nothing(app, db_session, http_mock):
    """Never fetches: http_mock has no routes registered, so any GET would raise."""
    make_site()
    signer = _remote('peer.example', 'alice')
    headers = signed_get_headers('/post/1', signer, key_id='https://stranger.example/users/bob#main-key')
    assert _requester_for(app, '/post/1', headers) is None


def test_a_signature_that_does_not_verify_names_nobody(app, db_session):
    make_site()
    signer = _remote('peer.example', 'alice')
    other = _remote('peer.example', 'bob')
    headers = signed_get_headers('/post/1', other, key_id=f'{signer.ap_profile_id}#main-key')
    assert _requester_for(app, '/post/1', headers) is None


def test_a_signed_path_other_than_the_requested_one_names_nobody(app, db_session):
    make_site()
    signer = _remote('peer.example', 'alice')
    assert _requester_for(app, '/post/2', signed_get_headers('/post/1', signer)) is None


def test_a_gotosocial_style_main_key_path_names_its_owner(app, db_session):
    """Review Focus 1: GoToSocial keyIds are '<actor>/main-key', no fragment."""
    make_site()
    signer = _remote('peer.example', 'alice')
    headers = signed_get_headers('/post/1', signer, key_id=f'{signer.ap_profile_id}/main-key')
    assert _requester_for(app, '/post/1', headers) == signer


@pytest.mark.parametrize('key_id', [
    'https://Peer.Example/users/alice#main-key',
    'https://peer.example:443/users/alice#main-key',
    'HTTPS://PEER.EXAMPLE/users/alice#main-key',
])
def test_a_keyid_host_differing_only_by_case_or_default_port_names_its_owner(app, db_session, key_id):
    """Review Focus 3."""
    make_site()
    signer = _remote('peer.example', 'alice')
    headers = signed_get_headers('/post/1', signer, key_id=key_id)
    assert _requester_for(app, '/post/1', headers) == signer


def test_a_keyid_with_no_host_names_nobody(app, db_session):
    make_site()
    signer = _remote('peer.example', 'alice')
    headers = signed_get_headers('/post/1', signer, key_id='main-key')
    assert _requester_for(app, '/post/1', headers) is None


def test_a_banned_signer_names_nobody(app, db_session):
    make_site()
    signer = _remote('peer.example', 'alice')
    signer.banned = True
    db.session.commit()
    assert _requester_for(app, '/post/1', signed_get_headers('/post/1', signer)) is None


# ---- addressees_of ----------------------------------------------------------

def test_addressees_are_the_union_of_every_addressing_field():
    doc = {'to': 'https://a.example/u/x', 'cc': ['https://b.example/u/y', {'id': 'https://c.example/u/z'}],
           'audience': 'https://d.example/c/g', 'bto': ['https://e.example/u/w'], 'bcc': 'https://f.example/u/v'}
    assert addressees_of(doc) == ['https://a.example/u/x', 'https://b.example/u/y', 'https://c.example/u/z',
                                  'https://d.example/c/g', 'https://e.example/u/w', 'https://f.example/u/v']


def test_non_string_and_empty_addressees_are_dropped():
    assert addressees_of({'to': [None, 3, '', {'id': 7}, {}], 'cc': None}) == []


def test_a_document_that_is_not_a_dict_has_no_addressees():
    assert addressees_of(['https://a.example/u/x']) == []


# ---- audience_entitles ------------------------------------------------------

@pytest.mark.parametrize('public', [PUBLIC, 'as:Public', 'Public'])
def test_rule_1_a_public_audience_entitles_anyone(public):
    assert audience_entitles([public], None) is True


def test_a_non_public_audience_entitles_no_anonymous_requester():
    assert audience_entitles(['https://peer.example/users/alice'], None) is False


def test_rule_2_an_addressed_requester_is_entitled(app, db_session):
    make_site()
    signer = _remote('peer.example', 'alice')
    assert audience_entitles([signer.ap_profile_id.upper()], signer) is True


def test_rule_3_a_requester_on_an_addressees_host_is_entitled(app, db_session):
    make_site()
    signer = _remote('peer.example', 'instanceactor')
    assert audience_entitles(['https://PEER.example:443/users/bob'], signer) is True


def test_rule_3_does_not_count_a_followers_collection_as_an_addressed_actor(app, db_session):
    make_site()
    signer = _remote('peer.example', 'alice')
    assert audience_entitles(['https://peer.example/users/bob/followers'], signer) is False


def test_a_requester_on_an_unrelated_host_is_not_entitled(app, db_session):
    make_site()
    signer = _remote('other.example', 'mallory')
    assert audience_entitles(['https://peer.example/users/bob'], signer) is False


def test_an_addressee_with_no_host_never_matches(app, db_session):
    make_site()
    signer = _remote('peer.example', 'alice')
    assert audience_entitles(['not a url', ''], signer) is False


def test_a_requester_with_no_host_is_not_entitled(app, db_session):
    make_site()
    signer = _remote('peer.example', 'alice')
    signer.ap_profile_id = 'garbage'
    signer.ap_public_url = 'garbage'
    db.session.commit()
    assert audience_entitles(['https://peer.example/users/bob'], signer) is False


def test_rule_4_an_accepted_follower_of_a_local_user_is_entitled(app, db_session):
    make_site()
    author = _local(app, 'author')
    follower = _remote('peer.example', 'alice')
    make_follow(author, follower, is_accepted=True, is_inward=True)
    followers = f'{author.public_url()}/followers'
    assert audience_entitles([followers], follower, local_actor=author) is True


def test_rule_4_another_account_on_a_followers_host_is_entitled(app, db_session):
    make_site()
    author = _local(app, 'author')
    follower = _remote('peer.example', 'alice')
    make_follow(author, follower, is_accepted=True, is_inward=True)
    instance_actor = _remote('peer.example', 'instanceactor')
    assert audience_entitles([f'{author.public_url()}/followers'], instance_actor, local_actor=author) is True


@pytest.mark.parametrize('is_accepted', [None, False])
def test_rule_4_a_pending_or_rejected_follower_is_not_entitled(app, db_session, is_accepted):
    make_site()
    author = _local(app, 'author')
    follower = _remote('peer.example', 'alice')
    make_follow(author, follower, is_accepted=is_accepted, is_inward=True)
    assert audience_entitles([f'{author.public_url()}/followers'], follower, local_actor=author) is False


def test_rule_4_an_outward_follow_does_not_count(app, db_session):
    """is_inward=False is the local author following the remote user, not the reverse."""
    make_site()
    author = _local(app, 'author')
    followed = _remote('peer.example', 'alice')
    make_follow(author, followed, is_accepted=True, is_inward=False)
    assert audience_entitles([f'{author.public_url()}/followers'], followed, local_actor=author) is False


def test_rule_4_needs_the_followers_collection_to_be_addressed(app, db_session):
    make_site()
    author = _local(app, 'author')
    follower = _remote('peer.example', 'alice')
    make_follow(author, follower, is_accepted=True, is_inward=True)
    assert audience_entitles(['https://elsewhere.example/users/z'], follower, local_actor=author) is False


def test_rule_4_a_member_host_of_a_local_community_is_entitled(app, db_session):
    make_site()
    seed_community_owner('peer.example')
    community = make_community('localgroup', host=app.config['SERVER_NAME'])
    member = _remote('peer.example', 'alice')
    make_community_member(member, community)
    instance_actor = _remote('peer.example', 'instanceactor')
    assert audience_entitles([f'{community.public_url()}/followers'], instance_actor,
                             local_actor=community) is True


def test_rule_4_a_banned_member_does_not_entitle_their_host(app, db_session):
    make_site()
    seed_community_owner('peer.example')
    community = make_community('localgroup', host=app.config['SERVER_NAME'])
    member = _remote('peer.example', 'alice')
    membership = make_community_member(member, community)
    membership.is_banned = True
    db.session.commit()
    assert audience_entitles([f'{community.public_url()}/followers'], member,
                             local_actor=community) is False


def test_rule_4_any_other_local_actor_type_entitles_nobody(app, db_session):
    make_site()
    signer = _remote('peer.example', 'alice')

    class NotAnActor:
        ap_followers_url = 'https://test.piefed.local/f/feed/followers'

        def public_url(self):
            return 'https://test.piefed.local/f/feed'

    assert audience_entitles(['https://test.piefed.local/f/feed/followers'], signer,
                             local_actor=NotAnActor()) is False


def test_private_headers_are_the_contract_values():
    assert PRIVATE_HEADERS == {'Cache-Control': 'private, no-store', 'Vary': 'Accept, Signature'}


# ---- signed_requestor_domain keeps its behaviour on the new helper ----------

def test_signed_requestor_domain_reports_the_signers_host(app, db_session):
    from app.activitypub.routes import signed_requestor_domain
    make_site()
    signer = _remote('peer.example', 'alice')
    with app.test_request_context('/post/1', method='GET', headers=signed_get_headers('/post/1', signer)):
        assert signed_requestor_domain() == 'peer.example'


def test_signed_requestor_domain_falls_back_to_the_user_agent(app, db_session):
    from app.activitypub.routes import signed_requestor_domain
    make_site()
    with app.test_request_context('/post/1', method='GET',
                                  headers={'User-Agent': 'Test (+https://agent.example)'}):
        assert signed_requestor_domain() == 'agent.example'
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `./run_tests.sh tests/test_ap_security_entitlement.py -q`
Expected: FAIL at collection with `ModuleNotFoundError: No module named 'app.activitypub.entitlement'`.

- [ ] **Step 4: Implement `app/activitypub/entitlement.py`**

```python
"""Who signed this ActivityPub GET, and may that signer see a given audience.

Security track (docs/superpowers/specs/2026-10-10-ap-security-track-design.md, "Shared helpers"). One answer
used by /activities, /private_message, the post and comment routes and secure mode, so a fix to who is entitled
lands everywhere at once.

Identifying a signer never fetches: only actors already stored here are considered, so an unauthenticated
request cannot make this instance issue a GET.
"""
from urllib.parse import urlparse

from flask import request
from sqlalchemy import or_

from app import db
from app.activitypub.actor import find_actor_by_url
from app.activitypub.signature import HttpSignature, VerificationError
from app.activitypub.util import AS_PUBLIC, host_of
from app.models import Community, CommunityMember, User, UserFollower

ADDRESS_FIELDS = ('to', 'cc', 'audience', 'bto', 'bcc')

# A response whose content depends on who signed the request must never be stored by a shared cache.
PRIVATE_HEADERS = {'Cache-Control': 'private, no-store', 'Vary': 'Accept, Signature'}

# How many trailing path segments a keyId may carry past its actor id ('<actor>/main-key' is GoToSocial's).
_KEY_PATH_TRIMS = 2


def _signature_key_id() -> str | None:
    """The keyId of the current request's Signature header, or None."""
    if 'signature' not in request.headers:
        return None
    try:
        return HttpSignature.parse_signature(request.headers['signature'])['keyid']
    except VerificationError:
        return None


def _owner_candidates(key_id: str) -> list[str]:
    """Actor ids a keyId may belong to, most specific first, all on the keyId's own host.

    The fragment is dropped (Mastodon '#main-key'), the host is lowercased and a default port removed
    (Review Focus 3), then up to _KEY_PATH_TRIMS trailing path segments are trimmed (GoToSocial '/main-key').
    A wrong candidate is harmless: the signature still has to verify against that actor's own key."""
    owner = key_id.split('#', 1)[0].strip()
    host = host_of(owner)
    if not host:
        return []
    parsed = urlparse(owner)
    base = f'{parsed.scheme.lower()}://{host}'
    if parsed.port and parsed.port not in (80, 443):
        base += f':{parsed.port}'
    path = parsed.path.rstrip('/')
    candidates = [base + path]
    for _ in range(_KEY_PATH_TRIMS):
        path = path.rsplit('/', 1)[0]
        if not path:
            break
        candidates.append(base + path)
    return candidates


def signed_requester_actor():
    """The stored User or Community whose key validly signed the current GET, else None."""
    key_id = _signature_key_id()
    if not key_id:
        return None
    for candidate in _owner_candidates(key_id):
        actor = find_actor_by_url(candidate)
        if not actor or not getattr(actor, 'public_key', None):
            continue
        try:
            HttpSignature.verify_request(request, actor.public_key)
        except (VerificationError, ValueError):
            return None
        return actor
    return None


def addressees_of(doc) -> list[str]:
    """Every addressee of an object or activity: to, cc, audience, bto and bcc, in that order."""
    if not isinstance(doc, dict):
        return []
    result = []
    for field in ADDRESS_FIELDS:
        value = doc.get(field)
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, dict):
                item = item.get('id')
            if isinstance(item, str) and item:
                result.append(item)
    return result


def _actor_id(actor) -> str:
    return (actor.ap_profile_id or actor.public_url()).lower()


def _followers_url(local_actor) -> str:
    return (local_actor.ap_followers_url or f'{local_actor.public_url()}/followers').lower()


def _host_prefixes(host: str):
    return f'https://{host}/', f'http://{host}/'


def _hosts_a_follower(local_actor, requester, host: str) -> bool:
    """True when `requester` follows `local_actor`, or another account on `host` does (accepted, not banned)."""
    https_prefix, http_prefix = _host_prefixes(host)
    on_host = or_(User.ap_profile_id.startswith(https_prefix, autoescape=True),
                  User.ap_profile_id.startswith(http_prefix, autoescape=True),
                  User.id == (requester.id if isinstance(requester, User) else None))
    if isinstance(local_actor, User):
        query = db.session.query(UserFollower).join(User, User.id == UserFollower.remote_user_id).filter(
            UserFollower.local_user_id == local_actor.id,
            UserFollower.is_inward.is_(True),
            UserFollower.is_accepted.is_(True),
            on_host)
    elif isinstance(local_actor, Community):
        query = db.session.query(CommunityMember).join(User, User.id == CommunityMember.user_id).filter(
            CommunityMember.community_id == local_actor.id,
            CommunityMember.is_banned.is_(False),
            on_host)
    else:
        return False
    return db.session.query(query.exists()).scalar()


def audience_entitles(addressees: list[str], requester, local_actor=None) -> bool:
    """May `requester` (signed_requester_actor()'s answer, possibly None) see something addressed to `addressees`?

    1. a public addressee; 2. the requester itself is addressed; 3. the requester's host hosts an addressed actor
    (followers collections excluded); 4. local_actor's followers collection is addressed and the requester, or
    another account on its host, is an accepted follower (member, for a community). Rules 3 and 4 hold because
    bulk delivery already went to that host's shared inbox."""
    if any(addressee in AS_PUBLIC for addressee in addressees):
        return True
    if requester is None:
        return False
    lowered = [addressee.lower() for addressee in addressees]
    requester_id = _actor_id(requester)
    if requester_id in lowered:
        return True
    requester_host = host_of(requester_id)
    if not requester_host:
        return False
    if any(not addressee.endswith('/followers') and host_of(addressee) == requester_host for addressee in lowered):
        return True
    if local_actor is not None and _followers_url(local_actor) in lowered:
        return _hosts_a_follower(local_actor, requester, requester_host)
    return False
```

In `app/activitypub/routes.py`:
- add `from app.activitypub.entitlement import signed_requester_actor` beside the other `app.activitypub` imports, and add `host_of` to the existing `from app.activitypub.util import (...)` list;
- replace `signed_requestor_domain` (R:2482-2496) with:

```python
def signed_requestor_domain():
    """The host of the actor that validly signed this GET, else the domain its User-Agent volunteers.

    Only actors already stored here are considered (entitlement.signed_requester_actor), so identifying a requester
    never fetches anything; a signature from an unknown actor, or one that does not verify, is ignored."""
    actor = signed_requester_actor()
    if actor is not None:
        return host_of(actor.ap_profile_id or actor.public_url())
    return requestor_domain()
```

If `furl` or `find_actor_by_url` become unused in routes.py after this, leave them: both are used elsewhere in the module (check with `grep -n "furl(\|find_actor_by_url(" app/activitypub/routes.py`; remove an import only if that grep shows no other use).

In `coverage_floors.ini`, add after `app/activitypub/signature.py = 100`:

```
app/activitypub/entitlement.py = 100
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `./run_tests.sh tests/test_ap_security_entitlement.py tests/test_ap_content_objects.py tests/test_no_inline_imports.py -q`
Expected: PASS. `test_ap_content_objects.py` holds the existing D194 tests for `signed_requestor_domain`; they must stay green unchanged.

- [ ] **Step 6: Commit**

```bash
git add app/activitypub/entitlement.py app/activitypub/routes.py tests/factories.py tests/test_ap_security_entitlement.py coverage_floors.ini
git commit -m "feat: one helper names the signer of a GET and decides who may see an audience

entitlement.signed_requester_actor resolves a keyId to a stored actor
without fetching (Mastodon '#main-key', GoToSocial '/main-key', host case
and default port normalised) and verifies the signature against it.
audience_entitles applies the security track's four rules: public,
addressed, same host as an addressed actor, or a follower's host when
the local actor's followers collection is addressed.

signed_requestor_domain now uses it, so the instance-block guards and
the new gates identify requesters the same way.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Gate `/activities/<type>/<id>`

**Files:**
- Modify: `app/activitypub/routes.py:2612-2631` (`activities_json`)
- Modify: `tests/test_ap_content_objects.py:1102-1144` (two existing tests whose rows were unaddressed)
- Test: `tests/test_ap_security_activities.py`

**Interfaces:**
- Consumes: `addressees_of`, `audience_entitles`, `signed_requester_actor`, `PRIVATE_HEADERS` (Task 2); `find_actor_by_url`; `AP_CACHE_CONTENT`, `AP_CACHE_MISS` (R:72, R:74); test helpers `signed_get_headers`, `make_activitypub_log`.
- Produces: the route contract later tasks (and peers) rely on — public: 200 + `AP_CACHE_CONTENT`; entitled non-public: 200 + `PRIVATE_HEADERS`; refused or missing: 404 + `AP_CACHE_MISS`, byte-identical.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ap_security_activities.py`:

```python
"""Security track S0.2: /activities/<type>/<id> serves a logged outbound activity
only to someone its own addressing entitles. It used to serve every logged
activity -- DMs and followers-only ones included -- to anyone with the id.
"""
import json

import pytest

from app import db
from app.models import Instance
from tests.factories import (make_activitypub_log, make_follow, make_instance, make_site, make_user,
                             signed_get_headers)

pytestmark = pytest.mark.usefixtures('redis_double')

PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'
SERVER = 'https://test.piefed.local'


def _instance(domain):
    return db.session.query(Instance).filter_by(domain=domain).first() or make_instance(domain)


def _remote(domain, name):
    return make_user(_instance(domain), name, with_keys=True)


def _local(name):
    user = make_user(None, name, local=True)
    user.ap_profile_id = f'{SERVER}/u/{name}'
    db.session.commit()
    return user


def _log(kind, ident, **activity):
    activity_id = f'{SERVER}/activities/{kind}/{ident}'
    make_activitypub_log(activity_id, direction='out', activity_type=kind.title(),
                         activity_json=json.dumps({'id': activity_id, 'type': kind.title(), **activity}))
    return f'/activities/{kind}/{ident}'


def _get(app, path, headers=None):
    with app.test_client() as client:
        return client.get(path, headers=headers or {})


def _assert_refused(response):
    assert response.status_code == 404
    assert response.headers['Cache-Control'] == 'no-store'
    assert response.data == b''


def test_a_public_activity_is_served_unsigned(app, db_session):
    make_site()
    author = _local('author')
    path = _log('create', 'pub', actor=author.ap_profile_id, to=[PUBLIC])
    response = _get(app, path)
    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'public, max-age=120'
    assert response.json['to'] == [PUBLIC]


def test_a_dm_is_refused_to_an_anonymous_requester(app, db_session):
    make_site()
    author = _local('author')
    recipient = _remote('peer.example', 'bob')
    path = _log('create', 'dm1', actor=author.ap_profile_id, to=[recipient.ap_profile_id])
    _assert_refused(_get(app, path))


def test_a_dm_is_served_privately_to_its_signed_addressee(app, db_session):
    make_site()
    author = _local('author')
    recipient = _remote('peer.example', 'bob')
    path = _log('create', 'dm2', actor=author.ap_profile_id, to=[recipient.ap_profile_id])
    response = _get(app, path, signed_get_headers(path, recipient))
    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'private, no-store'
    assert response.headers['Vary'] == 'Accept, Signature'
    assert response.json['to'] == [recipient.ap_profile_id]


def test_a_dm_is_refused_to_a_signed_stranger_on_another_host(app, db_session):
    make_site()
    author = _local('author')
    recipient = _remote('peer.example', 'bob')
    stranger = _remote('other.example', 'mallory')
    path = _log('create', 'dm3', actor=author.ap_profile_id, to=[recipient.ap_profile_id])
    _assert_refused(_get(app, path, signed_get_headers(path, stranger)))


def test_a_peer_instance_actor_verifying_our_activity_is_served(app, db_session):
    """Review Focus 2: a peer refetches one of our activities to verify it and
    signs with ITS instance actor, not with the addressee. It already received
    the activity through its shared inbox, so serving it exposes nothing new."""
    make_site()
    author = _local('author')
    recipient = _remote('peer.example', 'bob')
    peer_instance_actor = _remote('peer.example', 'instanceactor')
    path = _log('create', 'dm4', actor=author.ap_profile_id, to=[recipient.ap_profile_id])
    response = _get(app, path, signed_get_headers(path, peer_instance_actor))
    assert response.status_code == 200


def test_a_followers_addressed_activity_is_served_to_a_signer_on_a_followers_host(app, db_session):
    make_site()
    author = _local('author')
    follower = _remote('peer.example', 'bob')
    make_follow(author, follower, is_accepted=True, is_inward=True)
    peer_instance_actor = _remote('peer.example', 'instanceactor')
    path = _log('create', 'fo1', actor=author.ap_profile_id, to=[f'{author.public_url()}/followers'])
    assert _get(app, path, signed_get_headers(path, peer_instance_actor)).status_code == 200


def test_a_followers_addressed_activity_is_refused_to_a_non_follower_host(app, db_session):
    make_site()
    author = _local('author')
    stranger = _remote('other.example', 'mallory')
    path = _log('create', 'fo2', actor=author.ap_profile_id, to=[f'{author.public_url()}/followers'])
    _assert_refused(_get(app, path, signed_get_headers(path, stranger)))


def test_an_anonymous_request_right_after_an_entitled_one_is_still_refused(app, db_session):
    """The view used to be @cache.cached on the URL alone, which would hand an
    entitled requester's 200 to the next anonymous caller."""
    make_site()
    author = _local('author')
    recipient = _remote('peer.example', 'bob')
    path = _log('create', 'dm5', actor=author.ap_profile_id, to=[recipient.ap_profile_id])
    assert _get(app, path, signed_get_headers(path, recipient)).status_code == 200
    _assert_refused(_get(app, path))


def test_an_activity_with_no_addressing_is_refused(app, db_session):
    make_site()
    author = _local('author')
    path = _log('create', 'none', actor=author.ap_profile_id)
    _assert_refused(_get(app, path))


def test_an_activity_whose_actor_is_not_local_still_uses_its_addressing(app, db_session):
    """local_actor is None, so rule 4 is off; rules 1-3 still decide."""
    make_site()
    recipient = _remote('peer.example', 'bob')
    path = _log('create', 'noactor', actor='https://gone.example/u/x', to=[recipient.ap_profile_id])
    assert _get(app, path, signed_get_headers(path, recipient)).status_code == 200


def test_an_activity_with_a_non_string_actor_is_judged_on_its_addressing(app, db_session):
    make_site()
    recipient = _remote('peer.example', 'bob')
    path = _log('create', 'dictactor', actor={'id': 'x'}, to=[recipient.ap_profile_id])
    _assert_refused(_get(app, path))


def test_a_row_with_an_empty_document_is_refused(app, db_session):
    make_site()
    make_activitypub_log(f'{SERVER}/activities/create/empty', direction='out', activity_json='{}')
    _assert_refused(_get(app, '/activities/create/empty'))


def test_a_row_with_a_null_document_is_refused(app, db_session):
    make_site()
    make_activitypub_log(f'{SERVER}/activities/create/null', direction='out', activity_json=None)
    _assert_refused(_get(app, '/activities/create/null'))


def test_a_row_whose_document_is_not_an_object_is_refused(app, db_session):
    make_site()
    make_activitypub_log(f'{SERVER}/activities/create/list', direction='out', activity_json='["x"]')
    _assert_refused(_get(app, '/activities/create/list'))


def test_a_missing_row_is_refused_identically(app, db_session):
    make_site()
    _assert_refused(_get(app, '/activities/create/missing'))


def test_a_vote_with_only_an_audience_is_served_to_the_audience_host_only(app, db_session):
    """Pins today's spec rule for our outbound votes, which carry only
    `audience: <community>` (app/shared/tasks/likes.py). The community's host
    is entitled by rule 3; a third host that saw the vote through the
    community's Announce is not. If the owner rules that a public community
    audience counts as public, flip the second assertion."""
    make_site()
    voter = _local('voter')
    community_host_actor = _remote('lemmy.example', 'instanceactor')
    third_host_actor = _remote('third.example', 'instanceactor')
    path = _log('like', 'v1', actor=voter.ap_profile_id, object='https://lemmy.example/post/1',
                audience='https://lemmy.example/c/news')
    assert _get(app, path, signed_get_headers(path, community_host_actor)).status_code == 200
    _assert_refused(_get(app, path, signed_get_headers(path, third_host_actor)))
```

In `tests/test_ap_content_objects.py`, update the two tests whose rows carry no addressing (they described the old serve-everything behaviour):
- `test_a_logged_activity_is_served_as_its_stored_json`: change the `activity_json=` string to `'{"type": "Announce", "id": "https://test.piefed.local/activities/announce/abc123", "to": ["https://www.w3.org/ns/activitystreams#Public"]}'`, and delete the docstring paragraph about `@cache.cached(timeout=2400)` and the comment line "The view's own @cache.cached(timeout=2400) is server-side and unchanged." (the decorator is removed).
- `test_a_logged_activity_with_no_json_serves_an_empty_document`: rename to `test_a_logged_activity_with_no_json_is_refused`, replace its docstring with `"""S0.2: a row with no document has no addressing, so nobody is entitled to it."""`, and replace its two assertions with `assert response.status_code == 404` and `assert response.headers['Cache-Control'] == 'no-store'`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./run_tests.sh tests/test_ap_security_activities.py tests/test_ap_content_objects.py -q`
Expected: FAIL. Among others, `test_a_dm_is_refused_to_an_anonymous_requester` with `assert 200 == 404`, and the renamed `test_a_logged_activity_with_no_json_is_refused` with `assert 200 == 404`.

- [ ] **Step 3: Implement**

In `app/activitypub/routes.py`, extend the Task 2 import to `from app.activitypub.entitlement import PRIVATE_HEADERS, addressees_of, audience_entitles, signed_requester_actor`, then replace `activities_json` (decorator line included) with:

```python
@bp.route('/activities/<type>/<id>')
def activities_json(type, id):
    """A logged outbound activity, for whoever its own addressing entitles (security track S0.2).

    No @cache.cached: the answer depends on who signed the request. A refusal is the same 404 as a miss."""
    activity = ActivityPubLog.query.filter_by(
        activity_id=f"{current_app.config['SERVER_URL']}/activities/{type}/{id}").first()
    activity_json = json.loads(activity.activity_json) if activity and activity.activity_json else None
    if isinstance(activity_json, dict) and activity_json:
        addressees = addressees_of(activity_json)
        if audience_entitles(addressees, None):
            resp = jsonify(activity_json)
            resp.content_type = 'application/activity+json'
            resp.headers['Cache-Control'] = AP_CACHE_CONTENT
            return resp
        actor_url = activity_json.get('actor')
        local_actor = (find_actor_by_url(actor_url) or None) if isinstance(actor_url, str) and actor_url else None
        if audience_entitles(addressees, signed_requester_actor(), local_actor):
            resp = jsonify(activity_json)
            resp.content_type = 'application/activity+json'
            resp.headers.update(PRIVATE_HEADERS)
            return resp
    # a refusal and a miss are indistinguishable, and neither may be cached
    resp = make_response('', 404)
    resp.headers['Cache-Control'] = AP_CACHE_MISS
    return resp
```

`find_actor_by_url` for a remote URL that is not stored returns None; for a banned actor it returns False — `or None` folds both to None.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./run_tests.sh tests/test_ap_security_activities.py tests/test_ap_content_objects.py tests/test_ap_security_entitlement.py tests/test_no_inline_imports.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/activitypub/routes.py tests/test_ap_security_activities.py tests/test_ap_content_objects.py
git commit -m "fix: /activities serves a logged activity only to someone its addressing entitles

Every outbound activity, DMs and followers-only ones included, was
served to anyone holding its id, and cached on the URL for 40 minutes.
Public activities are still served to anyone; a non-public one goes
only to a signed addressee, a signer on an addressee's host, or a signer
on a follower's host when the author's followers are addressed. The
URL-keyed cache is gone. A refusal is the same no-store 404 as a miss.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 6: Phase S0 gate**

Run, in order:
1. `./run_tests.sh tests/ --cov=app --cov=fastapi_server --cov-branch --cov-report=json:coverage.json -q`
2. `python3 tests/check_changed_line_coverage.py coverage.json dafbb3aa0 --branches app/ fastapi_server.py`
3. `python3 tests/check_coverage_floors.py coverage.json coverage_floors.ini`

Expected: full suite passes (read the passed count, not just the exit code); changed-line coverage reports 100% lines and branches; no floor drops, and `app/activitypub/entitlement.py` is at 100. Any uncovered changed line is a missing test in Task 1–3: add it to that task's test file and amend nothing — make a follow-up `test:` commit.

---

## Phase S1: bugs

### Task 4: A pending follow request can be withdrawn

**Files:**
- Modify: `app/activitypub/routes.py:1993-1994` (the `isinstance(target, User)` arm of Undo Follow)
- Modify: `tests/test_inbox_dispatch_undo_follow.py:184-228`
- Test: same file

**Interfaces:**
- Consumes: `tests/test_inbox_dispatch_preamble.py:dispatch`, `make_follow(local_user, remote_user, is_accepted, is_inward)`.
- Produces: nothing new.

- [ ] **Step 1: Write the failing tests**

In `tests/test_inbox_dispatch_undo_follow.py`:

(a) In `test_undo_follow_of_a_local_user_deletes_an_accepted_follower`, change `make_follow(local, remote, is_accepted=True)` to `make_follow(local, remote, is_accepted=True, is_inward=True)` and change the docstring to `"""The user branch: an inbound (is_inward) accepted follower row is deleted."""`.

(b) Replace `test_undo_follow_of_a_user_with_a_pending_follow_logs_nothing` with these three tests:

```python
def test_undo_follow_of_a_user_withdraws_a_pending_follow_request(app, db_session, monkeypatch):
    """S1.1 (security track): the filter used to require is_accepted=True, so a
    follow request that had not been approved yet could never be withdrawn --
    the Undo found nothing and the request stayed in the local user's queue.
    is_accepted=None is what production stores for a pending inbound request
    (routes.py Follow arm, `auto_accept if auto_accept else None`)."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    remote = make_user(instance, 'remote')
    local = make_user(None, 'local', local=True)
    local.ap_profile_id = f"{app.config['SERVER_URL']}/u/local".lower()
    db.session.commit()
    make_follow(local, remote, is_accepted=None, is_inward=True)
    local_id, remote_id = local.id, remote.id

    dispatch(undo_follow_activity(remote, local.ap_profile_id))

    db.session.expire_all()
    assert db.session.query(UserFollower).filter_by(
        local_user_id=local_id, remote_user_id=remote_id).first() is None
    assert ActivityPubLog.query.one().result == 'success'


def test_undo_follow_of_a_user_clears_a_rejected_follow_row(app, db_session, monkeypatch):
    """is_accepted=False (rejected) is also the remote user's own row to withdraw."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    remote = make_user(instance, 'remote')
    local = make_user(None, 'local', local=True)
    local.ap_profile_id = f"{app.config['SERVER_URL']}/u/local".lower()
    db.session.commit()
    make_follow(local, remote, is_accepted=False, is_inward=True)
    local_id, remote_id = local.id, remote.id

    dispatch(undo_follow_activity(remote, local.ap_profile_id))

    db.session.expire_all()
    assert db.session.query(UserFollower).filter_by(
        local_user_id=local_id, remote_user_id=remote_id).first() is None


def test_undo_follow_from_a_remote_user_never_deletes_our_outward_follow_of_them(app, db_session, monkeypatch):
    """The same (local_user_id, remote_user_id) pair also stores OUR follow of
    them, with is_inward=False. Their Undo of a follow of us must not touch it
    -- pending or accepted -- and logs nothing, as before."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    remote = make_user(instance, 'remote')
    local = make_user(None, 'local', local=True)
    local.ap_profile_id = f"{app.config['SERVER_URL']}/u/local".lower()
    db.session.commit()
    make_follow(local, remote, is_accepted=None, is_inward=False)
    local_id, remote_id = local.id, remote.id

    dispatch(undo_follow_activity(remote, local.ap_profile_id))

    db.session.expire_all()
    assert db.session.query(UserFollower).filter_by(
        local_user_id=local_id, remote_user_id=remote_id).first() is not None
    assert ActivityPubLog.query.count() == 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./run_tests.sh tests/test_inbox_dispatch_undo_follow.py -q`
Expected: FAIL. `test_undo_follow_of_a_user_withdraws_a_pending_follow_request` with `assert <UserFollower ...> is None`; `test_undo_follow_of_a_user_clears_a_rejected_follow_row` the same. The outward-follow test passes already (the old filter required is_accepted=True); it guards the new filter.

- [ ] **Step 3: Implement**

In `app/activitypub/routes.py`, in the `if isinstance(target, User):` arm of Undo Follow, change:

```python
                            follower = session.query(UserFollower).filter_by(local_user_id=local_user.id,
                                                                    remote_user_id=remote_user.id, is_accepted=True).first()
```

to:

```python
                            # S1.1: any inbound row, so a pending (None) or rejected (False) request can be withdrawn
                            # too; is_inward keeps their Undo from touching OUR outward follow of them (same pair)
                            follower = session.query(UserFollower).filter_by(local_user_id=local_user.id,
                                                                    remote_user_id=remote_user.id, is_inward=True).first()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./run_tests.sh tests/test_inbox_dispatch_undo_follow.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/activitypub/routes.py tests/test_inbox_dispatch_undo_follow.py
git commit -m "fix: a remote user can withdraw a follow request that was never accepted

Undo Follow of a local user filtered on is_accepted=True, so a pending
request survived its own withdrawal. It now matches any inbound row
(is_inward=True), which also stops a remote Undo from deleting our own
outward follow of that user, stored under the same pair.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: `endpoints` without a usable `sharedInbox`

**Files:**
- Modify: `app/activitypub/util.py` (add `shared_inbox_of` directly after `host_of`, ~line 690; use it at the three `ap_inbox_url=` sites, currently U:1443, U:1578, U:1856; remove the Person branch's now-dead `try/except KeyError`, U:1426-1457)
- Modify: `tests/test_coverage_tail_266.py:435-460` (its row used `endpoints: {}` to reach that dead `except`)
- Test: `tests/test_ap_security_shared_inbox.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `shared_inbox_of(actor_json: dict) -> str | None` in `app/activitypub/util.py`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ap_security_shared_inbox.py`:

```python
"""Security track S1.2: an actor whose `endpoints` lacks a usable `sharedInbox`
is still created, with its own inbox. `activity_json['endpoints']['sharedInbox']`
was read whenever `endpoints` was present, so a dict without the key raised
KeyError and a string `endpoints` raised TypeError -- the actor was never
created, at all three creation sites.
"""
import pytest

from app import db
from app.activitypub.util import actor_json_to_model, shared_inbox_of
from app.models import Community, Feed, User
from tests.factories import peer_actor_json, peer_instance
from tests.test_ap_actor_json_feed import _owned_feed, _peer_with_one_owner

PEER = 'peer.example'
SHARED = f'https://{PEER}/inbox'

# (label, endpoints value, expected shared inbox)
SHAPES = [
    ('dict with sharedInbox', {'sharedInbox': SHARED}, SHARED),
    ('dict without sharedInbox', {'oauthTokenEndpoint': f'https://{PEER}/token'}, None),
    ('string endpoints', f'https://{PEER}/endpoints', None),
    ('non-string sharedInbox', {'sharedInbox': ['x']}, None),
    ('empty sharedInbox', {'sharedInbox': ''}, None),
]


@pytest.mark.parametrize('label,endpoints,expected', SHAPES, ids=[s[0] for s in SHAPES])
def test_shared_inbox_of(label, endpoints, expected):
    assert shared_inbox_of({'endpoints': endpoints}) == expected


def test_shared_inbox_of_without_endpoints_is_none():
    assert shared_inbox_of({'inbox': f'https://{PEER}/u/a/inbox'}) is None


@pytest.mark.parametrize('label,endpoints,expected', SHAPES, ids=[s[0] for s in SHAPES])
def test_a_person_is_created_whatever_shape_endpoints_takes(app, db_session, label, endpoints, expected):
    peer_instance(PEER)
    own_inbox = f'https://{PEER}/u/alice/inbox'
    document = peer_actor_json(name='alice', server=PEER,
                               fields={'endpoints': endpoints, 'inbox': own_inbox})
    user = actor_json_to_model(document, 'alice', PEER)
    assert user is not None
    assert user.ap_inbox_url == (expected or own_inbox)
    assert db.session.query(User).filter_by(user_name='alice').count() == 1


@pytest.mark.parametrize('label,endpoints,expected', SHAPES, ids=[s[0] for s in SHAPES])
def test_a_group_is_created_whatever_shape_endpoints_takes(app, db_session, label, endpoints, expected):
    peer_instance(PEER)
    document = peer_actor_json('Group', name='memes', server=PEER, fields={'endpoints': endpoints})
    community = actor_json_to_model(document, '!memes', PEER)
    assert community is not None
    assert community.ap_inbox_url == (expected or f'https://{PEER}/c/memes/inbox')
    assert db.session.query(Community).count() == 1


@pytest.mark.parametrize('label,endpoints,expected', SHAPES, ids=[s[0] for s in SHAPES])
def test_a_feed_is_created_whatever_shape_endpoints_takes(app, db_session, http_mock, label, endpoints, expected):
    _peer_with_one_owner(http_mock)
    document = _owned_feed(fields={'endpoints': endpoints})
    feed = actor_json_to_model(document, '~news', PEER)
    assert feed is not None
    assert feed.ap_inbox_url == (expected or f'https://{PEER}/f/news/inbox')
    assert db.session.query(Feed).count() == 1


def test_a_person_with_unusable_endpoints_and_no_inbox_stores_an_empty_inbox(app, db_session):
    peer_instance(PEER)
    document = peer_actor_json(name='alice', server=PEER, fields={'endpoints': {}})
    user = actor_json_to_model(document, 'alice', PEER)
    assert user.ap_inbox_url == ''
```

In `tests/test_coverage_tail_266.py`, replace `test_a_missing_key_answers_none_rather_than_raising` with:

```python
    def test_endpoints_without_a_shared_inbox_still_produces_a_row(self, db_session):
        """S1.2 (security track). This row used to reach the Person branch's
        `except KeyError` through `endpoints: {}` -- the only KeyError that
        constructor could raise. shared_inbox_of now reads endpoints safely, the
        `try` is gone as dead code, and the same document creates the actor with
        an empty inbox."""
        make_site()
        make_instance(PEER)
        db.session.commit()

        result = actor_json_to_model({
            'type': 'Person',
            'id': f'https://{PEER}/u/nokeys',
            'preferredUsername': 'nokeys',
            'publicKey': {'publicKeyPem': '-----BEGIN PUBLIC KEY-----x'},
            'endpoints': {},
        }, 'nokeys', PEER)

        assert result is not None
        assert result.ap_inbox_url == ''
        assert db.session.query(User).filter_by(user_name='nokeys').count() == 1
```

(Keep the method inside its existing class; match the surrounding indentation.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./run_tests.sh tests/test_ap_security_shared_inbox.py tests/test_coverage_tail_266.py -q`
Expected: FAIL at collection with `ImportError: cannot import name 'shared_inbox_of' from 'app.activitypub.util'`.

- [ ] **Step 3: Implement**

In `app/activitypub/util.py`, directly after the `host_of` function, add:

```python
def shared_inbox_of(actor_json: dict) -> str | None:
    """The actor's shared inbox, or None when `endpoints` does not carry a usable one.

    The spec lets `endpoints` omit sharedInbox or be a URI string naming a document to fetch. Neither is an
    error: the caller falls back to the actor's own inbox. A string `endpoints` is not dereferenced (roadmap
    tier C)."""
    endpoints = actor_json.get('endpoints')
    if not isinstance(endpoints, dict):
        return None
    shared_inbox = endpoints.get('sharedInbox')
    return shared_inbox if isinstance(shared_inbox, str) and shared_inbox else None
```

At each of the three sites (Person/Service ~U:1443, Group ~U:1578, Feed ~U:1856) replace

```python
ap_inbox_url=activity_json['endpoints']['sharedInbox'] if 'endpoints' in activity_json else activity_json['inbox'] if 'inbox' in activity_json else '',
```

with

```python
ap_inbox_url=shared_inbox_of(activity_json) or activity_json.get('inbox') or '',
```

keeping each site's existing indentation. (`activity_json.get('inbox') or ''` matches the old `activity_json['inbox'] if 'inbox' in activity_json else ''` except that an explicit `"inbox": null` now stores `''` instead of `None` — the column is a string and `''` is the existing "no inbox" value.)

In the Person/Service branch, the `try:` / `except KeyError:` around `user = User(...)` is now dead: the comment in `tests/test_coverage_tail_266.py` recorded that `endpoints` without `sharedInbox` was the only KeyError the constructor could raise, and every other key there is read with `in`/`.get`. Remove the `try:` line, dedent the `user = User(...)` statement one level, and delete:

```python
        except KeyError:
            current_app.logger.error(f'KeyError for {address}@{server} while parsing ' + str(activity_json))
            return None
```

Do **not** touch the Group and Feed branches' `except KeyError`: those constructors still read `activity_json['outbox']` (and Feed `['following']`) unguarded, so their handlers stay reachable.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./run_tests.sh tests/test_ap_security_shared_inbox.py tests/test_coverage_tail_266.py tests/test_ap_actor_json_person.py tests/test_ap_actor_json_group.py tests/test_ap_actor_json_feed.py -q`
Expected: PASS. The existing `TestInboxResolution` classes in the three `test_ap_actor_json_*` modules must stay green unchanged.

- [ ] **Step 5: Commit**

```bash
git add app/activitypub/util.py tests/test_ap_security_shared_inbox.py tests/test_coverage_tail_266.py
git commit -m "fix: a remote actor whose endpoints has no usable sharedInbox is still created

All three actor-creation sites read endpoints['sharedInbox'] whenever
endpoints was present. The spec allows it to be missing, or endpoints
to be a URI string, and those actors raised KeyError/TypeError and were
never created. shared_inbox_of returns None for anything unusable and
the actor's own inbox is used. The Person branch's except KeyError
guarded only that read and is removed as dead.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 6: Phase S1 gate**

Run, in order:
1. `./run_tests.sh tests/ --cov=app --cov=fastapi_server --cov-branch --cov-report=json:coverage.json -q`
2. `python3 tests/check_changed_line_coverage.py coverage.json dafbb3aa0 --branches app/ fastapi_server.py`
3. `python3 tests/check_coverage_floors.py coverage.json coverage_floors.ini`

Expected: full suite passes (read the passed count); 100% changed-line and branch coverage; no floor drops (`app/activitypub/util.py` stays ≥ 99).

## Phase S2: signature hardening

### Task 6: Required signed headers (enforcement flag off)

**Files:**
- Modify: `config.py:70` (add the flag next to `FULL_AP_CONTEXT`)
- Modify: `app/activitypub/signature.py:441-465` (`verify_request`), plus a new module-level `_check_required` and the constants
- Modify: `app/activitypub/routes.py:801` (inbox call)
- Modify: `app/relays/inbound.py:19-25` (`_verified`)
- Modify: `app/activitypub/entitlement.py` (the `verify_request` call inside `signed_requester_actor`, from Task 2)
- Modify: `tests/test_relays_inbound.py:37`, `tests/test_relays_forwarded.py:34` (fake verifier signature)
- Modify: `tests/factories.py` (new helper `cavage_signed_headers`)
- Test: `tests/test_ap_security_signature_required.py` (new)

**Interfaces:**
- Consumes: `signed_requester_actor()` (Task 2, `app/activitypub/entitlement.py`).
- Produces:
  - `POST_REQUIRED = frozenset({'(request-target)', 'host', 'date', 'digest'})` and `GET_REQUIRED = frozenset({'(request-target)', 'host', 'date'})`, in `app/activitypub/signature.py`;
  - `HttpSignature.verify_request(request, public_key, required: frozenset[str] = frozenset()) -> True`. The `skip_date` parameter is gone.
  - `Config.SIG_REQUIRED_HEADERS_ENFORCE: bool`;
  - test helper `tests.factories.cavage_signed_headers(private_key, key_id, *, method='post', path='/inbox', body=b'', signed=(...), host=None) -> dict`.

- [ ] **Step 1: Add the test helper to `tests/factories.py`**

Change the import at `tests/factories.py:23` from `from app.activitypub.signature import RsaKeys` to:

```python
from app.activitypub.signature import HttpSignature, RsaKeys, http_date
```

Add near `signed_inbox_post`:

```python
def cavage_signed_headers(private_key: str, key_id: str, *, method: str = 'post', path: str = '/inbox',
                          body: bytes = b'', signed=('(request-target)', 'host', 'date', 'digest'),
                          host: str = None) -> dict:
    """Headers for a draft-cavage signature over exactly the header names in `signed`.

    HttpSignature.signed_request always signs its own fixed set, so a test that needs a signature leaving a
    required header out builds it here. Host, Date and Digest are always SENT -- precheck needs Date and
    Digest present -- but only the names in `signed` are covered by the signature. The signing itself is
    production's: the same key loading, PKCS#1 v1.5 padding and SHA-256 signed_request uses.
    """
    host = host or current_app.config['SERVER_NAME']
    values = {'(request-target)': f'{method} {path}', 'host': host, 'date': http_date(),
              'digest': HttpSignature.calculate_digest(body)}
    signed_string = '\n'.join(f'{name}: {values[name]}' for name in signed)
    key = HttpSignature._get_private_key_instance(private_key)
    signature = key.sign(signed_string.encode('ascii'), padding.PKCS1v15(), hashes.SHA256())
    return {'Host': host, 'Date': values['date'], 'Digest': values['digest'],
            'Signature': HttpSignature.compile_signature({'keyid': key_id, 'headers': list(signed),
                                                          'signature': signature, 'algorithm': 'rsa-sha256'})}
```

and add at the top of `tests/factories.py`:

```python
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
```

- [ ] **Step 2: Write the failing tests**

`tests/test_ap_security_signature_required.py`:

```python
"""S2.1: the headers a signature must cover (spec 2026-10-10 ap-security-track, phase S2).

Every signature here is made with production's key handling (tests.factories.cavage_signed_headers or
signed_inbox_post) and checked by the unpatched HttpSignature.verify_request.
"""
import json

import pytest
from flask import request

from app.activitypub.signature import GET_REQUIRED, POST_REQUIRED, HttpSignature, VerificationError
from app.models import ActivityPubLog
from config import Config
from tests.factories import a_keypair, cavage_signed_headers, inbox_activity

pytestmark = pytest.mark.usefixtures('redis_double')

KEY_ID = 'https://peer.example/users/alice#main-key'


def _verify(app, headers, required, method='POST', public_key=None, body=b''):
    with app.test_request_context('/inbox', method=method, headers=headers, data=body):
        return HttpSignature.verify_request(request, public_key, required)


def test_the_flag_is_off_by_default():
    assert Config.SIG_REQUIRED_HEADERS_ENFORCE is False


def test_the_required_sets():
    assert POST_REQUIRED == frozenset({'(request-target)', 'host', 'date', 'digest'})
    assert GET_REQUIRED == frozenset({'(request-target)', 'host', 'date'})


def test_skip_date_is_gone(app):
    private_key, public_key = a_keypair()
    with app.test_request_context('/inbox', method='POST', headers=cavage_signed_headers(private_key, KEY_ID)):
        with pytest.raises(TypeError):
            HttpSignature.verify_request(request, public_key, skip_date=True)


@pytest.mark.parametrize('left_out', sorted(POST_REQUIRED))
def test_flag_off_a_missing_required_header_is_logged_and_accepted(app, caplog, monkeypatch, left_out):
    monkeypatch.setitem(app.config, 'SIG_REQUIRED_HEADERS_ENFORCE', False)
    private_key, public_key = a_keypair()
    headers = cavage_signed_headers(private_key, KEY_ID, signed=tuple(h for h in sorted(POST_REQUIRED) if h != left_out))
    with caplog.at_level('WARNING', logger=app.logger.name):
        assert _verify(app, headers, POST_REQUIRED, public_key=public_key) is True
    assert f'Signature from peer.example does not sign: {left_out}' in caplog.text


@pytest.mark.parametrize('left_out', sorted(POST_REQUIRED))
def test_flag_on_a_missing_required_header_is_refused(app, monkeypatch, left_out):
    monkeypatch.setitem(app.config, 'SIG_REQUIRED_HEADERS_ENFORCE', True)
    private_key, public_key = a_keypair()
    headers = cavage_signed_headers(private_key, KEY_ID, signed=tuple(h for h in sorted(POST_REQUIRED) if h != left_out))
    with pytest.raises(VerificationError, match=f'unsigned required header: {left_out}'):
        _verify(app, headers, POST_REQUIRED, public_key=public_key)


def test_flag_on_a_get_missing_date_is_refused(app, monkeypatch):
    monkeypatch.setitem(app.config, 'SIG_REQUIRED_HEADERS_ENFORCE', True)
    private_key, public_key = a_keypair()
    headers = cavage_signed_headers(private_key, KEY_ID, method='get', signed=('(request-target)', 'host'))
    with pytest.raises(VerificationError, match='unsigned required header: date'):
        _verify(app, headers, GET_REQUIRED, method='GET', public_key=public_key)


def test_flag_on_every_required_header_signed_passes_without_a_warning(app, caplog, monkeypatch):
    monkeypatch.setitem(app.config, 'SIG_REQUIRED_HEADERS_ENFORCE', True)
    private_key, public_key = a_keypair()
    with caplog.at_level('WARNING', logger=app.logger.name):
        assert _verify(app, cavage_signed_headers(private_key, KEY_ID), POST_REQUIRED, public_key=public_key) is True
    assert 'does not sign' not in caplog.text


def test_no_required_set_keeps_the_old_behaviour(app, caplog, monkeypatch):
    monkeypatch.setitem(app.config, 'SIG_REQUIRED_HEADERS_ENFORCE', True)
    private_key, public_key = a_keypair()
    headers = cavage_signed_headers(private_key, KEY_ID, signed=('date',))
    with caplog.at_level('WARNING', logger=app.logger.name):
        assert _verify(app, headers, frozenset(), public_key=public_key) is True
    assert 'does not sign' not in caplog.text


def test_the_inbox_refuses_an_unsigned_digest_when_enforcing(app, signing_peer, monkeypatch):
    monkeypatch.setitem(app.config, 'SIG_REQUIRED_HEADERS_ENFORCE', True)
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    monkeypatch.setitem(app.config, 'DEBUG', True)
    dispatched = []
    monkeypatch.setattr('app.activitypub.routes.process_inbox_request', lambda *a, **kw: dispatched.append(a))
    activity = inbox_activity(signing_peer)
    body = json.dumps(activity).encode('utf8')
    headers = cavage_signed_headers(signing_peer.private_key, f'{signing_peer.ap_profile_id}#main-key', body=body,
                                    signed=('(request-target)', 'host', 'date'))
    with app.test_client() as client:
        response = client.post('/inbox', data=body, headers=headers, content_type='application/activity+json')
    assert response.status_code == 400
    assert dispatched == []
    assert ActivityPubLog.query.one().exception_message == \
        'Could not verify HTTP signature: unsigned required header: digest'
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `./run_tests.sh tests/test_ap_security_signature_required.py -q`
Expected: FAIL. The first errors are `ImportError: cannot import name 'GET_REQUIRED' from 'app.activitypub.signature'` and `cannot import name 'cavage_signed_headers'` (until Step 1 lands), then `AttributeError: SIG_REQUIRED_HEADERS_ENFORCE`.

- [ ] **Step 4: Implement**

`config.py`, after `FULL_AP_CONTEXT` (line 70):

```python
    # S2.1 (ap-security-track): refuse an inbound HTTP signature that does not cover (request-target), host,
    # date (and digest on a POST). Off logs the gap instead; switched on only after the logs are reviewed.
    SIG_REQUIRED_HEADERS_ENFORCE = os.environ.get('SIG_REQUIRED_HEADERS_ENFORCE', '0') == '1'
```

`app/activitypub/signature.py`, after `parse_signature_header`/`signature_part` (before `class HttpSignature`):

```python
# The headers an inbound signature must cover (S2.1). A POST without a signed digest lets the body be swapped;
# without (request-target) or host a signature can be replayed against another endpoint or instance.
POST_REQUIRED = frozenset({'(request-target)', 'host', 'date', 'digest'})
GET_REQUIRED = frozenset({'(request-target)', 'host', 'date'})


def _check_required(signed: set[str], required: frozenset[str], key_id: str | None) -> None:
    """Refuse (or, with SIG_REQUIRED_HEADERS_ENFORCE off, log) a valid signature that leaves a required header
    uncovered."""
    missing = required - signed
    if not missing:
        return
    names = ' '.join(sorted(missing))
    if current_app.config.get('SIG_REQUIRED_HEADERS_ENFORCE'):
        raise VerificationError(f'unsigned required header: {names}')
    current_app.logger.warning(f'Signature from {activitypub_util.host_of(key_id or "")} does not sign: {names}')
```

Replace `verify_request` (S:441-465):

```python
    @classmethod
    def verify_request(cls, request: Request, public_key, required: frozenset[str] = frozenset()):
        """
        Verifies that the request has a valid signature for its body, and that the signature covers every
        header in `required` (see _check_required)
        """
        # Get the signature details
        if "signature" not in request.headers:
            raise VerificationFormatError("No signature header present")
        signature_details = cls.parse_signature(request.headers["signature"])

        # Reject unknown algorithms
        # hs2019 is used by some libraries to obfuscate the real algorithm per the spec
        # https://datatracker.ietf.org/doc/html/draft-cavage-http-signatures-12
        if (
                signature_details["algorithm"] != "rsa-sha256"
                and signature_details["algorithm"] != "hs2019"
        ):
            raise VerificationFormatError("Unknown signature algorithm")
        # Create the signature payload
        headers_string = cls.headers_from_request(request, signature_details["headers"])
        cls.verify_signature(
            signature_details["signature"],
            headers_string,
            public_key,
        )
        _check_required({name.lower() for name in signature_details["headers"]}, required,
                        signature_details["keyid"])
        return True
```

`app/activitypub/routes.py:801`. Add `POST_REQUIRED` to the `from app.activitypub.signature import ...` at R:19-20, then:

```python
        HttpSignature.verify_request(request, actor.public_key, POST_REQUIRED)
```

`app/relays/inbound.py`. Add `POST_REQUIRED` to its signature import, then in `_verified`:

```python
        HttpSignature.verify_request(request, relay.public_key, POST_REQUIRED)
```

`app/activitypub/entitlement.py`, inside `signed_requester_actor`: the `HttpSignature.verify_request(request, <key>)` call gains `GET_REQUIRED` as its third argument. Import `GET_REQUIRED` from `app.activitypub.signature`.

`tests/test_relays_inbound.py:37` and `tests/test_relays_forwarded.py:34`: change the fakes' signature to `def fake(request_, public_key, required=frozenset()):`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `./run_tests.sh tests/test_ap_security_signature_required.py tests/test_activitypub_signature.py tests/test_signature_header_parsing.py tests/test_inbox_gate_signatures.py tests/test_relays_inbound.py tests/test_relays_forwarded.py tests/test_no_inline_imports.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add config.py app/activitypub/signature.py app/activitypub/routes.py app/relays/inbound.py app/activitypub/entitlement.py tests/factories.py tests/test_ap_security_signature_required.py tests/test_relays_inbound.py tests/test_relays_forwarded.py
git commit -m "feat(ap): log signatures that leave required headers unsigned

An inbound signature must cover (request-target), host, date and, on a
POST, digest. With SIG_REQUIRED_HEADERS_ENFORCE off (the default) a gap is
logged with the signer's host; on, it is refused. Drops the unused
skip_date parameter.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Dedupe only verified activities, for 24 hours

**Files:**
- Modify: `app/activitypub/routes.py:758-761` and the end of the signature block (after R:824)
- Modify: `tests/factories.py:1136-1145` (`inbox_activity` docstring: "for 90 seconds" becomes "for ACTIVITY_DEDUPE_SECONDS")
- Test: `tests/test_ap_security_dedupe.py` (new)

**Interfaces:**
- Consumes: `tests.factories.signed_inbox_post`, `inbox_activity`; `tests.conftest.unsigned_but_precheck_clean_headers`.
- Produces: `ACTIVITY_DEDUPE_SECONDS = 86400` in `app/activitypub/routes.py`.

- [ ] **Step 1: Write the failing tests**

`tests/test_ap_security_dedupe.py`:

```python
"""S2.4: an activity id is remembered only once its signature has verified, for ACTIVITY_DEDUPE_SECONDS."""
import json

import pytest

from app.activitypub.routes import ACTIVITY_DEDUPE_SECONDS
from app.models import ActivityPubLog
from tests.conftest import unsigned_but_precheck_clean_headers
from tests.factories import inbox_activity, signed_inbox_post


@pytest.fixture
def dispatched(app, monkeypatch):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    monkeypatch.setitem(app.config, 'DEBUG', True)
    calls = []
    monkeypatch.setattr('app.activitypub.routes.process_inbox_request', lambda *a, **kw: calls.append(a))
    return calls


def test_the_window_is_a_day():
    assert ACTIVITY_DEDUPE_SECONDS == 86400


def test_a_forged_request_reusing_a_genuine_id_does_not_suppress_it(app, signing_peer, redis_double, dispatched):
    activity = inbox_activity(signing_peer)
    body = json.dumps(activity).encode('utf8')
    with app.test_client() as client:
        forged = client.post('/inbox', data=body, content_type='application/activity+json',
                             headers={'Host': app.config['SERVER_NAME'], **unsigned_but_precheck_clean_headers(body)})
        genuine = signed_inbox_post(client, activity, signing_peer)
    assert forged.status_code == 400
    assert genuine.status_code == 200
    assert len(dispatched) == 1


def test_a_verified_id_is_remembered_for_the_window(app, signing_peer, redis_double, dispatched):
    activity = inbox_activity(signing_peer)
    with app.test_client() as client:
        signed_inbox_post(client, activity, signing_peer)
    assert ACTIVITY_DEDUPE_SECONDS - 10 < redis_double.ttl(activity['id']) <= ACTIVITY_DEDUPE_SECONDS


def test_a_redelivery_verified_while_the_first_was_in_flight_is_dropped(app, signing_peer, redis_double,
                                                                        dispatched, monkeypatch):
    """The early exists() check missed it (the first delivery had not been verified yet); the post-verification
    set(nx=True) is what catches it."""
    activity = inbox_activity(signing_peer)
    redis_double.set(activity['id'], 1)
    monkeypatch.setattr(redis_double, 'exists', lambda *keys: 0)
    with app.test_client() as client:
        response = signed_inbox_post(client, activity, signing_peer)
    assert response.status_code == 200
    assert dispatched == []
    assert ActivityPubLog.query.one().exception_message == 'Already aware of this activity'
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./run_tests.sh tests/test_ap_security_dedupe.py -q`
Expected: FAIL with `ImportError: cannot import name 'ACTIVITY_DEDUPE_SECONDS'`.

- [ ] **Step 3: Implement**

`app/activitypub/routes.py`. Add a module-level constant after the imports:

```python
# How long an inbound activity id is remembered so a redelivery is not processed twice (S2.4)
ACTIVITY_DEDUPE_SECONDS = 86400
```

At R:758-761, keep the `exists` check and delete the `set` line:

```python
    if app_pkg.redis_client.exists(id):  # Something is sending same activity multiple times
        log_incoming_ap(id, APLOG_DUPLICATE, APLOG_IGNORED, saved_json, 'Already aware of this activity')
        return '', 200
```

Immediately after the signature `try/except` block (after the final `return '', 400` at R:824), before `relay = relay_inbound.relay_for_forwarded(...)`, insert:

```python
    # Remembered only once the signature has verified, so a forged request reusing a genuine activity's id cannot
    # make this instance drop the genuine one. set(nx=True) is also the gate for two deliveries verified at once.
    if not app_pkg.redis_client.set(id, 1, ex=ACTIVITY_DEDUPE_SECONDS, nx=True):
        log_incoming_ap(id, APLOG_DUPLICATE, APLOG_IGNORED, saved_json, 'Already aware of this activity')
        return '', 200
```

In `tests/factories.py` `inbox_activity`'s docstring, change "writes it to Redis for 90 seconds" to "writes it to Redis for ACTIVITY_DEDUPE_SECONDS (app/activitypub/routes.py)". Change `tests/README.md:1380` the same way: `(routes.py:680, ex=90)` becomes `(ACTIVITY_DEDUPE_SECONDS, set only after the signature verifies)`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./run_tests.sh tests/test_ap_security_dedupe.py tests/test_inbox_gate_refusals.py tests/test_activitypub_routes_floor.py tests/test_inbox_gate_signatures.py -q`
Expected: PASS. The existing duplicate tests sign both posts, so they still collide.

- [ ] **Step 5: Commit**

```bash
git add app/activitypub/routes.py tests/factories.py tests/README.md tests/test_ap_security_dedupe.py
git commit -m "fix(ap): remember an activity id only after its signature verifies

The dedupe key was written before verification, so an unsigned forgery
reusing a genuine id made the genuine activity look like a duplicate.
It is now set after verification (nx doubles as the gate) and kept for
a day instead of 90 seconds.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Refetch a rotated key once on verification failure

**Files:**
- Modify: `app/activitypub/signature.py`: imports; new `KEY_REFETCH_SECONDS`, `request_key_id`, `_fetch_actor_key`, `verify_refreshing_key`, `verify_with_key_refresh`
- Modify: `app/activitypub/routes.py:799-801` (inbox uses `verify_with_key_refresh`)
- Modify: `app/relays/inbound.py:19-57` (`_key_owner`, `_verified_with_refetch`; delete `REFETCH_SECONDS`, `_names_the_relay_actor`)
- Modify: `app/activitypub/entitlement.py` (`signed_requester_actor` reads the keyId through `request_key_id`)
- Test: `tests/test_ap_security_key_refresh.py` (new)

**Interfaces:**
- Consumes: `POST_REQUIRED` (Task 6); `tests.factories.cavage_signed_headers` (Task 6).
- Produces, all in `app/activitypub/signature.py`:
  - `KEY_REFETCH_SECONDS = 300`;
  - `request_key_id(request) -> str | None` (cavage here; Task 19 adds RFC 9421);
  - `verify_refreshing_key(request, stored_key, required=frozenset(), *, owner_url, rate_key, fetch_key, store_key) -> None`;
  - `verify_with_key_refresh(request, actor, required=frozenset()) -> None`.

  The last two raise `VerificationError`.

- [ ] **Step 1: Write the failing tests**

`tests/test_ap_security_key_refresh.py`:

```python
"""S2.3: on a failed verification, one signed refetch of the signer's key, rate-limited, then one retry."""
import json

import httpx
import pytest
from flask import request

from app import db
from app.activitypub.signature import (KEY_REFETCH_SECONDS, HttpSignature, VerificationError, _fetch_actor_key,
                                       request_key_id, verify_refreshing_key, verify_with_key_refresh)
from app.models import Site
from tests.factories import a_keypair, cavage_signed_headers, inbox_activity, make_instance, make_site, make_user

pytestmark = pytest.mark.usefixtures('redis_double')

OWNER = 'https://peer.example/users/alice'


def _two_keypairs():
    first = a_keypair()
    second = next(pair for pair in (a_keypair() for _ in range(16)) if pair[1] != first[1])
    return first, second


@pytest.fixture
def signing_site(app):
    site = db.session.get(Site, 1) or make_site()
    site.private_key, _ = a_keypair()
    db.session.commit()
    return site


class TestRequestKeyId:

    def test_reads_the_cavage_key_id(self, app):
        with app.test_request_context('/inbox', headers={'Signature': f'keyId="{OWNER}#main-key",signature="x"'}):
            assert request_key_id(request) == f'{OWNER}#main-key'

    def test_none_without_a_signature(self, app):
        with app.test_request_context('/inbox'):
            assert request_key_id(request) is None


class TestVerifyRefreshingKey:
    """The core, with real signatures and recording fetch/store callables."""

    def _run(self, app, headers, stored_key, *, owner_url=OWNER, fetched=None, rate_key='sig-refetch:test'):
        calls = {'fetch': 0, 'stored': []}

        def fetch_key():
            calls['fetch'] += 1
            return fetched

        with app.test_request_context('/inbox', method='POST', headers=headers):
            try:
                verify_refreshing_key(request, stored_key, owner_url=owner_url, rate_key=rate_key,
                                      fetch_key=fetch_key, store_key=calls['stored'].append)
                calls['result'] = 'verified'
            except VerificationError as e:
                calls['result'] = str(e)
        return calls

    def test_a_valid_stored_key_never_fetches(self, app):
        (private_key, public_key), _ = _two_keypairs()
        calls = self._run(app, cavage_signed_headers(private_key, f'{OWNER}#main-key'), public_key)
        assert calls == {'fetch': 0, 'stored': [], 'result': 'verified'}

    def test_a_rotated_key_is_fetched_stored_and_used(self, app):
        (private_key, public_key), (_, stale) = _two_keypairs()
        calls = self._run(app, cavage_signed_headers(private_key, f'{OWNER}#main-key'), stale, fetched=public_key)
        assert calls == {'fetch': 1, 'stored': [public_key], 'result': 'verified'}

    def test_a_gotosocial_style_key_id_is_eligible(self, app):
        """Review Focus 1: GoToSocial keyIds are a path under the actor, not a fragment."""
        (private_key, public_key), (_, stale) = _two_keypairs()
        calls = self._run(app, cavage_signed_headers(private_key, f'{OWNER}/main-key'), stale, fetched=public_key)
        assert calls['result'] == 'verified' and calls['fetch'] == 1

    def test_no_stored_key_goes_straight_to_a_fetch(self, app):
        (private_key, public_key), _ = _two_keypairs()
        calls = self._run(app, cavage_signed_headers(private_key, f'{OWNER}#main-key'), None, fetched=public_key)
        assert calls == {'fetch': 1, 'stored': [public_key], 'result': 'verified'}

    def test_an_unusable_stored_pem_goes_to_a_fetch(self, app):
        (private_key, public_key), _ = _two_keypairs()
        calls = self._run(app, cavage_signed_headers(private_key, f'{OWNER}#main-key'), 'not a pem', fetched=public_key)
        assert calls['result'] == 'verified'

    def test_a_key_id_on_another_host_never_fetches(self, app):
        (private_key, public_key), (_, stale) = _two_keypairs()
        calls = self._run(app, cavage_signed_headers(private_key, 'https://evil.example/actor#main-key'), stale,
                          fetched=public_key)
        assert calls['fetch'] == 0 and calls['result'] == 'Signature mismatch'

    def test_no_owner_never_fetches(self, app):
        (private_key, public_key), (_, stale) = _two_keypairs()
        calls = self._run(app, cavage_signed_headers(private_key, f'{OWNER}#main-key'), stale, owner_url=None,
                          fetched=public_key)
        assert calls['fetch'] == 0

    def test_an_unparseable_key_id_never_fetches(self, app):
        (_, public_key), (_, stale) = _two_keypairs()
        headers = {'Signature': 'keyId="not a url",headers="date",signature="YQ==",algorithm="rsa-sha256"'}
        calls = self._run(app, headers, stale, fetched=public_key)
        assert calls['fetch'] == 0

    def test_a_second_failure_inside_the_window_does_not_fetch(self, app, redis_double):
        (private_key, public_key), (_, stale) = _two_keypairs()
        headers = cavage_signed_headers(private_key, f'{OWNER}#main-key')
        self._run(app, headers, stale, fetched=stale)
        calls = self._run(app, headers, stale, fetched=public_key)
        assert calls['fetch'] == 0 and calls['result'] == 'Signature mismatch'
        assert 0 < redis_double.ttl('sig-refetch:test') <= KEY_REFETCH_SECONDS

    @pytest.mark.parametrize('fetched', [None, 'SAME'])
    def test_no_new_key_keeps_the_original_error(self, app, fetched):
        (private_key, _), (_, stale) = _two_keypairs()
        calls = self._run(app, cavage_signed_headers(private_key, f'{OWNER}#main-key'), stale,
                          fetched=stale if fetched == 'SAME' else None)
        assert calls['stored'] == [] and calls['result'] == 'Signature mismatch'

    def test_a_refetched_key_that_still_fails_is_refused_after_storing_it(self, app):
        (private_key, _), (_, other) = _two_keypairs()
        calls = self._run(app, cavage_signed_headers(private_key, f'{OWNER}#main-key'), 'not a pem', fetched=other)
        assert calls['stored'] == [other] and calls['result'] == 'Signature mismatch'

    def test_a_refetched_unusable_pem_is_a_verification_error(self, app):
        (private_key, _), (_, stale) = _two_keypairs()
        calls = self._run(app, cavage_signed_headers(private_key, f'{OWNER}#main-key'), stale, fetched='junk')
        assert calls['result'] == 'The refetched public key is not a usable PEM'


class TestFetchActorKey:

    def test_a_published_key_is_returned(self, app, http_mock, signing_site):
        http_mock.get(OWNER).respond(200, json={'id': OWNER, 'publicKey': {'publicKeyPem': 'PEM'}})
        assert _fetch_actor_key(OWNER) == 'PEM'

    @pytest.mark.parametrize('response', [httpx.Response(404), httpx.Response(200, content=b'not json'),
                                          httpx.Response(200, json={'id': OWNER})])
    def test_anything_else_is_none(self, app, http_mock, signing_site, response):
        http_mock.get(OWNER).mock(return_value=response)
        assert _fetch_actor_key(OWNER) is None

    def test_a_transport_failure_is_none(self, app, http_mock, signing_site):
        http_mock.get(OWNER).mock(side_effect=httpx.ConnectError('down'))
        assert _fetch_actor_key(OWNER) is None

    def test_no_site_key_is_none(self, app):
        site = db.session.get(Site, 1) or make_site()
        site.private_key = None
        db.session.commit()
        assert _fetch_actor_key(OWNER) is None


class TestThroughTheInbox:

    @pytest.fixture
    def dispatched(self, app, monkeypatch):
        monkeypatch.setitem(app.config, 'DEBUG', True)
        monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
        calls = []
        monkeypatch.setattr('app.activitypub.routes.process_inbox_request', lambda *a, **kw: calls.append(a))
        return calls

    @pytest.mark.parametrize('key_suffix', ['#main-key', '/main-key'])
    def test_a_rotated_key_is_accepted_after_one_refetch(self, app, signing_peer, http_mock, signing_site,
                                                         dispatched, key_suffix):
        (private_key, public_key), (_, stale) = _two_keypairs()
        signing_peer.private_key, signing_peer.public_key = private_key, stale
        db.session.commit()
        route = http_mock.get(signing_peer.ap_profile_id).respond(
            200, json={'id': signing_peer.ap_profile_id, 'publicKey': {'publicKeyPem': public_key}})
        activity = inbox_activity(signing_peer)
        _uri, headers, body = HttpSignature.signed_request(
            f"https://{app.config['SERVER_NAME']}/inbox", activity, private_key,
            f'{signing_peer.ap_profile_id}{key_suffix}', send_via_async=True)
        with app.test_client() as client:
            response = client.post('/inbox', data=body, headers=headers, content_type='application/activity+json')
        assert response.status_code == 200 and len(dispatched) == 1
        assert route.call_count == 1
        db.session.refresh(signing_peer)
        assert signing_peer.public_key == public_key

    def test_a_local_actor_is_never_refetched(self, app):
        """verify_with_key_refresh passes owner_url=None for a local actor."""
        (private_key, _), (_, other) = _two_keypairs()
        local = make_user(make_instance(app.config['SERVER_NAME'], software='piefed'), 'bob', local=True)
        local.public_key = other
        db.session.commit()
        headers = cavage_signed_headers(private_key, f"https://{app.config['SERVER_NAME']}/u/bob#main-key")
        with app.test_request_context('/inbox', method='POST', headers=headers):
            with pytest.raises(VerificationError, match='Signature mismatch'):
                verify_with_key_refresh(request, local)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./run_tests.sh tests/test_ap_security_key_refresh.py -q`
Expected: FAIL with `ImportError: cannot import name 'KEY_REFETCH_SECONDS'`.

- [ ] **Step 3: Implement**

`app/activitypub/signature.py` imports:
- `from app import celery, db, httpx_client` (adds `db`);
- `import app as app_pkg`;
- `from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm`;
- add `Site` and `public_key_pem` to the `from app.models import ...` line.

After `_check_required` add:

```python
# A signer whose signature fails gets at most one key refetch in this many seconds (S2.3)
KEY_REFETCH_SECONDS = 300


def request_key_id(request) -> str | None:
    """The keyId the request's signature names, or None. Every keyId read goes through here."""
    return parse_signature_header(request.headers.get('Signature')).get('keyid') or None


def _fetch_actor_key(actor_url: str) -> str | None:
    """The PEM `actor_url` publishes now, fetched with a signed GET as the instance actor; None on any failure.
    Only the key is taken: the daily profile refresh does the rest."""
    site = db.session.get(Site, 1)
    if site is None or not site.private_key:
        return None
    try:
        response = signed_get_request(actor_url, site.private_key,
                                      f"{current_app.config['SERVER_URL']}/actor#main-key")
    except (httpx.HTTPError, ValueError):
        return None
    try:
        if response.status_code != 200:
            return None
        return public_key_pem(response.json())
    except ValueError:   # JSONDecodeError
        return None
    finally:
        response.close()


def verify_refreshing_key(request, stored_key, required: frozenset[str] = frozenset(), *, owner_url,
                          rate_key: str, fetch_key, store_key) -> None:
    """Verify `request` against `stored_key`; on failure, refetch the signer's key once and verify again.

    The refetch happens only when the keyId names a key on `owner_url`'s host (an unauthenticated request must not
    make this instance fetch from a host of its choosing), `owner_url` is set (None for a local actor), and
    `rate_key` was not used in the last KEY_REFETCH_SECONDS. `fetch_key()` returns the published PEM or None;
    `store_key(pem)` persists a new one. Raises VerificationError when the request does not verify."""
    try:
        if not stored_key:
            raise VerificationError('No public key is stored for the signer')
        HttpSignature.verify_request(request, stored_key, required)
        return
    except (ValueError, TypeError, UnsupportedAlgorithm):
        first_error = VerificationError('The stored public key is not a usable PEM')
    except VerificationError as e:
        first_error = e
    key_host = activitypub_util.host_of(request_key_id(request) or '')
    if not owner_url or not key_host or key_host != activitypub_util.host_of(owner_url):
        raise first_error
    if not app_pkg.redis_client.set(rate_key, 1, nx=True, ex=KEY_REFETCH_SECONDS):
        raise first_error
    fetched = fetch_key()
    if not fetched or fetched == stored_key:
        raise first_error
    store_key(fetched)
    try:
        HttpSignature.verify_request(request, fetched, required)
    except (ValueError, TypeError, UnsupportedAlgorithm):
        raise VerificationError('The refetched public key is not a usable PEM')


def verify_with_key_refresh(request, actor, required: frozenset[str] = frozenset()) -> None:
    """verify_refreshing_key for a User, Community or Feed: its stored key, a signed refetch of its actor
    document, and the key stored back on the row. A local actor is never refetched."""
    def store(pem):
        actor.public_key = pem
        db.session.commit()

    verify_refreshing_key(request, actor.public_key, required,
                          owner_url=None if actor.is_local() else actor.ap_profile_id,
                          rate_key=f'sig-refetch:{actor.ap_profile_id}',
                          fetch_key=lambda: _fetch_actor_key(actor.ap_profile_id), store_key=store)
```

`signed_get_request` is defined further down the module. That is fine, because it is resolved at call time.

`app/activitypub/routes.py`. Add `verify_with_key_refresh` to the signature import, then replace R:801:

```python
        verify_with_key_refresh(request, actor, POST_REQUIRED)
```

`app/relays/inbound.py`. Change the import to `from app.activitypub.signature import (POST_REQUIRED, HttpSignature, VerificationError, request_key_id, verify_refreshing_key)`. Delete `REFETCH_SECONDS` and `_names_the_relay_actor`. Then:

```python
def _key_owner(request) -> str:
    """The actor a request's signature keyId belongs to (the keyId without its fragment)."""
    return (request_key_id(request) or '').split('#')[0]


def _relay_key(relay):
    try:
        return relay_subscribe.detect_relay(relay.url)['public_key']
    except relay_subscribe.RelayError:
        return None


def _store_relay_key(relay, pem):
    relay.public_key = pem
    db.session.commit()


def _verified_with_refetch(request, relay) -> bool:
    # One implementation of refetch-on-failure (signature.verify_refreshing_key): at most once per relay in
    # KEY_REFETCH_SECONDS, and only for a keyId on the relay actor's own host.
    try:
        verify_refreshing_key(request, relay.public_key, POST_REQUIRED, owner_url=relay.actor_id,
                              rate_key=f'relay-refetch:{relay.id}', fetch_key=lambda: _relay_key(relay),
                              store_key=lambda pem: _store_relay_key(relay, pem))
        return True
    except VerificationError:
        return False
```

`app/activitypub/entitlement.py`: in `signed_requester_actor`, replace the expression Task 2 uses to read the keyId (`HttpSignature.parse_signature(...)['keyid']` or `parse_signature_header(...)`) with `request_key_id(request)`, imported from `app.activitypub.signature`. A None result returns None.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./run_tests.sh tests/test_ap_security_key_refresh.py tests/test_relays_inbound.py tests/test_relays_forwarded.py tests/test_inbox_gate_signatures.py tests/test_ap_security_entitlement.py tests/test_no_inline_imports.py -q`
Expected: PASS. If Task 2 named its test file differently, use that name.

- [ ] **Step 5: Commit**

```bash
git add app/activitypub/signature.py app/activitypub/routes.py app/relays/inbound.py app/activitypub/entitlement.py tests/test_ap_security_key_refresh.py
git commit -m "feat(ap): refetch a signer's key once when its signature fails

A peer that rotated its key was refused until the daily actor refresh.
On failure the inbox now refetches the actor's key with a signed GET,
at most once per actor in five minutes and only for a keyId on the
actor's host, then verifies again. Relays share the same code.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: keyId host must match the actor's host (ends phase S2)

**Files:**
- Modify: `app/activitypub/routes.py`: the inbox after the signature block, before the Task 7 dedupe `set`; new helper `_ld_signature_verifies`; `SIGNATURE_FAILURE_MESSAGES` (R:880)
- Test: `tests/test_ap_security_key_id_host.py` (new)

**Interfaces:**
- Consumes: `request_key_id` (Task 8); `host_of` (`app/activitypub/util.py:657`); `relay_inbound.relay_for_forwarded`.
- Produces: the inbox refuses a verified signature whose keyId host differs from the actor's host, unless the body carries a valid LD signature or an accepted relay signed it. The refusal is logged as `'Signature keyId host does not match actor'` and added to `SIGNATURE_FAILURE_MESSAGES`.

- [ ] **Step 1: Write the failing tests**

`tests/test_ap_security_key_id_host.py`:

```python
"""S2.2: a verified signature whose keyId names another host is accepted only via an LD signature or a relay."""
import json

import pytest

from app import db
from app.activitypub.routes import SIGNATURE_FAILURE_MESSAGES
from app.activitypub.signature import HttpSignature
from app.models import ActivityPubLog, Relay
from app.relays import RELAY_ACCEPTED
from tests.conftest import ld_signed_body
from tests.factories import a_keypair, inbox_activity

pytestmark = pytest.mark.usefixtures('redis_double')


@pytest.fixture
def dispatched(app, monkeypatch):
    monkeypatch.setitem(app.config, 'DEBUG', True)
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    calls = []
    monkeypatch.setattr('app.activitypub.routes.process_inbox_request', lambda *a, **kw: calls.append((a, kw)))
    return calls


def _post(app, activity, private_key, key_id):
    _uri, headers, body = HttpSignature.signed_request(f"https://{app.config['SERVER_NAME']}/inbox", activity,
                                                       private_key, key_id, send_via_async=True)
    with app.test_client() as client:
        return client.post('/inbox', data=body, headers=headers, content_type='application/activity+json')


def test_the_refusal_counts_as_a_signature_failure():
    assert 'Signature keyId host does not match actor' in SIGNATURE_FAILURE_MESSAGES


def test_a_key_id_on_another_host_is_refused(app, signing_peer, dispatched):
    response = _post(app, inbox_activity(signing_peer), signing_peer.private_key, 'https://other.example/actor#main-key')
    assert response.status_code == 400 and dispatched == []
    assert ActivityPubLog.query.one().exception_message == 'Signature keyId host does not match actor'


@pytest.mark.parametrize('key_id', ['https://Peer.Example/users/alice#main-key',
                                    'https://peer.example:443/users/alice#main-key',
                                    'https://peer.example/users/alice/main-key'])
def test_case_port_and_path_differences_are_the_same_host(app, signing_peer, dispatched, key_id):
    """Review Focus 3 (case, default port) and Review Focus 1 (GoToSocial path keyId)."""
    response = _post(app, inbox_activity(signing_peer), signing_peer.private_key, key_id)
    assert response.status_code == 200 and len(dispatched) == 1


def test_a_valid_ld_signature_lets_it_through(app, signing_peer, dispatched, no_network_ld_signing):
    activity = json.loads(ld_signed_body(signing_peer))
    response = _post(app, activity, signing_peer.private_key, 'https://other.example/actor#main-key')
    assert response.status_code == 200 and len(dispatched) == 1


def test_an_invalid_ld_signature_does_not(app, signing_peer, dispatched, no_network_ld_signing):
    wrong_private, _ = next(pair for pair in (a_keypair() for _ in range(16)) if pair[1] != signing_peer.public_key)
    activity = json.loads(ld_signed_body(signing_peer, signing_key=wrong_private))
    response = _post(app, activity, signing_peer.private_key, 'https://other.example/actor#main-key')
    assert response.status_code == 400 and dispatched == []


def test_an_accepted_relay_signing_with_that_key_lets_it_through(app, signing_peer, dispatched):
    relay = Relay(url='https://relay.example/actor', style='mastodon', inbox_url='https://relay.example/inbox',
                  actor_id='https://relay.example/actor', public_key=signing_peer.public_key,
                  follow_activity_id='https://test.piefed.local/activities/relay-follow/1', state=RELAY_ACCEPTED)
    db.session.add(relay)
    db.session.commit()
    response = _post(app, inbox_activity(signing_peer), signing_peer.private_key, 'https://relay.example/actor#main-key')
    assert response.status_code == 200
    assert dispatched[0][1] == {'relay_id': relay.id}
```

Check the `Relay(...)` constructor against `app/models.py:5758` and the `make_relay` helper in `tests/test_relays_inbound.py`. If `make_relay` is importable from a shared place, use it. Otherwise copy only the required columns.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./run_tests.sh tests/test_ap_security_key_id_host.py -q`
Expected: FAIL. `test_the_refusal_counts_as_a_signature_failure` fails its assertion, and `test_a_key_id_on_another_host_is_refused` gets 200.

- [ ] **Step 3: Implement**

`app/activitypub/routes.py`:
- add `request_key_id` to the signature import;
- add a line `from app.activitypub.util import host_of`, or add `host_of` to the existing util import at R:21;
- add to `SIGNATURE_FAILURE_MESSAGES` (R:880):

```python
SIGNATURE_FAILURE_MESSAGES = ('Precheck failed', 'Could not verify LD signature', 'Could not verify HTTP signature',
                              'Signature keyId host does not match actor')
```

Module-level helper, near `SIGNATURE_FAILURE_MESSAGES`:

```python
def _ld_signature_verifies(request_json, actor) -> bool:
    if 'signature' not in request_json:
        return False
    try:
        LDSignature.verify_signature(request_json, actor.public_key)
        return True
    except VerificationError:
        return False
```

In `shared_inbox`, replace the line `relay = relay_inbound.relay_for_forwarded(request) if bounced else None` (R:826) and place the new check **before** the Task 7 dedupe `set`:

```python
    relay = relay_inbound.relay_for_forwarded(request) if bounced else None   # relays: a Mastodon-style relay signs forwards
    # S2.2: the HTTP signature verified with the actor's key, but a keyId on another host is accepted only when the
    # body carries the actor's LD signature or an accepted relay made the HTTP signature
    if not bounced and host_of(request_key_id(request) or '') != host_of(actor.ap_profile_id):
        if not _ld_signature_verifies(request_json, actor):
            relay = relay_inbound.relay_for_forwarded(request)
            if relay is None:
                log_incoming_ap(id, APLOG_NOTYPE, APLOG_FAILURE, saved_json, 'Signature keyId host does not match actor')
                return '', 400
```

Then the Task 7 block `if not app_pkg.redis_client.set(id, ...)` follows. Move it below this block so the order is: signature try/except, relay/keyId block, dedupe set, instance bookkeeping.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./run_tests.sh tests/test_ap_security_key_id_host.py tests/test_inbox_gate_signatures.py tests/test_relays_forwarded.py tests/test_relays_inbound.py tests/test_ap_security_dedupe.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/activitypub/routes.py tests/test_ap_security_key_id_host.py
git commit -m "fix(ap): refuse a signature whose keyId names another host

The inbox verified against the body actor's key and ignored the keyId.
A keyId on a different host is now accepted only with the actor's LD
signature or an accepted relay's HTTP signature; otherwise 400, logged
as a signature failure so admin replay refuses it.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 6: Phase S2 gate**

Run, in order:
1. `./run_tests.sh tests/ --cov=app --cov=fastapi_server --cov-branch --cov-report=json:coverage.json -q`. Expected: all pass. Read the passed count.
2. `python3 tests/check_changed_line_coverage.py coverage.json dafbb3aa0 --branches app/ fastapi_server.py`. Expected: 100% of changed lines and branches.
3. `python3 tests/check_coverage_floors.py coverage.json coverage_floors.ini`. Expected: no floor dropped (`signature.py = 100`).

Fix any gap before Phase S3.

---


## Phase S3: fetch surface

### Task 10: `/actor/outbox` (S3.1)

**Files:**
- Modify: `app/main/routes.py` (import line 61; new route after `instance_actor`, ~line 1333)
- Test: `tests/test_ap_security_actor_outbox.py` (new)

**Interfaces:**
- Consumes: `paged_outbox(outbox_id: str, total: int, items_from) -> dict` and `AP_CACHE_COLLECTION` from `app/activitypub/routes.py`.
- Produces: route `GET /actor/outbox`, endpoint `main.instance_actor_outbox`. Task 24 lists `/actor` (prefix) as exempt from secure mode.

- [ ] **Step 1: Write the failing test**

```python
"""S3.1: the instance actor's advertised outbox exists (spec 2026-10-10-ap-security-track-design.md)."""
from tests.factories import make_site

AP = {'Accept': 'application/activity+json'}


def test_instance_actor_outbox_is_an_empty_ordered_collection(app, db_session):
    make_site()
    with app.test_client() as client:
        response = client.get('/actor/outbox', headers=AP)

    assert response.status_code == 200
    assert response.content_type == 'application/activity+json'
    assert response.headers['Cache-Control'] == 'public, max-age=60'
    body = response.json
    assert body['type'] == 'OrderedCollection'
    assert body['id'] == 'https://test.piefed.local/actor/outbox'
    assert body['totalItems'] == 0
    assert body['orderedItems'] == []


def test_instance_actor_outbox_matches_what_the_actor_document_advertises(app, db_session):
    make_site()
    with app.test_client() as client:
        actor = client.get('/actor', headers=AP).json
        outbox = client.get('/actor/outbox', headers=AP).json

    assert actor['outbox'] == outbox['id']


def test_instance_actor_outbox_pages_like_every_other_outbox(app, db_session):
    make_site()
    with app.test_client() as client:
        page = client.get('/actor/outbox?page=1', headers=AP)
        bad = client.get('/actor/outbox?page=x', headers=AP)

    assert page.status_code == 200
    assert page.json['type'] == 'OrderedCollectionPage'
    assert page.json['partOf'] == 'https://test.piefed.local/actor/outbox'
    assert page.json['orderedItems'] == []
    assert 'next' not in page.json
    assert bad.status_code == 400
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./run_tests.sh tests/test_ap_security_actor_outbox.py -q`
Expected: FAIL. `/actor/outbox` has no route, so the first and third tests get 404 instead of 200, and the second gets `KeyError: 'id'` on the 404 body.

- [ ] **Step 3: Write minimal implementation**

In `app/main/routes.py`, extend the existing top-level import (line 61):

```python
from app.activitypub.routes import replay_inbox_request, paged_outbox, AP_CACHE_COLLECTION
```

Add directly after `instance_actor()` (after its `return resp`):

```python
@bp.route('/actor/outbox', methods=['GET'])
def instance_actor_outbox():
    """The outbox /actor advertises (S3.1). The instance actor publishes nothing, so it is empty, but it exists:
    peers that dereference an advertised outbox got 404 before."""
    outbox = paged_outbox(f"{current_app.config['SERVER_URL']}/actor/outbox", 0, lambda offset, limit: [])
    resp = jsonify(outbox)
    resp.content_type = 'application/activity+json'
    resp.headers.set('Cache-Control', AP_CACHE_COLLECTION)
    return resp
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./run_tests.sh tests/test_ap_security_actor_outbox.py tests/test_no_inline_imports.py tests/test_import_order.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/main/routes.py tests/test_ap_security_actor_outbox.py
git commit -m "fix: serve the instance actor's advertised /actor/outbox

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 11: 410 Tombstone for deleted local users (S3.2)

**Files:**
- Modify: `app/activitypub/routes.py`, `user_profile` (R:436-525). Add a lookup in the local (`else:`) branch after the two `User.query` lookups (R:451-457).
- Test: `tests/test_ap_security_tombstones.py` (new)

**Interfaces:**
- Consumes: `tombstone_response(ap_id: str, former_type: str)` (R:2474), unchanged.
- Produces: an AP GET or HEAD of `/u/<name>` for a local user with `deleted=True` returns 410 Tombstone. Banned-but-not-deleted local users and all communities are unchanged (404).

Note: account deletion sets **both** `banned=True` and `deleted=True` (`app/user/routes.py:1175-1176`, `:1306-1307`). So "deleted" is the deciding column, and `banned` is ignored when `deleted` is set.

- [ ] **Step 1: Write the failing test**

```python
"""S3.2: deleted local actors answer 410 Tombstone; bans stay a reversible 404 (spec 2026-10-10)."""
from app import db
from tests.factories import make_community, make_user
from tests.test_actor_profiles import seed_actors
from tests.test_ap_content_objects import ap_get, browser_get


def _local_user(name, *, deleted=False, banned=False, bot=False):
    seed_actors('test.piefed.local')
    user = make_user(None, name, local=True)
    user.deleted = deleted
    user.banned = banned
    user.bot = bot
    db.session.commit()
    return user


def test_a_deleted_local_user_is_a_person_tombstone(app, db_session):
    _local_user('gone', deleted=True, banned=True)   # what account deletion writes

    response = ap_get(app, '/u/gone')

    assert response.status_code == 410
    assert response.content_type == 'application/activity+json'
    assert response.headers['Cache-Control'] == 'no-store'
    assert response.json['type'] == 'Tombstone'
    assert response.json['formerType'] == 'Person'
    assert response.json['id'] == 'https://test.piefed.local/u/gone'


def test_the_tombstone_lookup_ignores_the_case_of_the_name(app, db_session):
    _local_user('gone', deleted=True, banned=True)

    assert ap_get(app, '/u/GONE').status_code == 410


def test_a_deleted_local_bot_is_a_service_tombstone(app, db_session):
    _local_user('gonebot', deleted=True, banned=True, bot=True)

    response = ap_get(app, '/u/gonebot')

    assert response.status_code == 410
    assert response.json['formerType'] == 'Service'


def test_a_banned_local_user_stays_404_so_the_ban_is_reversible(app, db_session):
    _local_user('benched', banned=True)

    assert ap_get(app, '/u/benched').status_code == 404


def test_a_browser_asking_for_a_deleted_user_is_unchanged(app, db_session):
    _local_user('gone', deleted=True, banned=True)

    assert browser_get(app, '/u/gone').status_code == 404


def test_an_unknown_local_name_is_still_404(app, db_session):
    seed_actors('test.piefed.local')

    assert ap_get(app, '/u/nobody').status_code == 404


def test_a_deleted_or_banned_community_still_answers_404(app, db_session):
    """Local community deletion sets banned=True and stays restorable for 7 days (app/shared/community.py:653),
    so a 410 would tell peers to purge a community that may come back."""
    seed_actors('test.piefed.local')
    community = make_community(name='dead')
    community.banned = True
    db.session.commit()

    assert ap_get(app, '/c/dead').status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./run_tests.sh tests/test_ap_security_tombstones.py -q`
Expected: three tests FAIL: `test_a_deleted_local_user_is_a_person_tombstone`, `test_the_tombstone_lookup_ignores_the_case_of_the_name` and `test_a_deleted_local_bot_is_a_service_tombstone`, each with `assert 404 == 410`. The other four PASS, because they pin unchanged behaviour.

- [ ] **Step 3: Write minimal implementation**

In `user_profile`'s local `else:` branch, directly after the second lookup (the `if user is None: user = User.query.filter_by(ap_profile_id=...)` block, R:455-457), still inside `else:`:

```python
        if user is None and is_activitypub_request():
            # S3.2: a deleted account is gone for good, so peers are told to purge it. A ban (banned, not deleted)
            # stays a 404 -- it can be lifted. Account deletion sets both flags, so `deleted` decides.
            gone = User.query.filter(func.lower(User.user_name) == actor.lower(), User.ap_id.is_(None),
                                     User.deleted.is_(True)).first()
            if gone is not None:
                return tombstone_response(gone.public_url(), 'Service' if gone.bot else 'Person')
```

`func` is already imported in R (used at R:452). `tombstone_response` is defined later in the same module, which is fine at call time.

- [ ] **Step 4: Run test to verify it passes**

Run: `./run_tests.sh tests/test_ap_security_tombstones.py tests/test_actor_profiles.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/activitypub/routes.py tests/test_ap_security_tombstones.py
git commit -m "fix: a deleted local user answers ActivityPub with a 410 Tombstone

Banned-only users and communities keep 404 so a ban stays reversible.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 12: real `formerType` for post Tombstones (S3.3)

**Files:**
- Modify: `app/activitypub/routes.py`. Add `former_type_of_post` next to `tombstone_response` (R:2474), and use it in `post_ap_refusal` (R:2509).
- Test: `tests/test_ap_security_former_type.py` (new)

**Interfaces:**
- Consumes: `post_to_page(post) -> dict` (U:155), already imported into R.
- Produces: `former_type_of_post(post: Post) -> str` in `app/activitypub/routes.py`. It returns the `type` `post_to_page` emits: `'Page'`, `'Question'` or `'Event'`. Comments keep `'Note'` (R:2455), which is already their real type.

- [ ] **Step 1: Write the failing test**

```python
"""S3.3: a deleted post's Tombstone names the type the post was federated as (spec 2026-10-10)."""
from datetime import timedelta

from app import db
from app.constants import POST_TYPE_EVENT, POST_TYPE_POLL
from app.models import Event
from app.utils import utcnow
from tests.factories import make_poll, make_post_reply
from tests.test_ap_content_objects import ap_get, seed_local_post


def _deleted_post(post_type=None):
    community, author, post = seed_local_post()
    post.ap_id = f'https://test.piefed.local/post/{post.id}'
    if post_type is not None:
        post.type = post_type
    post.deleted = True
    db.session.commit()
    return post


def test_a_deleted_plain_post_is_a_page_tombstone(app, db_session):
    post = _deleted_post()

    response = ap_get(app, f'/post/{post.id}')

    assert response.status_code == 410
    assert response.json['type'] == 'Tombstone'
    assert response.json['formerType'] == 'Page'
    assert response.json['id'] == post.ap_id


def test_a_deleted_poll_is_a_question_tombstone(app, db_session):
    post = _deleted_post(POST_TYPE_POLL)
    make_poll(post, end_poll=utcnow() + timedelta(days=1))

    assert ap_get(app, f'/post/{post.id}').json['formerType'] == 'Question'


def test_a_deleted_poll_with_no_end_was_federated_as_a_page(app, db_session):
    """post_to_page sends a poll without end_poll as a Page (D1330), so its Tombstone says Page."""
    post = _deleted_post(POST_TYPE_POLL)
    make_poll(post)

    assert ap_get(app, f'/post/{post.id}').json['formerType'] == 'Page'


def test_a_deleted_event_is_an_event_tombstone(app, db_session):
    post = _deleted_post(POST_TYPE_EVENT)
    db.session.add(Event(post_id=post.id, start=utcnow() + timedelta(days=2)))
    db.session.commit()

    assert ap_get(app, f'/post/{post.id}').json['formerType'] == 'Event'


def test_a_deleted_comment_is_still_a_note_tombstone(app, db_session):
    community, author, post = seed_local_post()
    reply = make_post_reply(post, author)
    reply.deleted = True
    db.session.commit()

    response = ap_get(app, f'/comment/{reply.id}')

    assert response.status_code == 410
    assert response.json['formerType'] == 'Note'
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./run_tests.sh tests/test_ap_security_former_type.py -q`
Expected: `test_a_deleted_poll_is_a_question_tombstone` and `test_a_deleted_event_is_an_event_tombstone` FAIL with `assert 'Page' == 'Question'` and `assert 'Page' == 'Event'`. The other three PASS.

- [ ] **Step 3: Write minimal implementation**

In `app/activitypub/routes.py`, directly above `tombstone_response`:

```python
def former_type_of_post(post: Post) -> str:
    """The ActivityStreams type `post` was federated as, for its Tombstone's formerType (S3.3). Delegates to
    post_to_page so the two can never disagree: a poll without an end time or an event without a start goes out
    as a Page, and its Tombstone says so."""
    return post_to_page(post)['type']
```

In `post_ap_refusal`, replace

```python
    if post.deleted:
        return tombstone_response(post.ap_id, 'Page')
```

with

```python
    if post.deleted:
        return tombstone_response(post.ap_id, former_type_of_post(post))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./run_tests.sh tests/test_ap_security_former_type.py tests/test_ap_content_objects.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/activitypub/routes.py tests/test_ap_security_former_type.py
git commit -m "fix: a deleted post's Tombstone carries the type it was federated as

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 13: community outbox refuses private and local-only communities (S3.4)

**Files:**
- Modify: `app/activitypub/routes.py`, `community_outbox` (R:2297-2321)
- Test: `tests/test_ap_security_community_outbox.py` (new)

**Interfaces:**
- Consumes: none new.
- Produces: `GET /c/<name>/outbox` (AP) returns 403 when `community.private` or `community.local_only` is set, the same as the community actor document (R:602-603).

- [ ] **Step 1: Write the failing test**

```python
"""S3.4: the community outbox is gated like the community actor document (spec 2026-10-10)."""
import pytest

from app import db
from tests.factories import make_community
from tests.test_actor_profiles import seed_actors
from tests.test_ap_content_objects import ap_get


@pytest.mark.parametrize('flag', ['private', 'local_only'])
def test_a_non_federating_community_outbox_is_403(app, db_session, flag):
    seed_actors('test.piefed.local')
    community = make_community(name='hidden')
    setattr(community, flag, True)
    db.session.commit()

    assert ap_get(app, '/c/hidden/outbox').status_code == 403
    assert ap_get(app, '/c/hidden').status_code == 403   # the actor document already agreed


def test_an_ordinary_community_outbox_is_still_served(app, db_session):
    seed_actors('test.piefed.local')
    make_community(name='open')

    response = ap_get(app, '/c/open/outbox')

    assert response.status_code == 200
    assert response.json['type'] == 'OrderedCollection'
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./run_tests.sh tests/test_ap_security_community_outbox.py -q`
Expected: both parametrized cases FAIL with `assert 200 == 403`. The ordinary-community test PASSES.

- [ ] **Step 3: Write minimal implementation**

In `community_outbox`, directly after `if community is not None:`:

```python
    if community is not None:
        if community.local_only or community.private:
            abort(403)   # S3.4: as the community actor document (community_profile); these never federate
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./run_tests.sh tests/test_ap_security_community_outbox.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/activitypub/routes.py tests/test_ap_security_community_outbox.py
git commit -m "fix: the community outbox refuses private and local-only communities

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 14: dereferenceable `/private_message/<id>` (S3.5), plus S3 phase gate

**Files:**
- Modify: `app/chat/util.py`. Add `chat_message_ap_object`, and use it in `update_message`.
- Modify: `app/activitypub/routes.py`. New route `private_message_ap`, plus imports.
- Test: `tests/test_ap_security_private_message.py` (new)

**Interfaces:**
- Consumes (Task 2, `app/activitypub/entitlement.py`):
  - `signed_requester_actor() -> User | Community | None`
  - `audience_entitles(addressees: list[str], requester, local_actor=None) -> bool`
  - `PRIVATE_HEADERS`
- Produces:
  - `chat_message_ap_object(reply: ChatMessage, recipient: User) -> dict` in `app/chat/util.py`. It returns the federated object: `ChatMessage` for a Lemmy or Mbin recipient, otherwise `Note`. The Mention tag is included except for Lemmy and PieFed recipients, as `send_message` does.
  - Route `GET|HEAD /private_message/<int:message_id>`, endpoint `activitypub.private_message_ap`.

- [ ] **Step 1: Write the failing test**

```python
"""S3.5: a DM's id is dereferenceable, but only by the parties to it (spec 2026-10-10)."""
from app import db
from tests.factories import make_chat_message, make_conversation, make_instance, make_user
from tests.test_actor_profiles import seed_actors
from tests.test_ap_content_objects import ap_get, browser_get, signed_ap_get

PM_PREFIX = 'https://test.piefed.local/private_message/'


def _dm(software='lemmy', deleted=False, local_origin=True):
    """A local alice -> remote bob DM, as send_message stores it. bob and dave share bob's host."""
    seed_actors('test.piefed.local')
    peer = make_instance('peer.example', software=software)
    alice = make_user(None, 'alice', local=True)
    bob = make_user(peer, 'bob', with_keys=True)
    dave = make_user(peer, 'dave', with_keys=True)
    conversation = make_conversation(alice, bob)
    message = make_chat_message(alice, bob, 'pending', body='hi bob', deleted=deleted)
    message.body_html = '<p>hi bob</p>'
    message.conversation_id = conversation.id
    message.ap_id = f'{PM_PREFIX}{message.id}' if local_origin else 'https://peer.example/pm/1'
    db.session.commit()
    return message, conversation, bob, dave


def test_the_recipient_can_fetch_the_message(app, db_session):
    message, conversation, bob, dave = _dm()

    response = signed_ap_get(app, f'/private_message/{message.id}', bob, 'Test')

    assert response.status_code == 200
    assert response.content_type == 'application/activity+json'
    assert response.headers['Cache-Control'] == 'private, no-store'
    assert response.headers['Vary'] == 'Accept, Signature'
    body = response.json
    assert body['id'] == message.ap_id
    assert body['type'] == 'ChatMessage'             # lemmy recipient, as send_message federates it
    assert body['attributedTo'] == 'https://test.piefed.local/u/alice'
    assert body['to'] == ['https://peer.example/users/bob']
    assert body['content'] == '<p>hi bob</p>'
    assert '@context' in body


def test_a_mastodon_recipient_gets_a_note_with_a_mention(app, db_session):
    message, conversation, bob, dave = _dm(software='mastodon')

    body = signed_ap_get(app, f'/private_message/{message.id}', bob, 'Test').json

    assert body['type'] == 'Note'
    assert body['tag'][0]['type'] == 'Mention'
    assert body['tag'][0]['href'] == 'https://peer.example/users/bob'


def test_a_signer_on_the_recipients_host_can_fetch_the_message(app, db_session):
    """The recipient's host already received it through its shared inbox (host rule 3)."""
    message, conversation, bob, dave = _dm()

    assert signed_ap_get(app, f'/private_message/{message.id}', dave, 'Test').status_code == 200


def test_a_signer_on_an_unrelated_host_gets_404(app, db_session):
    message, conversation, bob, dave = _dm()
    stranger = make_user(make_instance('other.example'), 'eve', with_keys=True)

    response = signed_ap_get(app, f'/private_message/{message.id}', stranger, 'Test')

    assert response.status_code == 404
    assert response.headers['Cache-Control'] == 'no-store'


def test_an_anonymous_fetch_gets_404(app, db_session):
    message, conversation, bob, dave = _dm()

    response = ap_get(app, f'/private_message/{message.id}')

    assert response.status_code == 404
    assert response.headers['Cache-Control'] == 'no-store'


def test_a_deleted_message_is_404_even_to_the_recipient(app, db_session):
    message, conversation, bob, dave = _dm(deleted=True)

    assert signed_ap_get(app, f'/private_message/{message.id}', bob, 'Test').status_code == 404


def test_a_message_that_did_not_originate_here_is_404(app, db_session):
    message, conversation, bob, dave = _dm(local_origin=False)

    assert signed_ap_get(app, f'/private_message/{message.id}', bob, 'Test').status_code == 404


def test_a_message_with_no_recipient_is_404(app, db_session):
    message, conversation, bob, dave = _dm()
    message.recipient_id = None
    db.session.commit()

    assert signed_ap_get(app, f'/private_message/{message.id}', bob, 'Test').status_code == 404


def test_a_missing_message_is_404(app, db_session):
    message, conversation, bob, dave = _dm()

    assert signed_ap_get(app, f'/private_message/{message.id + 999}', bob, 'Test').status_code == 404


def test_a_browser_is_sent_to_the_conversation(app, db_session):
    message, conversation, bob, dave = _dm()

    response = browser_get(app, f'/private_message/{message.id}')

    assert response.status_code == 302
    assert response.headers['Location'].endswith(f'/chat/{conversation.id}')


def test_a_browser_asking_for_a_message_without_a_conversation_gets_404(app, db_session):
    message, conversation, bob, dave = _dm()
    message.conversation_id = None
    db.session.commit()

    assert browser_get(app, f'/private_message/{message.id}').status_code == 404


def test_update_message_still_federates_the_same_object(app, db_session, monkeypatch):
    """update_message now builds its object with chat_message_ap_object; the pushed Update is unchanged."""
    from app.chat import util as chat_util
    message, conversation, bob, dave = _dm(software='mastodon')
    sent = []
    monkeypatch.setattr(chat_util, 'send_post_request', lambda inbox, body, key, key_id: sent.append(body))

    chat_util.update_message(message)

    obj = sent[0]['object']
    assert sent[0]['type'] == 'Update'
    assert obj['type'] == 'Note'
    assert obj['id'] == message.ap_id
    assert obj['to'] == ['https://peer.example/users/bob']
    assert obj['tag'][0]['href'] == 'https://peer.example/users/bob'
    assert obj['updated'].endswith('Z')
    assert obj['published'] == message.created_at.isoformat() + 'Z'
```

(The `from app.chat import util as chat_util` inside the last test is test code. The inline-import ratchet covers `app/` only. Hoist it to module level if the reviewer prefers.)

- [ ] **Step 2: Run test to verify it fails**

Run: `./run_tests.sh tests/test_ap_security_private_message.py -q`
Expected: FAIL.
- The route tests get 404 where they expect 200 or 302, because no route matches. `test_the_recipient_can_fetch_the_message` fails with `assert 404 == 200`. The browser test fails with `assert 404 == 302`.
- The 404-expecting tests pass by accident. That is acceptable: each of them is re-proven against the real route in Step 4.
- `test_update_message_still_federates_the_same_object` PASSES. It pins current behaviour before the refactor.

- [ ] **Step 3: Write minimal implementation**

In `app/chat/util.py`, add above `update_message`:

```python
def chat_message_ap_object(reply: ChatMessage, recipient: User) -> dict:
    """The ActivityPub object a DM is federated as: what update_message pushes and what /private_message/<id>
    serves (S3.5), so a fetched message and a pushed one cannot differ."""
    ap_type = "ChatMessage" if recipient.instance.software in ("lemmy", "mbin") else "Note"
    obj = {
        "attributedTo": reply.sender.public_url(),
        "content": reply.body_html,
        "id": reply.ap_id,
        "mediaType": "text/html",
        "published": reply.created_at.isoformat() + 'Z',
        "to": [recipient.public_url()],
        "type": ap_type
    }
    if recipient.instance.software != "lemmy" and recipient.instance.software != "piefed":
        obj['tag'] = [
            {
                "href": recipient.public_url(),
                "name": recipient.mention_tag(),
                "type": "Mention"
            }
        ]
    return obj
```

Replace the `else:` branch of `update_message` (from `if recipient.instance.software == "lemmy" ...` through the `tag` block) with:

```python
    else:
        # Federate reply
        reply_json = {
            "actor": user.public_url(),
            "id": f"{current_app.config['SERVER_URL']}/activities/update/{gibberish(15)}",
            "object": {**chat_message_ap_object(reply, recipient), "updated": reply.edited_at.isoformat() + 'Z'},
            "to": [recipient.public_url()],
            # an edit is an Update, as app/shared/tasks/notes.py:187 and
            # app/shared/tasks/pages.py:252 both have it
            "type": "Update"
        }
        send_post_request(recipient.ap_inbox_url, reply_json, user.private_key,
                                  user.public_url() + '#main-key')
```

In `app/activitypub/routes.py`, add top-level imports:

```python
from app.activitypub.entitlement import signed_requester_actor, audience_entitles, PRIVATE_HEADERS
from app.chat.util import chat_message_ap_object
```

(Task 2 may already have added the entitlement import. Merge the names.) Add the route directly after `tombstone_response`:

```python
@bp.route('/private_message/<int:message_id>', methods=['GET', 'HEAD'])
def private_message_ap(message_id):
    """A DM's id (S3.5). Served only to a party to it: the recipient, or a signer on the recipient's host, which
    received it through its shared inbox. Anything else -- missing, deleted, not ours, not entitled -- is the
    same 404, so a refusal never confirms a message exists."""
    message = db.session.get(ChatMessage, message_id)
    ours = message is not None and message.ap_id == f"{current_app.config['SERVER_URL']}/private_message/{message.id}"
    if not is_activitypub_request():
        if not ours or message.conversation_id is None:
            abort(404)
        return redirect(url_for('chat.chat_home', conversation_id=message.conversation_id))
    if not ours or message.deleted or message.recipient_id is None:
        abort(404)
    recipient = db.session.get(User, message.recipient_id)
    addressees = [message.sender.public_url(), recipient.public_url()]
    if not audience_entitles(addressees, signed_requester_actor()):
        abort(404)
    document = chat_message_ap_object(message, recipient) if request.method == 'GET' else {}
    if request.method == 'GET':
        document['@context'] = default_context()
    resp = jsonify(document)
    resp.content_type = 'application/activity+json'
    resp.headers.update(PRIVATE_HEADERS)
    return resp
```

The blueprint's `ap_miss_is_not_cached` (R:101) already sets `Cache-Control: no-store` on every 404, which gives the `AP_CACHE_MISS` the spec asks for.

- [ ] **Step 4: Run test to verify it passes**

Run: `./run_tests.sh tests/test_ap_security_private_message.py tests/test_chat_util.py tests/test_api_private_messages.py tests/test_no_inline_imports.py tests/test_import_order.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/chat/util.py app/activitypub/routes.py tests/test_ap_security_private_message.py
git commit -m "feat: serve /private_message/<id> to the parties to a DM, 404 to everyone else

The fetched object is built by the same helper update_message federates with.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 6: S3 phase gate**

Run each command and read the passed-test count, not only the exit code:

1. `./run_tests.sh tests/ --cov=app --cov=fastapi_server --cov-branch --cov-report=json:coverage.json -q`
   Expected: all pass, and the count is not below the S2 gate's count plus this phase's new tests. A run that hits `session_timeout` still exits 0.
2. `python3 tests/check_changed_line_coverage.py coverage.json dafbb3aa0 --branches app/ fastapi_server.py`
   Expected: 100% of new and changed lines and branches.
3. `python3 tests/check_coverage_floors.py coverage.json coverage_floors.ini`
   Expected: no floor drops.

A failure blocks Task 15. Fix it in a commit of its own before moving on.

## Phase S4: Announce verification

### Task 15: S4 — same-origin trust and host consistency for Announced activities

**Files:**
- Modify: `app/activitypub/util.py` (add `_id_of` and `verify_announced_inner` after `verify_object_from_source`, around U:5553)
- Modify: `app/activitypub/routes.py:21-31` (import), `app/activitypub/routes.py:1019-1020` (hook, between the OrderedCollection arm and `if not feed:`)
- Test: `tests/test_ap_security_announce.py` (new)

**Interfaces:**
- Consumes: `host_of(url_string: str) -> str` (U:657); `log_incoming_ap`, `APLOG_ANNOUNCE`, `APLOG_FAILURE` (existing).
- Produces:
  - `verify_announced_inner(announce: dict) -> tuple[dict | None, str | None]` in `app/activitypub/util.py`.
    - It returns `(inner, None)` to process. `inner` may be a replaced copy; Tasks 16–17 make it so.
    - It returns `(None, reason)` to drop.
    - `announce['object']` must be a dict. The dispatcher guarantees this.
  - `_id_of(value) -> str` (module-private): the string itself, or a dict's string `id`, else `''`.
  - Drop reasons fixed by this task: `'Announced activity id has no host'` and `'Announced activity host does not match its actor'`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ap_security_announce.py`:

```python
"""Phase S4 of the security track: an activity a Group Announces from another host is not trusted on the Group's
signature alone. Spec: docs/superpowers/specs/2026-10-10-ap-security-track-design.md, "Phase S4".

verify_announced_inner is tested directly; the dispatcher tests prove the hook sits before the inner actor is
looked up, and that an Announced list gets the same check for every element.
"""
import uuid

import pytest

from app.activitypub.util import verify_announced_inner
from app.models import ActivityPubLog, User
from tests.test_inbox_dispatch_announce import _seed_announcing_community
from tests.test_inbox_dispatch_preamble import dispatch

GROUP = 'https://peer.example/c/microblogs'


def _announce(inner, group=GROUP):
    return {'id': f'{group}/activities/announce/{uuid.uuid4().hex}', 'type': 'Announce', 'actor': group,
            'object': inner}


def _like(activity_id, actor, object_uri='https://peer.example/post/1'):
    return {'id': activity_id, 'type': 'Like', 'actor': actor, 'object': object_uri}


def _messages(activity):
    return [log.exception_message for log in ActivityPubLog.query.filter_by(activity_id=activity['id']).all()]


def test_a_same_origin_inner_activity_is_trusted_on_the_groups_signature():
    inner = _like('https://peer.example/activities/like/1', 'https://elsewhere.example/u/bob')

    assert verify_announced_inner(_announce(inner)) == (inner, None)


@pytest.mark.parametrize('group, inner_id', [
    ('https://Peer.Example:443/c/microblogs', 'https://peer.example/activities/like/1'),
    ('https://peer.example/c/microblogs', 'https://PEER.example:443/activities/like/1'),
])
def test_hosts_differing_only_by_case_or_port_are_the_same_origin(group, inner_id):
    """Review Focus 3."""
    inner = _like(inner_id, 'https://elsewhere.example/u/bob')

    assert verify_announced_inner(_announce(inner, group=group)) == (inner, None)


def test_a_cross_origin_inner_activity_whose_actor_is_on_another_host_is_dropped():
    inner = _like('https://other.example/activities/like/1', 'https://third.example/u/mallory')

    assert verify_announced_inner(_announce(inner)) == (None, 'Announced activity host does not match its actor')


@pytest.mark.parametrize('actor', ['https://Other.Example:443/u/bob',
                                   {'id': 'https://other.example/u/bob', 'type': 'Person'}])
def test_a_cross_origin_vote_with_a_consistent_actor_is_trusted(actor):
    """The accepted residual: votes and other small activities get host consistency only."""
    inner = _like('https://other.example/activities/like/1', actor)

    assert verify_announced_inner(_announce(inner)) == (inner, None)


@pytest.mark.parametrize('actor', ['MISSING', 42, {'type': 'Person'}, 'not a url'])
def test_a_cross_origin_inner_activity_with_no_usable_actor_is_dropped(actor):
    inner = _like('https://other.example/activities/like/1', actor)
    if actor == 'MISSING':
        del inner['actor']

    assert verify_announced_inner(_announce(inner)) == (None, 'Announced activity host does not match its actor')


def test_an_inner_activity_without_an_id_is_left_to_the_dispatchers_own_refusal():
    """D127 (R:1036) refuses an id-less inner activity and logs it; this check must not change that message."""
    inner = {'type': 'Like', 'actor': 'https://third.example/u/mallory'}

    assert verify_announced_inner(_announce(inner)) == (inner, None)


def test_an_inner_activity_whose_id_has_no_host_is_dropped():
    inner = _like('urn:uuid:1234', 'https://other.example/u/bob')

    assert verify_announced_inner(_announce(inner)) == (None, 'Announced activity id has no host')


def test_the_dispatcher_drops_and_logs_an_inconsistent_announced_activity(app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    _instance, community = _seed_announcing_community()
    activity = _announce(_like('https://other.example/activities/like/1', 'https://third.example/u/mallory'),
                         group=community.ap_profile_id)

    dispatch(activity)

    assert _messages(activity) == ['Announced activity host does not match its actor']
    # The check runs before the inner actor is looked up: the spoofed actor was neither fetched
    # (block_outbound_http would have raised) nor stored.
    assert User.query.filter_by(ap_profile_id='https://third.example/u/mallory').first() is None


def test_every_element_of_an_announced_list_gets_the_same_check(app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    _instance, community = _seed_announcing_community()
    activity = _announce([_like('https://other.example/activities/like/1', 'https://third.example/u/mallory'),
                          _like('https://other.example/activities/like/2', 'https://fourth.example/u/trent')],
                         group=community.ap_profile_id)

    dispatch(activity)

    assert _messages(activity) == ['Announced activity host does not match its actor'] * 2
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./run_tests.sh tests/test_ap_security_announce.py -q`
Expected: collection ERROR, `ImportError: cannot import name 'verify_announced_inner' from 'app.activitypub.util'`.

- [ ] **Step 3: Implement the check in `app/activitypub/util.py`**

Add these directly after `verify_object_from_source`:

```python
def _id_of(value) -> str:
    """The id an activity field names: the string itself, or a dict's string 'id'; '' for anything else."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get('id'), str):
        return value['id']
    return ''


def verify_announced_inner(announce: dict) -> Tuple[Union[dict, None], Union[str, None]]:
    """Decide whether the activity a Group's Announce wraps may be processed (security track, phase S4).

    The Group's HTTP signature proves only that the Group sent the Announce. An inner activity published on the
    Group's own host is the Group's to vouch for; one from any other host must at least name an actor on that
    same host. Returns `(inner, None)` to process -- `inner` may be replaced by what its origin serves -- or
    `(None, reason)` to drop, the reason going into the incoming-activity log.

    An inner activity with no string id is passed through: the dispatcher refuses it itself (D127) and logs that.
    """
    inner = announce['object']
    inner_id = inner.get('id')
    if not isinstance(inner_id, str):
        return inner, None
    inner_host = host_of(inner_id)
    if not inner_host:
        return None, 'Announced activity id has no host'
    if inner_host == host_of(_id_of(announce['actor'])):
        return inner, None
    # inner_host is non-empty here, so an actor whose host cannot be parsed ('') can only compare unequal
    if host_of(_id_of(inner.get('actor'))) != inner_host:
        return None, 'Announced activity host does not match its actor'
    return inner, None
```

- [ ] **Step 4: Hook it into the dispatcher in `app/activitypub/routes.py`**

Add `verify_announced_inner` to the `from app.activitypub.util import ...` list at R:21-31. Put it after `verify_object_from_source`:

```python
    log_incoming_ap, find_community, site_ban_remove_data, community_ban_remove_data, verify_object_from_source, \
    verify_announced_inner, \
```

Insert this between the OrderedCollection arm's `return` (R:1019) and `if not feed:` (R:1020):

```python
                    # S4: an inner activity from another host is not trusted on the Group's signature alone. Checked
                    # before the inner actor is looked up, so a spoofed actor is never fetched or stored.
                    verified_inner, refusal = verify_announced_inner(request_json)
                    if verified_inner is None:
                        log_incoming_ap(id, APLOG_ANNOUNCE, APLOG_FAILURE, saved_json, refusal)
                        return
                    request_json['object'] = verified_inner
```

- [ ] **Step 5: Run the new tests and the existing inbox suites**

Run: `./run_tests.sh tests/test_ap_security_announce.py -q`
Expected: PASS (14 tests).

Run: `./run_tests.sh tests/test_inbox_dispatch_announce.py tests/test_inbox_gate_dispatch.py tests/test_inbox_dispatch_preamble.py tests/test_ap_federated_reports.py tests/test_podcast_person_semantics.py tests/test_podcast_dual_actor_lookups.py tests/test_relays_forwarded.py tests/test_no_inline_imports.py -q`
Expected: PASS.

A failure here means a fixture Announces an activity whose actor is on a different host from its id. Fix the fixture so its hosts agree, which is the shape real peers send. Do not weaken the check.

- [ ] **Step 6: Commit**

```bash
git add app/activitypub/util.py app/activitypub/routes.py tests/test_ap_security_announce.py
git commit -m "fix: an Announced activity from another host must name an actor on that host

A Group's signature vouched for everything it wrapped. An inner activity on the
Group's own host is still trusted; one from any other host is dropped unless its
actor is on the same host as its id. Checked before the inner actor is looked
up, so a spoofed actor is never fetched.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 16: S4 — cross-origin Create/Update: inner LD signature or origin refetch

**Files:**
- Modify: `app/activitypub/util.py` (imports at U:3-29; `verify_announced_inner`; new `_ld_signed_by_actor`, `_verify_announced_content`)
- Test: `tests/test_ap_security_announce.py` (append)

**Interfaces:**
- Consumes:
  - `verify_announced_inner` and `_id_of` (Task 15).
  - `verify_object_from_source(request_json) -> (dict | None, str | None)` (U:5462). It reads `request_json['actor']` and `request_json['object']` (a URI), and on success replaces `request_json['object']` with the fetched document.
  - `LDSignature.verify_signature(document, public_key)`, which pops `signature` and so **mutates** its argument.
  - `VerificationError` (a `BaseException` subclass).
  - `find_actor_or_create_cached(actor, create_if_not_found=False)`.
- Produces new drop reasons: `'Announced object has no id'` and `'Announced object is on a different host than its activity'`. `verify_object_from_source`'s own reasons pass through unchanged.

- [ ] **Step 1: Write the failing tests**

Add these imports at the top of `tests/test_ap_security_announce.py`, merged with the existing ones:

```python
import httpx

from app import db
from app.activitypub.signature import LDSignature, default_context
from app.models import ActivityPubLog, Post, User, utcnow
from tests.factories import a_keypair, make_instance, make_user, note_document, serve_remote_object
```

Append:

```python
CAROL = 'https://other.example/users/carol'
OBJECT_ID = 'https://other.example/objects/1'


def _carol():
    """A remote author stored here with a real keypair; ap_fetched_at stops an inline profile refresh."""
    author = make_user(make_instance('other.example'), 'carol', with_keys=True)
    author.ap_fetched_at = utcnow()
    db.session.commit()
    assert author.ap_profile_id == CAROL
    return author


def _create(actor=CAROL, obj=None, activity_type='Create'):
    return {'@context': default_context(), 'id': f'https://other.example/activities/{activity_type.lower()}/1',
            'type': activity_type, 'actor': actor,
            'object': obj if obj is not None else note_document(attributed_to=actor, uri=OBJECT_ID)}


def _ld_sign(inner, private_key, actor=CAROL):
    inner['signature'] = LDSignature.create_signature(inner, private_key, f'{actor}#main-key')
    return inner


@pytest.mark.parametrize('activity_type', ['Create', 'Update'])
def test_a_valid_inner_ld_signature_is_accepted_without_a_fetch(app, db_session, http_mock, no_network_ld_signing,
                                                                 activity_type):
    """http_mock has no routes: any fetch is an unmatched request and fails the test."""
    carol = _carol()
    inner = _ld_sign(_create(activity_type=activity_type), carol.private_key)

    result, reason = verify_announced_inner(_announce(inner))

    assert reason is None
    assert result is inner
    assert 'signature' in inner    # verified on a copy: LDSignature.verify_signature pops it


@pytest.mark.parametrize('activity_type', ['Create', 'Update'])
def test_a_bad_inner_ld_signature_falls_back_to_the_origin_copy(app, db_session, http_mock, no_network_ld_signing,
                                                                activity_type):
    _carol()
    wrong_key, _public = a_keypair()
    inner = _ld_sign(_create(activity_type=activity_type), wrong_key)
    served = serve_remote_object(http_mock, uri=OBJECT_ID,
                                 document=note_document(attributed_to=CAROL, uri=OBJECT_ID,
                                                        fields={'content': 'what the origin says'}))

    result, reason = verify_announced_inner(_announce(inner))

    assert reason is None
    assert result['object'] == served
    assert result['id'] == inner['id']


def test_a_signature_by_an_actor_not_stored_here_is_not_trusted_and_fetches_no_actor(
        app, db_session, http_mock, no_network_ld_signing):
    stranger = 'https://other.example/users/nobody'
    key, _public = a_keypair()
    inner = _ld_sign(_create(actor=stranger), key, actor=stranger)
    served = serve_remote_object(http_mock, uri=OBJECT_ID, document=note_document(attributed_to=stranger, uri=OBJECT_ID))

    result, reason = verify_announced_inner(_announce(inner))

    assert reason is None
    assert result['object'] == served


def test_an_unsigned_inner_activity_naming_its_object_by_uri_is_checked_against_the_origin(app, db_session, http_mock):
    served = serve_remote_object(http_mock, uri=OBJECT_ID, document=note_document(attributed_to=CAROL, uri=OBJECT_ID))

    result, reason = verify_announced_inner(_announce(_create(obj=OBJECT_ID)))

    assert reason is None
    assert result['object'] == served


def test_an_object_on_another_host_than_its_activity_is_dropped(app, db_session):
    inner = _create(obj=note_document(attributed_to=CAROL, uri='https://elsewhere.example/objects/1'))

    assert verify_announced_inner(_announce(inner)) == (None, 'Announced object is on a different host than its activity')


@pytest.mark.parametrize('obj', [{'type': 'Note', 'content': 'no id'}, 42])
def test_an_inner_activity_whose_object_has_no_id_is_dropped(app, db_session, obj):
    assert verify_announced_inner(_announce(_create(obj=obj))) == (None, 'Announced object has no id')


def test_an_origin_copy_attributed_to_another_host_is_dropped(app, db_session, http_mock):
    serve_remote_object(http_mock, uri=OBJECT_ID,
                        document=note_document(attributed_to='https://elsewhere.example/u/x', uri=OBJECT_ID))

    assert verify_announced_inner(_announce(_create())) == (
        None, 'the fetched object is attributed to a different host than its URI')


def test_an_unreachable_origin_drops_the_activity(app, db_session, http_mock):
    http_mock.get(OBJECT_ID).mock(side_effect=httpx.ConnectError('down'))

    assert verify_announced_inner(_announce(_create())) == (None, 'the object could not be fetched')


def test_the_dispatcher_drops_and_logs_a_cross_origin_create_its_origin_disowns(app, db_session, monkeypatch, http_mock):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    _instance, community = _seed_announcing_community()
    serve_remote_object(http_mock, uri=OBJECT_ID, document={}, status=404)
    activity = _announce(_create(), group=community.ap_profile_id)

    dispatch(activity)

    assert _messages(activity) == ['the object fetch returned HTTP 404']
    assert Post.query.count() == 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./run_tests.sh tests/test_ap_security_announce.py -q`
Expected: the new tests FAIL. For example, `test_an_object_on_another_host_than_its_activity_is_dropped` fails with `assert (inner, None) == (None, 'Announced object is on a different host than its activity')`, and the refetch tests fail at teardown with respx `AllCalledAssertionError` because the registered route was never called. The 14 Task 15 tests still pass.

- [ ] **Step 3: Implement it in `app/activitypub/util.py`**

Add `import copy` with the stdlib imports at the top (U:3-7). Extend the signature import at U:29:

```python
from app.activitypub.signature import signed_get_request, send_post_request, default_context, RsaKeys, \
    LDSignature, VerificationError
```

Add these two helpers above `verify_announced_inner`:

```python
def _ld_signed_by_actor(inner: dict) -> bool:
    """True when `inner` carries an LD signature that verifies against its actor's stored key.

    Only an actor already stored here is used, so a forged signature never makes this instance fetch an actor.
    Verified on a copy: LDSignature.verify_signature pops 'signature' from what it is given."""
    actor = find_actor_or_create_cached(_id_of(inner.get('actor')), create_if_not_found=False)
    if not isinstance(actor, User) or not actor.public_key:
        return False
    try:
        LDSignature.verify_signature(copy.deepcopy(inner), actor.public_key)
    except VerificationError:
        return False
    return True


def _verify_announced_content(inner: dict, inner_host: str) -> Tuple[Union[dict, None], Union[str, None]]:
    """A cross-origin Announced Create or Update: its object must be on the activity's host, and is then trusted
    on a valid LD signature by the activity's actor, or replaced by the copy its origin serves."""
    object_id = _id_of(inner.get('object'))
    if not object_id:
        return None, 'Announced object has no id'
    if host_of(object_id) != inner_host:
        return None, 'Announced object is on a different host than its activity'
    if 'signature' in inner and _ld_signed_by_actor(inner):
        return inner, None
    verified, reason = verify_object_from_source({'actor': _id_of(inner.get('actor')), 'object': object_id})
    if verified is None:
        return None, reason
    return {**inner, 'object': verified['object']}, None
```

In `verify_announced_inner`, replace the final `return inner, None` with:

```python
    if inner.get('type') in ('Create', 'Update'):
        return _verify_announced_content(inner, inner_host)
    return inner, None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./run_tests.sh tests/test_ap_security_announce.py tests/test_ap_verify_object_from_source.py tests/test_inbox_dispatch_announce.py tests/test_no_inline_imports.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/activitypub/util.py tests/test_ap_security_announce.py
git commit -m "fix: a cross-origin Announced Create or Update is verified before it is stored

The inner object must be on the activity's host, and is then trusted on a valid
LD signature by the activity's actor, or replaced by the copy its origin serves.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 17: S4 — cross-origin Delete: the origin must no longer serve the object

**Files:**
- Modify: `app/activitypub/util.py` (new `_verify_announced_delete`; `verify_announced_inner`)
- Test: `tests/test_ap_security_announce.py` (append)

**Interfaces:**
- Consumes: `verify_announced_inner` and `_id_of` (Tasks 15–16). It also consumes `get_request(uri, headers=...)` (`app/utils.py:140`), which turns transport problems into `httpx.HTTPError`. It retries internally only on `httpx.ReadError`.
- Produces drop reasons `'Announced Delete names no object'`, `'Announced Delete of an object that still exists'`, `'Announced Delete: the object could not be fetched'` and `'Announced Delete: the object answers HTTP <status>'`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ap_security_announce.py`:

```python
DELETED_ID = 'https://other.example/objects/1'


def _delete(obj=DELETED_ID):
    return {'id': 'https://other.example/activities/delete/1', 'type': 'Delete', 'actor': CAROL, 'object': obj}


@pytest.mark.parametrize('status', [404, 410])
def test_an_announced_delete_is_processed_once_the_origin_no_longer_serves_the_object(app, db_session, http_mock,
                                                                                     status):
    http_mock.get(DELETED_ID).respond(status)
    inner = _delete()

    assert verify_announced_inner(_announce(inner)) == (inner, None)


def test_an_announced_delete_may_name_its_object_by_tombstone(app, db_session, http_mock):
    http_mock.get(DELETED_ID).respond(410)
    inner = _delete(obj={'id': DELETED_ID, 'type': 'Tombstone'})

    assert verify_announced_inner(_announce(inner)) == (inner, None)


def test_an_announced_delete_of_an_object_the_origin_still_serves_is_dropped(app, db_session, http_mock):
    http_mock.get(DELETED_ID).respond(200, json=note_document(attributed_to=CAROL, uri=DELETED_ID))

    assert verify_announced_inner(_announce(_delete())) == (None, 'Announced Delete of an object that still exists')


@pytest.mark.parametrize('status', [401, 500])
def test_any_other_origin_answer_drops_the_delete(app, db_session, http_mock, status):
    http_mock.get(DELETED_ID).respond(status)

    assert verify_announced_inner(_announce(_delete())) == (None, f'Announced Delete: the object answers HTTP {status}')


def test_an_unreachable_origin_drops_the_delete_without_a_retry(app, db_session, http_mock):
    """D775: no retry on inbox processing."""
    route = http_mock.get(DELETED_ID).mock(side_effect=httpx.ConnectError('down'))

    assert verify_announced_inner(_announce(_delete())) == (None, 'Announced Delete: the object could not be fetched')
    assert route.call_count == 1


@pytest.mark.parametrize('obj', ['MISSING', {'type': 'Tombstone'}, 42])
def test_an_announced_delete_naming_no_object_is_dropped(app, db_session, obj):
    inner = _delete()
    if obj == 'MISSING':
        del inner['object']
    else:
        inner['object'] = obj

    assert verify_announced_inner(_announce(inner)) == (None, 'Announced Delete names no object')


def test_a_same_origin_delete_is_not_fetched(app, db_session, http_mock):
    """http_mock has no routes: a fetch would be an unmatched request."""
    inner = _delete()

    assert verify_announced_inner(_announce(inner, group='https://other.example/c/news')) == (inner, None)


def test_the_dispatcher_drops_and_logs_a_delete_of_an_object_still_served(app, db_session, monkeypatch, http_mock):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    _instance, community = _seed_announcing_community()
    http_mock.get(DELETED_ID).respond(200, json=note_document(attributed_to=CAROL, uri=DELETED_ID))
    activity = _announce(_delete(), group=community.ap_profile_id)

    dispatch(activity)

    assert _messages(activity) == ['Announced Delete of an object that still exists']
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./run_tests.sh tests/test_ap_security_announce.py -q`
Expected: the new tests FAIL. For example, `test_an_announced_delete_of_an_object_the_origin_still_serves_is_dropped` fails with `assert (inner, None) == (None, ...)`, and the routed tests fail at teardown with respx `AllCalledAssertionError`. The Task 15 and 16 tests still pass.

- [ ] **Step 3: Implement it in `app/activitypub/util.py`**

Add above `verify_announced_inner`:

```python
def _verify_announced_delete(inner: dict) -> Tuple[Union[dict, None], Union[str, None]]:
    """A cross-origin Announced Delete is processed only once the object's origin no longer serves it (404 or 410).

    One fetch and no retry (D775): an unreachable origin, or any other answer, drops the Delete."""
    object_id = _id_of(inner.get('object'))
    if not object_id:
        return None, 'Announced Delete names no object'
    try:
        response = get_request(object_id, headers={'Accept': 'application/activity+json'})
    except httpx.HTTPError:
        return None, 'Announced Delete: the object could not be fetched'
    status = response.status_code
    response.close()
    if status in (404, 410):
        return inner, None
    if status == 200:
        return None, 'Announced Delete of an object that still exists'
    return None, f'Announced Delete: the object answers HTTP {status}'
```

In `verify_announced_inner`, before the final `return inner, None`, add:

```python
    if inner.get('type') == 'Delete':
        return _verify_announced_delete(inner)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./run_tests.sh tests/test_ap_security_announce.py tests/test_inbox_dispatch_announce.py tests/test_no_inline_imports.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/activitypub/util.py tests/test_ap_security_announce.py
git commit -m "fix: a cross-origin Announced Delete needs the object gone at its origin

The origin must answer 404 or 410. A 200, any other answer, or an unreachable
origin drops the Delete, with one fetch and no retry.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 6: Phase S4 gate**

Run: `./run_tests.sh tests/ --cov=app --cov=fastapi_server --cov-branch --cov-report=json:coverage.json -q`
Expected: all tests pass. Read the passed count: a session-budget stop still exits 0.

Run: `python3 tests/check_changed_line_coverage.py coverage.json dafbb3aa0 --branches app/ fastapi_server.py`
Expected: 100% of changed lines and branches.

Run: `python3 tests/check_coverage_floors.py coverage.json coverage_floors.ini`
Expected: no floor dropped.

---


## Phase S5: RFC 9421 signatures

### Task 18: RFC 9421 signature module

**Files:**
- Create: `app/activitypub/signature_errors.py`
- Modify: `app/activitypub/signature.py:208-225` (replace the two class definitions with a re-export)
- Create: `app/activitypub/signature_rfc9421.py`
- Create: `tests/fixtures/rfc9421/appendix_b.json` (copied verbatim from the RFC)
- Modify: `coverage_floors.ini` (two new floors of 100)
- Test: `tests/test_ap_security_rfc9421.py` (new)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces (`app/activitypub/signature_rfc9421.py`):
  - `RFC9421_POST_REQUIRED`, `RFC9421_GET_REQUIRED` (see Plan clarification 5);
  - `content_digest(body: bytes) -> str`;
  - `check_content_digest(headers: Mapping[str, str], body: bytes) -> None`;
  - `parse_dictionary(text: str) -> dict[str, tuple[value, params: dict, raw: str]]`;
  - `signature_base(covered: list[str], params: str, method: str, url: str, headers: Mapping[str, str]) -> str`;
  - `normalise_covered(covered) -> frozenset[str]`;
  - `sign(method: str, url: str, body: bytes | None, private_key_pem: str, key_id: str, created: int | None = None) -> dict[str, str]`;
  - `verify(method, url, headers, body, public_key_pem, required=frozenset(), *, now=None) -> frozenset[str]`;
  - `parse_key_id(headers) -> str | None`.

  Errors are raised as `VerificationError`/`VerificationFormatError` from `app/activitypub/signature_errors.py`.

- [ ] **Step 1: Copy the RFC test vectors**

Open RFC 9421 Appendix B (https://www.rfc-editor.org/rfc/rfc9421#appendix-B). Create `tests/fixtures/rfc9421/appendix_b.json` with this structure. Copy every string **verbatim** from the RFC: PEM blocks with their line breaks as `\n`, header values exactly as printed, signature bytes exactly as printed. Do not regenerate anything.

```json
{
  "source": "RFC 9421 Appendix B (B.1.2 test-key-rsa-pss, B.1.4 test-key-ed25519, B.2 request, B.2.1, B.2.6)",
  "keys": {
    "test-key-rsa-pss": "<B.1.2 public key PEM, '-----BEGIN PUBLIC KEY-----' ... '-----END PUBLIC KEY-----'>",
    "test-key-ed25519": "<B.1.4 public key PEM>"
  },
  "request": {
    "method": "POST",
    "url": "https://example.com/foo?param=Value&Pet=dog",
    "headers": {
      "Host": "example.com",
      "Date": "<B.2 request Date>",
      "Content-Type": "application/json",
      "Content-Digest": "<B.2 request Content-Digest>",
      "Content-Length": "18"
    },
    "body": "{\"hello\": \"world\"}"
  },
  "cases": [
    {"name": "B.2.1", "key": "test-key-rsa-pss", "label": "sig-b21",
     "signature_input": "<B.2.1 Signature-Input value, after 'Signature-Input: '>",
     "signature": "<B.2.1 Signature value>"},
    {"name": "B.2.6", "key": "test-key-ed25519", "label": "sig-b26",
     "signature_input": "<B.2.6 Signature-Input value>",
     "signature": "<B.2.6 Signature value>"}
  ],
  "created": 1618884473
}
```

The RFC folds long header lines with `\` line continuations (RFC 8792). Unfold them as the RFC's own folding note says before copying. B.2.1 has no `alg` parameter and covers no components. That exercises "RSA key without alg". B.2.6 is ed25519.

- [ ] **Step 2: Write the failing tests**

`tests/test_ap_security_rfc9421.py`:

```python
"""RFC 9421 / RFC 9530 (S5): the standalone module, against the RFC's own vectors and round trips."""
import base64
import hashlib
import json
import time
from pathlib import Path

import pytest
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding

from app.activitypub import signature_rfc9421 as rfc9421
from app.activitypub.signature import VerificationError as ReExportedError
from app.activitypub.signature_errors import VerificationError, VerificationFormatError
from tests.factories import a_keypair

VECTORS = json.loads((Path(__file__).parent / 'fixtures' / 'rfc9421' / 'appendix_b.json').read_text())
URL = 'https://peer.example/inbox'
BODY = b'{"type": "Create"}'
NOW = 1_800_000_000


def _pem(public_key) -> str:
    return public_key.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def _rsa_private(pem):
    return serialization.load_pem_private_key(pem.encode(), password=None)


def _custom(covered, private_sign, *, extra='', headers=None, created=NOW, label='sig1', url=URL, method='POST'):
    """Signature headers over `covered`, signed by `private_sign(base_bytes) -> bytes`."""
    headers = dict(headers or {})
    params = '(' + ' '.join(f'"{c}"' for c in covered) + f');created={created};keyid="k"{extra}'
    base = rfc9421.signature_base(covered, params, method, url, headers)
    headers['Signature-Input'] = f'{label}={params}'
    headers['Signature'] = f'{label}=:{base64.b64encode(private_sign(base.encode())).decode()}:'
    return headers


def _v15(private_pem):
    key = _rsa_private(private_pem)
    return lambda data: key.sign(data, padding.PKCS1v15(), hashes.SHA256())


class TestErrorsModule:

    def test_signature_re_exports_the_same_class(self):
        assert ReExportedError is VerificationError
        assert issubclass(VerificationFormatError, VerificationError)


class TestRfcVectors:

    @pytest.mark.parametrize('case', VECTORS['cases'], ids=lambda c: c['name'])
    def test_appendix_b_verifies(self, case):
        request = VECTORS['request']
        headers = dict(request['headers'], **{'Signature-Input': case['signature_input'],
                                              'Signature': case['signature']})
        try:
            covered = rfc9421.verify(request['method'], request['url'], headers, request['body'].encode(),
                                     VECTORS['keys'][case['key']], now=VECTORS['created'])
        except VerificationFormatError as e:
            if case['name'] == 'B.2.1' and 'usable PEM' in str(e):
                pytest.skip('this cryptography build cannot load an id-RSASSA-PSS SPKI key; B.2.6 still runs')
            raise
        assert isinstance(covered, frozenset)

    def test_a_tampered_vector_fails(self):
        case = next(c for c in VECTORS['cases'] if c['name'] == 'B.2.6')
        request = VECTORS['request']
        headers = dict(request['headers'], **{'Signature-Input': case['signature_input'],
                                              'Signature': case['signature'], 'Content-Type': 'text/plain'})
        with pytest.raises(VerificationError, match='Signature mismatch'):
            rfc9421.verify(request['method'], request['url'], headers, request['body'].encode(),
                           VECTORS['keys'][case['key']], now=VECTORS['created'])


class TestRoundTrip:

    def test_a_post_with_a_body(self):
        private_key, public_key = a_keypair()
        headers = rfc9421.sign('post', URL, BODY, private_key, 'https://test.piefed.local/actor#main-key', created=NOW)
        assert headers['Content-Digest'] == rfc9421.content_digest(BODY)
        assert headers['Signature-Input'].startswith('sig1=("@method" "@target-uri" "@authority" "content-digest")')
        assert 'alg="rsa-v1_5-sha256"' in headers['Signature-Input']
        covered = rfc9421.verify('POST', URL, headers, BODY, public_key, rfc9421.RFC9421_POST_REQUIRED, now=NOW)
        assert covered == frozenset({'@method', '@target-uri', '@authority', 'content-digest'})

    def test_a_get_without_a_body(self):
        private_key, public_key = a_keypair()
        headers = rfc9421.sign('get', URL, None, private_key, 'k', created=NOW)
        assert 'Content-Digest' not in headers
        assert rfc9421.verify('GET', URL, headers, b'', public_key, rfc9421.RFC9421_GET_REQUIRED, now=NOW) == \
            frozenset({'@method', '@target-uri', '@authority'})

    def test_created_defaults_to_now(self):
        private_key, public_key = a_keypair()
        headers = rfc9421.sign('get', URL, None, private_key, 'k')
        assert rfc9421.verify('GET', URL, headers, b'', public_key)

    def test_a_key_id_is_escaped(self):
        private_key, public_key = a_keypair()
        headers = rfc9421.sign('get', URL, None, private_key, 'a"b\\c', created=NOW)
        assert rfc9421.parse_key_id(headers) == 'a"b\\c'
        assert rfc9421.verify('GET', URL, headers, b'', public_key, now=NOW)

    def test_a_non_ascii_key_id_cannot_be_signed(self):
        with pytest.raises(ValueError):
            rfc9421.sign('get', URL, None, a_keypair()[0], 'clé')


class TestTampering:

    @pytest.fixture
    def signed(self):
        private_key, public_key = a_keypair()
        return rfc9421.sign('post', URL, BODY, private_key, 'k', created=NOW), public_key

    @pytest.mark.parametrize('method, url', [('POST', 'https://other.example/inbox'), ('PUT', URL),
                                             ('POST', 'https://peer.example/other')])
    def test_a_different_request_line_fails(self, signed, method, url):
        headers, public_key = signed
        with pytest.raises(VerificationError, match='Signature mismatch'):
            rfc9421.verify(method, url, headers, BODY, public_key, now=NOW)

    def test_a_different_body_fails_its_digest(self, signed):
        headers, public_key = signed
        with pytest.raises(VerificationFormatError, match='Content-Digest is incorrect'):
            rfc9421.verify('POST', URL, headers, b'{}', public_key, now=NOW)

    def test_a_replaced_digest_header_fails_the_signature(self, signed):
        headers, public_key = signed
        headers = dict(headers, **{'Content-Digest': rfc9421.content_digest(b'{}')})
        with pytest.raises(VerificationError):
            rfc9421.verify('POST', URL, headers, b'{}', public_key, now=NOW)

    @pytest.mark.parametrize('now, message', [(NOW + 3601, 'Signature is too old'),
                                              (NOW - 301, 'Signature is from the future')])
    def test_created_outside_the_window_fails(self, signed, now, message):
        headers, public_key = signed
        with pytest.raises(VerificationError, match=message):
            rfc9421.verify('POST', URL, headers, BODY, public_key, now=now)

    @pytest.mark.parametrize('now', [NOW + 3600, NOW - 300])
    def test_created_at_the_window_edges_passes(self, signed, now):
        headers, public_key = signed
        assert rfc9421.verify('POST', URL, headers, BODY, public_key, now=now)

    def test_a_missing_required_component_fails(self):
        private_key, public_key = a_keypair()
        headers = _custom(['@method', '@target-uri'], _v15(private_key))
        with pytest.raises(VerificationError, match='unsigned required component: content-digest'):
            rfc9421.verify('POST', URL, headers, b'', public_key, rfc9421.RFC9421_POST_REQUIRED, now=NOW)

    def test_authority_and_path_count_as_the_target_uri(self):
        private_key, public_key = a_keypair()
        headers = _custom(['@method', '@authority', '@path'], _v15(private_key), method='GET')
        assert rfc9421.verify('GET', URL, headers, b'', public_key, rfc9421.RFC9421_GET_REQUIRED, now=NOW) == \
            frozenset({'@method', '@authority', '@path'})


class TestLabels:

    def test_the_first_label_that_verifies_wins(self):
        private_key, public_key = a_keypair()
        good = _custom(['@method'], _v15(private_key), label='good', method='GET')
        headers = {'Signature-Input': f'bad=("@method");created={NOW};keyid="k", ' + good['Signature-Input'],
                   'Signature': 'bad=:YWJj:, ' + good['Signature']}
        assert rfc9421.verify('GET', URL, headers, b'', public_key, now=NOW) == frozenset({'@method'})

    def test_when_none_verifies_the_last_error_is_raised(self):
        _, public_key = a_keypair()
        headers = {'Signature-Input': f'a=("@method");created={NOW}, b=("@method")',
                   'Signature': 'a=:YWJj:, b=:YWJj:'}
        with pytest.raises(VerificationFormatError, match='no created time'):
            rfc9421.verify('GET', URL, headers, b'', public_key, now=NOW)

    @pytest.mark.parametrize('signature', ['', 'other=:YWJj:', 'sig1="text"'])
    def test_a_label_without_signature_bytes_fails(self, signature):
        _, public_key = a_keypair()
        headers = {'Signature-Input': f'sig1=("@method");created={NOW}', 'Signature': signature}
        with pytest.raises(VerificationFormatError, match='No signature for label sig1'):
            rfc9421.verify('GET', URL, headers, b'', public_key, now=NOW)

    def test_no_signature_input_fails(self):
        with pytest.raises(VerificationFormatError, match='No Signature-Input header present'):
            rfc9421.verify('GET', URL, {}, b'', a_keypair()[1], now=NOW)


class TestCoveredComponents:

    @pytest.mark.parametrize('signature_input, message', [
        ('sig1=:YWJj:', 'not an inner list'),
        ('sig1=(1)', 'Covered component is not a string'),
        ('sig1=("content-digest";sf)', 'Component parameters are not supported'),
        ('sig1=("@method" "@method")', 'Duplicate component @method'),
    ])
    def test_malformed_component_lists_fail(self, signature_input, message):
        headers = {'Signature-Input': f'{signature_input};created={NOW}', 'Signature': 'sig1=:YWJj:'}
        with pytest.raises(VerificationFormatError, match=message):
            rfc9421.verify('GET', URL, headers, b'', a_keypair()[1], now=NOW)

    def test_an_unsupported_derived_component_fails(self):
        with pytest.raises(VerificationFormatError, match='Unsupported derived component @query'):
            rfc9421.signature_base(['@query'], '()', 'GET', URL, {})

    def test_a_missing_covered_header_fails(self):
        with pytest.raises(VerificationError, match='Covered header date is missing'):
            rfc9421.signature_base(['date'], '()', 'GET', URL, {})

    def test_the_base_for_each_component(self):
        base = rfc9421.signature_base(['@method', '@target-uri', '@authority', '@path', 'content-type'], '(x)',
                                      'post', 'https://Peer.Example:8443?q=1', {'CONTENT-TYPE': ' a/b '})
        assert base == ('"@method": POST\n"@target-uri": https://Peer.Example:8443?q=1\n'
                        '"@authority": peer.example:8443\n"@path": /\n"content-type": a/b\n'
                        '"@signature-params": (x)')

    @pytest.mark.parametrize('url', ['https://peer.example:443/x', 'http://peer.example:80/x'])
    def test_a_default_port_is_dropped_from_the_authority(self, url):
        assert rfc9421.signature_base(['@authority'], '()', 'GET', url, {}).startswith('"@authority": peer.example\n')

    def test_normalise_covered(self):
        assert rfc9421.normalise_covered(['@authority', '@path']) == frozenset({'@authority', '@path', '@target-uri'})
        assert rfc9421.normalise_covered(['@authority']) == frozenset({'@authority'})


class TestKeysAndAlgorithms:

    def test_rsa_without_alg_accepts_pkcs1(self):
        private_key, public_key = a_keypair()
        headers = _custom(['@method'], _v15(private_key), method='GET')
        assert rfc9421.verify('GET', URL, headers, b'', public_key, now=NOW)

    @pytest.mark.parametrize('extra', ['', ';alg="rsa-pss-sha512"'])
    def test_rsa_pss_sha512(self, extra):
        private_key, public_key = a_keypair()
        key = _rsa_private(private_key)
        pss = lambda data: key.sign(data, padding.PSS(mgf=padding.MGF1(hashes.SHA512()), salt_length=64),
                                    hashes.SHA512())
        headers = _custom(['@method'], pss, extra=extra, method='GET')
        assert rfc9421.verify('GET', URL, headers, b'', public_key, now=NOW)

    def test_an_rsa_signature_matching_neither_scheme_fails(self):
        _, public_key = a_keypair()
        headers = {'Signature-Input': f'sig1=("@method");created={NOW}', 'Signature': 'sig1=:YWJj:'}
        with pytest.raises(VerificationError, match='Signature mismatch'):
            rfc9421.verify('GET', URL, headers, b'', public_key, now=NOW)

    @pytest.mark.parametrize('extra', ['', ';alg="ed25519"'])
    def test_ed25519(self, extra):
        key = ed25519.Ed25519PrivateKey.generate()
        headers = _custom(['@method'], key.sign, extra=extra, method='GET')
        assert rfc9421.verify('GET', URL, headers, b'', _pem(key.public_key()), now=NOW)

    def test_ed25519_mismatch(self):
        key, other = ed25519.Ed25519PrivateKey.generate(), ed25519.Ed25519PrivateKey.generate()
        headers = _custom(['@method'], key.sign, method='GET')
        with pytest.raises(VerificationError, match='Signature mismatch'):
            rfc9421.verify('GET', URL, headers, b'', _pem(other.public_key()), now=NOW)

    def test_ed25519_with_an_rsa_alg_fails(self):
        key = ed25519.Ed25519PrivateKey.generate()
        headers = _custom(['@method'], key.sign, extra=';alg="rsa-v1_5-sha256"', method='GET')
        with pytest.raises(VerificationFormatError, match='does not match an ed25519 key'):
            rfc9421.verify('GET', URL, headers, b'', _pem(key.public_key()), now=NOW)

    def test_an_unsupported_alg_fails(self):
        private_key, public_key = a_keypair()
        headers = _custom(['@method'], _v15(private_key), extra=';alg="hmac-sha256"', method='GET')
        with pytest.raises(VerificationFormatError, match='Unsupported algorithm hmac-sha256'):
            rfc9421.verify('GET', URL, headers, b'', public_key, now=NOW)

    def test_an_unsupported_key_type_fails(self):
        key = ec.generate_private_key(ec.SECP256R1())
        headers = {'Signature-Input': f'sig1=("@method");created={NOW}', 'Signature': 'sig1=:YWJj:'}
        with pytest.raises(VerificationFormatError, match='Unsupported key type'):
            rfc9421.verify('GET', URL, headers, b'', _pem(key.public_key()), now=NOW)

    @pytest.mark.parametrize('pem', ['junk', None])
    def test_an_unusable_public_key_fails(self, pem):
        headers = {'Signature-Input': f'sig1=("@method");created={NOW}', 'Signature': 'sig1=:YWJj:'}
        with pytest.raises(VerificationFormatError, match='Public key is not a usable PEM'):
            rfc9421.verify('GET', URL, headers, b'', pem, now=NOW)


class TestContentDigest:

    def test_the_header_format(self):
        expected = base64.b64encode(hashlib.sha256(BODY).digest()).decode()
        assert rfc9421.content_digest(BODY) == f'sha-256=:{expected}:'

    def test_an_absent_header_passes(self):
        assert rfc9421.check_content_digest({}, BODY) is None

    def test_sha512_and_unknown_algorithms_alongside(self):
        sha512 = base64.b64encode(hashlib.sha512(BODY).digest()).decode()
        assert rfc9421.check_content_digest({'content-digest': f'md5=:YWJj:, sha-512=:{sha512}:'}, BODY) is None

    @pytest.mark.parametrize('value, message', [('md5=:YWJj:', 'no supported algorithm'),
                                                ('sha-256="text"', 'Content-Digest is incorrect'),
                                                ('sha-256=:YWJj:', 'Content-Digest is incorrect')])
    def test_bad_digests(self, value, message):
        with pytest.raises(VerificationFormatError, match=message):
            rfc9421.check_content_digest({'Content-Digest': value}, BODY)


class TestParseKeyId:

    @pytest.mark.parametrize('headers, expected', [
        ({'Signature-Input': 'sig1=();keyid="https://a.example/u#k"'}, 'https://a.example/u#k'),
        ({}, None),
        ({'Signature-Input': 'Bad'}, None),
        ({'Signature-Input': 'sig1=()'}, None),
        ({'Signature-Input': 'sig1=();keyid=1'}, None),
    ])
    def test_parse_key_id(self, headers, expected):
        assert rfc9421.parse_key_id(headers) == expected


class TestStructuredFieldParser:

    def test_a_well_formed_dictionary(self):
        parsed = rfc9421.parse_dictionary(' a=("x" "y\\"z");p=1;q=tok, b=:YWJj:;r="s" , c=-5')
        assert parsed['a'] == ([('x', {}), ('y"z', {})], {'p': 1, 'q': 'tok'}, '("x" "y\\"z");p=1;q=tok')
        assert parsed['b'] == (b'abc', {'r': 's'}, ':YWJj:;r="s"')
        assert parsed['c'][0] == -5
        assert rfc9421.parse_dictionary('') == {}

    @pytest.mark.parametrize('text, message', [
        ('Sig1=("@method")', 'key'),
        ('sig1', 'expected "="'),
        ('sig1=:YQ==: sig2=:YQ==:', 'expected ","'),
        ('sig1=:YQ==:, ', 'trailing ","'),
        ('sig1=("a""b")', 'expected " " or ")"'),
        ('sig1=("a"', 'expected " " or ")"'),
        ('sig1=();=1', 'parameter key'),
        ('sig1=();created=-', 'integer'),
        ('sig1=();alg=?1', 'item'),
        ('sig1=("abc', 'unterminated string'),
        ('sig1=("\\x")', 'bad escape'),
        ('sig1=("\u00e9")', 'non-printable character'),
        ('sig1=:YQ==', 'unterminated byte sequence'),
        ('sig1=:Y!==:', 'bad base64'),
    ])
    def test_malformed_input_is_a_format_error(self, text, message):
        with pytest.raises(VerificationFormatError, match=message):
            rfc9421.parse_dictionary(text)
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `./run_tests.sh tests/test_ap_security_rfc9421.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.activitypub.signature_errors'`.

- [ ] **Step 4: Implement**

`app/activitypub/signature_errors.py`:

```python
"""The signature exceptions, in a module of their own so signature.py and signature_rfc9421.py can both raise them
without importing each other at load time. signature.py re-exports both names."""


class VerificationError(BaseException):
    """
    There was an error with verifying the signature
    """

    pass


class VerificationFormatError(VerificationError):
    """
    There was an error with the format of the signature (not if it is valid)
    """

    pass
```

`app/activitypub/signature.py`: delete the two class definitions (S:208-225) and add to the imports:

```python
from app.activitypub.signature_errors import VerificationError, VerificationFormatError  # re-exported
```

`app/activitypub/signature_rfc9421.py`:

```python
"""RFC 9421 HTTP Message Signatures and RFC 9530 Content-Digest: the subset the fediverse uses (S5).

Signing covers "@method" "@target-uri" "@authority", plus "content-digest" when there is a body, with
rsa-v1_5-sha256. Verification also accepts rsa-pss-sha512 and ed25519, any covered header field, and "@path".
Nothing here touches Flask: callers pass the method, the target URI, the headers and the body.
"""
import base64
import binascii
import hashlib
import re
import time
from collections.abc import Mapping
from functools import lru_cache
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from app.activitypub.signature_errors import VerificationError, VerificationFormatError

# The components an inbound signature must cover. "@target-uri" includes the authority, and normalise_covered
# counts "@authority" + "@path" as "@target-uri".
RFC9421_POST_REQUIRED = frozenset({'@method', '@target-uri', 'content-digest'})
RFC9421_GET_REQUIRED = frozenset({'@method', '@target-uri'})
CREATED_MAX_AGE_SECONDS = 3600     # matches HttpSignature.precheck's Date window
CREATED_MAX_SKEW_SECONDS = 300
SIGN_LABEL = 'sig1'
SIGN_ALGORITHM = 'rsa-v1_5-sha256'
DIGESTS = {'sha-256': hashlib.sha256, 'sha-512': hashlib.sha512}

_KEY = re.compile(r'[a-z*][a-z0-9_\-.*]*')
_INTEGER = re.compile(r'-?[0-9]{1,15}')
_TOKEN = re.compile(r"[A-Za-z*][!#$%&'*+\-.^_`|~0-9A-Za-z:/]*")


class _Parser:
    """RFC 8941 structured fields, the subset these headers use: a Dictionary whose members are Items or Inner
    Lists, with String, Byte Sequence, Integer and Token bare items. Anything else is a format error."""

    def __init__(self, text: str):
        self.text = text
        self.pos = 0

    def _peek(self) -> str:
        return self.text[self.pos:self.pos + 1]

    def _fail(self, what: str):
        raise VerificationFormatError(f'Malformed structured field: {what} at {self.pos}')

    def _match(self, pattern, what: str) -> str:
        found = pattern.match(self.text, self.pos)
        if not found:
            self._fail(what)
        self.pos = found.end()
        return found.group()

    def _skip(self, chars: str = ' '):
        while self._peek() and self._peek() in chars:
            self.pos += 1

    def dictionary(self) -> dict:
        members = {}
        self._skip()
        while self.pos < len(self.text):
            key = self._match(_KEY, 'key')
            if self._peek() != '=':
                self._fail('expected "="')
            self.pos += 1
            start = self.pos
            value, params = self._inner_list() if self._peek() == '(' else self._item()
            members[key] = (value, params, self.text[start:self.pos])
            self._skip(' \t')
            if self.pos == len(self.text):
                break
            if self._peek() != ',':
                self._fail('expected ","')
            self.pos += 1
            self._skip(' \t')
            if self.pos == len(self.text):
                self._fail('trailing ","')
        return members

    def _inner_list(self):
        self.pos += 1   # '('
        items = []
        while True:
            self._skip()
            if self._peek() == ')':
                self.pos += 1
                return items, self._params()
            items.append(self._item())
            if self._peek() not in (' ', ')'):
                self._fail('expected " " or ")"')

    def _item(self):
        return self._bare(), self._params()

    def _params(self) -> dict:
        params = {}
        while self._peek() == ';':
            self.pos += 1
            self._skip()
            key = self._match(_KEY, 'parameter key')
            params[key] = True
            if self._peek() == '=':
                self.pos += 1
                params[key] = self._bare()
        return params

    def _bare(self):
        char = self._peek()
        if char == '"':
            return self._string()
        if char == ':':
            return self._bytes()
        if char == '-' or char.isdigit():
            return int(self._match(_INTEGER, 'integer'))
        return self._match(_TOKEN, 'item')

    def _string(self) -> str:
        self.pos += 1
        out = []
        while True:
            char = self._peek()
            if not char:
                self._fail('unterminated string')
            self.pos += 1
            if char == '\\':
                escaped = self._peek()
                if escaped not in ('"', '\\'):
                    self._fail('bad escape')
                out.append(escaped)
                self.pos += 1
            elif char == '"':
                return ''.join(out)
            elif not ' ' <= char <= '~':
                self._fail('non-printable character')
            else:
                out.append(char)

    def _bytes(self) -> bytes:
        end = self.text.find(':', self.pos + 1)
        if end < 0:
            self._fail('unterminated byte sequence')
        encoded = self.text[self.pos + 1:end]
        self.pos = end + 1
        try:
            return base64.b64decode(encoded, validate=True)
        except binascii.Error:
            self._fail('bad base64')


def parse_dictionary(text: str) -> dict:
    """{key: (value, params, raw)} for an RFC 8941 Dictionary. `raw` is the member's text after '=' exactly as
    received, which is what "@signature-params" must reproduce."""
    return _Parser(text).dictionary()


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """The value of header `name` (lowercase), matched case-insensitively whatever mapping the caller passes."""
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return None


def _authority(url: str) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or '').lower()
    if parts.port is not None and (parts.scheme, parts.port) not in (('https', 443), ('http', 80)):
        host = f'{host}:{parts.port}'
    return host


def _component_value(name: str, method: str, url: str, headers: Mapping[str, str]) -> str:
    if name == '@method':
        return method.upper()
    if name == '@target-uri':
        return url
    if name == '@authority':
        return _authority(url)
    if name == '@path':
        return urlsplit(url).path or '/'
    if name.startswith('@'):
        raise VerificationFormatError(f'Unsupported derived component {name}')
    value = _header(headers, name)
    if value is None:
        raise VerificationError(f'Covered header {name} is missing')
    return value.strip()


def signature_base(covered: list[str], params: str, method: str, url: str, headers: Mapping[str, str]) -> str:
    """The RFC 9421 section 2.5 signature base: one line per covered component, then "@signature-params"."""
    lines = [f'"{name}": {_component_value(name, method, url, headers)}' for name in covered]
    lines.append(f'"@signature-params": {params}')
    return '\n'.join(lines)


def normalise_covered(covered) -> frozenset[str]:
    """`covered`, counting "@authority" with "@path" as "@target-uri" (the target URI is the two joined)."""
    names = set(covered)
    if {'@authority', '@path'} <= names:
        names.add('@target-uri')
    return frozenset(names)


def content_digest(body: bytes) -> str:
    return 'sha-256=:' + base64.b64encode(hashlib.sha256(body).digest()).decode('ascii') + ':'


def check_content_digest(headers: Mapping[str, str], body: bytes) -> None:
    """Raise unless every supported digest in the Content-Digest header matches `body`. An absent header passes."""
    value = _header(headers, 'content-digest')
    if value is None:
        return
    checked = False
    for algorithm, (expected, _params, _raw) in parse_dictionary(value).items():
        hasher = DIGESTS.get(algorithm)
        if hasher is None:
            continue
        if expected != hasher(body).digest():
            raise VerificationFormatError('Content-Digest is incorrect')
        checked = True
    if not checked:
        raise VerificationFormatError('Content-Digest has no supported algorithm')


def _covered(value) -> list[str]:
    if not isinstance(value, list):
        raise VerificationFormatError('Signature-Input member is not an inner list')
    names = []
    for item, params in value:
        if not isinstance(item, str):
            raise VerificationFormatError('Covered component is not a string')
        if params:
            raise VerificationFormatError(f'Component parameters are not supported: {item}')
        if item in names:
            raise VerificationFormatError(f'Duplicate component {item}')
        names.append(item)
    return names


def _check_created(params: dict, now: int) -> None:
    created = params.get('created')
    if not isinstance(created, int):
        raise VerificationFormatError('Signature-Input has no created time')
    if created < now - CREATED_MAX_AGE_SECONDS:
        raise VerificationError('Signature is too old')
    if created > now + CREATED_MAX_SKEW_SECONDS:
        raise VerificationError('Signature is from the future')


def _load_public_key(public_key_pem: str):
    try:
        return serialization.load_pem_public_key(public_key_pem.encode('ascii'))
    except (ValueError, TypeError, AttributeError, UnsupportedAlgorithm):
        raise VerificationFormatError('Public key is not a usable PEM')


def _pkcs1v15_sha256():
    return padding.PKCS1v15(), hashes.SHA256()


def _pss_sha512():
    return padding.PSS(mgf=padding.MGF1(hashes.SHA512()), salt_length=64), hashes.SHA512()


# With no alg parameter an RSA key is tried as the fediverse's usual rsa-v1_5-sha256 first, then rsa-pss-sha512
RSA_SCHEMES = {'rsa-v1_5-sha256': (_pkcs1v15_sha256,), 'rsa-pss-sha512': (_pss_sha512,),
               None: (_pkcs1v15_sha256, _pss_sha512)}


def _verify_signature(key, alg, signature: bytes, data: bytes) -> None:
    if isinstance(key, ed25519.Ed25519PublicKey):
        if alg not in (None, 'ed25519'):
            raise VerificationFormatError(f'Algorithm {alg} does not match an ed25519 key')
        try:
            key.verify(signature, data)
            return
        except InvalidSignature:
            raise VerificationError('Signature mismatch')
    if not isinstance(key, rsa.RSAPublicKey):
        raise VerificationFormatError('Unsupported key type')
    if alg not in RSA_SCHEMES:
        raise VerificationFormatError(f'Unsupported algorithm {alg}')
    for scheme in RSA_SCHEMES[alg]:
        try:
            key.verify(signature, data, *scheme())
            return
        except InvalidSignature:
            continue
    raise VerificationError('Signature mismatch')


def verify(method: str, url: str, headers: Mapping[str, str], body: bytes, public_key_pem: str,
           required: frozenset[str] = frozenset(), *, now: int | None = None) -> frozenset[str]:
    """Verify the request's RFC 9421 signature against `public_key_pem`; return the components it covers.

    With several labels the first that verifies wins; if none does, the last label's error is raised. Every
    component in `required` must be covered (after normalise_covered). Content-Digest, when present, must match
    `body` whether or not it is covered."""
    inputs = parse_dictionary(_header(headers, 'signature-input') or '')
    if not inputs:
        raise VerificationFormatError('No Signature-Input header present')
    signatures = parse_dictionary(_header(headers, 'signature') or '')
    check_content_digest(headers, body)
    key = _load_public_key(public_key_pem)
    now = int(time.time()) if now is None else now
    error = None
    for label, (value, params, raw) in inputs.items():
        try:
            covered = _covered(value)
            signature = signatures.get(label, (None,))[0]
            if not isinstance(signature, bytes):
                raise VerificationFormatError(f'No signature for label {label}')
            _check_created(params, now)
            base = signature_base(covered, raw, method, url, headers)
            _verify_signature(key, params.get('alg'), signature, base.encode('utf-8'))
            missing = required - normalise_covered(covered)
            if missing:
                raise VerificationError('unsigned required component: ' + ' '.join(sorted(missing)))
            return frozenset(covered)
        except VerificationError as e:
            error = e
    raise error


def parse_key_id(headers: Mapping[str, str]) -> str | None:
    """The keyid of the first Signature-Input label, or None when there is none to read."""
    try:
        inputs = parse_dictionary(_header(headers, 'signature-input') or '')
    except VerificationFormatError:
        return None
    first = next(iter(inputs.values()), None)
    key_id = first[1].get('keyid') if first else None
    return key_id if isinstance(key_id, str) else None


def _sf_string(value: str) -> str:
    if not all(' ' <= char <= '~' for char in value):
        raise ValueError('A structured-field string must be printable ASCII')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


@lru_cache(maxsize=256)
def _private_key(private_key_pem: str):
    return serialization.load_pem_private_key(private_key_pem.encode('ascii'), password=None)


def sign(method: str, url: str, body: bytes | None, private_key_pem: str, key_id: str,
         created: int | None = None) -> dict[str, str]:
    """The Signature-Input, Signature and (with a body) Content-Digest headers for a request."""
    covered = ['@method', '@target-uri', '@authority']
    headers = {}
    if body is not None:
        headers['Content-Digest'] = content_digest(body)
        covered.append('content-digest')
    created = int(time.time()) if created is None else created
    params = ('(' + ' '.join(f'"{name}"' for name in covered) + ')'
              f';created={created};keyid={_sf_string(key_id)};alg="{SIGN_ALGORITHM}"')
    base = signature_base(covered, params, method, url, headers)
    signature = _private_key(private_key_pem).sign(base.encode('utf-8'), *_pkcs1v15_sha256())
    headers['Signature-Input'] = f'{SIGN_LABEL}={params}'
    headers['Signature'] = f'{SIGN_LABEL}=:{base64.b64encode(signature).decode("ascii")}:'
    return headers
```

`coverage_floors.ini`: add next to `app/activitypub/signature.py = 100`:

```ini
app/activitypub/signature_errors.py = 100
app/activitypub/signature_rfc9421.py = 100
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `./run_tests.sh tests/test_ap_security_rfc9421.py tests/test_activitypub_signature.py tests/test_no_inline_imports.py tests/test_import_order.py -q`
Expected: PASS. The B.2.1 row may skip only for the stated PEM-loading reason. If it skips, record that in the commit message.

Then run module coverage:
`./run_tests.sh tests/test_ap_security_rfc9421.py --cov=app.activitypub.signature_rfc9421 --cov=app.activitypub.signature_errors --cov-branch --cov-report=term-missing -q`
Expected: 100% lines and branches on both modules. Add a test for any line still missing.

- [ ] **Step 6: Commit**

```bash
git add app/activitypub/signature_errors.py app/activitypub/signature_rfc9421.py app/activitypub/signature.py coverage_floors.ini tests/fixtures/rfc9421/appendix_b.json tests/test_ap_security_rfc9421.py
git commit -m "feat(ap): RFC 9421 HTTP message signatures module

Sign (rsa-v1_5-sha256 over @method @target-uri @authority and
content-digest) and verify (also rsa-pss-sha512 and ed25519), with RFC
9530 Content-Digest and the RFC 8941 parsing they need, checked against
the RFC's Appendix B vectors. Signature exceptions move to their own
module so both signature modules can raise them.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 19: Inbound RFC 9421 dispatch

**Files:**
- Modify: `app/activitypub/signature.py`: `precheck` (S:416-438), `verify_request` (dispatch), `request_key_id`, new `_target_uri`, `CAVAGE_TO_RFC9421`, `_rfc9421_required`
- Modify: `tests/factories.py` (new helper `rfc9421_inbox_post`)
- Test: `tests/test_ap_security_rfc9421_inbound.py` (new)

**Interfaces:**
- Consumes: Task 18's `rfc9421.verify`, `parse_key_id`, `check_content_digest`, `normalise_covered`; Task 6's `_check_required`, `POST_REQUIRED`, `GET_REQUIRED`; Task 8's `request_key_id`, `verify_with_key_refresh`; Task 2's `signed_requester_actor`.
- Produces:
  - `HttpSignature.verify_request` verifies either format: RFC 9421 when `Signature-Input` is present, otherwise cavage.
  - `request_key_id` reads either format.
  - `precheck` accepts `Digest` or `Content-Digest`, and needs no `Date` for an RFC 9421 request.
  - Test helper `tests.factories.rfc9421_inbox_post(client, activity, sender, *, path='/inbox', key_id=None, date=False)`.

- [ ] **Step 1: Add the test helper**

`tests/factories.py`: add `import json` to the stdlib imports, add `from app.activitypub import signature_rfc9421`, and add:

```python
def rfc9421_inbox_post(client, activity: dict, sender, *, path: str = '/inbox', key_id: str = None,
                       date: bool = False):
    """POST `activity` to `path` with a REAL RFC 9421 signature made by `sender` (production's signer).

    No Date header unless `date`: an RFC 9421 signature carries its own `created` time."""
    host = current_app.config['SERVER_NAME']
    body = json.dumps(activity).encode('utf8')
    headers = signature_rfc9421.sign('post', f'https://{host}{path}', body, sender.private_key,
                                     key_id or f'{sender.ap_profile_id}#main-key')
    headers['Host'] = host
    if date:
        headers['Date'] = http_date()
    return client.post(path, data=body, headers=headers, content_type='application/activity+json')
```

- [ ] **Step 2: Write the failing tests**

`tests/test_ap_security_rfc9421_inbound.py`:

```python
"""S5 inbound: RFC 9421 requests through precheck, verify_request, the inbox and signed GET identification."""
import json

import pytest
from flask import request

from app import db
from app.activitypub import signature_rfc9421 as rfc9421
from app.activitypub.entitlement import signed_requester_actor
from app.activitypub.signature import GET_REQUIRED, POST_REQUIRED, HttpSignature, VerificationError, \
    VerificationFormatError, request_key_id
from app.models import ActivityPubLog
from tests.factories import a_keypair, inbox_activity, rfc9421_inbox_post

pytestmark = pytest.mark.usefixtures('redis_double')

HOST = 'test.piefed.local'


@pytest.fixture
def dispatched(app, monkeypatch):
    monkeypatch.setitem(app.config, 'DEBUG', True)
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    calls = []
    monkeypatch.setattr('app.activitypub.routes.process_inbox_request', lambda *a, **kw: calls.append(a))
    return calls


def _context(app, headers, method='POST', path='/inbox', body=b''):
    return app.test_request_context(path, method=method, headers=dict(headers, Host=HOST), data=body)


class TestInbox:

    @pytest.mark.parametrize('date', [False, True])
    def test_a_genuine_rfc9421_activity_is_dispatched(self, app, signing_peer, dispatched, date):
        with app.test_client() as client:
            response = rfc9421_inbox_post(client, inbox_activity(signing_peer), signing_peer, date=date)
        assert response.status_code == 200 and len(dispatched) == 1

    def test_the_wrong_key_is_refused(self, app, signing_peer, dispatched):
        signing_peer.public_key = next(p for _, p in (a_keypair() for _ in range(16)) if p != signing_peer.public_key)
        db.session.commit()
        with app.test_client() as client:
            response = rfc9421_inbox_post(client, inbox_activity(signing_peer), signing_peer)
        assert response.status_code == 400 and dispatched == []
        assert ActivityPubLog.query.one().exception_message.startswith('Could not verify HTTP signature')

    def test_a_tampered_body_fails_precheck(self, app, signing_peer, dispatched):
        activity = inbox_activity(signing_peer)
        headers = rfc9421.sign('post', f'https://{HOST}/inbox', json.dumps(activity).encode(), signing_peer.private_key,
                               f'{signing_peer.ap_profile_id}#main-key')
        with app.test_client() as client:
            response = client.post('/inbox', data=json.dumps(dict(activity, type='Dislike')).encode(),
                                   headers=dict(headers, Host=HOST), content_type='application/activity+json')
        assert response.status_code == 400
        assert ActivityPubLog.query.one().exception_message == 'Precheck failed: Content-Digest is incorrect'


class TestPrecheck:

    def test_no_digest_of_either_kind_fails(self, app):
        with _context(app, {'Date': 'x'}):
            with pytest.raises(VerificationFormatError, match='No digest header present'):
                HttpSignature.precheck(request)

    def test_a_cavage_request_still_needs_a_date(self, app):
        body = b'{}'
        with _context(app, {'Digest': HttpSignature.calculate_digest(body)}, body=body):
            with pytest.raises(VerificationFormatError, match='No date header present'):
                HttpSignature.precheck(request)

    def test_both_digests_are_checked(self, app):
        body = b'{}'
        headers = {'Digest': HttpSignature.calculate_digest(body), 'Content-Digest': rfc9421.content_digest(b'[]'),
                   'Signature-Input': 'sig1=()'}
        with _context(app, headers, body=body):
            with pytest.raises(VerificationFormatError, match='Content-Digest is incorrect'):
                HttpSignature.precheck(request)


class TestVerifyRequest:

    def _signed(self, method, body):
        private_key, public_key = a_keypair()
        headers = rfc9421.sign(method, f'https://{HOST}/inbox', body, private_key, 'https://peer.example/u/a#k')
        return headers, public_key

    def test_a_post_meets_the_cavage_required_set(self, app, monkeypatch):
        monkeypatch.setitem(app.config, 'SIG_REQUIRED_HEADERS_ENFORCE', True)
        headers, public_key = self._signed('post', b'{}')
        with _context(app, headers, body=b'{}'):
            assert HttpSignature.verify_request(request, public_key, POST_REQUIRED) is True

    def test_a_get_meets_the_get_set(self, app, monkeypatch):
        monkeypatch.setitem(app.config, 'SIG_REQUIRED_HEADERS_ENFORCE', True)
        headers, public_key = self._signed('get', None)
        with _context(app, headers, method='GET'):
            assert HttpSignature.verify_request(request, public_key, GET_REQUIRED) is True

    def test_a_query_string_is_part_of_the_target_uri(self, app):
        private_key, public_key = a_keypair()
        headers = rfc9421.sign('get', f'https://{HOST}/post/1?page=2', None, private_key, 'k')
        with app.test_request_context('/post/1?page=2', method='GET', headers=dict(headers, Host=HOST)):
            assert HttpSignature.verify_request(request, public_key) is True

    @pytest.mark.parametrize('enforce', [False, True])
    def test_a_post_without_a_covered_digest(self, app, caplog, monkeypatch, enforce):
        monkeypatch.setitem(app.config, 'SIG_REQUIRED_HEADERS_ENFORCE', enforce)
        private_key, public_key = a_keypair()
        headers = rfc9421.sign('post', f'https://{HOST}/inbox', None, private_key, 'https://peer.example/u/a#k')
        with _context(app, headers, body=b''):
            if enforce:
                with pytest.raises(VerificationError, match='unsigned required header: content-digest'):
                    HttpSignature.verify_request(request, public_key, POST_REQUIRED)
            else:
                with caplog.at_level('WARNING', logger=app.logger.name):
                    assert HttpSignature.verify_request(request, public_key, POST_REQUIRED) is True
                assert 'Signature from peer.example does not sign: content-digest' in caplog.text

    def test_request_key_id_reads_rfc9421(self, app):
        headers, _ = self._signed('get', None)
        with _context(app, headers, method='GET'):
            assert request_key_id(request) == 'https://peer.example/u/a#k'


class TestSignedGet:

    def test_signed_requester_actor_identifies_an_rfc9421_signer(self, app, signing_peer):
        headers = rfc9421.sign('get', f'https://{HOST}/post/1', None, signing_peer.private_key,
                               f'{signing_peer.ap_profile_id}#main-key')
        with app.test_request_context('/post/1', method='GET', headers=dict(headers, Host=HOST)):
            assert signed_requester_actor() == signing_peer
```

The message in `test_a_post_without_a_covered_digest` comes from `_check_required`, which reports the 9421 names (see the implementation). `_check_required` builds the text as `unsigned required header: <names>`, and the same wording is used for both formats.

- [ ] **Step 3: Run the tests to verify they fail**

Run: `./run_tests.sh tests/test_ap_security_rfc9421_inbound.py -q`
Expected: FAIL. `test_a_genuine_rfc9421_activity_is_dispatched` gets 400 (`Precheck failed: No digest header present`), and `verify_request` raises `No signature header present`.

- [ ] **Step 4: Implement**

`app/activitypub/signature.py`. Add `from app.activitypub import signature_rfc9421 as rfc9421` to the imports. Add near `_check_required`:

```python
# The RFC 9421 components that stand in for each cavage header a signature must cover. "date" has none: an RFC 9421
# signature's `created` parameter is checked at verification instead.
CAVAGE_TO_RFC9421 = {'(request-target)': {'@method', '@target-uri'}, 'host': {'@target-uri'}, 'date': set(),
                     'digest': {'content-digest'}}


def _rfc9421_required(required: frozenset[str]) -> frozenset[str]:
    return frozenset().union(*(CAVAGE_TO_RFC9421[name] for name in required))


def _target_uri(request) -> str:
    """The URI an RFC 9421 signer signed: this instance's scheme, the Host it addressed, the path and query."""
    path = request.full_path if request.query_string else request.path
    return f"{current_app.config['HTTP_PROTOCOL']}://{request.host}{path}"
```

Replace `request_key_id`:

```python
def request_key_id(request) -> str | None:
    """The keyId the request's signature names (RFC 9421 or cavage), or None. Every keyId read goes through here."""
    if request.headers.get('Signature-Input'):
        return rfc9421.parse_key_id(request.headers)
    return parse_signature_header(request.headers.get('Signature')).get('keyid') or None
```

`precheck`, replacing its digest part:

```python
        has_digest = "digest" in request.headers
        has_content_digest = "content-digest" in request.headers
        if not has_digest and not has_content_digest:
            raise VerificationFormatError("No digest header present")
        if has_digest and request.headers["digest"] != HttpSignature.calculate_digest(request.data):
            raise VerificationFormatError("Digest is incorrect")
        if has_content_digest:
            rfc9421.check_content_digest(request.headers, request.data)

        if "date" not in request.headers:
            if "signature-input" in request.headers:
                return   # RFC 9421 carries its own created time, checked at verification
            raise VerificationFormatError("No date header present")
```

The rest of `precheck` (date parsing and window) is unchanged.

At the top of `verify_request`, before the `"signature" not in request.headers` check:

```python
        if "signature-input" in request.headers:
            covered = rfc9421.verify(request.method, _target_uri(request), request.headers, request.get_data(),
                                     public_key)
            _check_required(set(rfc9421.normalise_covered(covered)), _rfc9421_required(required),
                            rfc9421.parse_key_id(request.headers))
            return True
```

`_check_required` already words its error and log as `unsigned required header: …`/`does not sign: …` with the missing names. For an RFC 9421 request the names are component names such as `content-digest`.

`rfc9421.verify` can raise `VerificationFormatError` for an unusable PEM rather than `ValueError`. `verify_refreshing_key` already catches `VerificationError`, so a junk stored key still leads to the refetch path.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `./run_tests.sh tests/test_ap_security_rfc9421_inbound.py tests/test_ap_security_rfc9421.py tests/test_ap_security_signature_required.py tests/test_ap_security_key_refresh.py tests/test_ap_security_key_id_host.py tests/test_activitypub_signature.py tests/test_inbox_gate_signatures.py tests/test_inbox_gate_refusals.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/activitypub/signature.py tests/factories.py tests/test_ap_security_rfc9421_inbound.py
git commit -m "feat(ap): accept RFC 9421 signatures inbound

verify_request, precheck and keyId reading dispatch on Signature-Input:
RFC 9421 requests are verified with Content-Digest and their created
time, cavage ones as before. The required-header rule maps onto RFC
9421 components under the same enforcement flag.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 20: `Instance.signature_format` and POST double-knocking

**Files:**
- Create: `migrations/versions/c4a8e2f61d3b_instance_signature_format.py`
- Modify: `app/models.py` (`Instance`, after `popular`)
- Modify: `app/activitypub/signature.py`: `SIGNATURE_FORMATS`, `_other_signature_format`, `_instance_signature_format`, `_remember_signature_format`, `double_knock`; `HttpSignature.signed_request(..., signature_format='cavage')`; `post_request`
- Test: `tests/test_ap_security_double_knock.py` (new), `tests/test_migration_instance_signature_format.py` (new)

**Interfaces:**
- Consumes: `rfc9421.sign`, `rfc9421.verify` (Task 18).
- Produces:
  - `Instance.signature_format` (`String(16)`, not null, server default `'cavage'`);
  - `SIGNATURE_FORMATS = ('cavage', 'rfc9421')`;
  - `HttpSignature.signed_request(uri, body, private_key, key_id, content_type=..., method=..., timeout=5, send_via_async=False, signature_format='cavage')`;
  - `double_knock(uri: str, send: Callable[[str], httpx.Response]) -> httpx.Response`.

- [ ] **Step 1: Write the failing tests**

`tests/test_migration_instance_signature_format.py`:

```python
"""S5 migration c4a8e2f61d3b: Instance.signature_format, and that it reverses cleanly."""
import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text

from app import db
from tests.factories import make_instance

MIGRATION = Path(__file__).resolve().parent.parent / 'migrations' / 'versions' / 'c4a8e2f61d3b_instance_signature_format.py'


def load_migration():
    spec = importlib.util.spec_from_file_location('signature_format_migration', MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def column(connection):
    return next((c for c in inspect(connection).get_columns('instance') if c['name'] == 'signature_format'), None)


def test_the_revision_chain():
    migration = load_migration()
    assert (migration.revision, migration.down_revision) == ('c4a8e2f61d3b', 'b7e1c2d9f4a6')


def test_not_null_with_a_cavage_default(app, db_session):
    with db.engine.connect() as connection:
        found = column(connection)
    assert found is not None and found['nullable'] is False and "'cavage'" in found['default']
    instance = make_instance('fresh.example')
    db.session.refresh(instance)
    assert instance.signature_format == 'cavage'


def test_downgrade_then_upgrade_restores_the_column(app, db_session):
    instance_id = make_instance('peer.example').id
    db.session.close()
    migration = load_migration()
    with db.engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text("SET LOCAL lock_timeout = '5s'"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
                assert column(connection) is None
                migration.upgrade()
            assert connection.execute(text('SELECT signature_format FROM instance WHERE id = :id'),
                                      {'id': instance_id}).scalar() == 'cavage'
        finally:
            transaction.rollback()
```

`tests/test_ap_security_double_knock.py`:

```python
"""S5 outbound: signing in RFC 9421, and one retry in the other format on a 401."""
import httpx
import pytest

from app import db
from app.activitypub import signature_rfc9421 as rfc9421
from app.activitypub.signature import HttpSignature, double_knock, post_request
from app.models import ActivityPubLog, Instance, SendQueue
from tests.factories import a_keypair, make_instance

INBOX = 'https://peer.example/inbox'
KEY_ID = 'https://test.piefed.local/u/alice#main-key'


def _body():
    return {'id': 'https://test.piefed.local/activities/create/1', 'type': 'Create', 'actor': KEY_ID.split('#')[0]}


class TestSignedRequest:

    def test_rfc9421_headers_replace_cavage(self, app):
        private_key, public_key = a_keypair()
        uri, headers, body = HttpSignature.signed_request(INBOX, _body(), private_key, KEY_ID, send_via_async=True,
                                                          signature_format='rfc9421')
        assert 'Signature' in headers and 'Signature-Input' in headers and 'Content-Digest' in headers
        assert 'Digest' not in headers and '(request-target)' not in headers
        assert rfc9421.verify('POST', INBOX, headers, body, public_key, rfc9421.RFC9421_POST_REQUIRED)

    def test_cavage_is_the_default(self, app):
        _uri, headers, _body_bytes = HttpSignature.signed_request(INBOX, _body(), a_keypair()[0], KEY_ID,
                                                                  send_via_async=True)
        assert 'Digest' in headers and 'Signature-Input' not in headers

    def test_an_unknown_format_is_refused(self, app):
        with pytest.raises(ValueError, match='Unknown signature format'):
            HttpSignature.signed_request(INBOX, _body(), a_keypair()[0], KEY_ID, send_via_async=True,
                                         signature_format='other')


class TestDoubleKnock:

    def _send(self, statuses):
        sent = []

        def send(signature_format):
            sent.append(signature_format)
            return httpx.Response(statuses[len(sent) - 1])
        return send, sent

    def test_no_401_sends_once_in_the_stored_format(self, app, db_session):
        make_instance('peer.example').signature_format = 'rfc9421'
        db.session.commit()
        send, sent = self._send([202])
        assert double_knock(INBOX, send).status_code == 202
        assert sent == ['rfc9421']

    def test_an_unknown_instance_is_cavage_and_nothing_is_stored(self, app, db_session):
        send, sent = self._send([401, 202])
        assert double_knock(INBOX, send).status_code == 202
        assert sent == ['cavage', 'rfc9421'] and Instance.query.count() == 0

    @pytest.mark.parametrize('stored, alternate', [('cavage', 'rfc9421'), ('rfc9421', 'cavage')])
    def test_a_401_then_success_remembers_the_other_format(self, app, db_session, stored, alternate):
        instance = make_instance('peer.example')
        instance.signature_format = stored
        db.session.commit()
        send, sent = self._send([401, 200])
        double_knock(INBOX, send)
        assert sent == [stored, alternate]
        db.session.refresh(instance)
        assert instance.signature_format == alternate

    def test_a_peer_refusing_both_keeps_the_format_after_exactly_two_attempts(self, app, db_session):
        """Review Focus 5: a 401 that is not about the format (e.g. a domain block)."""
        instance = make_instance('peer.example')
        send, sent = self._send([401, 401])
        assert double_knock(INBOX, send).status_code == 401
        assert sent == ['cavage', 'rfc9421']
        db.session.refresh(instance)
        assert instance.signature_format == 'cavage'

    def test_a_stored_value_that_is_not_a_format_is_cavage(self, app, db_session):
        make_instance('peer.example').signature_format = 'bogus'
        db.session.commit()
        send, sent = self._send([202])
        double_knock(INBOX, send)
        assert sent == ['cavage']


class TestPostRequest:

    def test_a_401_then_202_delivers_with_a_real_rfc9421_signature(self, app, db_session, http_mock):
        private_key, public_key = a_keypair()
        instance = make_instance('peer.example')
        route = http_mock.post(INBOX).mock(side_effect=[httpx.Response(401), httpx.Response(202)])
        post_request(uri=INBOX, body=_body(), private_key=private_key, key_id=KEY_ID)
        first, second = (call.request for call in route.calls)
        assert 'signature-input' not in first.headers and 'signature' in first.headers
        assert rfc9421.verify('POST', INBOX, second.headers, second.content, public_key)
        db.session.refresh(instance)
        assert instance.signature_format == 'rfc9421'
        assert ActivityPubLog.query.one().result == 'success'

    def test_a_peer_refusing_both_is_a_logged_failure_and_not_queued(self, app, db_session, http_mock):
        """Spec S5's 'SendQueue retry' does not match post_request, which never queues a 4xx (see Plan clarification 6)."""
        make_instance('peer.example')
        route = http_mock.post(INBOX).mock(return_value=httpx.Response(401, text='blocked'))
        post_request(uri=INBOX, body=_body(), private_key=a_keypair()[0], key_id=KEY_ID)
        assert route.call_count == 2
        log = ActivityPubLog.query.one()
        assert log.result == 'failure' and log.exception_message.startswith('401: blocked')
        assert SendQueue.query.count() == 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./run_tests.sh tests/test_migration_instance_signature_format.py tests/test_ap_security_double_knock.py -q`
Expected: FAIL. The migration file is not found, and there is `ImportError: cannot import name 'double_knock'`.

- [ ] **Step 3: Implement**

`migrations/versions/c4a8e2f61d3b_instance_signature_format.py`:

```python
"""instance signature format

Revision ID: c4a8e2f61d3b
Revises: b7e1c2d9f4a6
"""
import sqlalchemy as sa
from alembic import op

revision = 'c4a8e2f61d3b'
down_revision = 'b7e1c2d9f4a6'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('instance', sa.Column('signature_format', sa.String(length=16), nullable=False,
                                        server_default='cavage'))


def downgrade():
    op.drop_column('instance', 'signature_format')
```

`app/models.py`, `Instance`, after `popular`:

```python
    signature_format = db.Column(db.String(16), nullable=False, server_default='cavage')  # S5: 'cavage' or 'rfc9421', learned by double-knocking
```

`app/activitypub/signature.py`. Module level, after `request_key_id`:

```python
# The HTTP signature formats this instance can send (S5). A peer's preference is learned by double-knocking.
SIGNATURE_FORMATS = ('cavage', 'rfc9421')


def _other_signature_format(signature_format: str) -> str:
    return 'rfc9421' if signature_format == 'cavage' else 'cavage'


def _instance_signature_format(domain: str) -> str:
    session = get_task_session()
    try:
        stored = session.query(Instance.signature_format).filter_by(domain=domain).scalar()
    finally:
        session.close()
    return stored if stored in SIGNATURE_FORMATS else 'cavage'


def _remember_signature_format(domain: str, signature_format: str) -> None:
    session = get_task_session()
    try:
        session.query(Instance).filter_by(domain=domain).update({'signature_format': signature_format})
        session.commit()
    finally:
        session.close()


def double_knock(uri: str, send) -> httpx.Response:
    """Send in the signature format `uri`'s instance is known to accept; on a 401 try the other format exactly
    once, remembering it when that succeeds. `send(signature_format)` makes one signed request. The format is read
    and written in short sessions of their own, so no database connection is held across the HTTP request."""
    domain = activitypub_util.host_of(uri)
    signature_format = _instance_signature_format(domain)
    response = send(signature_format)
    if response.status_code != 401:
        return response
    response.close()
    alternate = _other_signature_format(signature_format)
    response = send(alternate)
    if 200 <= response.status_code < 300:
        _remember_signature_format(domain, alternate)
    return response
```

`HttpSignature.signed_request`: add the parameter `signature_format: str = 'cavage'` after `send_via_async=False`. After the two `if "://"`/`is_invalid_get_request_uri` guards, add:

```python
        if signature_format not in SIGNATURE_FORMATS:
            raise ValueError(f"Unknown signature format {signature_format}")
```

Replace the block from `# Sign the headers` to `del headers["(request-target)"]` with:

```python
        if signature_format == 'rfc9421':
            del headers["(request-target)"]
            headers.update(rfc9421.sign(method, uri, body_bytes if body is not None else None, private_key, key_id))
        else:
            # Sign the headers
            signed_string = "\n".join(
                f"{name.lower()}: {value}" for name, value in headers.items()
            )

            private_key_instance = cls._get_private_key_instance(private_key)

            # RSA signature generation
            signature = private_key_instance.sign(
                signed_string.encode("ascii"),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
            headers["Signature"] = cls.compile_signature(
                {
                    "keyid": key_id,
                    "headers": list(headers.keys()),
                    "signature": signature,
                    "algorithm": "rsa-sha256",
                }
            )
            del headers["(request-target)"]

        headers["User-Agent"] = f'PieFed/{current_app.config["VERSION"]}; +https://{current_app.config["SERVER_NAME"]}'
```

The User-Agent and send code below stays as it is. Delete the old `# Send the request with all those headers except the pseudo one` / `del headers["(request-target)"]` pair after User-Agent, because each branch has already removed it.

`post_request`: replace the line `result = HttpSignature.signed_request(uri, body, private_key, key_id, content_type, method, timeout)` with:

```python
                result = double_knock(uri, lambda signature_format: HttpSignature.signed_request(
                    uri, body, private_key, key_id, content_type, method, timeout, signature_format=signature_format))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./run_tests.sh tests/test_migration_instance_signature_format.py tests/test_ap_security_double_knock.py tests/test_activitypub_signature.py tests/test_inbox_gate_signatures.py tests/test_no_inline_imports.py -q`
Expected: PASS. The existing `_deliver` tests patch `signed_request` with a MagicMock returning `_Response(...)`. That response has `status_code` and `close`, so `double_knock` works with it unchanged.

- [ ] **Step 5: Commit**

```bash
git add migrations/versions/c4a8e2f61d3b_instance_signature_format.py app/models.py app/activitypub/signature.py tests/test_migration_instance_signature_format.py tests/test_ap_security_double_knock.py
git commit -m "feat(ap): double-knock outbound deliveries with RFC 9421

Deliveries are signed in the format the target instance is known to
accept (cavage by default). A 401 gets exactly one retry in the other
format, and a success records it on Instance.signature_format. A peer
refusing both formats is a logged failure as before.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 21: Signed GET double-knocking (ends phase S5)

**Files:**
- Modify: `app/activitypub/signature.py:202-205` (`signed_get_request`)
- Modify: `tests/test_activitypub_signature.py:928-942` (`test_a_signed_get_is_the_same_request_without_a_body`: the double now returns a response object)
- Test: `tests/test_ap_security_double_knock.py` (append)

**Interfaces:**
- Consumes: `double_knock` (Task 20).
- Produces: `signed_get_request(uri, private_key, key_id, content_type=..., method='get', timeout=10)` double-knocks. Its signature is unchanged.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_ap_security_double_knock.py`:

```python
class TestSignedGet:

    def test_a_401_then_200_remembers_rfc9421(self, app, db_session, http_mock):
        private_key, public_key = a_keypair()
        instance = make_instance('peer.example')
        route = http_mock.get('https://peer.example/u/bob').mock(
            side_effect=[httpx.Response(401), httpx.Response(200, json={'id': 'https://peer.example/u/bob'})])
        response = signed_get_request('https://peer.example/u/bob', private_key, KEY_ID)
        assert response.status_code == 200 and route.call_count == 2
        second = route.calls[1].request
        assert rfc9421.verify('GET', 'https://peer.example/u/bob', second.headers, b'', public_key,
                              rfc9421.RFC9421_GET_REQUIRED)
        db.session.refresh(instance)
        assert instance.signature_format == 'rfc9421'
```

Add `signed_get_request` to the file's `from app.activitypub.signature import ...` line.

Update `tests/test_activitypub_signature.py:928-942` so the double returns a response object:

```python
    with patch('app.activitypub.signature.HttpSignature.signed_request') as signed:
        signed.return_value = _Response(200)
        result = signed_get_request('https://remote.example/u/bob', 'k', 'kid')

    assert result is signed.return_value
    assert signed.call_args.args[1] is None
    assert signed.call_args.args[5] == 'get'
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `./run_tests.sh tests/test_ap_security_double_knock.py::TestSignedGet -q`
Expected: FAIL. `route.call_count == 1` and the response status is 401.

- [ ] **Step 3: Implement**

```python
def signed_get_request(uri: str, private_key: str, key_id: str, content_type: str = "application/activity+json",
                       method: Literal["get", "post"] = "get", timeout: int = 10, ):
    return double_knock(uri, lambda signature_format: HttpSignature.signed_request(
        uri, None, private_key, key_id, content_type, method, timeout, signature_format=signature_format))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./run_tests.sh tests/test_ap_security_double_knock.py tests/test_activitypub_signature.py tests/test_ap_security_key_refresh.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/activitypub/signature.py tests/test_ap_security_double_knock.py tests/test_activitypub_signature.py
git commit -m "feat(ap): double-knock signed GETs too

Actor and object fetches use the same format memory as deliveries, so
a peer that only accepts RFC 9421 can still be fetched from.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 6: Phase S5 gate**

Run, in order:
1. `./run_tests.sh tests/ --cov=app --cov=fastapi_server --cov-branch --cov-report=json:coverage.json -q`. Expected: all pass. Read the passed count.
2. `python3 tests/check_changed_line_coverage.py coverage.json dafbb3aa0 --branches app/ fastapi_server.py`. Expected: 100%.
3. `python3 tests/check_coverage_floors.py coverage.json coverage_floors.ini`. Expected: no floor dropped, and `signature_errors.py`/`signature_rfc9421.py` at 100.

Fix any gap before Phase S6.

## Phase S6: authorized fetch, and follow-up

### Task 22: S6 — signer-dependent post and comment responses say so in `Vary`

**Files:**
- Modify: `app/activitypub/routes.py` (new `ap_content_headers` next to `tombstone_response` around R:2474; `comment_ap` R:2459-2466; `post_ap` R:2535-2540; `post_replies_ap` R:2577-2578; `post_ap_context` R:2605-2606)
- Modify: `tests/test_ap_content_objects.py:310` and `:413` (their expected `Vary` strings)
- Test: `tests/test_ap_security_ap_caching.py` (new)

**Interfaces:**
- Consumes: `AP_CACHE_CONTENT` (R:72), and `User.has_blocked_instances()`.
- Produces: `ap_content_headers(resp, obj) -> None` in `app/activitypub/routes.py`.
  - `obj` is the `Post` or `PostReply` being served. For the replies and context collections, it is the post.
  - It sets `Cache-Control` and `Vary`.
  - Task 23 adds its non-public branch.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ap_security_ap_caching.py`:

```python
"""Phase S6 groundwork: a post or comment response whose answer depends on who signed the request says so.

The author-blocks-instances guard (D194) already answers differently per signer: a blocked instance gets 401,
everyone else 200. Vary named only User-Agent, so a shared cache could hand one signer's 200 to another. None
of these views is cached server-side either: Flask-Caching's @cache.cached keys on the URL alone.
"""
import pytest

from app.activitypub import routes as activitypub_routes
from tests.factories import make_instance, make_instance_block, make_post_reply
from tests.test_ap_content_objects import _double_the_delegates, ap_get, seed_local_post

PATHS = ['/post/{post}', '/post/{post}/replies', '/post/{post}/context', '/comment/{reply}']


def _vary(response):
    return [part.strip() for part in response.headers['Vary'].split(',')]


@pytest.mark.parametrize('path', PATHS)
def test_a_response_from_an_author_who_blocks_instances_varies_on_the_signature(app, db_session, monkeypatch, path):
    _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()
    make_instance_block(author, make_instance('other.example'))
    make_instance('peer2.example')
    reply = make_post_reply(post, author)

    response = ap_get(app, path.format(post=post.id, reply=reply.id), user_agent='Test (+https://peer2.example)')

    assert response.status_code == 200
    assert _vary(response)[:3] == ['Accept', 'User-Agent', 'Signature']
    assert response.headers['Cache-Control'] == 'public, max-age=120'


@pytest.mark.parametrize('path', PATHS)
def test_a_response_from_an_author_who_blocks_nobody_varies_on_accept_only(app, db_session, monkeypatch, path):
    _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()
    reply = make_post_reply(post, author)

    response = ap_get(app, path.format(post=post.id, reply=reply.id))

    assert response.status_code == 200
    assert _vary(response)[0] == 'Accept'
    assert 'Signature' not in _vary(response) and 'User-Agent' not in _vary(response)
    assert response.headers['Cache-Control'] == 'public, max-age=120'


@pytest.mark.parametrize('view', ['comment_ap', 'post_ap', 'post_ap2', 'post_replies_ap', 'post_ap_context'])
def test_no_post_or_comment_view_is_cached_server_side_by_url(view):
    """Flask-Caching's cached() marks the view it wraps with `uncached`. A signer-dependent answer cached by URL
    would be served to the next requester, whoever signed."""
    assert not hasattr(getattr(activitypub_routes, view), 'uncached')
```

In `tests/test_ap_content_objects.py`, change both assertions at lines 310 and 413 from

```python
    assert response.headers['Vary'] == 'Accept, User-Agent, Accept-Encoding'
```

to

```python
    assert response.headers['Vary'] == 'Accept, User-Agent, Signature, Accept-Encoding'
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./run_tests.sh tests/test_ap_security_ap_caching.py tests/test_ap_content_objects.py -q`
Expected:
- The four `..._varies_on_the_signature` rows FAIL. Post and comment show `['Accept', 'User-Agent', 'Accept-Encoding']`; replies and context show `['Accept', 'Accept-Encoding']`.
- The two edited rows in `test_ap_content_objects.py` FAIL.
- The ratchet rows PASS. They guard against regressions only.

- [ ] **Step 3: Implement `ap_content_headers` and use it in all four views**

In `app/activitypub/routes.py`, add directly after `tombstone_response`:

```python
def ap_content_headers(resp, obj):
    """Cache-Control and Vary for a served post or comment, or a post's replies or context collection.

    An author who blocks instances is refused to some signers and served to others (D194), so the response then
    varies on the signature as well as on the User-Agent that identifies an unsigned requester."""
    resp.headers.set('Cache-Control', AP_CACHE_CONTENT)
    resp.headers.set('Vary', 'Accept, User-Agent, Signature' if obj.author.has_blocked_instances() else 'Accept')
```

In `comment_ap`, replace

```python
        if reply.author.has_blocked_instances():
            resp.headers.set('Vary', 'Accept, User-Agent')
        else:
            resp.headers.set('Vary', 'Accept')
        resp.headers.set('Cache-Control', AP_CACHE_CONTENT)
```

with

```python
        ap_content_headers(resp, reply)
```

In `post_ap`, replace

```python
            resp.headers.set('Cache-Control', AP_CACHE_CONTENT)
            if post.author.has_blocked_instances():
                resp.headers.set('Vary', 'Accept, User-Agent')
            else:
                resp.headers.set('Vary', 'Accept')
```

with

```python
            ap_content_headers(resp, post)
```

In both `post_replies_ap` and `post_ap_context`, replace

```python
        resp.headers.set('Vary', 'Accept')
        resp.headers.set('Cache-Control', AP_CACHE_CONTENT)
```

with

```python
        ap_content_headers(resp, post)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./run_tests.sh tests/test_ap_security_ap_caching.py tests/test_ap_content_objects.py tests/test_ap_collections.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/activitypub/routes.py tests/test_ap_security_ap_caching.py tests/test_ap_content_objects.py
git commit -m "fix: post and comment AP responses that depend on the signer vary on Signature

An author who blocks instances is refused to some signers and served to others,
but Vary named only User-Agent, so a shared cache could serve one signer's
answer to another. The replies and context collections now vary the same way.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 23: S6 — a signed fetch by an entitled requester unlocks followers-only content

**Files:**
- Modify: `app/activitypub/routes.py`:
  - imports (R:19-53: `app.activitypub.entitlement`, plus `is_open` from `app.visibility`)
  - new `followers_only_addressees` and `followers_entitled` next to `ap_content_headers`
  - `ap_content_headers`
  - `comment_ap` (visibility check and the served document)
  - `post_ap_refusal`
  - `post_ap` (the served document)
- Test: `tests/test_ap_security_authorized_fetch.py` (new)

**Interfaces:**
- Consumes:
  - From Task 2: `signed_requester_actor() -> User | Community | None`, `audience_entitles(addressees, requester, local_actor=None) -> bool` (rule 4 as described in Plan clarification 12), and `PRIVATE_HEADERS`.
  - `is_open(obj)` (`app/visibility.py`), `VISIBILITY_FOLLOWERS` (`app.constants`, already star-imported in R), `User.followers_url()` and `User.is_local()`.
  - `ap_content_headers` (Task 22).
- Produces:
  - `followers_only_addressees(obj) -> list[str]` (`[obj.author.followers_url()]`).
  - `followers_entitled(obj) -> bool`.
  - `ap_content_headers` now sets `PRIVATE_HEADERS` for any non-open `obj`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ap_security_authorized_fetch.py`:

```python
"""Phase S6 of the security track: a signed GET by a requester entitled to see it unlocks a local author's
followers-only post or comment. Spec: "Unlocking followers-only content".

Local followers-only content is set directly on the rows here: no local authoring path produces it yet (plan,
clarification 11). Entitled = an accepted follower of the author, or any signer on a host that holds one.
"""
from types import SimpleNamespace

import pytest

from app import db
from tests.factories import make_follow, make_instance, make_post, make_post_reply, make_user
from tests.test_ap_content_objects import _double_the_delegates, ap_get, seed_local_post, signed_ap_get

UA = 'Test (+https://peer2.example)'


def _vary(response):
    return [part.strip() for part in response.headers['Vary'].split(',')]


@pytest.fixture
def world(app, db_session, monkeypatch):
    _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()
    post.visibility = 'followers'
    reply = make_post_reply(post, author)
    reply.visibility = 'followers'
    db.session.commit()
    follower_host = make_instance('peer2.example')
    follower = make_user(follower_host, 'bob', with_keys=True)
    neighbour = make_user(follower_host, 'dave', with_keys=True)
    stranger = make_user(make_instance('peer3.example'), 'eve', with_keys=True)
    make_follow(author, follower, is_inward=True)    # bob follows the local author, accepted
    return SimpleNamespace(community=community, author=author, post=post, reply=reply, follower=follower,
                           neighbour=neighbour, stranger=stranger)


def _assert_private(response, author):
    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'private, no-store'
    assert 'Signature' in _vary(response)


def test_an_accepted_follower_gets_a_followers_only_post_privately(app, world):
    response = signed_ap_get(app, f'/post/{world.post.id}', world.follower, UA)

    _assert_private(response, world.author)
    # post_to_page always says Public; what an entitled signer gets says who it is for
    assert response.json['to'] == [world.author.followers_url()]
    assert response.json['cc'] == []


def test_a_signer_on_a_followers_host_gets_it_too(app, world):
    _assert_private(signed_ap_get(app, f'/post/{world.post.id}', world.neighbour, UA), world.author)


def test_a_signer_sharing_no_host_with_a_follower_gets_404(app, world):
    assert signed_ap_get(app, f'/post/{world.post.id}', world.stranger, UA).status_code == 404


def test_an_unsigned_request_gets_404(app, world):
    assert ap_get(app, f'/post/{world.post.id}').status_code == 404


def test_a_pending_follow_does_not_unlock_it(app, world):
    pending = make_user(make_instance('peer4.example'), 'pat', with_keys=True)
    make_follow(world.author, pending, is_accepted=None, is_inward=True)

    assert signed_ap_get(app, f'/post/{world.post.id}', pending, UA).status_code == 404


def test_an_entitled_answer_is_not_left_for_the_next_requester(app, world):
    assert signed_ap_get(app, f'/post/{world.post.id}', world.follower, UA).status_code == 200
    assert ap_get(app, f'/post/{world.post.id}').status_code == 404


def test_a_followers_only_comment_is_served_privately_to_a_follower(app, world):
    response = signed_ap_get(app, f'/comment/{world.reply.id}', world.follower, UA)

    _assert_private(response, world.author)
    assert response.json['to'] == [world.author.followers_url()]
    assert response.json['cc'] == []


def test_a_followers_only_comment_is_404_unsigned(app, world):
    assert ap_get(app, f'/comment/{world.reply.id}').status_code == 404


@pytest.mark.parametrize('suffix', ['/replies', '/context'])
def test_a_followers_only_posts_collections_are_served_privately_to_a_follower(app, world, suffix):
    _assert_private(signed_ap_get(app, f'/post/{world.post.id}{suffix}', world.follower, UA), world.author)


def test_a_remote_authors_followers_only_post_stays_404_even_for_a_follower(app, world):
    remote_post = make_post(world.community, world.stranger, 'https://peer3.example/p/1')
    remote_post.visibility = 'followers'
    db.session.commit()
    make_follow(world.stranger, world.follower, is_inward=True)

    assert signed_ap_get(app, f'/post/{remote_post.id}/replies', world.follower, UA).status_code == 404


def test_a_direct_post_stays_404_for_a_follower(app, world):
    world.post.visibility = 'direct'
    db.session.commit()

    assert signed_ap_get(app, f'/post/{world.post.id}', world.follower, UA).status_code == 404


def test_a_public_post_keeps_its_public_cache_headers(app, world):
    world.post.visibility = 'public'
    db.session.commit()

    response = ap_get(app, f'/post/{world.post.id}')

    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'public, max-age=120'
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./run_tests.sh tests/test_ap_security_authorized_fetch.py -q`
Expected: every row in which a follower or neighbour should get 200 FAILS with `assert 404 == 200`. The 404 rows and the public row pass already.

- [ ] **Step 3: Implement the unlock in `app/activitypub/routes.py`**

Imports. Merge these with whatever Task 3 already added:

```python
from app.activitypub.entitlement import PRIVATE_HEADERS, audience_entitles, signed_requester_actor
from app.visibility import OPEN_VISIBILITIES, is_open, listable_clause, post_title_for
```

Add next to `ap_content_headers`:

```python
def followers_only_addressees(obj) -> list[str]:
    """Who a followers-only post or comment is for: its author's followers collection (S6)."""
    return [obj.author.followers_url()]


def followers_entitled(obj) -> bool:
    """True when this GET's signer may see `obj`, a local author's followers-only post or comment (S6)."""
    return (obj.visibility == VISIBILITY_FOLLOWERS and obj.author.is_local() and
            audience_entitles(followers_only_addressees(obj), signed_requester_actor(), local_actor=obj.author))
```

Make `ap_content_headers` handle non-open objects first:

```python
def ap_content_headers(resp, obj):
    """Cache-Control and Vary for a served post or comment, or a post's replies or context collection.

    A non-public object is served only to an entitled signer (S6), so it is never stored by a shared cache. An
    author who blocks instances is refused to some signers and served to others (D194), so the response then
    varies on the signature as well as on the User-Agent that identifies an unsigned requester."""
    if not is_open(obj):
        resp.headers.update(PRIVATE_HEADERS)
        return
    resp.headers.set('Cache-Control', AP_CACHE_CONTENT)
    resp.headers.set('Vary', 'Accept, User-Agent, Signature' if obj.author.has_blocked_instances() else 'Accept')
```

In `post_ap_refusal`, change

```python
    if post.visibility not in OPEN_VISIBILITIES:
        abort(404)
```

to

```python
    if post.visibility not in OPEN_VISIBILITIES and not followers_entitled(post):
        abort(404)
```

In `comment_ap`, change

```python
        if reply.visibility not in OPEN_VISIBILITIES:
            abort(404)
```

to

```python
        if reply.visibility not in OPEN_VISIBILITIES and not followers_entitled(reply):
            abort(404)
```

Still in `comment_ap`, directly after `reply_data = comment_model_to_json(reply) if request.method == 'GET' else []`, add:

```python
        if request.method == 'GET' and not is_open(reply):   # S6: say who it is for, not Public
            reply_data['to'] = followers_only_addressees(reply)
            reply_data['cc'] = []
```

In `post_ap`, inside `if request.method == 'GET':` after `post_data['@context'] = default_context()`, add:

```python
                if not is_open(post):   # S6: say who it is for, not Public
                    post_data['to'] = followers_only_addressees(post)
                    post_data['cc'] = []
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./run_tests.sh tests/test_ap_security_authorized_fetch.py tests/test_ap_security_ap_caching.py tests/test_ap_content_objects.py tests/test_visibility*.py tests/test_no_inline_imports.py tests/test_import_order.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/activitypub/routes.py tests/test_ap_security_authorized_fetch.py
git commit -m "feat: a signed fetch by an entitled requester unlocks followers-only content

A local author's followers-only post or comment, and the post's replies and
context, are served to an accepted follower or a signer on a follower's host.
The response is private and no-store, and is addressed to the author's
followers rather than Public. Everyone else still gets 404.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 24: S6 — secure mode: an admin can require signed fetches

**Files:**
- Create: `migrations/versions/d9f3b7a15c2e_site_require_signed_fetch.py`
- Modify: `app/models.py:5198` (`Site`, after `allowlist_mode`)
- Modify: `app/activitypub/entitlement.py` (`SIGNED_FETCH_EXEMPT_PREFIXES`, `signed_fetch_exempt`, `signed_fetch_refusal`)
- Modify: `app/request_hooks.py:91-120` (the app-wide `before_request`)
- Modify: `app/admin/forms.py:110-127` (`FederationForm`), `app/admin/routes.py:477-533` (`admin_federation`)
- Test: `tests/test_ap_security_secure_mode.py` (new), `tests/test_admin_federation.py` (append)

**Interfaces:**
- Consumes: `signed_requester_actor()` (Task 2); `is_activitypub_request()` (`app/utils.py:2135`); `g.site`, which the app-wide `before_request` loads from `get_site_as_dict()` for every path except `/inbox` and `/static/`.
- Produces:
  - `Site.require_signed_fetch` (Boolean, not null, server default false).
  - In `app/activitypub/entitlement.py`:
    - `SIGNED_FETCH_EXEMPT_PREFIXES = ('/actor', '/.well-known/', '/nodeinfo/')`. A prefix ending in `/` matches by prefix. One without matches exactly, or as a path segment followed by `/`.
    - `signed_fetch_exempt(path: str) -> bool`.
    - `signed_fetch_refusal() -> Response | None`.
  - `FederationForm.require_signed_fetch`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ap_security_secure_mode.py`:

```python
"""Phase S6 of the security track: secure mode. With Site.require_signed_fetch on, an ActivityPub GET or HEAD that
no stored actor validly signed gets 401. The instance actor, WebFinger and NodeInfo stay open; HTML is never
affected. Spec: "Secure mode". Review Focus 4.
"""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from flask import g
from sqlalchemy import inspect, text

from app import db
from app.activitypub.entitlement import signed_fetch_exempt, signed_fetch_refusal
from app.models import Site
from tests.factories import a_keypair, make_instance, make_site, make_user
from tests.test_ap_content_objects import AP_ACCEPT, _double_the_delegates, ap_get, browser_get, seed_local_post, \
    signed_ap_get

MIGRATION = Path(__file__).resolve().parent.parent / 'migrations' / 'versions' / 'd9f3b7a15c2e_site_require_signed_fetch.py'
UA = 'Test (+https://peer2.example)'


# --- the exemption and the refusal, unit level ---

@pytest.mark.parametrize('path, exempt', [
    ('/actor', True), ('/actor/outbox', True), ('/actor/inbox', True),
    ('/.well-known/webfinger', True), ('/.well-known/nodeinfo', True),
    ('/nodeinfo/2.0', True), ('/nodeinfo/2.1.json', True),
    ('/actors', False), ('/actorx/outbox', False), ('/nodeinfo', False), ('/post/1', False), ('/u/alice', False),
])
def test_the_exempt_routes(path, exempt):
    assert signed_fetch_exempt(path) is exempt


def _refusal(app, path='/post/1', method='GET', accept=AP_ACCEPT, secure=True):
    with app.test_request_context(path, method=method, headers={'Accept': accept}):
        g.site = SimpleNamespace(require_signed_fetch=secure)
        return signed_fetch_refusal()


@pytest.mark.parametrize('method', ['GET', 'HEAD'])
def test_an_unsigned_activitypub_fetch_is_refused_with_a_no_store_401(app, method):
    response = _refusal(app, method=method)

    assert response.status_code == 401
    assert response.headers['Cache-Control'] == 'no-store'


@pytest.mark.parametrize('kwargs', [{'method': 'POST'}, {'accept': 'text/html'}, {'secure': False},
                                    {'path': '/actor/outbox'}, {'path': '/.well-known/nodeinfo'}])
def test_nothing_else_is_refused(app, kwargs):
    assert _refusal(app, **kwargs) is None


def test_a_request_with_no_site_loaded_is_not_refused(app):
    """/inbox and /static/ never load g.site."""
    with app.test_request_context('/post/1', headers={'Accept': AP_ACCEPT}):
        assert signed_fetch_refusal() is None


# --- through the real request hook ---

@pytest.fixture
def secure_post(app, db_session, monkeypatch):
    calls = _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()
    db.session.get(Site, 1).require_signed_fetch = True
    db.session.commit()
    return SimpleNamespace(post=post, calls=calls)


def test_secure_mode_refuses_an_unsigned_fetch_of_a_post(app, secure_post):
    response = ap_get(app, f'/post/{secure_post.post.id}')

    assert response.status_code == 401
    assert response.headers['Cache-Control'] == 'no-store'


def test_secure_mode_refuses_an_unsigned_head_like_a_get(app, secure_post):
    with app.test_client() as client:
        assert client.head(f'/post/{secure_post.post.id}', headers={'Accept': AP_ACCEPT}).status_code == 401


def test_secure_mode_serves_a_signer_stored_here(app, secure_post):
    signer = make_user(make_instance('peer2.example'), 'bob', with_keys=True)

    assert signed_ap_get(app, f'/post/{secure_post.post.id}', signer, UA).status_code == 200


def test_secure_mode_refuses_a_signer_never_seen_here(app, secure_post):
    """No fetch on the request path: an unknown signer is as good as no signature."""
    private_key, _public = a_keypair()
    ghost = SimpleNamespace(private_key=private_key, ap_profile_id='https://unknown.example/users/ghost')

    assert signed_ap_get(app, f'/post/{secure_post.post.id}', ghost, UA).status_code == 401


def test_secure_mode_leaves_a_browser_alone(app, secure_post):
    response = browser_get(app, f'/post/{secure_post.post.id}')

    assert response.status_code == 200
    assert len(secure_post.calls['show_post']) == 1


def test_secure_mode_leaves_nodeinfo_open(app, secure_post):
    assert ap_get(app, '/.well-known/nodeinfo').status_code == 200


def test_with_secure_mode_off_an_unsigned_fetch_is_served(app, db_session, monkeypatch):
    _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()

    assert ap_get(app, f'/post/{post.id}').status_code == 200


# --- the migration ---

def _load_migration():
    spec = importlib.util.spec_from_file_location('require_signed_fetch_migration', MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _column(connection):
    return next((c for c in inspect(connection).get_columns('site') if c['name'] == 'require_signed_fetch'), None)


def test_require_signed_fetch_is_not_null_and_defaults_to_false(app):
    with db.engine.connect() as connection:
        column = _column(connection)
    assert column is not None
    assert column['nullable'] is False
    assert 'false' in column['default'].lower()


def test_downgrade_then_upgrade_restores_the_column_off(app, db_session):
    make_site()
    db.session.commit()
    db.session.close()    # nothing of the session's may still hold a lock on site
    migration = _load_migration()
    assert migration.down_revision == 'c4a8e2f61d3b'

    with db.engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text("SET LOCAL lock_timeout = '5s'"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
                assert _column(connection) is None
                migration.upgrade()
            assert _column(connection)['nullable'] is False
            assert connection.execute(text('SELECT require_signed_fetch FROM site WHERE id = 1')).scalar() is False
        finally:
            transaction.rollback()
```

Append to `tests/test_admin_federation.py`:

```python
# --------------------------------------------------------------------------
# admin_federation: secure mode (security track S6)
# --------------------------------------------------------------------------


def test_secure_mode_is_saved_from_the_federation_form(admin_client):
    client, token = admin_client

    _post(client, token, require_signed_fetch='y')

    db.session.expire_all()
    assert db.session.get(Site, 1).require_signed_fetch is True


def test_secure_mode_is_turned_off_when_the_box_is_unticked(admin_client):
    client, token = admin_client
    db.session.get(Site, 1).require_signed_fetch = True
    db.session.commit()

    _post(client, token)

    db.session.expire_all()
    assert db.session.get(Site, 1).require_signed_fetch is False


def test_the_federation_form_shows_the_stored_secure_mode(admin_client):
    client, token = admin_client
    db.session.get(Site, 1).require_signed_fetch = True
    db.session.commit()

    with patch('app.admin.routes.render_template', return_value='rendered') as render:
        client.get('/admin/federation')

    assert render.call_args.kwargs['form'].require_signed_fetch.data is True


def test_saving_the_federation_form_drops_the_memoized_site(admin_client, monkeypatch):
    """g.site comes from get_site_as_dict, memoized for 60 s: without this, secure mode would lag the checkbox."""
    client, token = admin_client
    dropped = []
    monkeypatch.setattr('app.admin.routes.cache.delete_memoized',
                        lambda function, *args: dropped.append(getattr(function, '__name__', function)))

    _post(client, token)

    assert 'get_site_as_dict' in dropped
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./run_tests.sh tests/test_ap_security_secure_mode.py tests/test_admin_federation.py -q`
Expected: collection ERROR for `tests/test_ap_security_secure_mode.py`: `ImportError: cannot import name 'signed_fetch_exempt' from 'app.activitypub.entitlement'`. The four new admin rows FAIL: `AttributeError: 'Site' object has no attribute 'require_signed_fetch'` on the first three, `'get_site_as_dict' not in []` on the last.

- [ ] **Step 3: Add the column and the migration**

In `app/models.py`, in `class Site`, after `allowlist_mode`:

```python
    require_signed_fetch = db.Column(db.Boolean, default=False, server_default='false', nullable=False)  # S6 secure mode: refuse unsigned AP GETs
```

Create `migrations/versions/d9f3b7a15c2e_site_require_signed_fetch.py`:

```python
"""site require_signed_fetch (security track S6: secure mode)

Revision ID: d9f3b7a15c2e
Revises: c4a8e2f61d3b
"""
import sqlalchemy as sa
from alembic import op

revision = 'd9f3b7a15c2e'
down_revision = 'c4a8e2f61d3b'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('site', sa.Column('require_signed_fetch', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    op.drop_column('site', 'require_signed_fetch')
```

If the test stack's template database predates this migration, rebuild it with `./run_tests.sh --down`. The next run then replays every migration.

- [ ] **Step 4: Add the exemption and the refusal to `app/activitypub/entitlement.py`**

Merge these imports with the module's existing ones (Task 2):

```python
from flask import g, make_response, request

from app.utils import is_activitypub_request
```

Add:

```python
# Routes secure mode never refuses: the instance actor (whose key a peer needs before it can sign anything at all),
# WebFinger and NodeInfo. A prefix ending in '/' matches by prefix; one without matches exactly or as a whole
# path segment, so '/actor' covers '/actor/outbox' and '/actor/inbox' but not '/actors'.
SIGNED_FETCH_EXEMPT_PREFIXES = ('/actor', '/.well-known/', '/nodeinfo/')


def signed_fetch_exempt(path: str) -> bool:
    for prefix in SIGNED_FETCH_EXEMPT_PREFIXES:
        if prefix.endswith('/'):
            if path.startswith(prefix):
                return True
        elif path == prefix or path.startswith(prefix + '/'):
            return True
    return False


def signed_fetch_refusal():
    """The 401 secure mode answers an ActivityPub GET or HEAD that no actor stored here validly signed, else None.

    Never fetches: a signer this instance has not seen is refused until it is learned of some other way."""
    if request.method not in ('GET', 'HEAD') or not is_activitypub_request():
        return None
    site = getattr(g, 'site', None)
    if site is None or not site.require_signed_fetch:
        return None
    if signed_fetch_exempt(request.path) or signed_requester_actor() is not None:
        return None
    response = make_response('', 401)
    response.headers['Cache-Control'] = 'no-store'
    return response
```

- [ ] **Step 5: Call it from the app-wide hook in `app/request_hooks.py`**

Add the import with the other `app.` imports:

```python
from app.activitypub.entitlement import signed_fetch_refusal
```

In `before_request`, directly after the `if request.path != '/inbox' and not request.path.startswith('/static/'):` block that loads `g.site` and `g.admin_ids`, add:

```python
        refusal = signed_fetch_refusal()   # S6 secure mode: an unsigned ActivityPub fetch is refused
        if refusal is not None:
            return refusal
```

If `tests/test_import_order.py` reports a new cycle, move the import into the function body with a `# cycle:` comment that names the cycle, as Global Constraints allow.

- [ ] **Step 6: Add the admin checkbox**

In `app/admin/forms.py`, add this to `FederationForm` before `submit`:

```python
    require_signed_fetch = BooleanField(
        _l('Require signed fetches (secure mode)'),
        description=_l('Refuse ActivityPub requests that are not signed by a server this instance already knows. '
                       'Unsigned fetchers, including some crawlers and tools, will be refused. The instance actor, '
                       'WebFinger and NodeInfo stay open.'))
```

`app/templates/admin/federation.html` renders the form with `render_form(form)`, so it needs no template change.

In `app/admin/routes.py` `admin_federation`, in the POST branch after `site.allowlist_mode = int(form.allowlist_mode.data)`:

```python
        site.require_signed_fetch = form.require_signed_fetch.data
```

After the `db.session.commit()` that follows `cache.delete_memoized(get_setting, 'actor_blocked_words')`:

```python
        cache.delete_memoized(get_site_as_dict)   # g.site is read from it: secure mode and allowlist mode apply now
```

In the GET branch, after `form.auto_add_remote_communities.data = ...`:

```python
        form.require_signed_fetch.data = g.site.require_signed_fetch
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `./run_tests.sh tests/test_ap_security_secure_mode.py tests/test_admin_federation.py tests/test_request_hooks.py tests/test_ap_security_authorized_fetch.py tests/test_import_order.py tests/test_no_inline_imports.py -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add migrations/versions/d9f3b7a15c2e_site_require_signed_fetch.py app/models.py app/activitypub/entitlement.py \
    app/request_hooks.py app/admin/forms.py app/admin/routes.py \
    tests/test_ap_security_secure_mode.py tests/test_admin_federation.py
git commit -m "feat: secure mode lets an admin require signed ActivityPub fetches

With Site.require_signed_fetch on, an AP GET or HEAD that no stored actor
validly signed gets 401 no-store. The instance actor, WebFinger and NodeInfo
stay open, and HTML is never affected. The setting is a federation-page
checkbox, off by default. Saving that page now drops the memoized site, so the
setting applies at once.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 9: Phase S6 gate**

Run: `./run_tests.sh tests/ --cov=app --cov=fastapi_server --cov-branch --cov-report=json:coverage.json -q`
Expected: all tests pass. Read the passed count: a session-budget stop still exits 0.

Run: `python3 tests/check_changed_line_coverage.py coverage.json dafbb3aa0 --branches app/ fastapi_server.py`
Expected: 100% of changed lines and branches.

Run: `python3 tests/check_coverage_floors.py coverage.json coverage_floors.ini`
Expected: no floor dropped. `app/activitypub/entitlement.py` stays at 100.

---

### Task 25: Follow-up — enforce required signed headers by default (BLOCKED ON OWNER)

**Files:**
- Modify: `config.py` (the `SIG_REQUIRED_HEADERS_ENFORCE` line Task 6 added)
- Modify: `env.sample` (its `SIG_REQUIRED_HEADERS_ENFORCE` entry, added in Task 6; add one if absent)
- Modify: the Task 6 test that pins the old default (find it with `grep -rn "SIG_REQUIRED_HEADERS_ENFORCE" tests/`)
- Test: `tests/test_ap_security_enforcement_default.py` (new)

**Interfaces:**
- Consumes: `SIG_REQUIRED_HEADERS_ENFORCE` (Task 6). `verify_request(..., required=...)` reads it, so the verifier needs no change here.
- Produces: the default becomes `True`. Operators opt out with `SIG_REQUIRED_HEADERS_ENFORCE=0`.

- [ ] **Step 1: STOP — requires owner go-ahead**

Do not start this task until the owner says go in this session. They will first review production warnings from S2.1 commit 1 (Task 6): `journalctl` / container logs for the "unsigned required header" warning, grouped by keyId host. If any peer still signs too few headers, the owner decides whether to wait, contact it, or accept refusing it. Record the go-ahead and the date in the commit message.

- [ ] **Step 2: Write the failing test**

Create `tests/test_ap_security_enforcement_default.py`:

```python
"""Security track, Task 25: after the owner reviewed production's S2.1 warnings, an inbound signature that leaves
a required header unsigned is refused by default. An operator can still opt out."""
import importlib

import pytest

import config


@pytest.fixture
def reload_config(monkeypatch):
    yield lambda: importlib.reload(config)
    monkeypatch.undo()
    importlib.reload(config)    # leave the module as the rest of the suite imported it


def test_required_signed_headers_are_enforced_by_default(monkeypatch, reload_config):
    monkeypatch.delenv('SIG_REQUIRED_HEADERS_ENFORCE', raising=False)

    assert reload_config().Config.SIG_REQUIRED_HEADERS_ENFORCE is True


def test_an_operator_can_still_turn_enforcement_off(monkeypatch, reload_config):
    monkeypatch.setenv('SIG_REQUIRED_HEADERS_ENFORCE', '0')

    assert reload_config().Config.SIG_REQUIRED_HEADERS_ENFORCE is False
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `./run_tests.sh tests/test_ap_security_enforcement_default.py -q`
Expected: `test_required_signed_headers_are_enforced_by_default` FAILS with `assert False is True`. The opt-out row passes.

- [ ] **Step 4: Change the default**

In `config.py`:

```python
    SIG_REQUIRED_HEADERS_ENFORCE = os.environ.get('SIG_REQUIRED_HEADERS_ENFORCE', '1') == '1'
```

In `env.sample`:

```
# Refuse inbound HTTP signatures that leave a required header unsigned
# ((request-target), host, date, digest for POST; RFC 9421 equivalents). 1 = refuse (default), 0 = only log.
SIG_REQUIRED_HEADERS_ENFORCE=1
```

Update the Task 6 test that asserted the old default (off) to assert the new one. Any Task 6 row that tests the log-only path keeps working only if it sets the flag itself: `monkeypatch.setitem(app.config, 'SIG_REQUIRED_HEADERS_ENFORCE', False)`. Add that line where it is missing. Do not change what the row asserts.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `./run_tests.sh tests/test_ap_security_enforcement_default.py $(grep -rln "SIG_REQUIRED_HEADERS_ENFORCE\|POST_REQUIRED\|GET_REQUIRED" tests/) tests/test_inbox_gate_signatures.py tests/test_activitypub_signature.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add config.py env.sample tests/
git commit -m "fix: enforce required signed headers by default

Owner go-ahead given <date> after reviewing production's S2.1 warnings. An
inbound signature that leaves (request-target), host, date or digest (or their
RFC 9421 equivalents) unsigned is now refused. SIG_REQUIRED_HEADERS_ENFORCE=0
restores log-only.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

Replace `<date>` with the go-ahead date before committing.

- [ ] **Step 7: Full-suite gate**

Run: `./run_tests.sh tests/ --cov=app --cov=fastapi_server --cov-branch --cov-report=json:coverage.json -q`, then `python3 tests/check_changed_line_coverage.py coverage.json dafbb3aa0 --branches app/ fastapi_server.py` and `python3 tests/check_coverage_floors.py coverage.json coverage_floors.ini`.
Expected: all pass, 100% of changed lines and branches covered, no floor dropped.

Rollout: the production `.env.quadlet` needs no change. Leaving the variable unset now means enforce. To opt out, an operator adds `SIG_REQUIRED_HEADERS_ENFORCE=0` there and redeploys.

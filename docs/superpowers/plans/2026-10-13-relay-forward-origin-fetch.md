# Relay forwards without an LD signature: fetch from the origin — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A forward from an accepted relay whose own HTTP signature verifies but which has no verifiable LD
signature is answered 200, and a `Create`'s object is fetched from its origin in the background instead of the
request being refused with 400.

**Architecture:** A new `accept_unsigned_forward(relay, request_json, saved_json)` in `app/relays/inbound.py`
dedupes, filters and queues the existing Celery task `process_relayed_announce(relay_id, object_uri)` (which
fetches from the origin, re-checks the relay rules and creates under `relay_context`). `shared_inbox` calls it at
the two places that answer 400 after the actor's-key HTTP check fails, when `relay_for_forwarded(request)` names an
accepted relay.

**Tech Stack:** Flask, Celery, SQLAlchemy, pytest via `./run_tests.sh`.

**Spec:** `docs/superpowers/specs/2026-10-13-relay-forward-origin-fetch-design.md`

## Global Constraints

- No path creates content from the relay's copy of the object; only the origin fetch in the Celery task creates.
- Only `Create` is acted on; every other type from this path is answered 200 and logged ignored
  `relayed {type} without a verifiable signature`.
- The object URI's host must equal the activity actor's host and must not be `instance_banned`.
- Always 2xx from `accept_unsigned_forward`; a keyId that is not an accepted relay still gets the existing 400.
- No fetch, sleep or retry on the request path (D738/D775).
- TDD; 100% line and branch coverage of changed lines; one commit per task.
- Never assign `app.config` in tests; use `monkeypatch.setitem`.
- No function-level imports (`tests/test_no_inline_imports.py`); import order ratchet (`tests/test_import_order.py`).
- Run tests only via `./run_tests.sh <paths> -q -p no:cacheprovider`.
- Commit trailer: `Co-Authored-By: <your model> <noreply@anthropic.com>`.

## Review Focus

1. An object given as a bare string URI (no dict) is queued without the pre-filter, which needs a dict — Task 1.
2. A forward whose object host differs from the actor host is never queued, so a relay cannot steer our fetch —
   Task 1.
3. A retried forward is processed once (claim), and a queueing failure releases the claim — Task 1.
4. A delivery signed by a non-relay key, a pending relay, or a relay signature that fails (including a missing
   required header with enforcement on) still gets 400 — Task 2.
5. An LD-signed forward keeps today's path and keywords (`relay_id`, `ld_signed=True`) — Task 2 runs
   tests/test_relays_forwarded.py unchanged.

## File Structure

- Modify `app/relays/inbound.py`: add `accept_unsigned_forward`; import `instance_banned` from `app.utils` and
  `APLOG_CREATE` from `app.constants` if not yet imported; update `process_relayed_announce`'s docstring.
- Modify `app/activitypub/routes.py` (`shared_inbox`, the `except VerificationError` branch ~858-878): add
  `_unsigned_relay_forward(request, request_json, saved_json)` helper and call it at both 400 sites.
- Create `tests/test_relays_unsigned_forward.py`.

---

### Task 1: `accept_unsigned_forward`

**Files:**
- Modify: `app/relays/inbound.py` (add after `relay_actor_gate`)
- Test: `tests/test_relays_unsigned_forward.py` (create)

**Interfaces:**
- Consumes (existing, `app/relays/inbound.py`): `claim_activity(activity_id, actor_url) -> bool`,
  `release_activity(activity_id, actor_url)`, `relayed_activity_allowed(activity) -> (bool, str)`,
  `process_relayed_announce` (Celery task, `.apply_async(args=(relay_id, uri), queue='background')`),
  `host_of(url) -> str`, `log_incoming_ap(id, type_tuple, result, saved_json, message)`; `instance_banned(domain)`
  from `app.utils`.
- Produces: `accept_unsigned_forward(relay: Relay, request_json: dict, saved_json) -> tuple[str, int]` (always
  `('', 200)`).

- [ ] **Step 1: Write the failing tests**

```python
"""Relay forwards with no verifiable LD signature are fetched from the origin (spec 2026-10-13)."""
import uuid

import pytest

from app import db
from app.models import BannedInstances
from app.relays import RELAY_ACCEPTED
from app.relays import inbound
from tests.test_relays_inbound import NOTE_URI, make_relay, note

pytestmark = pytest.mark.usefixtures('site', 'redis_double')

AUTHOR = 'https://other.example/users/alice'


@pytest.fixture
def queued(app, monkeypatch):
    """Record what would be fetched; never run the task."""
    monkeypatch.setitem(app.config, 'DEBUG', True)
    calls = []
    monkeypatch.setattr(inbound, 'process_relayed_announce', lambda *args: calls.append(args))
    return calls


@pytest.fixture
def logged(monkeypatch):
    lines = []
    monkeypatch.setattr(inbound, 'log_incoming_ap', lambda *args: lines.append(args))
    return lines


def forward(obj=None, activity_type='Create', actor=AUTHOR):
    return {'id': f'{AUTHOR}/activities/{uuid.uuid4().hex}', 'type': activity_type, 'actor': actor,
            'object': note() if obj is None else obj}


class TestAcceptUnsignedForward:

    def test_a_create_is_queued_for_fetch_from_its_origin(self, app, queued, logged):
        relay = make_relay(state=RELAY_ACCEPTED)

        assert inbound.accept_unsigned_forward(relay, forward(), None) == ('', 200)

        assert queued == [(relay.id, NOTE_URI)]
        assert logged[-1][4] == 'relayed object queued for fetch from its origin'

    def test_a_bare_string_object_is_queued_without_the_pre_filter(self, app, queued, logged):
        relay = make_relay(state=RELAY_ACCEPTED)

        assert inbound.accept_unsigned_forward(relay, forward(obj=NOTE_URI), None) == ('', 200)

        assert queued == [(relay.id, NOTE_URI)]

    @pytest.mark.parametrize('activity_type', ['Update', 'Delete', 'Like', 'Announce'])
    def test_any_other_type_is_ignored(self, app, queued, logged, activity_type):
        relay = make_relay(state=RELAY_ACCEPTED)

        assert inbound.accept_unsigned_forward(relay, forward(activity_type=activity_type), None) == ('', 200)

        assert queued == []
        assert logged[-1][4] == f'relayed {activity_type} without a verifiable signature'

    @pytest.mark.parametrize('obj', [{'type': 'Note'}, {'id': 7}, 42, ''])
    def test_an_object_without_an_id_is_ignored(self, app, queued, logged, obj):
        relay = make_relay(state=RELAY_ACCEPTED)

        assert inbound.accept_unsigned_forward(relay, forward(obj=obj), None) == ('', 200)

        assert queued == []
        assert logged[-1][4] == 'relayed object has no id'

    def test_the_pre_filter_refuses_a_reply_to_a_post_this_instance_lacks(self, app, queued, logged):
        relay = make_relay(state=RELAY_ACCEPTED)
        obj = note(inReplyTo='https://other.example/notes/unknown')

        assert inbound.accept_unsigned_forward(relay, forward(obj=obj), None) == ('', 200)

        assert queued == []
        assert logged[-1][4] == 'relayed reply to a post this instance does not have'

    def test_an_object_on_another_host_than_the_actor_is_never_fetched(self, app, queued, logged):
        relay = make_relay(state=RELAY_ACCEPTED)
        obj = note(uri='https://elsewhere.example/notes/1')

        assert inbound.accept_unsigned_forward(relay, forward(obj=obj), None) == ('', 200)

        assert queued == []
        assert logged[-1][4] == 'relayed object host does not match actor'

    def test_an_object_from_a_banned_instance_is_never_fetched(self, app, queued, logged):
        relay = make_relay(state=RELAY_ACCEPTED)
        db.session.add(BannedInstances(domain='other.example'))
        db.session.commit()

        assert inbound.accept_unsigned_forward(relay, forward(), None) == ('', 200)

        assert queued == []
        assert logged[-1][4] == 'relayed object from a banned instance'

    def test_a_retried_forward_is_queued_once(self, app, queued, logged):
        relay = make_relay(state=RELAY_ACCEPTED)
        activity = forward()

        inbound.accept_unsigned_forward(relay, activity, None)
        assert inbound.accept_unsigned_forward(relay, activity, None) == ('', 200)

        assert len(queued) == 1
        assert logged[-1][4] == 'Already aware of this activity'

    def test_a_queueing_failure_releases_the_claim_so_the_retry_is_processed(self, app, logged, monkeypatch):
        monkeypatch.setitem(app.config, 'DEBUG', True)
        relay = make_relay(state=RELAY_ACCEPTED)
        activity = forward()

        def boom(*args):
            raise RuntimeError('broker down')

        monkeypatch.setattr(inbound, 'process_relayed_announce', boom)
        with pytest.raises(RuntimeError):
            inbound.accept_unsigned_forward(relay, activity, None)
        done = []
        monkeypatch.setattr(inbound, 'process_relayed_announce', lambda *args: done.append(args))

        assert inbound.accept_unsigned_forward(relay, activity, None) == ('', 200)
        assert done == [(relay.id, NOTE_URI)]

    def test_outside_debug_the_task_goes_to_the_background_queue(self, app, logged, monkeypatch):
        monkeypatch.setitem(app.config, 'DEBUG', False)
        relay = make_relay(state=RELAY_ACCEPTED)
        sent = []
        monkeypatch.setattr(inbound.process_relayed_announce, 'apply_async',
                            lambda args, queue: sent.append((args, queue)))

        inbound.accept_unsigned_forward(relay, forward(), None)

        assert sent == [((relay.id, NOTE_URI), 'background')]
```

- [ ] **Step 2: Run to verify they fail**

Run: `./run_tests.sh tests/test_relays_unsigned_forward.py -q -p no:cacheprovider`
Expected: FAIL with `AttributeError: module 'app.relays.inbound' has no attribute 'accept_unsigned_forward'`.

- [ ] **Step 3: Implement** (in `app/relays/inbound.py`, after `relay_actor_gate`)

```python
def accept_unsigned_forward(relay, request_json, saved_json):
    """A forward that accepted `relay` HTTP-signed but whose body has no verifiable LD signature (spec 2026-10-13).
    The relay's copy is never used: a Create's object is fetched from its origin in the background, where
    process_relayed_announce re-checks the relay rules. Always answered 200, so the relay keeps delivering."""
    activity_id = request_json.get('id') or ''
    actor = request_json.get('actor')
    if not claim_activity(activity_id, actor):
        log_incoming_ap(activity_id, APLOG_DUPLICATE, APLOG_IGNORED, saved_json, 'Already aware of this activity')
        return '', 200
    activity_type = request_json.get('type')
    if activity_type != 'Create':
        log_incoming_ap(activity_id, APLOG_NOTYPE, APLOG_IGNORED, saved_json,
                        f'relayed {activity_type} without a verifiable signature')
        return '', 200
    obj = request_json.get('object')
    uri = obj.get('id') if isinstance(obj, dict) else obj
    if not isinstance(uri, str) or not uri:
        log_incoming_ap(activity_id, APLOG_CREATE, APLOG_IGNORED, saved_json, 'relayed object has no id')
        return '', 200
    if isinstance(obj, dict):   # the unverified copy can only make us skip a fetch, never accept content
        allowed, reason = relayed_activity_allowed(request_json)
        if not allowed:
            log_incoming_ap(activity_id, APLOG_CREATE, APLOG_IGNORED, saved_json, reason)
            return '', 200
    host = host_of(uri)
    if not host or not isinstance(actor, str) or host != host_of(actor):
        log_incoming_ap(activity_id, APLOG_CREATE, APLOG_IGNORED, saved_json, 'relayed object host does not match actor')
        return '', 200
    if instance_banned(host):
        log_incoming_ap(activity_id, APLOG_CREATE, APLOG_IGNORED, saved_json, 'relayed object from a banned instance')
        return '', 200
    try:
        if current_app.debug:
            process_relayed_announce(relay.id, uri)
        else:
            process_relayed_announce.apply_async(args=(relay.id, uri), queue='background')
    except Exception:
        release_activity(activity_id, actor)   # R-DEDUPEUNDO: the relay's retry must be processed
        raise
    log_incoming_ap(activity_id, APLOG_CREATE, APLOG_SUCCESS, saved_json,
                    'relayed object queued for fetch from its origin')
    return '', 200
```

Add `APLOG_CREATE` to the existing `from app.constants import ...` line and `instance_banned` to the existing
`from app.utils import ...` line. Change `process_relayed_announce`'s first line to a docstring:
`"""Fetch a relayed object from its origin and store it (an Announce-style relay's Announce, or a Mastodon-style
forward with no verifiable LD signature)."""`

- [ ] **Step 4: Run tests**

Run: `./run_tests.sh tests/test_relays_unsigned_forward.py tests/test_relays_inbound.py tests/test_relays_forwarded.py tests/test_no_inline_imports.py tests/test_import_order.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/relays/inbound.py tests/test_relays_unsigned_forward.py
git commit -m "feat: a relay forward with no verifiable LD signature can be queued for fetch from its origin"
```

---

### Task 2: `shared_inbox` hands unsigned relay forwards to `accept_unsigned_forward`

**Files:**
- Modify: `app/activitypub/routes.py` (`shared_inbox`, the `except VerificationError` branch after
  `verify_with_key_refresh(request, actor, POST_REQUIRED)`; new module-level helper just above `shared_inbox`)
- Test: `tests/test_relays_unsigned_forward.py`

**Interfaces:**
- Consumes: `relay_inbound.relay_for_forwarded(request) -> Relay | None` (existing; `relay_inbound` is the name
  `routes.py` already imports `app.relays.inbound` under — verify); `relay_inbound.accept_unsigned_forward` (Task 1).
- Produces: `_unsigned_relay_forward(request, request_json, saved_json)` — the response tuple when an accepted
  relay signed the request, else None.

- [ ] **Step 1: Write the failing tests** (append; add to the imports: `from app.activitypub import routes`,
  `from app.activitypub.signature import LDSignature, VerificationError`, `from app.models import utcnow`,
  `from app.relays import RELAY_PENDING`, `from tests.factories import a_keypair, make_instance, make_user,
  signed_inbox_post`, `from tests.test_relays_inbound import ACTOR`)

```python
@pytest.fixture
def author(app, monkeypatch):
    monkeypatch.setitem(app.config, 'DEBUG', True)
    instance = make_instance('other.example')
    alice = make_user(instance, 'alice', with_keys=True)
    alice.ap_fetched_at = utcnow()
    db.session.commit()
    return alice


def deliver(app, author, relay_private_key, monkeypatch, ld=None):
    """POST a Create by `author` to /inbox, HTTP-signed by the relay. `ld`: None = no LD signature; else the body
    carries one and LDSignature.verify_signature does `ld` (True or an exception)."""
    processed = []
    monkeypatch.setattr(routes, 'process_inbox_request', lambda *a, **k: processed.append((a, k)))
    activity = {'id': f'{author.ap_profile_id}/a/{uuid.uuid4().hex}', 'type': 'Create',
                'actor': author.ap_profile_id, 'object': note(attributedTo=author.ap_profile_id,
                                                              uri=f'{author.ap_profile_id}/notes/1')}
    if ld is not None:
        activity['signature'] = {'type': 'RsaSignature2017'}

        def verify(*args, **kwargs):
            if ld is True:
                return True
            raise ld
        monkeypatch.setattr(LDSignature, 'verify_signature', verify)
    signer = type('Sender', (), {'private_key': relay_private_key, 'ap_profile_id': ACTOR})()
    with app.test_client() as client:
        return signed_inbox_post(client, activity, signer), processed, activity


class TestSharedInboxUnsignedForward:

    def test_an_accepted_relays_forward_with_no_ld_signature_is_queued_not_refused(self, app, author, queued,
                                                                                    monkeypatch):
        keys = a_keypair()
        relay = make_relay(state=RELAY_ACCEPTED, public_key=keys[1])

        response, processed, activity = deliver(app, author, keys[0], monkeypatch)

        assert response.status_code == 200
        assert queued == [(relay.id, activity['object']['id'])]
        assert processed == []   # nothing is created from the relay's copy

    def test_an_ld_signature_that_does_not_verify_is_queued_too(self, app, author, queued, monkeypatch):
        keys = a_keypair()
        relay = make_relay(state=RELAY_ACCEPTED, public_key=keys[1])

        response, processed, activity = deliver(app, author, keys[0], monkeypatch,
                                                ld=VerificationError('does not cover the document'))

        assert response.status_code == 200
        assert queued == [(relay.id, activity['object']['id'])]
        assert processed == []

    def test_an_ld_signed_forward_keeps_its_path(self, app, author, queued, monkeypatch):
        keys = a_keypair()
        relay = make_relay(state=RELAY_ACCEPTED, public_key=keys[1])

        response, processed, _activity = deliver(app, author, keys[0], monkeypatch, ld=True)

        assert response.status_code == 200
        assert queued == []
        assert len(processed) == 1 and processed[0][1] == {'relay_id': relay.id, 'ld_signed': True}

    @pytest.mark.parametrize('ld', [None, VerificationError('bad')])
    def test_a_pending_relay_still_gets_400(self, app, author, queued, monkeypatch, ld):
        keys = a_keypair()
        make_relay(state=RELAY_PENDING, public_key=keys[1])

        response, processed, _activity = deliver(app, author, keys[0], monkeypatch, ld=ld)

        assert response.status_code == 400
        assert queued == [] and processed == []

    @pytest.mark.parametrize('ld', [None, VerificationError('bad')])
    def test_a_key_that_is_no_relays_still_gets_400(self, app, author, queued, monkeypatch, ld):
        keys = a_keypair()
        other = next(pair[1] for pair in (a_keypair() for _ in range(10)) if pair[1] != keys[1])
        make_relay(state=RELAY_ACCEPTED, public_key=other)

        response, processed, _activity = deliver(app, author, keys[0], monkeypatch, ld=ld)

        assert response.status_code == 400
        assert queued == [] and processed == []

    def test_a_relay_signature_refused_for_an_unsigned_required_header_still_gets_400(self, app, author, queued,
                                                                                       monkeypatch):
        keys = a_keypair()
        make_relay(state=RELAY_ACCEPTED, public_key=keys[1])

        def refuse(*args, **kwargs):
            raise VerificationError('unsigned required header: digest')
        monkeypatch.setattr(inbound.HttpSignature, 'verify_request', refuse)

        response, processed, _activity = deliver(app, author, keys[0], monkeypatch)

        assert response.status_code == 400
        assert queued == [] and processed == []
```

- [ ] **Step 2: Run to verify they fail**

Run: `./run_tests.sh tests/test_relays_unsigned_forward.py -q -p no:cacheprovider`
Expected: the first two new tests FAIL with status 400 instead of 200; the others pass already (they pin today's
behaviour).

- [ ] **Step 3: Implement** (in `app/activitypub/routes.py`)

Above `shared_inbox`:

```python
def _unsigned_relay_forward(request, request_json, saved_json):
    """The answer to a forward an accepted relay HTTP-signed whose body carries no verifiable LD signature, or None
    when no accepted relay signed it (spec 2026-10-13)."""
    relay = relay_inbound.relay_for_forwarded(request)
    if relay is None:
        return None
    return relay_inbound.accept_unsigned_forward(relay, request_json, saved_json)
```

In `shared_inbox`, the LD-signature failure branch becomes:

```python
            except VerificationError as e:
                forwarded = _unsigned_relay_forward(request, request_json, saved_json)
                if forwarded is not None:
                    return forwarded
                log_incoming_ap(id, APLOG_NOTYPE, APLOG_FAILURE, saved_json, 'Could not verify LD signature: ' + str(e))
                return '', 400
```

and the no-LD-signature `else:` branch becomes:

```python
        else:
            forwarded = _unsigned_relay_forward(request, request_json, saved_json)
            if forwarded is not None:
                return forwarded
            log_incoming_ap(id, APLOG_NOTYPE, APLOG_FAILURE, saved_json, 'Could not verify HTTP signature: ' + str(e))
            return '', 400
```

Keep the commented-out block between them untouched.

- [ ] **Step 4: Run tests**

Run: `./run_tests.sh tests/test_relays_unsigned_forward.py tests/test_relays_forwarded.py tests/test_relays_inbound.py $(grep -rl --include=*.py "shared_inbox\|'/inbox'" tests | head -30) tests/test_no_inline_imports.py tests/test_import_order.py -q -p no:cacheprovider`
Expected: PASS, with no edits to existing tests.

- [ ] **Step 5: Commit**

```bash
git add app/activitypub/routes.py tests/test_relays_unsigned_forward.py
git commit -m "fix: an accepted relay's forward with no verifiable LD signature is fetched from its origin, not refused"
```

---

## Final gate (controller)

```
./run_tests.sh tests/ -q -p no:cacheprovider --cov=app --cov-branch --cov-report=json:coverage.json
python3 tests/check_changed_line_coverage.py coverage.json <base> --branches
python3 tests/check_coverage_floors.py coverage.json coverage_floors.ini
```

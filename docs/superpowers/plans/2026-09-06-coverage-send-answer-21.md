# Sub-project 21: closing `app/shared/tasks/notes.py` — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cover `send_answer` and the four Celery wrappers in `app/shared/tasks/notes.py`, landing one production guard, taking the module from 70.32% to a full close.

**Architecture:** A new `tests/test_shared_tasks_send_answer.py` covers `send_answer` (`:242-310`) and its two wrappers by asserting on **serialized outbound request bytes** captured with respx, never on in-memory dicts. `make_reply`/`edit_reply` tests are appended to the existing `tests/test_shared_tasks_send_reply.py`, beside the function they delegate to. `send_answer` is a 2×2 (`is_undo` × `is_local`) plus a follower loop, so the tests are organised by path rather than by line.

**Tech Stack:** pytest, respx/httpx, SQLAlchemy, Flask, Celery (eager), podman-compose via `./run_tests.sh`.

**Spec:** `docs/superpowers/specs/2026-09-06-coverage-send-answer-21-design.md`

## Global Constraints

Copied verbatim from the spec. Every task's requirements implicitly include this section.

- **Delete nothing the task did not create. `claude_test` in the repository root is not the campaign's.**
- Only the controller runs the full suite, one pytest session at a time, and **in the foreground**.
- **`pytest` exits 1 on a session timeout and `run_tests.sh` propagates it** (`pytest.ini:26-27`). The campaign's long-standing "still exits 0" claim was measured false on 2026-09-06 and is corrected in fact 118; the observed exit 0 was a shell **pipeline** eating the status. Read `${PIPESTATUS[0]}`, or do not pipe. Continue to check the test count and the coverage report's mtime, which catch more than a timeout.
- **A wrong `--cov` target fails silently and green** (fact 117, still true and a different trap): `--cov` takes a module path, so `--cov=app/shared/tasks/notes.py` warns `module-not-imported`, collects nothing, writes no JSON, and exits 0. The dotted form works.
- Coverage command: `./run_tests.sh --cov=app --cov-branch --cov-report=json:scratch_full_cov.json -q`.
- **A wedged podman stack reports failures that are not regressions.** Run `./run_tests.sh --down` and retry before believing any failure.
- **Mutation instrument.** One mutation at a time, applied as a targeted single-line edit (`sed -i 'NNNs/old/new/'`), never a whole-file rewrite. `app/shared/tasks/notes.py` is **310 lines** before the guard and **310 lines** after it, because the guard extends an existing line rather than adding one; assert the line count and an empty `git diff -- app/` after every apply and every restore. A sub-project-18 implementer truncated a 1174-line production module at this step.
- **Commit the fix before running the mutations** (sub-project 19's Ruling 7): the clean-tree assertion is impossible while the fix is uncommitted, because `git checkout -- app/` would discard it.
- Mutation record: the mutation, the single test that killed it, **assertion-kill or crash-kill**, **sole or multi**.
- **Use `ast.parse` and `FunctionDef.end_lineno` for extents, never a convention.**
- Every line number copied from anywhere must be re-derived against the current tree before it is written down (fact 99). Citation sweeps run in two passes, `file:line` then bare paths against `git ls-files` (fact 100), and **read the first pass's output to the end** — `run_tests.sh:21` was missed in sub-project 20 because a two-hit grep was read as one.
- **When a file shifts, sweep by the cause (the moved file), not by the topic that made you notice.**
- **Fix a citation's symbol, not just its number.**
- Enumerate conditional expressions by AST walk, not grep (fact 94) — coverage.py emits no arc for one (fact 87).
- Commit messages containing backticks are committed with `git commit -F <file>`, never `-m`.

---

## File Structure

| file | responsibility |
|---|---|
| `tests/test_shared_tasks_send_answer.py` (create) | `send_answer`, `choose_answer`, `unchoose_answer` |
| `tests/test_shared_tasks_send_reply.py` (modify) | gains `make_reply` and `edit_reply`, beside `send_reply` which they delegate to |
| `app/shared/tasks/notes.py:248` (modify) | the one production guard |
| `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` (modify) | D312 onward; D302 and D309 updated |
| `tests/README.md` (modify) | harness facts from 119 |
| `coverage_floors.ini` (modify) | raise `app/shared/tasks/notes.py` from 70 |

---

### Task 1: Open the send_answer harness

**Files:**
- Create: `tests/test_shared_tasks_send_answer.py`

**Interfaces:**
- Produces: `_seed(local_community=True, with_keys=False)` returning `SimpleNamespace(instance, user, community, post, reply)`; `_send(s, is_undo=False)`; `_sent_activity(route, index=-1)`; `_make_deliverable(s, inbox)`; `_remote_inbox(s, http_mock, inbox)`; `_community_follower(s, http_mock, ...)`; `_key_id_of(route, index=-1)`; the autouse `_peer_example_resolves_without_a_resolver` fixture; constant `PEER_INBOX`.

- [ ] **Step 1: Write the module prelude**

Port the helpers from `tests/test_shared_tasks_send_reply.py` — do not import them across test modules. Read that file's `_seed` (`:97`), `_remote_inbox` (`:260`), `_sent_activity` (`:325`), `_key_id_of` (`:1324`) and `_community_follower` (`:1339`) docstrings first; they explain why each assignment exists, and those reasons carry over unchanged.

```python
"""`send_answer` -- the Celery-path builder and deliverer of a ChooseAnswer.

`app/shared/tasks/notes.py:242-310` (extent by ast). The sibling of
`send_reply` (`:80-229`), which sub-project 20 took to two unreachable
statements and zero unreachable arms.

`send_answer` is a 2x2 plus a loop. `:265` (`if is_undo:`) and `:279`
(`if post_reply.community.is_local():`) are INDEPENDENT, so there are four
end-to-end paths, each with a different wire shape:

    local,  choose -> Announce(:290-298) wrapping the ChooseAnswer
    local,  undo   -> Announce wrapping the Undo wrapping the ChooseAnswer
    remote, choose -> the bare ChooseAnswer to community.ap_inbox_url (:304)
    remote, undo   -> the bare Undo to the same

THE `@context` ASYMMETRY IS THE LOAD-BEARING ASSERTION. It is deleted from
whichever object becomes an INNER object and kept on the outermost one:
`:266` strips `lock` when it is about to be nested inside `undo` at `:272`;
`:281` strips `undo` and `:284` strips `lock` when either is about to be
nested inside `announce` at `:294`. The outermost object keeps the
`@context` it was built with (`:259`, `:273`, `:295`). Every path test below
asserts presence on the outer object and absence on every nested one.
Sub-project 19 got a `@context` claim wrong by REASONING about which deletes
run; its amendment declaring an arm unreachable was false because the delete
ran on the ordinary path. Assert, do not argue.

DIFFERENCES FROM `send_reply` THAT CHANGE HOW IT IS TESTED:

1. `send_answer` takes NO `session` parameter. It opens its own at `:243`
   with `get_task_session()` (`app/utils.py:3673-3675`), which returns
   `Session(bind=db.engine)` -- an independent session, not `db.session`.
   Seeded rows must therefore be COMMITTED before the call, or the function
   will not see them. `_seed` below commits.

2. `send_answer` never calls `patch_db_session` (`app/utils.py:3679`), where
   `make_reply` (`:58`) and `edit_reply` (`:71`) both wrap their callee in
   it. Anything `send_answer` reaches that touches `db.session` therefore
   gets the request-scoped session rather than the task session it just
   opened. Registered as a finding; measured, not assumed.

3. `:303` is a conditional expression, `undo if is_undo else lock`, and it
   is the ONLY one in the function -- established by an AST walk over the
   `send_answer` FunctionDef, not by grep (fact 94). coverage.py emits no
   arc for a ternary (fact 87), so it reports zero missing arms whether or
   not both arms run; both have named tests below.
"""

import json
import socket
from types import SimpleNamespace

import pytest

from app import db
from app.models import ActivityPubLog
from app.shared.tasks.notes import choose_answer, send_answer, unchoose_answer
from tests.factories import (
    make_community, make_community_member, make_instance, make_post,
    make_post_reply, make_user,
)


def _seed(local_community=True, with_keys=False):
    """instance, author, community, post, reply -- committed.

    ORDER IS LOAD-BEARING. `make_community` (tests/factories.py:122)
    hardcodes `instance_id=1` and the db_session teardown resets every
    sequence, so the local instance must be created FIRST; a peer built
    before this call would take id 1 and leave the community's FK pointing
    at it.

    `local_community=False` sets `ap_id` AND `ap_profile_id` on peer.example,
    because `Community.is_local()` (app/models.py:795) is a DISJUNCTION --
    `ap_id is None or profile_id().startswith(SERVER_URL)` -- whose
    `profile_id()` falls back to a computed default. Setting `ap_id` alone
    leaves the community silently LOCAL, and `:279`'s false arm would never
    be taken while these tests still passed (fact 112).

    THE COMMIT IS NOT OPTIONAL: `send_answer` reads through its own session
    (see the module docstring), which cannot see this one's uncommitted rows.
    """
    instance = make_instance('test.piefed.local', software='piefed')
    user = make_user(instance, 'author', local=True, with_keys=with_keys)
    community = make_community('c1')
    post = make_post(community, user, ap_id='https://test.piefed.local/post/1')
    reply = make_post_reply(post, user, body='an answer')
    if not local_community:
        community.ap_id = 'c1@peer.example'
        community.ap_profile_id = 'https://peer.example/c/c1'
        community.ap_public_url = 'https://peer.example/c/c1'
        community.ap_followers_url = 'https://peer.example/c/c1/followers'
        community.ap_domain = 'peer.example'
    db.session.commit()
    return SimpleNamespace(instance=instance, user=user, community=community,
                           post=post, reply=reply)


def _peer(domain='peer.example', software='lemmy'):
    """A remote Instance. ALWAYS call AFTER `_seed()` -- see `_seed` for why."""
    return make_instance(domain, software=software)


def _send(s, is_undo=False):
    """`send_answer(post_reply_id, user_id, is_undo)` -- no session parameter."""
    return send_answer(s.reply.id, s.user.id, is_undo)


PEER_INBOX = 'https://peer.example/c/c1/inbox'

_REAL_GETADDRINFO = socket.getaddrinfo

# Any globally routable literal will do -- the only property
# app/utils.py:5530 reads off it is `is_global`.
_EXAMPLE_TLD_ADDRESS = '93.184.216.34'


def _getaddrinfo_without_the_network(host, *args, **kwargs):
    """`socket.getaddrinfo`, answering for `.example` hosts without a resolver.

    Everything else is delegated to the real function unchanged.
    """
    if isinstance(host, str) and (host == 'example' or host.endswith('.example')):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '',
                 (_EXAMPLE_TLD_ADDRESS, 0))]
    return _REAL_GETADDRINFO(host, *args, **kwargs)


@pytest.fixture(autouse=True)
def _peer_example_resolves_without_a_resolver(monkeypatch):
    """Keep delivery off the machine's DNS resolver.

    THIS IS NOT OPTIONAL AND MUST NOT BE DELETED AS UNNECESSARY. Signing runs
    `is_invalid_get_request_uri`, which reaches `socket.getaddrinfo` at
    app/utils.py:5520 for POST as well as GET, and FAILS OPEN at :5521-5522.
    That is what makes it dangerous rather than merely slow: on a machine
    whose resolver hijacks NXDOMAIN into a wildcard A record, these tests
    start failing at app/utils.py:5530's `is_global` check instead. The stub
    is the narrowest thing that removes the lookup -- only the resolver, only
    for the RFC-2606 `.example` TLD -- so `is_invalid_get_request_uri` still
    runs in full against a real answer.

    Autouse because `_remote_inbox` is a plain function and cannot request a
    fixture.
    """
    monkeypatch.setattr(socket, 'getaddrinfo', _getaddrinfo_without_the_network)


def _make_deliverable(s, inbox=PEER_INBOX):
    """Attach the community to a real peer Instance and give it an inbox.

    The half of `_remote_inbox` that touches the database and nothing else.
    The early-return tests call this ALONE, because they assert the delivery
    never happens and `http_mock` is built with `assert_all_called=True` --
    registering a route they expect never to fire would fail them for the
    wrong reason.
    """
    s.community.instance_id = _peer().id
    s.community.ap_inbox_url = inbox
    db.session.commit()


def _remote_inbox(s, http_mock, inbox=PEER_INBOX):
    """THE CAPTURE MECHANISM. Returns the respx route the outbound activity
    lands on; pair it with `_sent_activity`.

    Everything `send_answer` builds lives in locals (`lock`, `undo`,
    `announce`) and is never persisted, so the only way to read it is to let
    the function deliver. Three things must be true for `:304`'s call to
    become an observable request, and this helper plus `_seed` supply all
    three:

      1. The community must be remote -- `_seed(local_community=False)`,
         which closes BOTH disjuncts of `Community.is_local()`.
      2. It must have an `ap_inbox_url`; `make_community` leaves it None, and
         `post_request` (app/activitypub/signature.py:109-111) short-circuits
         to an "empty uri" failure row without reaching the transport.
      3. The user must have a keypair -- `_seed(with_keys=True)` -- because
         signing calls `.encode()` on `user.private_key`.

    Delivery is synchronous: `send_post_request`
    (app/activitypub/signature.py:82-91) calls `post_request.delay(...)` and
    conftest runs Celery eagerly, so the POST has happened by the time
    `_send` returns.

    WHY THE HTTP BODY AND NOT A RECORDER: `:266`, `:281` and `:284` mutate
    `lock` and `undo` in place with `del`, so a recorder holding the dict
    would be read back in its post-`del` state. respx captures the bytes that
    actually left, at app/activitypub/signature.py:494.
    """
    _make_deliverable(s, inbox)
    return http_mock.post(inbox).respond(200, json={})


def _sent_activity(route, index=-1):
    """The JSON body of the request `route` captured, decoded from the bytes
    that were actually sent. Defaults to the most recent call."""
    return json.loads(route.calls[index].request.content)


def _key_id_of(route, index=-1):
    """The `keyId` the captured request was signed under.

    The only observable that separates `:301`'s signer from `:304`'s: the
    Announce signs as the COMMUNITY (`community.public_url() + '#main-key'`),
    the direct post signs as the USER. respx never verifies a signature, so
    the key material leaves no trace on the wire -- only the declared keyId
    does.
    """
    return route.calls[index].request.headers['signature'].split('"')[1]


def _community_follower(s, http_mock=None, domain='fan.example',
                        member_name='fan', with_inbox=True, dormant=False):
    """A remote instance `community.following_instances()` returns at `:299`,
    plus the respx route its delivery lands on.

    Returns `SimpleNamespace(instance, member, route)`; `route` is None when
    none was registered.

    THE COMMUNITY'S KEYPAIR IS SET HERE. `:301` signs with
    `community.private_key`, which `make_community` leaves None, and signing
    calls `.encode()` on it. The author's key is reused rather than a second
    generated: generation costs about a second, respx never verifies a
    signature, and the actor a delivery was signed AS is observable through
    `keyId`, not through the key material.

    `with_inbox=False` leaves `Instance.inbox` None, which closes `:300`'s
    FIRST conjunct. `dormant=True` sets the column
    `Community.following_instances` already filters on in SQL
    (app/models.py:849) -- registered as unreachable-False, not exercised as
    a live branch.
    """
    assert s.user.private_key is not None, '_seed(with_keys=True) is required'
    s.community.private_key = s.user.private_key
    s.community.public_key = s.user.public_key
    instance = make_instance(domain, software='lemmy')
    instance.inbox = f'https://{domain}/inbox' if with_inbox else None
    instance.dormant = dormant
    member = make_user(instance, member_name, local=False)
    make_community_member(member, s.community)
    db.session.commit()
    route = (http_mock.post(instance.inbox).respond(200, json={})
             if http_mock is not None and with_inbox else None)
    return SimpleNamespace(instance=instance, member=member, route=route)
```

- [ ] **Step 2: Add one smoke test proving the harness delivers**

```python
def test_a_remote_community_receives_the_bare_choose_answer(db_session, http_mock):
    """The harness itself: `:279`'s FALSE arm reaches `:304` and the bytes
    are readable. Every later test in this file depends on this working."""
    s = _seed(local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)

    _send(s)

    assert route.called
    assert _sent_activity(route)['type'] == 'ChooseAnswer'
```

- [ ] **Step 3: Run it**

Run: `./run_tests.sh tests/test_shared_tasks_send_answer.py -q`
Expected: 1 passed. If it fails on delivery, re-read `_remote_inbox`'s three preconditions before changing anything.

- [ ] **Step 4: Commit**

```bash
git add tests/test_shared_tasks_send_answer.py
git commit -F <message-file>
```

Subject: `test: open tests/test_shared_tasks_send_answer.py with the send_answer harness prelude`

---

### Task 2: The `:248` early return, and the one production guard

**Files:**
- Modify: `app/shared/tasks/notes.py:248`
- Test: `tests/test_shared_tasks_send_answer.py`

**Interfaces:**
- Consumes: `_seed`, `_send`, `_make_deliverable` from Task 1.
- Produces: nothing later tasks depend on.

`:248` currently reads:

```python
        if post_reply.community.local_only or not post_reply.community.instance.online():
```

**The discriminator is `ActivityPubLog.query.count()`, not a call count.** `post_request` (`app/activitypub/signature.py:103-105`) writes a failure row unconditionally before it touches the transport, so a count of 0 proves the early return at `:249` fired and a count of 1 proves it did not. These tests call `_make_deliverable` and **not** `_remote_inbox`, because `http_mock` is built with `assert_all_called=True` and a registered route that never fires would fail the test for the wrong reason.

- [ ] **Step 1: Write the two arms of the existing guard**

```python
def test_a_local_only_community_does_not_federate_the_answer(db_session, http_mock):
    """:248's TRUE arm via `local_only`, returning at :249."""
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    s.community.local_only = True
    db.session.commit()

    _send(s)

    assert db.session.query(ActivityPubLog).count() == 0


def test_a_dormant_instance_does_not_receive_the_answer(db_session, http_mock):
    """:248's TRUE arm via `not instance.online()`.

    `Instance.online()` (app/models.py:118-119) is
    `not (self.dormant or self.gone_forever)`, so setting `dormant` on the
    community's OWN instance closes it. This is the community's instance, not
    a follower's -- the follower filter at :299 is a different mechanism
    entirely, covered in Task 5.
    """
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    s.community.instance.dormant = True
    db.session.commit()

    _send(s)

    assert db.session.query(ActivityPubLog).count() == 0
```

- [ ] **Step 2: Run them**

Run: `./run_tests.sh tests/test_shared_tasks_send_answer.py -q`
Expected: 3 passed.

- [ ] **Step 3: Write the failing test for the guard that does not exist yet**

```python
def test_a_private_community_does_not_federate_the_answer(db_session, http_mock):
    """:248's `private` conjunct -- THE ONE PRODUCTION CHANGE THIS
    SUB-PROJECT LANDS.

    `Community.private` (app/models.py:611) is commented "only members can
    view. no federation.", and before this conjunct landed `:248` tested only
    `local_only` and `instance.online()` -- so a private, non-local-only
    community federated its chosen answers out.

    This is the SECOND site of D309 the campaign has closed; sub-project 20
    closed `:143` in the same file for the same finding. `local_only` is left
    False deliberately: with it True the test would pass on the pre-existing
    conjunct and prove nothing.

    THE UNCOUPLED STATE THIS SEEDS HAS NO CURRENT UI PATH -- the two form
    fields are tied at app/community/routes.py:103-104 and :1230-1231, and
    the three write statements are :122, :1234 and :1237 -- so the severity
    is latent and the guard is defence in depth.
    """
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    s.community.private = True
    db.session.commit()

    _send(s)

    assert db.session.query(ActivityPubLog).count() == 0
```

- [ ] **Step 4: Run it and watch it FAIL**

Run: `./run_tests.sh tests/test_shared_tasks_send_answer.py::test_a_private_community_does_not_federate_the_answer -q`
Expected: FAIL, `assert 1 == 0` — the delivery was attempted and wrote a failure row.

**Do not proceed until you have seen this fail.** A guard that lands before its test has failed is a guard nothing proves.

- [ ] **Step 5: Land the guard as a single-line edit**

```bash
sed -i '248s/local_only or not/local_only or post_reply.community.private or not/' app/shared/tasks/notes.py
sed -n '248p' app/shared/tasks/notes.py
wc -l app/shared/tasks/notes.py
```

Expected: the line now reads

```python
        if post_reply.community.local_only or post_reply.community.private or not post_reply.community.instance.online():
```

and `wc -l` is still **310** — the guard extends an existing line rather than adding one.

- [ ] **Step 6: Run the file**

Run: `./run_tests.sh tests/test_shared_tasks_send_answer.py -q`
Expected: 4 passed.

- [ ] **Step 7: Commit the fix before any mutation runs**

```bash
git add app/shared/tasks/notes.py tests/test_shared_tasks_send_answer.py
git commit -F <message-file>
```

Subject: `fix: stop send_answer federating a private community's chosen answer`

Committing here is required by Ruling 7: `git checkout -- app/` in Task 6 would discard an uncommitted fix.

---

### Task 3: The remote paths and the `:303` ternary

**Files:**
- Test: `tests/test_shared_tasks_send_answer.py`

**Interfaces:**
- Consumes: `_seed`, `_send`, `_remote_inbox`, `_sent_activity`, `_key_id_of` from Task 1.

`:279`'s false arm runs `:303-304`:

```python
            payload = undo if is_undo else lock
            send_post_request(post_reply.community.ap_inbox_url, payload, user.private_key, user.public_url() + '#main-key')
```

Both arms of the ternary at `:303` need a named test asserting a value the other arm could not produce, because coverage emits no arc for it.

- [ ] **Step 1: Write both remote paths**

```python
def test_the_remote_choose_carries_its_full_key_set_and_keeps_its_context(
        db_session, http_mock):
    """:303's FALSE arm -- `undo if is_undo else lock` selecting `lock` --
    delivered by :304.

    `type` is what separates the two arms: the ChooseAnswer could not be
    produced by the true arm, which sends an Undo. `@context` is asserted
    PRESENT because nothing nests this object on this path -- :266 runs only
    under `is_undo` and :284 only under `is_local`, and neither is taken
    here, so the `@context` built at :259 survives to the wire.
    """
    s = _seed(local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)

    _send(s, is_undo=False)

    sent = _sent_activity(route)
    assert sent['type'] == 'ChooseAnswer'
    assert set(sent) == {'id', 'type', 'actor', 'object', '@context',
                         'audience', 'to', 'cc'}
    assert sent['actor'] == s.user.public_url()
    assert sent['object'] == s.reply.public_url()
    assert sent['audience'] == s.community.public_url()
    assert sent['to'] == ['https://www.w3.org/ns/activitystreams#Public']
    assert sent['cc'] == [s.community.public_url()]


def test_the_remote_undo_wraps_the_choose_and_strips_its_inner_context(
        db_session, http_mock):
    """:303's TRUE arm -- selecting `undo` -- and :266's `del`.

    The companion to the test above: `type` is `Undo` here, which the false
    arm could not produce. The nesting is the point -- `undo['object']` is
    the ChooseAnswer, and :266 stripped ITS `@context` before :272 nested it,
    while the Undo itself keeps the one built at :273. That asymmetry is what
    a mutant deleting the wrong object's `@context` would break.
    """
    s = _seed(local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)

    _send(s, is_undo=True)

    sent = _sent_activity(route)
    assert sent['type'] == 'Undo'
    assert '@context' in sent
    assert sent['object']['type'] == 'ChooseAnswer'
    assert '@context' not in sent['object']
    assert sent['actor'] == s.user.public_url()
    assert sent['object']['object'] == s.reply.public_url()


def test_the_remote_delivery_is_signed_as_the_user(db_session, http_mock):
    """:304 signs with `user.public_url() + '#main-key'`, where :301 signs as
    the COMMUNITY. `keyId` is the only observable that separates them, and
    the user's and community's public urls differ in path (`/u/author` vs
    `/c/c1`), so a mutant swapping the signer produces a valid but wrong
    keyId and this fails rather than merely not-noticing.
    """
    s = _seed(local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)

    _send(s)

    assert _key_id_of(route) == s.user.public_url() + '#main-key'
```

- [ ] **Step 2: Run the file**

Run: `./run_tests.sh tests/test_shared_tasks_send_answer.py -q`
Expected: 7 passed.

- [ ] **Step 3: Commit**

Subject: `test: cover send_answer's remote arm and both sides of the :303 ternary`

---

### Task 4: The local paths and the `@context` asymmetry

**Files:**
- Test: `tests/test_shared_tasks_send_answer.py`

**Interfaces:**
- Consumes: `_seed`, `_send`, `_sent_activity`, `_key_id_of`, `_community_follower` from Task 1.

`:279`'s true arm builds an `Announce` at `:290-298` and delivers it per follower instance at `:301`. `:280` chooses which object gets nested and which `@context` is stripped: `:281` strips `undo`, `:284` strips `lock`.

`_seed()` with the default `local_community=True` leaves the community local. A follower is required, or `:299`'s loop body never runs and nothing is delivered — that empty case is Task 5's.

- [ ] **Step 1: Write both local paths**

```python
def test_a_local_community_announces_the_choose_to_a_following_instance(
        db_session, http_mock):
    """:279's TRUE arm with :280 FALSE -- :284's `del lock['@context']` and
    the Announce built at :290-298.

    The Announce keeps the `@context` built at :295; the ChooseAnswer nested
    at :294 has had its own stripped at :284. Asserting both directions is
    what catches a mutant that deletes from the wrong object.

    `cc` is the community's followers collection here (:289), NOT the
    `[community.public_url()]` the inner object carries (:253) -- :289
    rebinds the name to a NEW list, so the inner object's `cc` still points
    at the old one. Asserting both proves the rebinding did not alias.
    """
    s = _seed(with_keys=True)
    fan = _community_follower(s, http_mock)

    _send(s, is_undo=False)

    sent = _sent_activity(fan.route)
    assert sent['type'] == 'Announce'
    assert '@context' in sent
    assert sent['actor'] == s.community.public_url()
    assert sent['object']['type'] == 'ChooseAnswer'
    assert '@context' not in sent['object']
    assert sent['cc'] == [s.community.ap_followers_url]
    assert sent['object']['cc'] == [s.community.public_url()]


def test_a_local_community_announces_the_undo_and_strips_two_contexts(
        db_session, http_mock):
    """:279's TRUE arm with :280 TRUE -- :281's `del undo['@context']`, on
    top of :266's `del lock['@context']` which already ran.

    THIS IS THE THREE-LEVEL PATH and the only one where two deletes fire.
    The Announce keeps its `@context`; the Undo nested inside it lost its at
    :281; the ChooseAnswer nested inside THAT lost its at :266. All three
    levels are asserted, because a mutant that skipped either delete would
    still produce a well-formed activity and only this shape would catch it.
    """
    s = _seed(with_keys=True)
    fan = _community_follower(s, http_mock)

    _send(s, is_undo=True)

    sent = _sent_activity(fan.route)
    assert sent['type'] == 'Announce'
    assert '@context' in sent
    assert sent['object']['type'] == 'Undo'
    assert '@context' not in sent['object']
    assert sent['object']['object']['type'] == 'ChooseAnswer'
    assert '@context' not in sent['object']['object']


def test_the_announce_is_signed_as_the_community(db_session, http_mock):
    """:301 signs with `community.private_key` and
    `community.public_url() + '#main-key'` -- the companion to Task 3's
    user-signed assertion. See `_key_id_of`."""
    s = _seed(with_keys=True)
    fan = _community_follower(s, http_mock)

    _send(s)

    assert _key_id_of(fan.route) == s.community.public_url() + '#main-key'
```

- [ ] **Step 2: Run the file**

Run: `./run_tests.sh tests/test_shared_tasks_send_answer.py -q`
Expected: 10 passed.

- [ ] **Step 3: Commit**

Subject: `test: cover send_answer's Announce arm and the @context nesting asymmetry`

---

### Task 5: The follower loop's three arms

**Files:**
- Test: `tests/test_shared_tasks_send_answer.py`

**Interfaces:**
- Consumes: `_seed`, `_send`, `_community_follower` from Task 1.

The loop at `:299-301`:

```python
            for instance in post_reply.community.following_instances():
                if instance.inbox and instance.online() and not user.has_blocked_instance(instance.id) and not instance_banned(instance.domain):
                    send_post_request(instance.inbox, announce, post_reply.community.private_key, post_reply.community.public_url() + '#main-key')
```

Three arms remain: `(299, 310)` the loop never entered, `(300, 299)` the guard skipping an instance, `(300, 301)` the guard delivering. `(299, 300)` is already taken by Task 4.

**`instance.online()` is NOT exercised as a live branch.** `Community.following_instances()` (`app/models.py:842-851`) already filters `Instance.dormant == False` at `:849` and `Instance.gone_forever == False` at `:850`, and `Instance.online()` is exactly `not (self.dormant or self.gone_forever)` — so the conjunct is unreachable-False. It is registered in Task 8, not tested here.

- [ ] **Step 1: Write the three arms**

```python
def test_a_local_community_with_no_followers_sends_nothing(db_session, http_mock):
    """:299's loop never entered -- arc (299, 310), straight to the `finally`.

    `following_instances()` returns empty because no CommunityMember exists
    on a remote instance. The Announce is still BUILT at :290-298; nothing
    delivers it. `ActivityPubLog.query.count() == 0` is the witness, since
    `post_request` would have written a row for any attempt.
    """
    s = _seed(with_keys=True)

    _send(s)

    assert db.session.query(ActivityPubLog).count() == 0


def test_a_following_instance_without_an_inbox_is_skipped(db_session, http_mock):
    """:300's FALSE arm -- arc (300, 299), the loop continuing.

    `with_inbox=False` leaves `Instance.inbox` None, closing :300's FIRST
    conjunct before any of the other three is evaluated. No route is
    registered, so `http_mock`'s `assert_all_called=True` is not tripped, and
    the `ActivityPubLog` count of 0 rules out a request having been attempted
    against a None inbox -- which would have produced an "empty uri" failure
    row rather than nothing at all.
    """
    s = _seed(with_keys=True)
    _community_follower(s, http_mock, with_inbox=False)

    _send(s)

    assert db.session.query(ActivityPubLog).count() == 0


def test_one_following_instance_is_skipped_while_another_receives(
        db_session, http_mock):
    """Both loop arms in ONE run -- (300, 299) then (300, 301).

    The discriminating case: a single-instance test cannot show that the
    guard skips an instance WITHOUT also stopping the loop, because with one
    member "skipped" and "loop ended" look identical. With two, the delivered
    one proves iteration continued past the skipped one.
    """
    s = _seed(with_keys=True)
    _community_follower(s, http_mock, domain='mute.example',
                        member_name='mute', with_inbox=False)
    good = _community_follower(s, http_mock, domain='fan.example',
                               member_name='fan')

    _send(s)

    assert good.route.call_count == 1
    assert db.session.query(ActivityPubLog).count() == 1
```

- [ ] **Step 2: Run the file**

Run: `./run_tests.sh tests/test_shared_tasks_send_answer.py -q`
Expected: 13 passed.

- [ ] **Step 3: Commit**

Subject: `test: cover send_answer's follower loop, including the skip-then-deliver case`

---

### Task 6: Mutation-test the guards

**Files:**
- Modify (temporarily): `app/shared/tasks/notes.py`

**Interfaces:**
- Consumes: the tests from Tasks 2-5.

Temporary mutations are permitted and expected, provided each is a **targeted single-line edit**, restored with `git checkout -- app/`, and followed by asserting BOTH an empty `git diff -- app/` AND `wc -l app/shared/tasks/notes.py` = **310**.

**One at a time. Never batched. Never a whole-file rewrite.**

- [ ] **Step 1: Confirm the tree is clean before starting**

```bash
git diff -- app/ && wc -l app/shared/tasks/notes.py
```

Expected: no diff, 310 lines. If the guard from Task 2 is uncommitted, stop — Ruling 7 requires it committed first.

- [ ] **Step 2: Run each mutation in turn**

For each row: apply, run `./run_tests.sh tests/test_shared_tasks_send_answer.py -q`, record which test failed and whether it was an **assertion-kill** or a **crash-kill** and **sole** or **multi**, then restore and re-assert the clean tree.

| # | line | mutation | `sed` |
|---|---|---|---|
| M1 | 248 | negate the whole guard | `sed -i '248s/if post_reply/if not post_reply/'` |
| M2 | 248 | drop the `private` conjunct | `sed -i '248s/ or post_reply.community.private//'` |
| M3 | 265 | invert `is_undo` | `sed -i '265s/if is_undo:/if not is_undo:/'` |
| M4 | 266 | skip the inner `del` | `sed -i '266s/del lock/lock.pop("@context", None) if False else None; del lock/'` |
| M5 | 279 | invert `is_local()` | `sed -i '279s/if post_reply.community.is_local():/if not post_reply.community.is_local():/'` |
| M6 | 280 | invert the inner `is_undo` | `sed -i '280s/if is_undo:/if not is_undo:/'` |
| M7 | 303 | invert the ternary | `sed -i '303s/undo if is_undo else lock/lock if is_undo else undo/'` |

M4's form looks odd deliberately: `del lock['@context']` cannot be removed by a substitution that keeps the line's length discipline, so the mutation makes the delete conditional on a constant. If it does not apply cleanly, substitute `sed -i "266s/^/#/"` (comment the line out) and record that form instead.

**Expect M5 to be a multi-kill mixing assertion and crash kills** — it flips every test's path at once, and the remote tests will die reaching `following_instances()` on a community with no followers rather than failing an assertion. That is expected and is recorded as a multi-kill, not treated as a problem.

**If any mutant SURVIVES**, do not claim a kill. Either write the test that kills it, or — if it is genuinely equivalent — document the equivalence with the reason, the way sub-project 20 handled `:225`'s re-add mutant.

- [ ] **Step 3: Assert the tree is clean and commit the record**

```bash
git diff -- app/ && wc -l app/shared/tasks/notes.py
```

Expected: no diff, 310 lines.

Commit the mutation record as a comment block at the end of the test file. Subject: `test: record send_answer's mutation kills`

---

### Task 7: `choose_answer` and `unchoose_answer`

**Files:**
- Test: `tests/test_shared_tasks_send_answer.py`

**Interfaces:**
- Consumes: `_seed`, `_remote_inbox`, `_sent_activity` from Task 1.

```python
@celery.task
def choose_answer(send_async, post_reply_id, user_id):
    send_answer(post_reply_id, user_id, False)


@celery.task
def unchoose_answer(send_async, post_reply_id, user_id):
    send_answer(post_reply_id, user_id, True)
```

Both are one-line wrappers differing only in the `is_undo` they pass. `send_async` is accepted and ignored. Celery runs eagerly in tests, so call the task function directly.

- [ ] **Step 1: Write both wrapper tests**

```python
def test_choose_answer_passes_is_undo_false(db_session, http_mock):
    """:234 -- `send_answer(post_reply_id, user_id, False)`.

    `type == 'ChooseAnswer'` is the witness: the flag it passes is not
    observable any other way, and `unchoose_answer` differs from this
    function ONLY in that argument, so a mutant swapping the two would be
    caught here and in its companion below.

    `send_async` is accepted and ignored by the task; None is passed to prove
    it is not read.
    """
    s = _seed(local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)

    choose_answer(None, s.reply.id, s.user.id)

    assert _sent_activity(route)['type'] == 'ChooseAnswer'


def test_unchoose_answer_passes_is_undo_true(db_session, http_mock):
    """:239 -- `send_answer(post_reply_id, user_id, True)`. The companion to
    the test above; `type == 'Undo'` is a value the False flag could not
    produce."""
    s = _seed(local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)

    unchoose_answer(None, s.reply.id, s.user.id)

    assert _sent_activity(route)['type'] == 'Undo'
```

- [ ] **Step 2: Write `send_answer`'s own except/finally test**

`send_answer` has the same `try/except: rollback; raise` / `finally: close` shape as the wrappers, at `:306-310`. Reach it with a **natural raise** — an id with no row makes `:246`'s `.get()` return `None` and `:248` raise `AttributeError` on its own.

```python
def test_a_missing_post_reply_rolls_back_and_re_raises(db_session, http_mock):
    """:306-308's except arm, reached without a faked exception.

    `session.query(PostReply).get(<absent id>)` returns None at :246, and
    :248's `post_reply.community` raises AttributeError. The wrapper catches
    it at :306, rolls back, and RE-RAISES at :308 -- so the exception
    escaping is itself half the assertion, and a mutant that swallowed it
    would fail here.

    A monkeypatched sentinel would prove the handler catches a fake
    exception; this proves it catches the one the real path produces.
    """
    s = _seed(with_keys=True)

    with pytest.raises(AttributeError):
        send_answer(s.reply.id + 1000, s.user.id, False)
```

- [ ] **Step 3: Run the file**

Run: `./run_tests.sh tests/test_shared_tasks_send_answer.py -q`
Expected: 16 passed.

- [ ] **Step 4: Commit**

Subject: `test: cover choose_answer, unchoose_answer and send_answer's rollback arm`

---

### Task 8: `make_reply` and `edit_reply`

**Files:**
- Modify: `tests/test_shared_tasks_send_reply.py`

**Interfaces:**
- Consumes: that file's existing `_seed`, `_remote_inbox`, `_sent_activity`, `PostReply` import.
- Produces: `_recording_task_session(monkeypatch)`, a helper later readers may reuse.

```python
@celery.task
def make_reply(send_async, reply_id, parent_id):
    session = get_task_session()
    try:
        with patch_db_session(session):
            send_reply(reply_id, parent_id, session=session)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
```

`edit_reply` is identical but passes `edit=True`.

`patch_db_session` (`app/utils.py:3679`) no-ops inside a Flask **request** context and patches otherwise. The `app` fixture pushes an **app** context, not a request context, so `has_request_context()` is False and the patched arm is the one under test.

- [ ] **Step 1: Add the recording-session helper**

```python
def _recording_task_session(monkeypatch):
    """Make `get_task_session` hand back a GENUINE Session that records
    `rollback()` and `close()`.

    The session is real and does real work -- only the observation is added,
    by wrapping the two methods rather than replacing the object. A fake
    session would prove the wrapper calls methods on a mock; this proves it
    calls them on the session the function actually used.

    Returns a `SimpleNamespace(calls=[])`; the wrapper appends 'rollback' and
    'close' to it in the order they happened, so `finally` running after
    `except` is observable rather than assumed.
    """
    from app import db as _db
    from sqlalchemy.orm import Session as _Session
    import app.shared.tasks.notes as notes_module

    record = SimpleNamespace(calls=[])

    def _make():
        session = _Session(bind=_db.engine)
        real_rollback, real_close = session.rollback, session.close

        def rollback():
            record.calls.append('rollback')
            return real_rollback()

        def close():
            record.calls.append('close')
            return real_close()

        session.rollback = rollback
        session.close = close
        return session

    monkeypatch.setattr(notes_module, 'get_task_session', _make)
    return record
```

- [ ] **Step 2: Write the happy paths**

```python
def test_make_reply_delivers_a_create(db_session, http_mock):
    """`make_reply` (:55-64) delegates to `send_reply` with `edit=False`.

    `type == 'Create'` is the witness that separates it from `edit_reply`,
    which is byte-identical except for the `edit=True` at :74. `send_async`
    is accepted and ignored; None is passed to prove it is not read.
    """
    s = _seed(local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)

    make_reply(None, s.reply.id, None)

    assert _sent_activity(route)['type'] == 'Create'


def test_edit_reply_delivers_an_update(db_session, http_mock):
    """`edit_reply` (:68-77) delegates with `edit=True`, so :185/:187 build
    an Update rather than a Create. The companion to the test above; `Update`
    is a value `edit=False` could not produce."""
    s = _seed(local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)

    edit_reply(None, s.reply.id, None)

    assert _sent_activity(route)['type'] == 'Update'
```

- [ ] **Step 3: Write the error paths**

```python
def test_make_reply_rolls_back_and_closes_when_send_reply_raises(
        db_session, monkeypatch):
    """:62-63's except arm and :64-65's finally, reached by a NATURAL raise.

    A reply id with no row makes `send_reply`'s `.get()` return None, and the
    first attribute read on it raises AttributeError. Nothing is faked: the
    exception is the one the real path produces, so a refactor that stopped
    raising would fail this test rather than leave it green.

    The recorded call ORDER is the assertion that `finally` ran after
    `except`, which a bare "was close called" check could not distinguish
    from a wrapper that closed instead of rolling back.
    """
    s = _seed()
    record = _recording_task_session(monkeypatch)

    with pytest.raises(AttributeError):
        make_reply(None, s.reply.id + 1000, None)

    assert record.calls == ['rollback', 'close']


def test_edit_reply_rolls_back_and_closes_when_send_reply_raises(
        db_session, monkeypatch):
    """:75-76's except arm and :77's finally -- `edit_reply`'s own copy of
    the handler, which is a SEPARATE function body from `make_reply`'s and so
    a separate pair of arcs. Written out rather than parametrised so each
    function's arms are attributable to a named test."""
    s = _seed()
    record = _recording_task_session(monkeypatch)

    with pytest.raises(AttributeError):
        edit_reply(None, s.reply.id + 1000, None)

    assert record.calls == ['rollback', 'close']


def test_make_reply_closes_the_session_on_the_happy_path(
        db_session, http_mock, monkeypatch):
    """:64-65's finally on the SUCCESS path -- `close` with no `rollback`.

    The control for the two tests above: without it, `finally` running is
    only ever observed alongside an exception, and a wrapper that closed only
    in the except arm would pass everything else in this file.
    """
    s = _seed(local_community=False, with_keys=True)
    _remote_inbox(s, http_mock)
    record = _recording_task_session(monkeypatch)

    make_reply(None, s.reply.id, None)

    assert record.calls == ['close']
```

- [ ] **Step 4: Add the imports**

`tests/test_shared_tasks_send_reply.py:88` currently reads `from app.shared.tasks.notes import send_reply`. Widen it:

```python
from app.shared.tasks.notes import edit_reply, make_reply, send_reply
```

Re-derive `:88` against the current tree before editing — the file has shifted twice already this branch, and a line number written from this plan rather than from the file is exactly the defect the campaign keeps repairing.

`SimpleNamespace` (`:78`) and `pytest` (`:80`) are already imported; do not re-add them.

- [ ] **Step 5: Run the file**

Run: `./run_tests.sh tests/test_shared_tasks_send_reply.py -q`
Expected: 46 passed (41 existing + 5 new: two happy paths, two error paths, one happy-path close control).

- [ ] **Step 6: Commit**

Subject: `test: cover make_reply and edit_reply, including their rollback arms`

---

### Task 9: Residuals, the AST walk, the register and the floor

**Files:**
- Modify: `tests/test_shared_tasks_send_answer.py`, `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`, `tests/README.md`, `coverage_floors.ini`

- [ ] **Step 1: Ask the controller for the scoped measurement**

Do NOT run the full suite. Report ready and ask for the missing statements and branch arms in all five functions: `send_answer` (`:242-310`), `make_reply` (`:55-64`), `edit_reply` (`:68-77`), `choose_answer` (`:233-234`), `unchoose_answer` (`:238-239`). Starting point was 51 missing statements and 12 missing arms.

- [ ] **Step 2: Write a test for each remaining reachable arm**

One named test per arm, docstring naming the line and which arm it takes.

**One is expected to remain and must NOT be chased:** `:300`'s `instance.online()` conjunct, already filtered in SQL by `Community.following_instances` (`app/models.py:849-850`).

If any other arm is unreachable, write the argument rather than a test, consult `tests/README.md` fact 75's catalogue of causes, and **name the establisher**.

- [ ] **Step 3: Run the AST walk and reconcile**

```bash
python3 - <<'PY'
import ast
src = open('app/shared/tasks/notes.py').read()
targets = {'send_answer', 'make_reply', 'edit_reply', 'choose_answer',
           'unchoose_answer'}
for node in ast.walk(ast.parse(src)):
    if isinstance(node, ast.FunctionDef) and node.name in targets:
        print(node.name, node.lineno, node.end_lineno)
        for sub in ast.walk(node):
            if isinstance(sub, ast.IfExp):
                print('   TERNARY', sub.lineno, ast.get_source_segment(src, sub))
PY
```

One is expected — `:303` — but **report the walk's raw output rather than confirming the expectation**. Sub-project 19's controller named three ternaries where the walk found eight, and this sub-project's own spec claimed `send_answer` had none until the walk was run.

- [ ] **Step 4: Write the unreachability comment block**

Add a comment block at the end of `tests/test_shared_tasks_send_answer.py` naming every unreachable item with its establisher, and reconciling every ternary the walk found against the named test taking each arm. Re-derive every line number in it before committing — this is committed prose a later sub-project will trust.

- [ ] **Step 5: Register the findings**

Establish the next free number:

```bash
grep -o 'Next free number: D[0-9]*' docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md | sort -u
```

Expected: `D312`. Confirm rather than assume.

Register:

1. **`:300`'s redundant `instance.online()` conjunct** — a **fifth** instance of D302's shape. D302's cell currently names four (`pages.py:295`, `:350`, `:351`, `notes.py:217`). Sub-project 20 appended the fourth **in place** rather than taking a new number; follow that precedent unless the register says otherwise.
2. **`send_answer` never calls `patch_db_session`** — `make_reply` (`:58`) and `edit_reply` (`:71`) both do. Establish the severity by execution, not argument, and record it at its measured strength.
3. **`object=` at `:282`/`:285` shadows the builtin** — minor, reading-level.
4. **D309's site count drops from ten to nine**, because `:248` now tests `private`. Update the cell rather than leaving it claiming ten.

Do a two-pass citation sweep (fact 100), and grep for the value being corrected rather than the text being edited.

- [ ] **Step 6: Add the harness facts**

```bash
grep -oE '^\*\*1[0-9][0-9]\.' tests/README.md | sort -u | tail -3
```

Facts end at **118**; append from **119**. Note that a naive `grep -o '^1[0-9][0-9]\.'` returns line-number references inside prose, not fact numbers — match the `**NNN.` heading form.

Candidates, each written only if it bit:
- `send_answer` takes no `session` parameter and opens its own, so seeded rows must be committed before the call or the function cannot see them.
- The `@context` nesting asymmetry: deleted from whichever object becomes inner, kept on the outermost.
- `:289` rebinds `cc` to a new list rather than mutating it, so the inner object's `cc` is unaffected.
- Whatever the `patch_db_session` measurement established.

- [ ] **Step 7: Ask the controller for the blended figure and raise the floor**

`coverage_floors.ini` has `app/shared/tasks/notes.py = 70`. Ask for the post-work blended `summary['percent_covered']`, then raise the entry to the measured value floored to a whole percent. **Floors only ever rise.**

- [ ] **Step 8: Verify the floor check**

```bash
python3 tests/check_coverage_floors.py scratch_full_cov.json coverage_floors.ini
```

Expect exit 0. **Read the report mtime it prints** — the file is gitignored and persists, so a run that failed to write it leaves stale data the ratchet would pass against.

- [ ] **Step 9: Ask the controller for the full-suite run, then commit**

Report ready. The controller runs the suite once, **in the foreground**, and reports counts, wall time and the report mtime.

Subject: `docs: register sub-project 21's findings and raise app/shared/tasks/notes.py's floor`

---

## Success criteria

From spec §8:

1. `tests/test_shared_tasks_send_answer.py` exists and calls `send_answer` directly; `tests/test_shared_tasks_send_reply.py` gains `make_reply` and `edit_reply` coverage.
2. All five functions at zero uncovered statements and zero uncovered branch arms, except any proved unreachable with a written argument **naming its establisher**.
3. All four `is_undo` × `is_local` paths asserted on **serialized outbound bytes**, with `@context` asserted present on the outermost object and absent on every nested one, per path.
4. `:303`'s ternary has both arms exercised by named tests, reconciled by an AST walk rather than by the coverage number.
5. The follower loop's three arms — never entered, guard-continue, guard-send — each covered by a named test.
6. Both wrapper arms covered for all four wrappers, the error arm reached by a natural raise and witnessed by a recording `Session`.
7. At most one production guard lands, at `:248`, proved by a test that fails before it; D309's site count updated from ten to nine.
8. The register carries D312 onward, with `:300` handled per D302's precedent.
9. `coverage_floors.ini`'s `app/shared/tasks/notes.py` entry raised from 70 to the measured blended figure.

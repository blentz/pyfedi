# Sub-project 23: closing the `adds.py`/`removes.py` twins — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close both twin modules — `add_object`/`remove_object` and their four Celery wrappers — landing the `private` conjunct in each, taking both from 18.03% to near-100.

**Architecture:** One new test file, `tests/test_shared_tasks_add_remove.py`, with a shared prelude and every test written out separately for adds and removes — never parametrised across the twins, because a parametrised failure names the parameter rather than the function. Assertions are on **serialized outbound request bytes** via respx, never on in-memory dicts.

**Tech Stack:** pytest, respx/httpx, SQLAlchemy, Flask, Celery (eager), podman-compose via `./run_tests.sh`.

**Spec:** `docs/superpowers/specs/2026-09-07-coverage-add-remove-23-design.md`

## Global Constraints

Copied verbatim from the spec. Every task's requirements implicitly include this section.

- **Delete nothing the task did not create. `claude_test` in the repository root is not the campaign's.**
- Only the controller runs the full suite, one pytest session at a time, and **in the foreground**.
- **`pytest` exits 1 on a session timeout and `run_tests.sh` propagates it** (`pytest.ini:26-27`). The campaign's old "still exits 0" claim was measured false; the observed exit 0 was a shell **pipeline** eating the status. Read `${PIPESTATUS[0]}`, or do not pipe. Continue to check the test count and the coverage report's mtime, which catch more than a timeout.
- **A wrong `--cov` target fails silently and green** (fact 117): `--cov` takes a module path, so a file path collects nothing, writes no JSON, and exits 0.
- Coverage command: `./run_tests.sh --cov=app --cov-branch --cov-report=json:scratch_full_cov.json -q`.
- **Test counts come from collection, never from `grep -c '^def test_'`** — the campaign has been bitten by that gap three times.
- **A wedged podman stack reports failures that are not regressions.** Run `./run_tests.sh --down` and retry before believing any failure.
- **Mutation instrument.** One mutation at a time, applied as a targeted single-line `sed`, never a whole-file rewrite. **Dry-run every substitution without `-i` and confirm the produced line before trusting it** — a plan's `sed` is untested code, and three written across sub-projects 21-22 were defective, one of which did not parse. **Every mutation must be run against BOTH twins**: the files are identical, so a mutation that kills in one and survives in the other is a divergence in the tests, not in the code, and it is the only signal that would catch a test-design bug replicated across both. Each file is **100 lines** before and after; assert the line count and an empty `git diff -- app/` after every apply and restore.
- Mutation record: the mutation, the test that killed it, **assertion-kill or crash-kill**, **sole or multi**, and **as-of which commit**. A mutant that applies but does not change the program is a **no-op substitution** — not a survivor and not an equivalent mutant; one that does not parse is neither.
- **Commit the production changes before running the mutations**: the clean-tree assertion is impossible while a fix is uncommitted.
- **Use `ast.parse` and `FunctionDef.end_lineno` for extents, never a convention.**
- Every line number must be re-derived against the current tree before it is written down (fact 99). Citation sweeps run in two passes, `file:line` then bare paths against `git ls-files` (fact 100), and **read the first pass's output to the end**.
- **When a file shifts, sweep by the cause — the moved file — not by the topic that made you notice.**
- **Fix a citation's symbol, not just its number**, and the converse: a corrected symbol beside a stale number is still a wrong citation.
- **Where a total drifts, lead with the invariant.** A `--stat` total over a range ending at `HEAD` moves whenever anything is appended; a per-file `--numstat` of modified-only lines is a property of the change itself.
- **Anchor every `HEAD`-relative number to the commit it was true at.**
- Enumerate conditional expressions by AST walk, not grep (fact 94) — coverage.py emits no arc for one (fact 87).
- Commit messages containing backticks are committed with `git commit -F <file>`, never `-m`.

---

## File Structure

| file | responsibility |
|---|---|
| `tests/test_shared_tasks_add_remove.py` (create) | all tests for both twins, shared prelude, written out per module |
| `app/shared/tasks/adds.py:63` (modify) | D309's `private` conjunct — extends an existing line |
| `app/shared/tasks/removes.py:63` (modify) | the same fix, same line, other twin |
| `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` (modify) | D316 onward; D302 extended |
| `tests/README.md` (modify) | harness facts from 129 |
| `coverage_floors.ini` (modify) | **add** two entries |

Neither production file may change its line count. Both stay at **100 lines**.

---

### Task 1: The shared prelude and two smoke tests

**Files:**
- Create: `tests/test_shared_tasks_add_remove.py`

**Interfaces:**
- Produces: `_seed(local_community=True, with_keys=False)` → `SimpleNamespace(instance, user, community, post, mod)`; `_peer(domain='peer.example', software='lemmy')`; `_make_deliverable(s, inbox=PEER_INBOX)`; `_remote_inbox(s, http_mock, inbox=PEER_INBOX)`; `_sent_activity(route, index=-1)`; `_key_id_of(route, index=-1)`; `_community_follower(s, http_mock=None, domain='fan.example', member_name='fan', with_inbox=True)`; `_recording_task_session(monkeypatch, module)`; constants `PEER_INBOX`, `FEATURED_URL`, `MODERATORS_URL`; the autouse `_peer_example_resolves_without_a_resolver` fixture.

**THE TRAP THIS TASK EXISTS TO CLOSE.** `make_community` (`tests/factories.py:122`) sets `ap_followers_url` but leaves **`ap_featured_url` and `ap_moderators_url` as `None`**. The `:74` ternary selects between exactly those two columns. If `_seed` does not set them to **distinct non-None values**, both arms produce `None`, every `target` assertion passes under either arm, and the ternary tests are tests that cannot fail. `_seed` sets them, and the smoke tests assert they differ.

- [ ] **Step 1: Write the module prelude**

```python
"""`add_object` and `remove_object` -- the twin builders of AP Add and Remove.

`app/shared/tasks/adds.py:56-100` and `app/shared/tasks/removes.py:56-100`
(extents by ast). THE TWO MODULES ARE STRUCTURALLY IDENTICAL: both are 100
lines and 51 statements, their coverage residuals are the same lines and the
same arcs function for function, and after normalising names the only textual
difference between the files is one docstring word. They share even their dead
imports -- `post_request` is imported at `:2` of each and called in neither.

That equivalence is why one file tests both, and it is asserted rather than
assumed (see `test_the_twins_are_structurally_identical` below), so a future
divergence becomes a detectable event rather than a later discovery.

EVERY TEST IS WRITTEN OUT TWICE, ONCE PER TWIN, AND NEVER PARAMETRISED ACROSS
THEM. A parametrised failure names the parameter rather than the function, and
an arm covered under only one parameter is invisible in the failure output --
the same reasoning that kept `make_post`'s and `edit_post`'s error tests
separate in sub-project 22.

THE `:74` TERNARY IS THE REASON `_seed` SETS TWO URL COLUMNS.
`community.ap_moderators_url if community_id else community.ap_featured_url`
selects between two columns that `make_community` (tests/factories.py:122)
leaves at None. With both None, the ternary's two arms produce the same value,
every `target` assertion passes under either, and the tests prove nothing.
`_seed` sets them to distinct values and the smoke tests assert they differ.

THE TWO WRAPPER LOOKUPS DIFFER, TWELVE LINES APART, IN BOTH TWINS:
`:32` is `session.query(Post).get(post_id)` and returns None for a missing id,
so the raise arrives later as `AttributeError` when `:59` reads
`object.community`; `:47` is `session.query(User).filter_by(id=mod_id).one()`
and raises `NoResultFound`. Establish each by reading, never by inheriting from
its neighbour -- this is the third module in which the campaign has met this
trap, and sub-project 21 lost a fix round to it.
"""

import json
import socket
from types import SimpleNamespace

import pytest

from app import db
from app.models import ActivityPubLog
from app.shared.tasks.adds import add_mod, add_object, sticky_post
from app.shared.tasks.removes import remove_mod, remove_object, unsticky_post
from tests.factories import (
    make_community, make_community_member, make_instance, make_post, make_user,
)

PEER_INBOX = 'https://peer.example/c/c1/inbox'
FEATURED_URL = 'https://test.piefed.local/c/c1/featured'
MODERATORS_URL = 'https://test.piefed.local/c/c1/moderators'

_REAL_GETADDRINFO = socket.getaddrinfo

# Any globally routable literal will do -- the only property
# app/utils.py:5530 reads off it is `is_global`.
_EXAMPLE_TLD_ADDRESS = '93.184.216.34'


def _seed(local_community=True, with_keys=False):
    """instance, author, community, post, mod -- committed.

    ORDER IS LOAD-BEARING. `make_community` (tests/factories.py:122) hardcodes
    `instance_id=1` and the db_session teardown resets every sequence, so the
    local instance is created FIRST; a peer built before this call would take
    id 1 and leave the community's FK pointing at it.

    `ap_featured_url` AND `ap_moderators_url` ARE SET HERE AND MUST STAY SET.
    `make_community` leaves both None, and `:74`'s ternary selects between
    exactly these two columns -- with both None the ternary's arms are
    indistinguishable on the wire.

    `local_community=False` sets `ap_id` AND `ap_profile_id` on peer.example,
    because `Community.is_local()` (app/models.py:795) is a DISJUNCTION whose
    `profile_id()` falls back to a computed default; `ap_id` alone leaves the
    community silently LOCAL and `:100` never runs (fact 112).
    """
    instance = make_instance('test.piefed.local', software='piefed')
    user = make_user(instance, 'author', local=True, with_keys=with_keys)
    community = make_community('c1')
    community.ap_featured_url = FEATURED_URL
    community.ap_moderators_url = MODERATORS_URL
    post = make_post(community, user, ap_id='https://test.piefed.local/post/1')
    mod = make_user(instance, 'themod', local=True)
    if not local_community:
        community.ap_id = 'c1@peer.example'
        community.ap_profile_id = 'https://peer.example/c/c1'
        community.ap_public_url = 'https://peer.example/c/c1'
        community.ap_followers_url = 'https://peer.example/c/c1/followers'
        community.ap_domain = 'peer.example'
    db.session.commit()
    return SimpleNamespace(instance=instance, user=user, community=community,
                           post=post, mod=mod)


def _peer(domain='peer.example', software='lemmy'):
    """A remote Instance. ALWAYS call AFTER `_seed()` -- see `_seed` for why."""
    return make_instance(domain, software=software)


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
    That is what makes it dangerous rather than merely slow: on a machine whose
    resolver hijacks NXDOMAIN into a wildcard A record, these tests start
    failing at app/utils.py:5530's `is_global` check instead.
    """
    monkeypatch.setattr(socket, 'getaddrinfo', _getaddrinfo_without_the_network)


def _make_deliverable(s, inbox=PEER_INBOX):
    """Attach the community to a real peer Instance and give it an inbox.

    The database half of `_remote_inbox`. The early-return tests call this
    ALONE, because they assert the delivery never happens and `http_mock` is
    built with `assert_all_called=True` -- a registered route that never fires
    would fail them for the wrong reason.
    """
    s.community.instance_id = _peer().id
    s.community.ap_inbox_url = inbox
    db.session.commit()


def _remote_inbox(s, http_mock, inbox=PEER_INBOX):
    """THE CAPTURE MECHANISM. Returns the respx route the activity lands on.

    Three things must be true for `:100`'s call to become an observable
    request: the community must be remote (`_seed(local_community=False)`,
    closing BOTH disjuncts of `Community.is_local()`); it must have an
    `ap_inbox_url`, which `make_community` leaves None and which `post_request`
    (app/activitypub/signature.py:109-111) short-circuits on; and the user must
    have a keypair (`_seed(with_keys=True)`), because signing calls `.encode()`
    on `user.private_key`.

    WHY THE HTTP BODY AND NOT A RECORDER: `:82` mutates `add` in place with
    `del`, so a recorder holding the dict would be read back post-`del`. respx
    captures the bytes that actually left, at
    app/activitypub/signature.py:494.
    """
    _make_deliverable(s, inbox)
    return http_mock.post(inbox).respond(200, json={})


def _sent_activity(route, index=-1):
    """The JSON body of the request `route` captured, decoded from the bytes
    that were actually sent. Defaults to the most recent call."""
    return json.loads(route.calls[index].request.content)


def _key_id_of(route, index=-1):
    """The `keyId` the captured request was signed under.

    The only observable separating `:98`'s signer (the COMMUNITY) from
    `:100`'s (the USER). respx never verifies a signature, so the key material
    leaves no trace on the wire -- only the declared keyId does.
    """
    return route.calls[index].request.headers['signature'].split('"')[1]


def _community_follower(s, http_mock=None, domain='fan.example',
                        member_name='fan', with_inbox=True):
    """A remote instance `community.following_instances()` returns at `:96`,
    plus the respx route its delivery lands on.

    Returns `SimpleNamespace(instance, member, route)`; `route` is None when
    none was registered.

    THE COMMUNITY'S KEYPAIR IS SET HERE. `:98` signs with
    `community.private_key`, which `make_community` leaves None, and signing
    calls `.encode()` on it. The author's key is reused rather than a second
    generated: generation costs about a second, respx never verifies a
    signature, and the actor a delivery was signed AS is observable through
    `keyId`, not through the key material.

    `with_inbox=False` leaves `Instance.inbox` None, closing `:97`'s FIRST
    conjunct.
    """
    assert s.user.private_key is not None, '_seed(with_keys=True) is required'
    s.community.private_key = s.user.private_key
    s.community.public_key = s.user.public_key
    instance = make_instance(domain, software='lemmy')
    instance.inbox = f'https://{domain}/inbox' if with_inbox else None
    member = make_user(instance, member_name, local=False)
    make_community_member(member, s.community)
    db.session.commit()
    route = (http_mock.post(instance.inbox).respond(200, json={})
             if http_mock is not None and with_inbox else None)
    return SimpleNamespace(instance=instance, member=member, route=route)


def _recording_task_session(monkeypatch, module):
    """Make `get_task_session` hand back a GENUINE Session that records
    `rollback()` and `close()`.

    `module` is the twin whose binding to patch -- `app.shared.tasks.adds` or
    `app.shared.tasks.removes`. BOTH import `get_task_session` into their own
    namespace, so patching `app.utils.get_task_session` would miss the binding
    the wrappers actually call and leave these tests green while observing
    nothing. Two twins, two patch targets.

    The session is real and does real work; only the observation is added, by
    wrapping the two methods rather than replacing the object.

    Returns a `SimpleNamespace(calls=[])`; the wrapper appends 'rollback' and
    'close' in the order they happened, so `finally` running after `except` is
    observable rather than assumed.
    """
    from app import db as _db
    from sqlalchemy.orm import Session as _Session

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

    monkeypatch.setattr(module, 'get_task_session', _make)
    return record
```

- [ ] **Step 2: Write the equivalence assertion and two smoke tests**

```python
def test_the_twins_are_structurally_identical(db_session):
    """The premise this whole file rests on, asserted rather than assumed.

    `adds.py` and `removes.py` are the same file with different names. If that
    ever stops being true, this test fails and the divergence becomes a
    detectable event rather than something a later sub-project discovers.

    The comparison is deliberately coarse -- function names, extents and line
    count -- because a stricter one would fail on the legitimate naming
    differences, and a looser one would not notice a function being added.
    """
    # [SUPERSEDED BY THE FINAL REVIEW WAVE -- this block is the plan as written,
    #  kept so citations into it resolve. The committed test compares NORMALISED
    #  TEXT, not shape: the justification above is false, because the
    #  hunk-directed recipe at the end of the test file normalises exactly the
    #  legitimate naming differences and nothing else. Shape alone passes when
    #  one twin loses a conjunct, gains a swapped ternary, or renames a local --
    #  all three demonstrated. Read the committed function, not this one.]
    import ast

    def shape(path):
        src = open(path).read()
        return (
            len(src.splitlines()),
            [(n.lineno, n.end_lineno) for n in ast.parse(src).body
             if isinstance(n, ast.FunctionDef)],
        )

    adds_lines, adds_extents = shape('app/shared/tasks/adds.py')
    removes_lines, removes_extents = shape('app/shared/tasks/removes.py')

    assert adds_lines == removes_lines == 100
    assert adds_extents == removes_extents == [(27, 38), (42, 53), (56, 100)]


def test_a_remote_community_receives_the_bare_add(db_session, http_mock):
    """The harness itself: `:81`'s FALSE arm reaches `:100` and the bytes are
    readable. `target` is asserted non-None because the whole ternary suite
    depends on `_seed` setting the two URL columns `make_community` leaves
    empty."""
    s = _seed(local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)

    add_object(db.session, s.user.id, s.post)

    sent = _sent_activity(route)
    assert sent['type'] == 'Add'
    assert sent['target'] == FEATURED_URL
    assert FEATURED_URL != MODERATORS_URL


def test_a_remote_community_receives_the_bare_remove(db_session, http_mock):
    """The removes twin of the test above -- `removes.py:100`."""
    s = _seed(local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)

    remove_object(db.session, s.user.id, s.post)

    sent = _sent_activity(route)
    assert sent['type'] == 'Remove'
    assert sent['target'] == FEATURED_URL
```

- [ ] **Step 3: Run the file**

Run: `./run_tests.sh tests/test_shared_tasks_add_remove.py -q`
Expected: **3 passed**.

- [ ] **Step 4: Commit**

Subject: `test: open tests/test_shared_tasks_add_remove.py with the twin harness`

---

### Task 2: The `:63` guards — one fix, both twins

**Files:**
- Modify: `app/shared/tasks/adds.py:63`, `app/shared/tasks/removes.py:63`
- Test: `tests/test_shared_tasks_add_remove.py`

**Interfaces:**
- Consumes: `_seed`, `_make_deliverable` from Task 1.

Both files' `:63` currently read:

```python
    if community.local_only or not community.instance.online():
```

The discriminator is `ActivityPubLog.query.count()`, not a call count: `post_request` (`app/activitypub/signature.py:103-105`) writes a row unconditionally before touching the transport, so 0 proves `:64` returned and 1 proves it did not. These tests call `_make_deliverable` and **not** `_remote_inbox`.

- [ ] **Step 1: Write the four existing-guard tests**

```python
def test_a_local_only_community_does_not_federate_the_add(db_session, http_mock):
    """adds.py:63's TRUE arm via `local_only`, returning at :64."""
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    s.community.local_only = True
    db.session.commit()

    add_object(db.session, s.user.id, s.post)

    assert db.session.query(ActivityPubLog).count() == 0


def test_a_local_only_community_does_not_federate_the_remove(db_session, http_mock):
    """removes.py:63's TRUE arm via `local_only`. The twin of the test above."""
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    s.community.local_only = True
    db.session.commit()

    remove_object(db.session, s.user.id, s.post)

    assert db.session.query(ActivityPubLog).count() == 0


def test_a_dormant_instance_does_not_receive_the_add(db_session, http_mock):
    """adds.py:63's TRUE arm via `not instance.online()`.

    `Instance.online()` (app/models.py:118-119) is
    `not (self.dormant or self.gone_forever)`. `_make_deliverable` reassigns
    `community.instance_id` to the peer before its commit, so this lands on
    the instance the guard actually reads.
    """
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    s.community.instance.dormant = True
    db.session.commit()

    add_object(db.session, s.user.id, s.post)

    assert db.session.query(ActivityPubLog).count() == 0


def test_a_dormant_instance_does_not_receive_the_remove(db_session, http_mock):
    """removes.py:63's TRUE arm via `not instance.online()`. The twin of the
    test above."""
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    s.community.instance.dormant = True
    db.session.commit()

    remove_object(db.session, s.user.id, s.post)

    assert db.session.query(ActivityPubLog).count() == 0
```

- [ ] **Step 2: Run them**

Run: `./run_tests.sh tests/test_shared_tasks_add_remove.py -q`
Expected: **7 passed**.

- [ ] **Step 3: Write the two failing guard tests**

```python
def test_a_private_community_does_not_federate_the_add(db_session, http_mock):
    """adds.py:63's `private` conjunct -- one of this sub-project's two
    production changes.

    `Community.private` (app/models.py:611) is commented "only members can
    view. no federation.", and before this conjunct landed `:63` tested only
    `local_only` and `instance.online()` -- so a private, non-local-only
    community federated its stickies and moderator adds out.

    D309's FOURTH closed site of twelve gates; sub-projects 20, 21 and 22
    closed `notes.py:143`, `notes.py:248` and `pages.py:398`. `local_only` is
    left False deliberately: with it True the test would pass on the
    pre-existing conjunct and prove nothing.
    """
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    s.community.private = True
    db.session.commit()

    add_object(db.session, s.user.id, s.post)

    assert db.session.query(ActivityPubLog).count() == 0


def test_a_private_community_does_not_federate_the_remove(db_session, http_mock):
    """removes.py:63's `private` conjunct -- the twin, and D309's FIFTH closed
    site.

    THE TWINS ARE FIXED TOGETHER DELIBERATELY. Guarding one and not the other
    would manufacture a structural divergence between two files that are
    currently identical -- precisely the defect this campaign hunts for.
    """
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    s.community.private = True
    db.session.commit()

    remove_object(db.session, s.user.id, s.post)

    assert db.session.query(ActivityPubLog).count() == 0
```

- [ ] **Step 4: Run them and watch BOTH FAIL**

Run: `./run_tests.sh tests/test_shared_tasks_add_remove.py -q`
Expected: **2 failed, 7 passed** — each failing `assert 1 == 0`, the delivery attempted and a failure row written.

**Do not proceed until you have seen both fail.** A guard that lands before its test has failed is a guard nothing proves. Record both failures in your report.

- [ ] **Step 5: Land both guards, one targeted `sed` each**

```bash
sed -n '63p' app/shared/tasks/adds.py
sed -i '63s/local_only or not/local_only or community.private or not/' app/shared/tasks/adds.py
sed -i '63s/local_only or not/local_only or community.private or not/' app/shared/tasks/removes.py
sed -n '63p' app/shared/tasks/adds.py; sed -n '63p' app/shared/tasks/removes.py
wc -l app/shared/tasks/adds.py app/shared/tasks/removes.py
```

Expected: both `:63` now read

```python
    if community.local_only or community.private or not community.instance.online():
```

and both files are still **100 lines**.

- [ ] **Step 6: Run the file**

Run: `./run_tests.sh tests/test_shared_tasks_add_remove.py -q`
Expected: **9 passed**.

- [ ] **Step 7: Commit both guards together**

Subject: `fix: stop add_object and remove_object federating a private community`

Committing here is required: Task 7's mutations begin with `git checkout -- app/`, which would discard an uncommitted fix.

---

### Task 3: The remote path and the `:74` ternary

**Files:**
- Test: `tests/test_shared_tasks_add_remove.py`

**Interfaces:**
- Consumes: `_seed`, `_remote_inbox`, `_sent_activity`, `_key_id_of`, `FEATURED_URL`, `MODERATORS_URL` from Task 1.

`:81`'s false arm runs `:100`, posting the bare `Add`/`Remove` to `community.ap_inbox_url`, signed as the **user**. The `:74` ternary picks `target`, and `:58` picks how `community` is resolved — both driven by whether `community_id` is passed.

**coverage.py emits no arc for `:74`**, so it reports zero missing arms whether or not both arms run. Only these named tests can tell.

- [ ] **Step 1: Write the ternary and key-set tests**

```python
def test_the_add_without_a_community_id_targets_the_featured_url(
        db_session, http_mock):
    """:58's TRUE arm (:59) and :74's FALSE arm, in adds.py.

    No `community_id` means the community comes from `object.community` at
    :59, and `:74` selects `ap_featured_url`. `target` is the witness for
    both at once, and it can only discriminate because `_seed` sets the two
    URL columns to different values -- `make_community` leaves both None.
    """
    s = _seed(local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)

    add_object(db.session, s.user.id, s.post)

    sent = _sent_activity(route)
    assert sent['target'] == FEATURED_URL
    assert set(sent) == {'id', 'type', 'actor', 'object', 'target',
                         '@context', 'audience', 'to', 'cc'}
    assert sent['actor'] == s.user.public_url()
    assert sent['object'] == s.post.public_url()
    assert sent['audience'] == s.community.public_url()
    assert sent['to'] == ['https://www.w3.org/ns/activitystreams#Public']
    assert sent['cc'] == [s.community.public_url()]


def test_the_add_with_a_community_id_targets_the_moderators_url(
        db_session, http_mock):
    """:58's FALSE arm (:61) and :74's TRUE arm, in adds.py.

    The companion to the test above: passing `community_id` resolves the
    community by query at :61 and selects `ap_moderators_url` at :74. Asserting
    a value the other arm could not produce is what makes the pair
    discriminating.
    """
    s = _seed(local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)

    add_object(db.session, s.user.id, s.mod, s.community.id)

    sent = _sent_activity(route)
    assert sent['target'] == MODERATORS_URL
    assert sent['object'] == s.mod.public_url()


def test_the_remove_without_a_community_id_targets_the_featured_url(
        db_session, http_mock):
    """:58's TRUE arm and :74's FALSE arm, in removes.py -- the twin."""
    s = _seed(local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)

    remove_object(db.session, s.user.id, s.post)

    sent = _sent_activity(route)
    assert sent['type'] == 'Remove'
    assert sent['target'] == FEATURED_URL
    assert sent['object'] == s.post.public_url()


def test_the_remove_with_a_community_id_targets_the_moderators_url(
        db_session, http_mock):
    """:58's FALSE arm and :74's TRUE arm, in removes.py -- the twin."""
    s = _seed(local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)

    remove_object(db.session, s.user.id, s.mod, s.community.id)

    sent = _sent_activity(route)
    assert sent['type'] == 'Remove'
    assert sent['target'] == MODERATORS_URL
    assert sent['object'] == s.mod.public_url()


def test_the_remote_add_keeps_its_context_and_is_signed_as_the_user(
        db_session, http_mock):
    """`@context` is asserted PRESENT: nothing nests this object on this path,
    because :82's `del` runs only under `is_local()`. And :100 signs with
    `user.public_url() + '#main-key'` where :98 signs as the COMMUNITY --
    `keyId` is the only observable that separates them.
    """
    s = _seed(local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)

    add_object(db.session, s.user.id, s.post)

    assert '@context' in _sent_activity(route)
    assert _key_id_of(route) == s.user.public_url() + '#main-key'


def test_the_remote_remove_keeps_its_context_and_is_signed_as_the_user(
        db_session, http_mock):
    """The removes twin of the test above."""
    s = _seed(local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)

    remove_object(db.session, s.user.id, s.post)

    assert '@context' in _sent_activity(route)
    assert _key_id_of(route) == s.user.public_url() + '#main-key'
```

- [ ] **Step 2: Run the file**

Run: `./run_tests.sh tests/test_shared_tasks_add_remove.py -q`
Expected: **15 passed**.

- [ ] **Step 3: Commit**

Subject: `test: cover the twins' remote arm and both sides of the :74 ternary`

---

### Task 4: The local Announce path and the `@context` asymmetry

**Files:**
- Test: `tests/test_shared_tasks_add_remove.py`

**Interfaces:**
- Consumes: `_seed`, `_sent_activity`, `_key_id_of`, `_community_follower` from Task 1.

`:81`'s true arm deletes `add['@context']` at `:82`, builds the `Announce` at `:87-95`, and delivers per follower at `:98`. `_seed()`'s default leaves the community local. A follower is **required** or `:96`'s loop body never runs.

- [ ] **Step 1: Write the local-path tests**

```python
def test_a_local_community_announces_the_add_and_strips_its_inner_context(
        db_session, http_mock):
    """adds.py:81's TRUE arm -- :82's `del add['@context']` and the Announce
    built at :87-95.

    The Announce keeps the `@context` built at :92; the Add nested at :91 has
    had its own stripped at :82. Asserting BOTH directions is what catches a
    mutant deleting from the wrong object -- either alone would still accept a
    well-formed activity.

    `cc` is the followers collection here (:86), NOT the
    `[community.public_url()]` the inner Add carries (:68) -- :86 rebinds the
    name to a NEW list, so the inner object's `cc` still points at the old one.
    Asserting both proves the rebinding did not alias.
    """
    s = _seed(with_keys=True)
    fan = _community_follower(s, http_mock)

    add_object(db.session, s.user.id, s.post)

    sent = _sent_activity(fan.route)
    assert sent['type'] == 'Announce'
    assert '@context' in sent
    assert sent['actor'] == s.community.public_url()
    assert sent['object']['type'] == 'Add'
    assert '@context' not in sent['object']
    assert sent['cc'] == [s.community.ap_followers_url]
    assert sent['object']['cc'] == [s.community.public_url()]


def test_a_local_community_announces_the_remove_and_strips_its_inner_context(
        db_session, http_mock):
    """removes.py:81's TRUE arm -- the twin of the test above, asserting the
    same asymmetry on the Remove."""
    s = _seed(with_keys=True)
    fan = _community_follower(s, http_mock)

    remove_object(db.session, s.user.id, s.post)

    sent = _sent_activity(fan.route)
    assert sent['type'] == 'Announce'
    assert '@context' in sent
    assert sent['object']['type'] == 'Remove'
    assert '@context' not in sent['object']
    assert sent['cc'] == [s.community.ap_followers_url]
    assert sent['object']['cc'] == [s.community.public_url()]


def test_the_announced_add_is_signed_as_the_community(db_session, http_mock):
    """adds.py:98 signs with `community.private_key` and
    `community.public_url() + '#main-key'` -- the companion to Task 3's
    user-signed assertion. The community's and user's public urls differ in
    path, so a mutant swapping the signer produces a valid but wrong keyId."""
    s = _seed(with_keys=True)
    fan = _community_follower(s, http_mock)

    add_object(db.session, s.user.id, s.post)

    assert _key_id_of(fan.route) == s.community.public_url() + '#main-key'


def test_the_announced_remove_is_signed_as_the_community(db_session, http_mock):
    """removes.py:98 -- the twin of the test above."""
    s = _seed(with_keys=True)
    fan = _community_follower(s, http_mock)

    remove_object(db.session, s.user.id, s.post)

    assert _key_id_of(fan.route) == s.community.public_url() + '#main-key'
```

- [ ] **Step 2: Run the file**

Run: `./run_tests.sh tests/test_shared_tasks_add_remove.py -q`
Expected: **19 passed**.

- [ ] **Step 3: Commit**

Subject: `test: cover the twins' Announce arm and the @context nesting asymmetry`

---

### Task 5: The follower loop's three arms

**Files:**
- Test: `tests/test_shared_tasks_add_remove.py`

**Interfaces:**
- Consumes: `_seed`, `_community_follower` from Task 1.

The loop is `:96-98` in both twins. Arc `(96, 97)` is already covered by Task 4. The three remaining are `(96, -56)` — loop never entered — `(97, 96)` — guard skips, loop continues — and `(97, 98)` — guard delivers.

**`:97`'s `instance.online()` conjunct is NOT to be exercised as a live branch.** `Community.following_instances()` (`app/models.py:842-851`) already filters `Instance.dormant == False` at `:849` and `Instance.gone_forever == False` at `:850`, and `Instance.online()` (`:118-119`) is exactly `not (self.dormant or self.gone_forever)` — the conjunct is unreachable-False and belongs to Task 8's register entry. Do not write a test that tries to close it.

- [ ] **Step 1: Write the six loop tests**

```python
def test_a_local_community_with_no_followers_sends_no_add(db_session, http_mock):
    """adds.py:96's loop never entered -- straight past to the return.

    `following_instances()` returns empty because no CommunityMember exists on
    a remote instance. The Announce is still BUILT at :87-95; nothing delivers
    it.
    """
    s = _seed(with_keys=True)

    add_object(db.session, s.user.id, s.post)

    assert db.session.query(ActivityPubLog).count() == 0


def test_a_local_community_with_no_followers_sends_no_remove(
        db_session, http_mock):
    """removes.py:96's loop never entered -- the twin."""
    s = _seed(with_keys=True)

    remove_object(db.session, s.user.id, s.post)

    assert db.session.query(ActivityPubLog).count() == 0


def test_a_following_instance_without_an_inbox_gets_no_add(db_session, http_mock):
    """adds.py:97's FALSE arm -- the loop continuing.

    `with_inbox=False` leaves `Instance.inbox` None, closing :97's FIRST
    conjunct before any other is evaluated. No route is registered, so
    `http_mock`'s `assert_all_called=True` is not tripped, and the count of 0
    rules out a request attempted against a None inbox.
    """
    s = _seed(with_keys=True)
    _community_follower(s, http_mock, with_inbox=False)

    add_object(db.session, s.user.id, s.post)

    assert db.session.query(ActivityPubLog).count() == 0


def test_a_following_instance_without_an_inbox_gets_no_remove(
        db_session, http_mock):
    """removes.py:97's FALSE arm -- the twin."""
    s = _seed(with_keys=True)
    _community_follower(s, http_mock, with_inbox=False)

    remove_object(db.session, s.user.id, s.post)

    assert db.session.query(ActivityPubLog).count() == 0


def test_one_instance_is_skipped_while_another_receives_the_add(
        db_session, http_mock):
    """Both of adds.py's loop arms in ONE run -- the skip, then the delivery.

    The discriminating case: with a single follower, "skipped" and "loop
    ended" both produce zero deliveries and are indistinguishable. The second,
    delivering instance is what proves iteration continued PAST the skipped
    one.

    THE ORDER ASSERTION IS LOAD-BEARING, NOT DECORATION.
    `Community.following_instances()` (app/models.py:842-851) ends in an
    unordered `.distinct().all()` -- no ORDER BY -- so the order is a property
    of Postgres's query plan, not of the code. If it ever reverses, a mutant
    turning "skip and continue" into "skip and break" would still leave the
    delivered route called once and this test would pass while no longer
    proving what it claims.
    """
    s = _seed(with_keys=True)
    _community_follower(s, http_mock, domain='mute.example',
                        member_name='mute', with_inbox=False)
    good = _community_follower(s, http_mock, domain='fan.example',
                               member_name='fan')

    ordered = [i.domain for i in s.community.following_instances()]
    assert ordered == ['mute.example', 'fan.example'], (
        f'this test proves the loop CONTINUES past a skip, which requires the '
        f'skipped instance first; got {ordered}')

    add_object(db.session, s.user.id, s.post)

    assert good.route.call_count == 1
    assert db.session.query(ActivityPubLog).count() == 1


def test_one_instance_is_skipped_while_another_receives_the_remove(
        db_session, http_mock):
    """Both of removes.py's loop arms in one run -- the twin of the test
    above, carrying the same order assertion for the same reason."""
    s = _seed(with_keys=True)
    _community_follower(s, http_mock, domain='mute.example',
                        member_name='mute', with_inbox=False)
    good = _community_follower(s, http_mock, domain='fan.example',
                               member_name='fan')

    ordered = [i.domain for i in s.community.following_instances()]
    assert ordered == ['mute.example', 'fan.example'], (
        f'this test proves the loop CONTINUES past a skip, which requires the '
        f'skipped instance first; got {ordered}')

    remove_object(db.session, s.user.id, s.post)

    assert good.route.call_count == 1
    assert db.session.query(ActivityPubLog).count() == 1
```

- [ ] **Step 2: Run the file**

Run: `./run_tests.sh tests/test_shared_tasks_add_remove.py -q`
Expected: **25 passed**.

- [ ] **Step 3: Commit**

Subject: `test: cover the twins' follower loops, including skip-then-deliver`

---

### Task 6: The four Celery wrappers

**Files:**
- Test: `tests/test_shared_tasks_add_remove.py`

**Interfaces:**
- Consumes: `_seed`, `_remote_inbox`, `_sent_activity`, `_recording_task_session`, `FEATURED_URL`, `MODERATORS_URL` from Task 1.

The four wrappers are `sticky_post`/`unsticky_post` (`:27-38`) and `add_mod`/`remove_mod` (`:42-53`). Each wraps its body in `with current_app.app_context():`, opens a task session, and carries `except: rollback; raise` / `finally: close`.

**The two lookups differ and must each be established by reading:**
- `:32` is `session.query(Post).get(post_id)` — returns `None`, so `:59`'s `object.community` raises **`AttributeError`**.
- `:47` is `session.query(User).filter_by(id=mod_id).one()` — raises **`NoResultFound`**.

Re-derive both lines in both twins before writing the assertions. Do not inherit one from the other.

- [ ] **Step 1: Write the four happy-path tests**

```python
def test_sticky_post_delivers_an_add_targeting_featured(db_session, http_mock):
    """`sticky_post` (:27-38) calls `add_object` with NO community_id, so it
    drives :58's true arm and :74's false arm. `send_async` is accepted and
    ignored; None is passed to prove it is not read."""
    s = _seed(local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)

    sticky_post(None, s.user.id, s.post.id)

    sent = _sent_activity(route)
    assert sent['type'] == 'Add'
    assert sent['target'] == FEATURED_URL


def test_unsticky_post_delivers_a_remove_targeting_featured(
        db_session, http_mock):
    """`unsticky_post` (:27-38 of removes.py) -- the twin."""
    s = _seed(local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)

    unsticky_post(None, s.user.id, s.post.id)

    sent = _sent_activity(route)
    assert sent['type'] == 'Remove'
    assert sent['target'] == FEATURED_URL


def test_add_mod_delivers_an_add_targeting_moderators(db_session, http_mock):
    """`add_mod` (:42-53) passes community_id, driving :58's false arm and
    :74's true arm. The `target` difference from `sticky_post` is the whole
    witness that the two wrappers take opposite arms."""
    s = _seed(local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)

    add_mod(None, s.user.id, s.mod.id, s.community.id)

    sent = _sent_activity(route)
    assert sent['type'] == 'Add'
    assert sent['target'] == MODERATORS_URL
    assert sent['object'] == s.mod.public_url()


def test_remove_mod_delivers_a_remove_targeting_moderators(
        db_session, http_mock):
    """`remove_mod` (:42-53 of removes.py) -- the twin."""
    s = _seed(local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)

    remove_mod(None, s.user.id, s.mod.id, s.community.id)

    sent = _sent_activity(route)
    assert sent['type'] == 'Remove'
    assert sent['target'] == MODERATORS_URL
```

- [ ] **Step 2: Write the four error-path tests and two controls**

```python
def test_sticky_post_rolls_back_and_closes_on_a_missing_post(
        db_session, monkeypatch):
    """adds.py:34-36's except arm and :37-38's finally, reached by a NATURAL
    raise.

    `:32` uses `.get()`, which returns None for an absent id, and `:59`'s
    `object.community` then raises AttributeError -- NOT NoResultFound, which
    is what `:47`'s `.one()` raises twelve lines away in the same file.

    The recorded call ORDER is the assertion that `finally` ran after
    `except`, which a bare "was close called" check could not distinguish from
    a wrapper that closed instead of rolling back.
    """
    import app.shared.tasks.adds as adds_module
    s = _seed()
    record = _recording_task_session(monkeypatch, adds_module)

    with pytest.raises(AttributeError):
        sticky_post(None, s.user.id, s.post.id + 1000)

    assert record.calls == ['rollback', 'close']


def test_unsticky_post_rolls_back_and_closes_on_a_missing_post(
        db_session, monkeypatch):
    """removes.py's copy of the same handler -- a SEPARATE function body and
    so a separate pair of arcs. Written out rather than parametrised so each
    twin's arms are attributable to a named test."""
    import app.shared.tasks.removes as removes_module
    s = _seed()
    record = _recording_task_session(monkeypatch, removes_module)

    with pytest.raises(AttributeError):
        unsticky_post(None, s.user.id, s.post.id + 1000)

    assert record.calls == ['rollback', 'close']


def test_add_mod_rolls_back_and_closes_on_a_missing_mod(
        db_session, monkeypatch):
    """adds.py:49-51's except arm, reached by the OTHER lookup style.

    `:47` uses `.filter_by(id=mod_id).one()`, which raises NoResultFound for an
    absent id -- a different exception from the sibling wrapper twelve lines
    above, in the same file. This is why each wrapper's mechanic is established
    by reading rather than inherited.
    """
    from sqlalchemy.exc import NoResultFound
    import app.shared.tasks.adds as adds_module
    s = _seed()
    record = _recording_task_session(monkeypatch, adds_module)

    with pytest.raises(NoResultFound):
        add_mod(None, s.user.id, s.mod.id + 1000, s.community.id)

    assert record.calls == ['rollback', 'close']


def test_remove_mod_rolls_back_and_closes_on_a_missing_mod(
        db_session, monkeypatch):
    """removes.py's copy of the `.one()` handler -- the twin."""
    from sqlalchemy.exc import NoResultFound
    import app.shared.tasks.removes as removes_module
    s = _seed()
    record = _recording_task_session(monkeypatch, removes_module)

    with pytest.raises(NoResultFound):
        remove_mod(None, s.user.id, s.mod.id + 1000, s.community.id)

    assert record.calls == ['rollback', 'close']


def test_sticky_post_closes_the_session_on_the_happy_path(
        db_session, http_mock, monkeypatch):
    """adds.py's finally on the SUCCESS path -- `close` with no `rollback`.

    The control for the error tests: without it, `finally` running is only ever
    observed alongside an exception, and a wrapper that closed only in the
    except arm would pass everything else in this file.
    """
    import app.shared.tasks.adds as adds_module
    s = _seed(local_community=False, with_keys=True)
    _remote_inbox(s, http_mock)
    record = _recording_task_session(monkeypatch, adds_module)

    sticky_post(None, s.user.id, s.post.id)

    assert record.calls == ['close']


def test_unsticky_post_closes_the_session_on_the_happy_path(
        db_session, http_mock, monkeypatch):
    """removes.py's happy-path control -- the twin."""
    import app.shared.tasks.removes as removes_module
    s = _seed(local_community=False, with_keys=True)
    _remote_inbox(s, http_mock)
    record = _recording_task_session(monkeypatch, removes_module)

    unsticky_post(None, s.user.id, s.post.id)

    assert record.calls == ['close']
```

- [ ] **Step 3: Run the file**

Run: `./run_tests.sh tests/test_shared_tasks_add_remove.py -q`
Expected: **35 passed**.

- [ ] **Step 4: Commit**

Subject: `test: cover the twins' four Celery wrappers and both lookup styles`

---

### Task 7: Mutation-test both twins

**Files:**
- Modify (temporarily): `app/shared/tasks/adds.py`, `app/shared/tasks/removes.py`

**Interfaces:**
- Consumes: the tests from Tasks 2-6.

**THE SAFETY PROTOCOL.** A sub-project-18 implementer **truncated a 1174-line production module to zero bytes** at exactly this step. For every mutation:

1. **Dry-run the substitution WITHOUT `-i` and read the produced line.** A plan's `sed` is untested code — three written across sub-projects 21-22 were defective, one of which did not parse.
2. Apply with **one targeted single-line `sed`**. Never batched, never a whole-file rewrite.
3. Run **only** `./run_tests.sh tests/test_shared_tasks_add_remove.py -q`.
4. Restore with `git checkout -- app/`.
5. Assert **both**: `git diff -- app/` empty, **and** `wc -l` is **100** for both twins.

Do 4 and 5 before the next mutation. If either assertion fails, stop and report BLOCKED.

**EVERY MUTATION RUNS AGAINST BOTH TWINS, SEPARATELY.** The files are identical, so a mutation that kills in one and survives in the other is a divergence in the **tests**, not the code — and it is the only signal that would catch a test-design bug replicated across both. Record each twin's result separately; an asymmetry is a finding to report loudly.

- [ ] **Step 1: Confirm the tree is clean**

```bash
git diff -- app/ && wc -l app/shared/tasks/adds.py app/shared/tasks/removes.py
```

Expected: no diff, 100 lines each. Task 2's guards must be committed.

- [ ] **Step 2: Run each mutation against each twin**

For `FILE` in `adds.py`, `removes.py`:

| # | line | mutation | `sed` |
|---|---|---|---|
| M1 | 58 | invert the `community_id` test | `sed -i '58s/if not community_id:/if community_id:/'` |
| M2 | 63 | drop the `private` conjunct | `sed -i '63s/ or community.private//'` |
| M3 | 63 | drop the `local_only` conjunct | `sed -i '63s/community.local_only or //'` |
| M4 | 63 | drop the `online()` conjunct | `sed -i '63s/ or not community.instance.online()//'` |
| M5 | 74 | swap the ternary's arms | `sed -i '74s/ap_moderators_url if community_id else community.ap_featured_url/ap_featured_url if community_id else community.ap_moderators_url/'` |
| M6 | 81 | invert `is_local()` | `sed -i '81s/if community.is_local():/if not community.is_local():/'` |
| M7 | 82 | comment out the inner `del` | `sed -i '82s|^|#|'` |
| M8 | 97 | drop the `inbox` conjunct | `sed -i '97s/instance.inbox and //'` |

**All eight were dry-run against both twins while this plan was written, and seven produce the intended mutation verbatim.** The exception is M2: at the time of writing, `or community.private` does not yet exist, so its `sed` is a **no-op** against the pre-Task-2 line. After Task 2 lands the guard it strips the conjunct correctly. **Re-dry-run M2, M3 and M4 after Task 2**, because all three target `:63` and the line they match changes when the guard lands — and a `sed` that silently matches nothing is the shape sub-project 21 mislabelled a survivor.

For the record, the verified post-substitution lines at the time of writing were:

```
M1  if community_id:
M3  if not community.instance.online():
M4  if community.local_only:
M5  'target': community.ap_featured_url if community_id else community.ap_moderators_url,
M6  if not community.is_local():
M7  #        del add['@context']            (and #        del remove['@context'])
M8  if instance.online() and not user.has_blocked_instance(instance.id) and not instance_banned(instance.domain):
```

M3 and M4 will produce different — still valid — lines once `:63` carries three conjuncts.

That is 8 mutations × 2 twins = **16 runs**. For each: the mutation, the twin, the test(s) that failed, **assertion-kill or crash-kill**, **sole or multi**, and the pass counts **as-of this commit**.

**Expect M6 to be a multi-kill mixing assertion and crash kills** — it flips every test's path at once, and the remote tests will die reaching `following_instances()` rather than failing an assertion. That is the expected result.

**M5 is the one that matters most.** It is the only check that the `:74` ternary's two arms are genuinely discriminated — coverage cannot see it, and if `_seed` ever stopped setting the two URL columns, M5 would survive while every other mutation still killed.

**If any mutant SURVIVES, do not claim a kill.** Distinguish a **survivor** (real change, undetected — a gap in the tests), an **equivalent mutant** (real change, undetectable — a documented dead end), and a **no-op substitution** (nothing mutated — says nothing about the tests).

- [ ] **Step 3: Assert the tree is clean and commit the record**

```bash
git diff -- app/ && wc -l app/shared/tasks/adds.py app/shared/tasks/removes.py
```

Commit the record as a comment block at the end of the test file, with both twins' results side by side so an asymmetry is visible at a glance. Subject: `test: record the twins' mutation kills`

---

### Task 8: Residuals, the AST walk, the register and two new floors

**Files:**
- Modify: `tests/test_shared_tasks_add_remove.py`, `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`, `tests/README.md`, `coverage_floors.ini`

- [ ] **Step 1: Ask the controller for the scoped measurement**

Do NOT run the full suite. Report ready and ask for the missing statements and branch arms in all six functions across both twins. Starting point was 40 missing statements and 10 arms **per file**.

- [ ] **Step 2: Write a test for each remaining reachable arm**

One named test per arm, docstring naming the line, the twin, and which arm it takes.

**One is expected to remain in each twin and must NOT be chased:** `:97`'s `instance.online()` conjunct, already filtered in SQL by `Community.following_instances` (`app/models.py:849-850`).

If any other arm is unreachable, write the argument rather than a test, consult `tests/README.md` fact 75's catalogue of causes, and **name the establisher**.

- [ ] **Step 3: Run the AST walk and reconcile**

```bash
python3 - <<'PY'
import ast
for path in ('app/shared/tasks/adds.py', 'app/shared/tasks/removes.py'):
    src = open(path).read()
    print(path)
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef):
            print('  ', node.name, node.lineno, node.end_lineno)
            for sub in ast.walk(node):
                if isinstance(sub, ast.IfExp):
                    print('     TERNARY', sub.lineno,
                          ast.get_source_segment(src, sub))
PY
```

One per twin is expected, at `:74` — but **report the walk's raw output rather than confirming the expectation**. Sub-project 21's spec claimed a function had no ternaries by inspection and the walk found one.

- [ ] **Step 4: Write the unreachability comment block**

Add a comment block at the end of the test file naming every unreachable item with its establisher, per twin, and reconciling every ternary the walk found against the named test taking each arm. Re-derive every line number before committing.

- [ ] **Step 5: Register the findings**

```bash
grep -o 'Next free number: D[0-9]*' docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md | sort -u -t D -k2 -n | tail -1
```

Expected: **D316**. Confirm rather than assume.

1. **`:97` in both twins** — D302's **seventh and eighth** instances. D302's cell currently names **six** (`pages.py:295`, `:350`, `:351`, `notes.py:217`, `:300`, `pages.py:432`); **verify that count** before writing, and append in place per the precedent sub-projects 20-22 set.
2. **`post_request` imported and never used**, at `:2` of both twins — only `send_post_request` is called. Reading-level, and evidence for the equivalence claim: the twins share even their dead imports.
3. **The two lookup styles twelve lines apart** (`:32` vs `:47`) in both twins, with the argument that a test reaching an error path by "pass a missing id" must ask which style the function uses, and that this is the third module in which the campaign has met it.
4. **The twins' exact structural equivalence**, recorded so a future divergence is detectable. Name what was compared and how, and cite the test that asserts it.
5. **D309's site count drops from eight to six**, because both `:63` now test `private`. Update the cell.

Do a two-pass citation sweep (fact 100), and grep for the value being corrected rather than the text being edited.

- [ ] **Step 6: Add the harness facts**

```bash
grep -oE '^\*\*1[0-9][0-9]\.' tests/README.md | sort -u | tail -3
```

Facts end at **128**; append from **129**. A naive `grep -o '^1[0-9][0-9]\.'` returns line-number references inside prose — match the `**NNN.` heading form.

Candidates, each written only if it genuinely bit:
- `make_community` leaves `ap_featured_url` and `ap_moderators_url` at None, so a ternary selecting between them is invisible to any test that does not set them — both arms produce None and every assertion passes.
- Two lookup styles twelve lines apart in one file, and the different exceptions they raise.
- Running every mutation against both twins is what catches a test-design bug replicated across identical files; a kill/survive asymmetry between twins is a defect in the tests, not the code.

- [ ] **Step 7: Ask the controller for the blended figures and ADD two floors**

`coverage_floors.ini` has **no entry** for either twin. Ask for both post-work blended `summary['percent_covered']` figures, then add both entries in the file's existing ordering, floored to whole percents.

**This is the campaign's first sub-project to add floors rather than raise them.** After adding them, **prove each bites**: copy `coverage_floors.ini` to a scratch file, raise one entry above its measured figure, run the checker against the copy, and confirm it exits non-zero naming that module. Do this for each new entry, and report both results. A floor that does not bite is a ratchet tooth that is not engaged.

- [ ] **Step 8: Verify the floor check**

```bash
python3 tests/check_coverage_floors.py scratch_full_cov.json coverage_floors.ini
```

Expect exit 0 with both new modules listed. **Read the report mtime it prints** — the file is gitignored and persists, so a run that failed to write it leaves stale data the ratchet would pass against.

- [ ] **Step 9: Ask the controller for the full-suite run, then commit**

Report ready. The controller runs the suite once, **in the foreground**, and reports counts, wall time and the report mtime.

Subject: `docs: register sub-project 23's findings and add floors for the twins`

---

## Success criteria

From spec §9:

1. All six functions across the two twins are at zero uncovered statements and zero uncovered branch arms, except any proved unreachable with a written argument **naming its establisher**.
2. Both end-to-end paths — local `Announce` and remote direct post — are asserted on **serialized outbound bytes** for **each** twin, with `@context` asserted present on the outermost object and absent on the nested one.
3. `:74`'s ternary has both arms exercised by named tests in each twin, reconciled by an AST walk, with `target` asserted as `ap_featured_url` on one arm and `ap_moderators_url` on the other — **and those two columns set to distinct non-None values**, or the assertion cannot discriminate.
4. `:58`'s two arms are covered, and the `NoResultFound` from `:61` is reached by a natural raise. **[Corrected by the final review wave, not backdated.] Ticked at the round's close and not met at that point** -- both `pytest.raises(NoResultFound)` tests raised at `:47`, twelve lines before `add_object` ran. Met now by `test_add_object_raises_for_a_community_id_with_no_row` and its `remove_` twin; see the design's criterion 4.
5. Each wrapper's error mechanic is established **by reading** and asserted accordingly — `AttributeError` for the `.get()` path, `NoResultFound` for the `.one()` path — with both arms witnessed by a recording `Session`.
6. Exactly two production changes land, the `private` conjunct at `adds.py:63` and `removes.py:63`, each proved by a test that fails before it, with both files still **100 lines**.
7. Every mutation is run against **both** twins, and any kill/survive asymmetry between them is reported as a finding.
8. The register carries D316 onward, with `:97` handled per D302's precedent and the twins' equivalence recorded.
9. `coverage_floors.ini` gains entries for both modules at their measured figures, and **each is proved to bite**.

---

## Appended 2026-09-07 by sub-project 23's register round (Task 8) -- one figure in this document is superseded

**This document says the twins' only textual difference, after normalising names,
is "one docstring word". That is short of the truth in both directions, and the
corrected form is in the register (`docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`,
sub-project 23's section, subsection 3) and in the module docstring of
`tests/test_shared_tasks_add_remove.py`.** Measured rather than summarised:
`diff app/shared/tasks/adds.py app/shared/tasks/removes.py` reports exactly
**eight** hunks, all of them name substitutions, and **two of them are string
literals** (`'Add'`/`'Remove'` and the `/activities/add/`-vs-`/activities/remove/`
path segment) rather than a docstring word. Conversely, once the docstring word
`Add:`/`Remove:` is normalised along with everything else, the two files are
**byte-identical** -- there is no residual difference at all. The original claim
was written by reading the diff and summarising it, and the summary lost two
hunks; registering the equivalence as **D318** is what forced it to be counted.

**A second figure is dated rather than wrong**: this document's "coverage
residuals are the same lines and the same arcs" was true when written, and both
twins now measure **100.0000%** with zero missing statements and zero partial
branches, so the residual is empty in both. The ten arcs it lists are the ten
**executed** arcs at this commit.

**Appended rather than rewritten**, in D275's two-way-pointer form and per
sub-project 22's precedent: no line above this annotation moved, so every
citation into this document still resolves, and a reader who opens this document
first is sent to the register.
